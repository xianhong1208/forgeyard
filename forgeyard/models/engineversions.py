"""Newer versions of the built-in engines (1.5.2, issue #95 PR 1): what an admin's "Check for newer versions" finds.

Only when an admin asks, never on a timer, and only through the server's egress rules (forgeyard.models.fetch, via
the image relay's anonymous registry client): for every built-in engine, the tags of each variant's **own**
repository (never another image, never an image string from a request), and the GitHub release of each newer
version (its notes and, for a process variant, its archive and SHA-256).

What counts as newer (Q3 A, the rule Renovate uses for Docker tags):

- The built-in variant's tag with its version replaced by any version of the same shape, everything else kept as a
  compatibility suffix: `v0.32.0-cu129` -> `^v(\\d+\\.\\d+\\.\\d+)-cu129$`, `0.8.3-cuda-12.4.1` ->
  `^(\\d+\\.\\d+\\.\\d+)-cuda-12\\.4\\.1$`. One extra: llama.cpp's release tags may carry `-b<build>-<commit>`
  (`server-cuda-v1.0.0-b12359-b1b770d`), matched as an optional part. So rc tags (`v0.33.0rc1`, `0.9.0-rc.3-cpu`),
  nightlies (`server-cuda-b12648`, `nightly-…`), single-architecture and model-specific tags never match, and a
  GitHub release without an image is never offered: the registry decides, not GitHub.
- Every version greater than the recipe's, newest first (at most MAX_CANDIDATES), so the smaller jump stays a choice.
- An engine whose tags carry no version at all (stable-diffusion.cpp's `master-cuda`) is "rolling": a newer *build*
  when the tag's digest moved to an image made later than the pinned one; named after the GitHub release of the
  image's `org.opencontainers.image.revision`.

Per version: each variant's tag and digest at check time, or why it has none (Q1 A: a copy leaves it out); a process
variant (Q5 A) when the release publishes the same archive with a SHA-256; the release-notes link and the flags and
environment variables of this recipe its notes mention (Q6 A). A registry or GitHub that cannot be reached, or an
egress rule that refuses it, is "could not check: …" for the engines it concerns; the others are still reported.
Forgeyard's own vision image is not checked: it moves with Forgeyard.

The last result is one `server_settings` row (SETTING_KEY), shown until the next check.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional
from urllib.parse import quote

from sqlalchemy.orm import Session as DbSession

from forgeyard.db import ServerSetting, utcnow
from forgeyard.models import fetch
from forgeyard.models.recipes import Recipe, Variant, builtin_recipes
from forgeyard.workloads import registry as oci

SETTING_KEY = "engine_updates"
CHECK_SECONDS = 90.0          # the whole check; what is left after it is "could not check: took too long"
REQUEST_TIMEOUT = 20.0        # one request
MAX_CANDIDATES = 8            # newer versions listed per engine
GITHUB_API = "https://api.github.com"
GITHUB_MAX_CALLS = 24         # anonymous GitHub allows 60 an hour per address
GITHUB_MAX_BYTES = 16 * 1024 * 1024
RELEASES_PAGE = 30
EXCERPTS_PER_FLAG = 3
EXCERPT_CHARS = 240
WORKERS = 4

# Each built-in engine's GitHub repository: release notes, and the release archives of process variants.
GITHUB_REPOS = {
    "vllm": "vllm-project/vllm",
    "vllm-omni": "vllm-project/vllm-omni",
    "llama.cpp": "ggml-org/llama.cpp",
    "stable-diffusion.cpp": "leejet/stable-diffusion.cpp",
    "speaches": "speaches-ai/speaches",
}
# Not checked: Forgeyard's own image moves with Forgeyard's releases.
OWN_ENGINES = ("forgeyard-vision",)

_FLAG = re.compile(r"^--[a-z0-9][a-z0-9-]*[a-z0-9]$")
_BUILD = re.compile(r"^(?P<head>.*?)-b\d+-[0-9a-f]{7,40}$")
_RELEASE_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download/([^/]+)/([^/]+)$")
_SEMVER_TAG = re.compile(r"^v?(\d+(?:\.\d+)+)$")


class CheckTimeout(fetch.FetchError):
    pass


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


# ------------------------------------------------------------------ the tag rule
class TagRule:
    """A built-in variant's tag with its version as the one moving part (see the module's doc)."""

    def __init__(self, tag: str, version: str):
        m = re.search(r"(?<![\d.])" + re.escape(version) + r"(?![\d]|\.\d)", tag)
        if m is None:
            raise ValueError("the tag carries no version")
        self.tag, self.version = tag, version
        prefix, suffix = tag[:m.start()], tag[m.end():]
        shape = re.sub(r"\d+", lambda _: r"\d+", re.escape(version))
        b = _BUILD.match(suffix)
        self.build = b is not None
        self.prefix, self.suffix = prefix, (b.group("head") if b else suffix)
        tail = (re.escape(b.group("head")) + r"(?:-b(?P<build>\d+)-[0-9a-f]{7,40})?") if b else re.escape(suffix)
        self.regex = re.compile("^" + re.escape(prefix) + "(?P<version>" + shape + ")" + tail + "$")

    def match(self, tag: str) -> Optional[tuple[str, Optional[int]]]:
        """(version, build or None) when the tag has this variant's form."""
        m = self.regex.match(tag)
        if m is None:
            return None
        build = m.groupdict().get("build")
        return m.group("version"), (int(build) if build else None)

    def tag_for(self, version: str) -> str:
        """The tag of a version in this form, without a build ('v0.34.0-cu129'), to name what is missing."""
        return f"{self.prefix}{version}{self.suffix}"

    def versions(self, tags: Iterable[str]) -> dict[str, list[tuple[Optional[int], str]]]:
        """{version: [(build, tag), …]} of the tags of this form."""
        out: dict[str, list[tuple[Optional[int], str]]] = {}
        for t in tags:
            hit = self.match(t)
            if hit is not None:
                out.setdefault(hit[0], []).append((hit[1], t))
        return out


def tag_rule(variant: Variant, version: str) -> Optional[TagRule]:
    if variant.runtime != "container" or not variant.image:
        return None
    _, _, tag, _ = oci.split_image(variant.image)
    try:
        return TagRule(tag, version)
    except ValueError:
        return None


def pick_tag(found: list[tuple[Optional[int], str]], want_build: bool) -> tuple[Optional[int], str]:
    """Of the tags of one version, the one in the built-in's form (with its build, the highest build), else any."""
    return sorted(found, key=lambda x: ((x[0] is not None) == want_build, x[0] or 0, x[1]), reverse=True)[0]


# ------------------------------------------------------------------ flags in release notes (Q6 A)
def recipe_flags(recipe: Recipe) -> tuple[list[str], list[str]]:
    """(the long --flags the recipe passes, the environment variables it sets), each once, in order."""
    flags: list[str] = []
    for token in [*recipe.args, *(a for o in recipe.optional_args for a in o.args),
                  *(a for v in recipe.variants for a in [*(v.entrypoint or []), *(v.args or [])])]:
        name = str(token).split("=", 1)[0]
        if _FLAG.match(name) and name not in flags:
            flags.append(name)
    envs: list[str] = []
    names = [*recipe.env, *(k for v in recipe.variants for k in v.env)]
    if recipe.api_key_env:
        names.append(recipe.api_key_env)
    if recipe.engine_cache is not None:
        names.append(recipe.engine_cache.env)
    for name in names:
        if name not in envs:
            envs.append(name)
    return flags, envs


def _excerpt(line: str, start: int, end: int) -> str:
    line = line.strip()
    if len(line) <= EXCERPT_CHARS:
        return line
    half = (EXCERPT_CHARS - (end - start)) // 2
    a = max(0, start - half)
    b = min(len(line), a + EXCERPT_CHARS)
    return ("…" if a > 0 else "") + line[a:b].strip() + ("…" if b < len(line) else "")


def flag_mentions(notes: str, flags: list[str], envs: list[str]) -> list[dict]:
    """The recipe's flags (as `--flag-name`, or `flag_name` for a name of two words or more: vLLM's notes speak of
    `gpu_memory_utilization`) and environment variables the notes mention, with up to EXCERPTS_PER_FLAG lines each.
    A one-word flag counts only with its dashes: "quantization" alone is just a word."""
    if not notes:
        return []
    pats: list[tuple[str, re.Pattern]] = []
    for f in flags:
        bare = f[2:]
        forms = [re.escape(f)]
        if "-" in bare:
            forms.append(r"(?<![A-Za-z0-9_-])" + re.escape(bare.replace("-", "_")) + r"(?![A-Za-z0-9_])")
        pats.append((f, re.compile(r"(?<![A-Za-z0-9-])(?:" + "|".join(forms) + r")(?![A-Za-z0-9-])", re.I)))
    for e in envs:
        pats.append((e, re.compile(r"(?<![A-Za-z0-9_])" + re.escape(e) + r"(?![A-Za-z0-9_])")))
    lines = notes.splitlines()
    out = []
    for name, pat in pats:
        excerpts: list[str] = []
        count = 0
        for line in lines:
            m = pat.search(line)
            if m is None:
                continue
            count += 1
            if len(excerpts) < EXCERPTS_PER_FLAG:
                text = _excerpt(line, m.start(), m.end())
                if text not in excerpts:
                    excerpts.append(text)
        if count:
            out.append({"flag": name, "lines": count, "excerpts": excerpts})
    return out


# ------------------------------------------------------------------ talking to registries and GitHub
def why(code: str, text: str, **params) -> dict:
    """A reason the console shows in the reader's language (by its code), with the English sentence as sent."""
    return {"code": code, "text": text, **{k: v for k, v in params.items() if v is not None}}


def _short(e: BaseException) -> str:
    return (str(e) or type(e).__name__)[:300]


class Checker:
    """One check: its deadline, its registry client and GitHub budget, and what it asked already (two recipes on
    one repository share one tag listing)."""

    def __init__(self, *, allow_private: bool = False, seconds: float = CHECK_SECONDS,
                 resolver: Optional[Callable] = None, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds
        self.seconds = seconds
        self.allow_private = allow_private
        self.resolver = resolver
        self.registry = oci.Registry(self.policy)
        self.github_calls = 0
        self._lock = threading.Lock()
        self._tags: dict[tuple[str, str], object] = {}
        self._digests: dict[tuple[str, str, str], object] = {}
        self._labels: dict[tuple[str, str, str], object] = {}
        self._gh: dict[str, object] = {}
        self._inflight: dict[object, threading.Lock] = {}

    # The policy of every request: the server's egress rules, a timeout no longer than what is left.
    def policy(self) -> fetch.Policy:
        left = self.left()
        kw = {"resolver": self.resolver} if self.resolver else {}
        return fetch.Policy(allow_private=self.allow_private, timeout=max(1.0, min(REQUEST_TIMEOUT, left)), **kw)

    def left(self) -> float:
        return self.deadline - self.clock()

    def check_time(self) -> None:
        if self.left() <= 0:
            raise CheckTimeout(f"the check stopped after {int(self.seconds)} s")

    def _cached(self, cache: dict, key, make):
        with self._lock:
            gate = self._inflight.setdefault((id(cache), key), threading.Lock())
        with gate:   # two engines asking for one thing at once: the second waits for the first's answer
            return self._cached_once(cache, key, make)

    def _cached_once(self, cache: dict, key, make):
        with self._lock:
            if key in cache:
                got = cache[key]
                if isinstance(got, BaseException):
                    raise got
                return got
        try:
            self.check_time()
            value = make()
        except (fetch.FetchError, ValueError, OSError) as e:
            err = e if isinstance(e, fetch.FetchError) else fetch.FetchError(_short(e))
            with self._lock:
                cache[key] = err
            raise err from None
        with self._lock:
            cache[key] = value
        return value

    def tags(self, registry: str, repo: str) -> list[str]:
        return self._cached(self._tags, (registry, repo),
                            lambda: self.registry.tags(registry, repo, check=self.check_time))

    def digest(self, registry: str, repo: str, tag: str) -> str:
        return self._cached(self._digests, (registry, repo, tag), lambda: self.registry.resolve(registry, repo, tag))

    def labels(self, registry: str, repo: str, digest: str) -> dict:
        """The linux/amd64 image's config: {"created", "revision", "version"} (empty strings when not given)."""
        def make() -> dict:
            data, _ = self.registry.manifest(registry, repo, digest)
            body = json.loads(data)
            if isinstance(body, dict) and "manifests" in body:
                entries = [m for m in body.get("manifests") or [] if isinstance(m, dict)
                           and (m.get("platform") or {}).get("os") == "linux"]
                pick = next((m for m in entries if (m.get("platform") or {}).get("architecture") == "amd64"),
                            entries[0] if entries else None)
                if pick is None:
                    raise oci.RegistryError(f"{repo}@{digest[:19]}… has no Linux image")
                data, _ = self.registry.manifest(registry, repo, str(pick.get("digest")))
            manifest = oci.parse_manifest(data)
            with self.registry.open_blob(registry, repo, manifest["config"]["digest"]) as resp:
                if resp.status >= 400:
                    raise oci.RegistryError(f"{registry} answered HTTP {resp.status} for the image config", resp.status)
                raw = resp.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise oci.RegistryError("the image config is larger than 1 MiB")
            config = json.loads(raw)
            lab = ((config.get("config") or {}).get("Labels") or {}) if isinstance(config, dict) else {}
            return {"created": str(lab.get("org.opencontainers.image.created") or config.get("created") or ""),
                    "revision": str(lab.get("org.opencontainers.image.revision") or ""),
                    "version": str(lab.get("org.opencontainers.image.version") or "")}
        return self._cached(self._labels, (registry, repo, digest), make)

    def github(self, path: str):
        """GitHub's JSON answer (None for a 404), within GITHUB_MAX_CALLS."""
        def make():
            with self._lock:
                if self.github_calls >= GITHUB_MAX_CALLS:
                    raise fetch.FetchError(f"more than {GITHUB_MAX_CALLS} GitHub requests in one check")
                self.github_calls += 1
            try:
                data = fetch.get_bytes(GITHUB_API + path, self.policy(), limit=GITHUB_MAX_BYTES,
                                       headers={"Accept": "application/vnd.github+json",
                                                "X-GitHub-Api-Version": "2022-11-28"})
            except fetch.FetchError as e:
                if e.status == 404:
                    return None
                if e.status in (403, 429):
                    raise fetch.FetchError("GitHub's limit for anonymous requests was reached (60 an hour); "
                                           "try again later", e.status) from None
                raise
            return json.loads(data)
        return self._cached(self._gh, path, make)

    def releases(self, gh_repo: str) -> list[dict]:
        found = self.github(f"/repos/{gh_repo}/releases?per_page={RELEASES_PAGE}")
        return [r for r in (found or []) if isinstance(r, dict) and not r.get("draft")]

    def release_by_tag(self, gh_repo: str, tag: str) -> Optional[dict]:
        for r in self.releases(gh_repo):
            if r.get("tag_name") == tag:
                return r
        found = self.github(f"/repos/{gh_repo}/releases/tags/{quote(tag, safe='')}")
        return found if isinstance(found, dict) and not found.get("draft") else None

    def release_for_version(self, gh_repo: str, version: str) -> Optional[dict]:
        """The GitHub release of a version: tagged v1.2.3 or 1.2.3, else *named* so (vLLM published 0.33.0 under
        the tag v0.33.0rc3, named "v0.33.0")."""
        names = (f"v{version}", version)
        listed = self.releases(gh_repo)
        for r in listed:
            if r.get("tag_name") in names:
                return r
        for r in listed:
            if str(r.get("name") or "").strip() in names:
                return r
        for tag in names:
            found = self.github(f"/repos/{gh_repo}/releases/tags/{quote(tag, safe='')}")
            if isinstance(found, dict) and not found.get("draft"):
                return found
        return None


def _release_info(rel: Optional[dict]) -> dict:
    if not rel:
        return {}
    url = str(rel.get("html_url") or "")
    return {"release": {"tag": rel.get("tag_name"), "name": rel.get("name"), "url": url if url.startswith(
        "https://github.com/") else None, "published_at": rel.get("published_at")}}


# ------------------------------------------------------------------ process variants (Q5 A)
def _swap_ids(asset: str, old_tag: str, new_tag: str) -> str:
    """The asset name of the new release: the old release's identifying parts (build, commit) replaced."""
    old, new = old_tag.split("-"), new_tag.split("-")
    if len(old) != len(new):
        return asset.replace(old_tag, new_tag) if old_tag in asset else asset
    for a, b in zip(old, new):
        if a != b and len(a) >= 3:
            asset = re.sub(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", b, asset)
    return asset


def process_variant(ck: Checker, v: Variant, new_tag: Optional[str], new_release: Optional[dict] = None) -> dict:
    """The process variant's archive in the newer release, or why it is left out."""
    base = {"name": v.name, "runtime": "process", "vendor": v.when.vendor}
    if v.engine is None:
        return {**base, "available": False, "why": why("installed_command",
                "It runs a command installed on the machine; Forgeyard does not update it.")}
    m = _RELEASE_URL.match(v.engine.url)
    if m is None:
        return {**base, "available": False, "why": why("not_github",
                "Its archive is not a GitHub release download, so newer ones cannot be found.")}
    gh_repo, old_tag, asset = m.group(1), m.group(2), m.group(3)
    if not new_tag:
        return {**base, "available": False, "why": why("release_unknown",
                "Could not tell which GitHub release holds this version's archive.")}
    try:
        rel = new_release if new_release and new_release.get("tag_name") == new_tag else ck.release_by_tag(gh_repo, new_tag)
    except fetch.FetchError as e:
        return {**base, "available": False, "why": why("github_error", f"GitHub: {_short(e)}", error=_short(e))}
    if rel is None:
        return {**base, "available": False, "why": why("no_release", f"GitHub has no release {new_tag}.",
                                                       release=new_tag)}
    want = _swap_ids(asset, old_tag, new_tag)
    assets = [a for a in rel.get("assets") or [] if isinstance(a, dict)]
    hit = next((a for a in assets if a.get("name") == want), None)
    if hit is None:
        shape = re.compile("^" + re.sub(r"\d+", lambda _: r"\d+", re.escape(want)) + "$")
        similar = [str(a.get("name")) for a in assets if shape.match(str(a.get("name") or ""))][:3]
        text = f"Release {new_tag} has no {want}, the archive the built-in uses"
        text += (f"; it has {', '.join(similar)}, built for another system." if similar else ".")
        return {**base, "available": False, "why": why("no_archive", text, release=new_tag, archive=want,
                                                       similar=similar or None)}
    digest = str(hit.get("digest") or "")
    url = str(hit.get("browser_download_url") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not url.startswith(f"https://github.com/{gh_repo}/"):
        return {**base, "available": False, "why": why("no_sha256",
                f"GitHub publishes no SHA-256 for {want}, so it cannot be pinned.", archive=want)}
    return {**base, "available": True, "url": url, "sha256": digest.removeprefix("sha256:"), "archive": want,
            "release": new_tag}


# ------------------------------------------------------------------ one engine
def _image_name(image: str) -> str:
    """'vllm/vllm-openai:v0.32.0@sha256:…' -> 'vllm/vllm-openai' (as the recipe writes it)."""
    ref = image.partition("@")[0]
    head, _, last = ref.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def _where(image: str) -> str:
    registry, repo, _, _ = oci.split_image(image)
    return f"{'docker.io' if registry == 'registry-1.docker.io' else registry}/{repo}"


def own_image(recipe: Recipe) -> bool:
    if recipe.engine in OWN_ENGINES:
        return True
    from forgeyard.install.images import DOCKERHUB_NAMESPACE

    return any(v.image and _image_name(v.image).removeprefix("docker.io/").startswith(f"{DOCKERHUB_NAMESPACE}/forgeyard")
               for v in recipe.variants)


def check_recipe(ck: Checker, recipe: Recipe) -> dict:
    out = {"recipe": recipe.name, "title": recipe.title, "engine": recipe.engine, "version": recipe.version,
           "candidates": [], "errors": []}
    if own_image(recipe):
        return {**out, "status": "not_checked", "kind": "own",
                "why": why("own_image", "Forgeyard's own image: it moves with Forgeyard's releases.")}
    containers = [v for v in recipe.variants if v.runtime == "container" and v.image]
    rules = {v.name: tag_rule(v, recipe.version) for v in containers}
    if containers and not any(rules.values()):
        return _check_rolling(ck, recipe, out)
    return _check_releases(ck, recipe, out, rules)


def _registry_error(out: dict, where: str, e: BaseException) -> None:
    text = _short(e)
    if not any(x["where"] == where for x in out["errors"]):
        out["errors"].append({"where": where, "text": text})


def _notes(ck: Checker, recipe: Recipe, gh_repo: Optional[str], rel: Optional[dict], cand: dict) -> None:
    if not gh_repo:
        return
    if rel is None:
        cand.setdefault("notes", why("no_notes", "No GitHub release found for it, so no release notes were read."))
        return
    flags, envs = recipe_flags(recipe)
    cand["mentions"] = flag_mentions(str(rel.get("body") or ""), flags, envs)


def _check_releases(ck: Checker, recipe: Recipe, out: dict, rules: dict) -> dict:
    current = version_key(recipe.version)
    gh_repo = GITHUB_REPOS.get(recipe.engine)
    found: dict[str, dict] = {}      # variant -> {version: [(build, tag)]}
    failed: dict[str, BaseException] = {}
    for v in recipe.variants:
        rule = rules.get(v.name)
        if rule is None:
            continue
        registry, repo, _, _ = oci.split_image(v.image)
        try:
            found[v.name] = rule.versions(ck.tags(registry, repo))
        except fetch.FetchError as e:
            failed[v.name] = e
            _registry_error(out, _where(v.image), e)
    seen = {ver for vs in found.values() for ver in vs}
    newer = sorted((x for x in seen if version_key(x) > current), key=version_key, reverse=True)
    out["kind"] = "release"
    out["newest"] = max(seen, key=version_key) if seen else None
    for ver in newer[:MAX_CANDIDATES]:
        cand: dict = {"version": ver, "kind": "release", "variants": []}
        rel = None
        if gh_repo:
            try:
                rel = ck.release_for_version(gh_repo, ver)
            except fetch.FetchError as e:
                cand["notes"] = why("github_error", f"GitHub: {_short(e)}", error=_short(e))
        cand.update(_release_info(rel))
        builds = set()
        for v in recipe.variants:
            base = {"name": v.name, "runtime": v.runtime, "vendor": v.when.vendor}
            if v.runtime == "process":
                continue
            rule = rules.get(v.name)
            if rule is None:
                cand["variants"].append({**base, "available": False, "why": why(
                    "tag_unversioned", "Its image tag carries no version, so it has no newer one to offer.")})
                continue
            if v.name in failed:
                cand["variants"].append({**base, "available": False, "why": why(
                    "registry_error", f"Could not list its tags: {_short(failed[v.name])}",
                    error=_short(failed[v.name]))})
                continue
            vs = found[v.name]
            if ver not in vs:
                last = max(vs, key=version_key) if vs else None
                want = rule.tag_for(ver)
                text = (f"No {want} image in {_where(v.image)}"
                        + (f"; this variant's images stop at {last}." if last else "."))
                cand["variants"].append({**base, "available": False, "why": why(
                    "no_image", text, tag=want, repo=_where(v.image), last=last)})
                continue
            build, tag = pick_tag(vs[ver], rule.build)
            if build is not None:
                builds.add(build)
            registry, repo, _, _ = oci.split_image(v.image)
            try:
                dig = ck.digest(registry, repo, tag)
            except fetch.FetchError as e:
                cand["variants"].append({**base, "available": False, "tag": tag, "why": why(
                    "digest_failed", f"Could not read the digest of {tag}: {_short(e)}", error=_short(e))})
                continue
            cand["variants"].append({**base, "available": True, "tag": tag, "digest": dig,
                                     "image": f"{_image_name(v.image)}:{tag}@{dig}"})
        # Process variants: the release of the build the images name (llama.cpp's b<build>), else of the version.
        for v in recipe.variants:
            if v.runtime != "process":
                continue
            old_tag = (_RELEASE_URL.match(v.engine.url).group(2) if v.engine and _RELEASE_URL.match(v.engine.url)
                       else None)
            if old_tag and re.fullmatch(r"b\d+", old_tag):
                new_tag = f"b{max(builds)}" if len(builds) == 1 else None
                pv = process_variant(ck, v, new_tag)
            else:
                pv = process_variant(ck, v, rel.get("tag_name") if rel else None, rel)
            cand["variants"].append(pv)
        order = {v.name: i for i, v in enumerate(recipe.variants)}
        cand["variants"].sort(key=lambda x: order.get(x["name"], 99))
        _notes(ck, recipe, gh_repo, rel, cand)
        out["candidates"].append(cand)
    # A release on GitHub newer than every image: said, not offered (vLLM-Omni 0.34.0 had no image for days).
    if gh_repo and not failed:
        try:
            listed = ck.releases(gh_repo)
        except fetch.FetchError:
            listed = []
        top = out["candidates"][0]["version"] if out["candidates"] else out.get("newest") or recipe.version
        ahead = [m.group(1) for r in listed if not r.get("prerelease")
                 for m in [_SEMVER_TAG.match(str(r.get("tag_name") or ""))] if m and version_key(m.group(1)) >
                 version_key(top) and len(m.group(1).split(".")) == len(top.split("."))]
        if ahead:
            best = max(ahead, key=version_key)
            out["upstream"] = why("release_without_image", f"GitHub has a release {best} with no image yet: it is "
                                  "offered once its images are published.", version=best)
    return _status(out, bool(found))


def _check_rolling(ck: Checker, recipe: Recipe, out: dict) -> dict:
    """Engines published only under moving tags: a newer build when a tag points at a later image."""
    out["kind"] = "rolling"
    gh_repo = GITHUB_REPOS.get(recipe.engine)
    state: dict[str, dict] = {}
    for v in recipe.variants:
        if v.runtime != "container" or not v.image:
            continue
        registry, repo, tag, pinned = oci.split_image(v.image)
        try:
            now = ck.digest(registry, repo, tag)
            if pinned and now == pinned:
                state[v.name] = {"moved": False}
                continue
            new = ck.labels(registry, repo, now)
            old = ck.labels(registry, repo, pinned) if pinned else {"created": ""}
            if old.get("created") and new.get("created") and new["created"] <= old["created"]:
                state[v.name] = {"moved": False, "older": True}
                continue
            key = new.get("revision") or now
            state[v.name] = {"moved": True, "key": key, "tag": tag, "digest": now, "labels": new}
        except fetch.FetchError as e:
            _registry_error(out, _where(v.image), e)
            state[v.name] = {"error": e}
    groups: dict[str, list[str]] = {}
    for name, st in state.items():
        if st.get("moved"):
            groups.setdefault(st["key"], []).append(name)
    cands = []
    for key, names in groups.items():
        first = state[names[0]]
        rev = first["labels"].get("revision") or ""
        rel, note = None, None
        if gh_repo and rev:
            try:
                rel = next((r for r in ck.releases(gh_repo) if str(r.get("tag_name") or "").endswith("-" + rev[:7])),
                           None)
            except fetch.FetchError as e:
                note = why("github_error", f"GitHub: {_short(e)}", error=_short(e))
        label = (rel or {}).get("tag_name") or (rev[:7] if rev else first["digest"][:19])
        cand: dict = {"version": label, "kind": "build", "revision": rev or None,
                      "created": first["labels"].get("created") or None, "variants": []}
        cand.update(_release_info(rel))
        if note:
            cand["notes"] = note
        for v in recipe.variants:
            base = {"name": v.name, "runtime": v.runtime, "vendor": v.when.vendor}
            if v.runtime == "process":
                cand["variants"].append(process_variant(ck, v, rel.get("tag_name") if rel else None, rel))
                continue
            st = state.get(v.name) or {}
            if st.get("error") is not None:
                cand["variants"].append({**base, "available": False, "why": why(
                    "registry_error", f"Could not read its tag: {_short(st['error'])}", error=_short(st["error"]))})
            elif st.get("moved") and st["key"] == key:
                cand["variants"].append({**base, "available": True, "tag": st["tag"], "digest": st["digest"],
                                         "image": f"{_image_name(v.image)}:{st['tag']}@{st['digest']}"})
            elif st.get("moved"):
                cand["variants"].append({**base, "available": False, "why": why(
                    "other_build", "Its tag points at another newer build.", build=state[v.name]["key"][:12])})
            elif st.get("older"):
                cand["variants"].append({**base, "available": False, "why": why(
                    "not_newer", "Its tag moved, but to an image made before the pinned one.")})
            else:
                cand["variants"].append({**base, "available": False, "why": why(
                    "not_moved", "Its tag still points at the pinned build.")})
        _notes(ck, recipe, gh_repo, rel, cand)
        cands.append(cand)
    out["candidates"] = sorted(cands, key=lambda c: c.get("created") or "", reverse=True)[:MAX_CANDIDATES]
    return _status(out, any("error" not in st for st in state.values()))


def _status(out: dict, read_any: bool) -> dict:
    if out["candidates"]:
        out["status"] = "newer_build" if out["kind"] == "rolling" else "newer"
    elif out["errors"] and not read_any:
        out["status"] = "could_not_check"
    elif out["errors"]:
        out["status"] = "partly_checked"
    else:
        out["status"] = "up_to_date"
    return out


# ------------------------------------------------------------------ all of them
def check_all(*, by: str, allow_private: bool = False, recipes: Optional[dict[str, Recipe]] = None,
              seconds: float = CHECK_SECONDS, resolver=None) -> dict:
    """Every built-in engine (or the given recipes). Never raises for a registry or GitHub: each engine says."""
    started = time.monotonic()
    ck = Checker(allow_private=allow_private, seconds=seconds, resolver=resolver)
    todo = recipes if recipes is not None else {n: r for n, (r, _) in builtin_recipes().items()}
    # The tag listings first, in parallel (one per repository); then each engine reads them from the cache.
    repos = sorted({oci.split_image(v.image)[:2] for r in todo.values() if not own_image(r)
                    for v in r.variants if v.runtime == "container" and v.image
                    and tag_rule(v, r.version) is not None})

    def prefetch(pair):
        try:
            ck.tags(*pair)
        except fetch.FetchError:
            pass   # each engine reports it

    def one(item) -> tuple[str, dict]:
        name, recipe = item
        try:
            return name, check_recipe(ck, recipe)
        except Exception as e:   # noqa: BLE001 - one engine's surprise must not hide the others
            return name, {"recipe": name, "title": recipe.title, "engine": recipe.engine, "version": recipe.version,
                          "kind": "release", "status": "could_not_check", "candidates": [],
                          "errors": [{"where": name, "text": _short(e)}]}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(prefetch, repos))
        engines = dict(pool.map(one, sorted(todo.items())))
    return {"checked_at": utcnow().isoformat(), "checked_by": by,
            "seconds": round(time.monotonic() - started, 1), "github_requests": ck.github_calls, "engines": engines}


# ------------------------------------------------------------------ the stored result
def stored(db: DbSession) -> Optional[dict]:
    row = db.get(ServerSetting, SETTING_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) and row.value.get("engines") else None


def store(db: DbSession, result: dict, by: str) -> None:
    row = db.get(ServerSetting, SETTING_KEY)
    if row is None:
        row = ServerSetting(key=SETTING_KEY)
        db.add(row)
    row.value = result
    row.updated_by = by
    row.updated_at = utcnow()


def summary(result: dict) -> dict:
    """For the audit log: the newer versions per engine and the engines that could not be checked."""
    engines = result.get("engines") or {}
    return {"newer": {n: [c["version"] for c in e.get("candidates") or []] for n, e in engines.items()
                      if e.get("candidates")} or None,
            "could_not_check": sorted(n for n, e in engines.items() if e.get("status") == "could_not_check") or None,
            "seconds": result.get("seconds")}

