"""Try this version (1.5.2, issue #95 PR 2): a custom copy of a built-in engine, pinned to a newer version that the
last check found.

    POST /api/recipes/{name}/try {version, name?}       admins; forgeyard/api/engine_updates.py

What it takes and from where:

- Only a version from the last check's result (forgeyard/models/engineversions.py), never an image string: the
  request names a version, and each variant's tag comes from that result, checked again against the built-in
  variant's own tag form (or, for a rolling engine, its own tag). The registry and repository always come from the
  built-in variant's image, never from the stored result.
- Each included image's digest is resolved again now; if the tag moved since the check, the copy is refused (check
  again). The image is then pinned by that digest, and its recorded ids (index, each platform's manifest and config,
  a content id) are read from the registry by digest (forgeyard/workloads/pinning.py) and kept in `image_pins`, so
  the copy's image comes through the server, matches after `docker load` and is listed and removable on machine
  pages like a built-in's.
- A process variant's archive: the release asset the check found, its SHA-256 read again from GitHub now.
- Signatures where the project publishes them (forgeyard/models/signatures.py): a failed or missing verification, or
  one that cannot run, refuses the copy.
- Variants without the new version (Q1 A) or without the same archive (Q5 A) are left out, with the reason recorded
  in the copy (`copy.left_out`).

The copy is the built-in's YAML text with: a header comment, its name (`<recipe>-x-y-z` unless given), title
("vLLM 0.34.0"), version, the new images and archives, the left-out variants removed, and `based_on` / `copy`
(forgeyard/models/recipes.py). Everything else, the arguments included, is the built-in's as it is now.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from forgeyard.models import engineversions as ev
from forgeyard.models import fetch, recipes, signatures
from forgeyard.models.recipes import Recipe, RecipeError
from forgeyard.workloads import pinning
from forgeyard.workloads import registry as oci


class TryError(Exception):
    def __init__(self, status: int, code: str, text: str, **extra):
        super().__init__(text)
        self.status, self.code, self.text, self.extra = status, code, text, extra


@dataclass
class Plan:
    """What the copy will hold, before anything is written."""
    builtin: Recipe
    builtin_text: str
    version: str
    kind: str                                   # release | build
    name: str
    images: dict[str, str] = field(default_factory=dict)             # variant -> repo:tag@sha256:…
    entries: dict[str, dict] = field(default_factory=dict)           # image ref -> its recorded ids
    image_signatures: dict[str, dict] = field(default_factory=dict)  # image ref -> signature record
    archives: dict[str, dict] = field(default_factory=dict)          # variant -> {url, sha256, release, signature}
    left_out: list[dict] = field(default_factory=list)               # [{variant, code, text, params}]
    # Tony 2026-11-23: an admin may make the copy although its signatures could not be checked (cannot_verify).
    allow_unverified: bool = False
    made_by: str = ""
    made_at: str = ""
    overridden: list[str] = field(default_factory=list)              # variants recorded as not_verified


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def default_name(builtin: str, version: str) -> str:
    """`vllm` + `0.34.0` -> `vllm-0-34-0` (dots are not allowed in recipe names), at most 40 characters."""
    return f"{builtin}-{slug(version)}"[:40].rstrip("-")


def candidate_of(stored: Optional[dict], builtin: Recipe, version: str) -> tuple[dict, dict]:
    """(the engine's last check, the candidate) or TryError: the request's version must be one the check found for
    this built-in at its current version."""
    engine = ((stored or {}).get("engines") or {}).get(builtin.name)
    if not engine:
        raise TryError(409, "check_again", f"There is no check result for {builtin.name}. Check for newer versions "
                                           "first.")
    if engine.get("version") != builtin.version:
        raise TryError(409, "check_again", f"The last check was made for {builtin.name} {engine.get('version')}; the "
                                           f"built-in is now {builtin.version}. Check for newer versions again.")
    cand = next((c for c in engine.get("candidates") or [] if c.get("version") == version), None)
    if cand is None:
        raise TryError(409, "check_again", f"{version} is not among the newer versions of {builtin.name} that the last "
                                           "check found. Check for newer versions again.")
    return engine, cand


def _why_params(why: dict) -> dict:
    return {k: str(v) if not isinstance(v, list) else ", ".join(map(str, v)) for k, v in (why or {}).items()
            if k not in ("code", "text") and v is not None}


def _left_out(plan: Plan, variant: str, why: dict) -> None:
    plan.left_out.append({"variant": variant, "code": str(why.get("code") or "")[:40],
                          "text": str(why.get("text") or "")[:400], "params": _why_params(why)})


def plan_copy(builtin: Recipe, builtin_text: str, cand: dict, name: str, *, client: oci.Registry,
              gh: ev.Checker, policy: fetch.Policy, data_dir: Path, allow_unverified: bool = False,
              made_by: str = "", made_at: str = "") -> Plan:
    """Resolve, pin, verify: every network step of a Try (no database)."""
    plan = Plan(builtin, builtin_text, cand["version"], "build" if cand.get("kind") == "build" else "release", name,
                allow_unverified=allow_unverified, made_by=made_by, made_at=made_at)
    reader = pinning.RegistryReader(client)
    by_name = {v.get("name"): v for v in cand.get("variants") or [] if isinstance(v, dict)}
    signed = signatures.source_for(builtin.engine)
    for v in builtin.variants:
        got = by_name.get(v.name)
        if got is None:
            _left_out(plan, v.name, {"code": "not_in_check", "text": "The last check said nothing about this variant."})
            continue
        if not got.get("available"):
            _left_out(plan, v.name, got.get("why") or {"code": "left_out", "text": "No image of this version."})
            continue
        if v.runtime == "container":
            _pin_image(plan, v, got, reader, client, signed, policy, data_dir)
        elif v.engine is not None:
            _pin_archive(plan, v, got, gh, signed, policy, data_dir)
        else:
            _left_out(plan, v.name, {"code": "installed_command", "text": "It runs a command installed on the "
                                                                          "machine; Forgeyard does not update it."})
    if not plan.images and not plan.archives:
        raise TryError(422, "nothing_to_pin", f"No variant of {builtin.name} has {plan.version}: there is nothing to "
                                              "copy.")
    return plan


def _signature(plan: Plan, variant: str, digest: str, what: str, policy, data_dir) -> dict:
    """What is recorded about one image or archive of a signed project: verified; or, when it could not be checked and
    the admin asked for it (and the policy lets them), not_verified. A failed or missing signature always refuses."""
    try:
        return signatures.verify(plan.builtin.engine, digest, what=what, policy=policy, data_dir=data_dir)
    except signatures.SignatureError as e:
        if e.code == "cannot_verify" and plan.allow_unverified and signatures.override_allowed(plan.builtin.engine):
            plan.overridden.append(variant)
            return signatures.not_verified(e.text, plan.made_by, plan.made_at)
        extra = {"can_override": signatures.override_allowed(plan.builtin.engine)} if e.code == "cannot_verify" else {}
        raise TryError(502 if e.code == "cannot_verify" else 422, e.code, e.text, variant=variant, **extra) from None


def _expected_tag(builtin: Recipe, v, tag: str, kind: str, version: str) -> bool:
    """The tag the stored result names is one of this built-in variant's own form, for that version."""
    if not isinstance(tag, str) or not oci.TAG_RE.match(tag):
        return False
    if kind == "build":
        return tag == oci.split_image(v.image)[2]
    rule = ev.tag_rule(v, builtin.version)
    hit = rule.match(tag) if rule is not None else None
    return hit is not None and hit[0] == version


def _pin_image(plan: Plan, v, got: dict, reader, client: oci.Registry, signed: Optional[dict], policy, data_dir) -> None:
    registry, repo, _, _ = oci.split_image(v.image)
    tag, checked = got.get("tag"), got.get("digest")
    if not _expected_tag(plan.builtin, v, tag, plan.kind, plan.version) or not oci.DIGEST_RE.match(str(checked or "")):
        raise TryError(409, "check_again", f"The last check's answer for {v.name} is not one Forgeyard can use. Check "
                                           "for newer versions again.")
    try:
        now = client.resolve(registry, repo, tag)
    except fetch.FetchError as e:
        raise TryError(502, "registry_error", f"Could not read {tag} from {ev._where(v.image)}: {e}") from None
    if now != checked:
        raise TryError(409, "digest_moved", f"{ev._where(v.image)}:{tag} points at another image than when you "
                                            f"checked ({checked[:19]}… then, {now[:19]}… now). Check for newer "
                                            "versions again, then try.", variant=v.name)
    ref = f"{ev._image_name(v.image)}:{tag}@{now}"
    try:
        entry = pinning.describe(reader, ref)
    except (fetch.FetchError, ValueError, KeyError, TypeError) as e:
        raise TryError(502, "registry_error", f"Could not read the image {tag} from {ev._where(v.image)}: "
                                              f"{str(e)[:200]}") from None
    missing = pinning.missing_platforms(entry, v.when.arch)
    if missing:
        _left_out(plan, v.name, {"code": "no_platform", "text": f"Its image {tag} has no {', '.join(missing)} build.",
                                 "tag": tag, "platforms": missing})
        return
    if signed is not None and signed.get("images"):
        sig = _signature(plan, v.name, now, f"the image {tag}", policy, data_dir)
    else:
        sig = signatures.unsigned(plan.builtin.engine)
    plan.images[v.name] = ref
    plan.entries[ref] = entry
    plan.image_signatures[ref] = sig


def _pin_archive(plan: Plan, v, got: dict, gh: ev.Checker, signed: Optional[dict], policy, data_dir) -> None:
    m = ev._RELEASE_URL.match(v.engine.url)
    url, sha, release, archive = got.get("url"), got.get("sha256"), got.get("release"), got.get("archive")
    if m is None or not isinstance(url, str) or not isinstance(release, str) or not isinstance(archive, str) or \
            not re.fullmatch(r"[0-9a-f]{64}", str(sha or "")) or \
            url != f"https://github.com/{m.group(1)}/releases/download/{release}/{archive}":
        raise TryError(409, "check_again", f"The last check's answer for {v.name} is not one Forgeyard can use. Check "
                                           "for newer versions again.")
    try:
        rel = gh.release_by_tag(m.group(1), release)
    except fetch.FetchError as e:
        raise TryError(502, "github_error", f"Could not read the release {release} from GitHub: {e}") from None
    asset = next((a for a in (rel or {}).get("assets") or [] if isinstance(a, dict) and a.get("name") == archive), None)
    now = str((asset or {}).get("digest") or "").removeprefix("sha256:")
    if now != sha:
        raise TryError(409, "digest_moved", f"The archive {archive} of {release} is not the one you checked (its "
                                            "SHA-256 changed or it is gone). Check for newer versions again, then "
                                            "try.", variant=v.name)
    if signed is not None and signed.get("archives"):
        sig = _signature(plan, v.name, f"sha256:{sha}", f"the archive {archive}", policy, data_dir)
    else:
        sig = signatures.unsigned(plan.builtin.engine)
    plan.archives[v.name] = {"url": url, "sha256": sha, "release": release, "archive": archive, "signature": sig}


# ------------------------------------------------------------------ the copy's YAML
_TOP = re.compile(r"^(name|title|version):.*$", re.M)


def _variant_blocks(lines: list[str], start: int) -> list[tuple[str, int, int]]:
    """[(variant name, first line, end line)] of the `variants:` list that starts after line `start`. A block owns the
    comment lines just above its `- name:`."""
    items: list[tuple[str, int]] = []
    for i in range(start + 1, len(lines)):
        m = re.match(r"^\s*-\s*name:\s*['\"]?([A-Za-z0-9._-]+)['\"]?\s*$", lines[i])
        if m:
            first = i
            while first - 1 > start and lines[first - 1].strip().startswith("#"):
                first -= 1
            items.append((m.group(1), first))
    out = []
    for k, (name, first) in enumerate(items):
        end = items[k + 1][1] if k + 1 < len(items) else len(lines)
        out.append((name, first, end))
    return out


def _sig_comment(sig: dict) -> str:
    if sig.get("status") == "verified":
        return f"signature verified: {sig.get('repository')} {sig.get('workflow')}"
    if sig.get("status") == "not_verified":
        return f"signature NOT verified (could not be checked; made anyway by {sig.get('overridden_by')})"
    return "upstream publishes no signature"


def copy_yaml(plan: Plan, *, made_by: str, made_at: str) -> str:
    b = plan.builtin
    title = f"{b.title} {plan.version}"[:80]
    lines = plan.builtin_text.splitlines()
    head = [f"# A copy of the built-in recipe {b.name} ({b.title} {b.version}) for {plan.version}, made with \"Try this "
            f"version\" on {made_at[:10]} by {made_by}.",
            "# Not tested by Forgeyard: the built-in recipe's arguments, with this version's images pinned by digest. "
            f"The built-in {b.name} stays as it is."]
    if plan.overridden:
        head.append(f"# Signatures NOT verified for {', '.join(plan.overridden)}: they could not be checked, and "
                    f"{made_by} made the copy anyway.")
    for lo in plan.left_out:
        head.append(f"# Left out: {lo['variant']} ({' '.join(lo['text'].split())})")
    head.append("#")
    start = next((i for i, x in enumerate(lines) if re.match(r"^variants:\s*$", x)), None)
    if start is None:
        raise ValueError("the built-in recipe has no variants list")
    blocks = _variant_blocks(lines, start)
    keep_out = {lo["variant"] for lo in plan.left_out}
    body = lines[:start + 1]
    for name, first, end in blocks:
        if name in keep_out:
            continue
        block = lines[first:end]
        if name in plan.images:
            new = plan.images[name]
            block = [re.sub(r"^(\s*(?:-\s*)?image:\s*)\S+(\s*)$", lambda m: f"{m.group(1)}{new}  # "
                            f"{_sig_comment(plan.image_signatures[new])}", x) for x in block]
        if name in plan.archives:
            a = plan.archives[name]
            block = [re.sub(r"^(\s*url:\s*)\S+\s*$", lambda m: f"{m.group(1)}{a['url']}", x) for x in block]
            block = [re.sub(r"^(\s*sha256:\s*)\S+\s*$", lambda m: f"{m.group(1)}{a['sha256']}", x) for x in block]
        body += block
    body = [x for x in body]
    text = "\n".join(body).rstrip("\n") + "\n"

    def top(m: re.Match) -> str:
        key = m.group(1)
        return {"name": f"name: {plan.name}", "title": f"title: {json.dumps(title)}",
                "version": f"version: {json.dumps(plan.version)}"}[key]

    seen: set[str] = set()

    def once(m: re.Match) -> str:
        if m.group(1) in seen:
            return m.group(0)
        seen.add(m.group(1))
        return top(m)

    text = _TOP.sub(once, text)
    copy = {"from_version": b.version, "kind": plan.kind, "made_at": made_at, "made_by": made_by,
            "left_out": plan.left_out,
            "archives": [{"variant": n, "release": a["release"], "signature": a["signature"]}
                         for n, a in plan.archives.items()]}
    tail = yaml.safe_dump({"based_on": b.name, "copy": copy}, sort_keys=False, allow_unicode=True, width=1000)
    return "\n".join(head) + "\n" + text + "\n# What \"Try this version\" did (shown on this engine's page).\n" + tail


def check_copy(plan: Plan, text: str) -> Recipe:
    """The YAML parses, and holds exactly what the plan says (else ValueError: the text surgery missed)."""
    try:
        r = recipes.parse_recipe(text)
    except RecipeError as e:
        raise ValueError(str(e)) from None
    kept = [v.name for v in plan.builtin.variants if v.name in plan.images or v.name in plan.archives]
    if r.name != plan.name or r.version != plan.version or r.based_on != plan.builtin.name:
        raise ValueError("name, version or based_on")
    if [v.name for v in r.variants] != kept:
        raise ValueError("the variants")
    for v in r.variants:
        if v.runtime == "container" and v.image != plan.images.get(v.name):
            raise ValueError(f"the image of {v.name}")
        if v.engine is not None and (v.engine.url != plan.archives[v.name]["url"]
                                     or v.engine.sha256 != plan.archives[v.name]["sha256"]):
            raise ValueError(f"the archive of {v.name}")
    base = plan.builtin.model_dump(exclude={"name", "title", "version", "variants", "builtin", "based_on", "copy_of"})
    if r.model_dump(exclude={"name", "title", "version", "variants", "builtin", "based_on", "copy_of"}) != base:
        raise ValueError("the arguments or settings")
    return r


def build(plan: Plan, *, made_by: str, made_at: str) -> tuple[str, Recipe]:
    text = copy_yaml(plan, made_by=made_by, made_at=made_at)
    try:
        return text, check_copy(plan, text)
    except ValueError as e:
        raise TryError(500, "copy_failed", f"The copy of {plan.builtin.name} could not be written ({e}).") from None
