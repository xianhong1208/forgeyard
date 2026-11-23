"""Upstream signatures of engine images and archives (1.5.2, issue #95, decision Q8 B): verified on the server when an
admin tries a newer engine version, for the engines whose projects publish them. Agents never verify: they run the
digest the server verified and pinned.

Who signs what (2026-11-23; tests/fixtures/engine_updates/ holds real bundles):

- llama.cpp: GitHub artifact attestations (SLSA build provenance in a Sigstore bundle) for every image it pushes to
  ghcr.io and for its release archives, signed by its own GitHub Actions workflows.
- vLLM, vLLM-Omni (Docker Hub), speaches (ghcr.io): no signatures and no attestations.
- stable-diffusion.cpp: an unsigned BuildKit provenance manifest inside the image index: nothing to verify.

For a signed engine (SOURCES), Try fetches the attestations of the exact digest (an image index digest, or an archive's
SHA-256) from GitHub's API (`GET /repos/<repo>/attestations/sha256:<hex>`, anonymous) through the server's egress
rules, and verifies one with sigstore-python against Sigstore's public-good trust root: the certificate chains to
Fulcio and was valid when the entry was logged in Rekor (its inclusion proof and signed checkpoint), the signature
covers the statement, the signer is a GitHub Actions workflow *of that same repository* (OIDC issuer, source
repository and the workflow's own path all checked), and the statement's subject is that digest. The identity is
recorded (repository, workflow, ref, commit, the run) and shown on the copy's page.

- No attestation for the digest, or none that verifies: the copy is refused (the reason says which check failed).
- GitHub or Sigstore's trust root cannot be reached: "cannot verify", the copy is refused as well. Not verifying would
  make a signed engine's copy look like an unsigned one's; an air-gapped site adds such an engine by hand (Add a
  custom engine), and nothing is verified for it.
- An engine that publishes nothing: recorded as "unsigned upstream", said plainly in the dialog and on the page. Trust
  then rests on the repository being the built-in's own and the digest being fixed at Try.

The trust root (Fulcio's certificates, Rekor's and the CT logs' keys) comes from Sigstore's TUF repository
(tuf-repo-cdn.sigstore.dev), refreshed at each Try through the same egress rules (a TUF fetcher over
forgeyard.models.fetch), its metadata kept in DATA_DIR/sigstore. The first refresh starts from the root.json shipped in
the sigstore package; TUF then follows every root rotation signed by the previous root. When the refresh fails the
Try is refused (cannot verify): a cached root could be one that Sigstore has since replaced.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import quote

from forgeyard.models import fetch

log = logging.getLogger("forgeyard")

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
TUF_URL = "https://tuf-repo-cdn.sigstore.dev"
ATTESTATIONS_MAX_BYTES = 8 * 1024 * 1024
TUF_MAX_BYTES = 4 * 1024 * 1024
PREDICATES = ("https://slsa.dev/provenance/v1",)
STATEMENT_TYPE = "application/vnd.in-toto+json"

# Engines (a recipe's `engine`) whose projects sign what they publish, and how.
SOURCES: dict[str, dict] = {
    "llama.cpp": {"kind": "github-attestation", "repository": "ggml-org/llama.cpp", "images": True, "archives": True},
}
# What the dialog and the page say about the others (checked 2026-11-23).
UNSIGNED = {
    "vllm": "vLLM publishes no signatures or attestations for its images.",
    "vllm-omni": "vLLM-Omni publishes no signatures or attestations for its images.",
    "speaches": "speaches publishes no signatures or attestations for its images.",
    "stable-diffusion.cpp": "stable-diffusion.cpp publishes only an unsigned build record (BuildKit provenance) with "
                            "its images: there is no signature to verify.",
}

# Certificate extensions (Fulcio's OIDs, https://github.com/sigstore/fulcio/blob/main/docs/oid-info.md).
_OID_SIGNER = "1.3.6.1.4.1.57264.1.9"        # build signer URI: the workflow file @ ref
_OID_REPO = "1.3.6.1.4.1.57264.1.12"         # source repository URI
_OID_COMMIT = "1.3.6.1.4.1.57264.1.13"       # source repository digest
_OID_REF = "1.3.6.1.4.1.57264.1.14"          # source repository ref
_OID_RUN = "1.3.6.1.4.1.57264.1.21"          # run invocation URI


class SignatureError(Exception):
    """Try refuses the copy. `code`: signature_failed (verification failed: the reason says which check),
    signature_missing (the project signs, but nothing for this digest), cannot_verify (GitHub or Sigstore's trust root
    could not be reached)."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


def source_for(engine: str) -> Optional[dict]:
    return SOURCES.get(engine)


def about(engine: str) -> dict:
    """What the console says before a Try: {"signed": True, "repository", "kind"} or {"signed": False, "text"}."""
    src = SOURCES.get(engine)
    if src is not None:
        return {"signed": True, "kind": src["kind"], "repository": src["repository"]}
    return {"signed": False, "text": UNSIGNED.get(engine, f"{engine} publishes no signatures that Forgeyard can "
                                                          "verify.")}


def unsigned(engine: str) -> dict:
    """The record of an image or archive whose project signs nothing."""
    return {"status": "unsigned", "note": about(engine).get("text")}


# ------------------------------------------------------------------ the trust root (TUF, through the egress rules)
def _tuf_fetcher(policy: fetch.Policy):
    from tuf.api import exceptions as tuf_errors
    from tuf.ngclient import FetcherInterface

    class Fetcher(FetcherInterface):
        """TUF's downloads through forgeyard.models.fetch: https and public addresses only, every redirect checked."""

        def _fetch(self, url: str) -> Iterator[bytes]:
            try:
                resp = fetch.open_url(url, policy)
            except fetch.FetchError as e:
                raise tuf_errors.DownloadError(str(e)) from None
            if resp.status >= 400:
                resp.close()
                raise tuf_errors.DownloadHTTPError(f"HTTP {resp.status}", resp.status)

            def chunks() -> Iterator[bytes]:
                try:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            return
                        yield chunk
                finally:
                    resp.close()

            return chunks()

    return Fetcher()


_tuf_lock = threading.Lock()


def tuf_trusted_root(policy: fetch.Policy, data_dir: Path):
    """Sigstore's trusted root, refreshed now from its TUF repository (SignatureError cannot_verify when that fails)."""
    from sigstore._utils import read_embedded
    from sigstore.models import TrustedRoot
    from tuf.ngclient import Updater, UpdaterConfig

    base = data_dir / "sigstore" / quote(TUF_URL, safe="")
    meta, targets = base / "metadata", base / "targets"
    with _tuf_lock:
        try:
            meta.mkdir(parents=True, exist_ok=True)
            targets.mkdir(parents=True, exist_ok=True)
            local = meta / "root.json"
            bootstrap = local.read_bytes() if local.exists() else read_embedded("root.json", TUF_URL)
            updater = Updater(metadata_dir=str(meta), metadata_base_url=TUF_URL, target_dir=str(targets),
                              target_base_url=f"{TUF_URL}/targets/", fetcher=_tuf_fetcher(policy),
                              config=UpdaterConfig(app_user_agent=fetch.USER_AGENT, targets_max_length=TUF_MAX_BYTES),
                              bootstrap=bootstrap)
            updater.refresh()
            info = updater.get_targetinfo("trusted_root.json")
            if info is None:
                raise SignatureError("cannot_verify", "Sigstore's trust root lists no trusted_root.json.")
            path = updater.find_cached_target(info) or updater.download_target(info)
            return TrustedRoot.from_file(path)
        except SignatureError:
            raise
        except Exception as e:   # noqa: BLE001 - TUF's errors are many; each means "not refreshed"
            raise SignatureError("cannot_verify", f"Could not refresh Sigstore's trust root from {TUF_URL}: "
                                                  f"{str(e)[:200] or type(e).__name__}. Signatures cannot be verified "
                                                  "without it.") from None


# A refreshed root serves the other digests of the same Try (llama.cpp: four images and an archive) and the next
# few minutes' Tries, so one Try refreshes it once.
ROOT_TTL = 300.0
_roots: dict[str, tuple[float, object]] = {}


def cached_root(policy: fetch.Policy, data_dir: Path):
    import time

    key = str(data_dir)
    held = _roots.get(key)
    if held is not None and held[0] > time.monotonic():
        return held[1]
    root = tuf_trusted_root(policy, data_dir)
    _roots[key] = (time.monotonic() + ROOT_TTL, root)
    return root


def reset() -> None:
    _roots.clear()


# Tests replace this: (policy, data_dir) -> sigstore.models.TrustedRoot.
trusted_root: Callable = cached_root


# ------------------------------------------------------------------ verifying one digest
def _utf8_value(ext_value) -> str:
    """The text of one of Fulcio's v2 extensions (a DER UTF8String) or of a v1 one (raw bytes)."""
    raw = getattr(ext_value, "value", b"") or b""
    if len(raw) >= 2 and raw[0] == 0x0C:
        n, i = raw[1], 2
        if n & 0x80:
            k = n & 0x7F
            n, i = int.from_bytes(raw[2:2 + k], "big"), 2 + k
        return raw[i:i + n].decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def identity(cert) -> dict:
    from cryptography.x509 import ObjectIdentifier

    out = {}
    for key, oid in (("signer", _OID_SIGNER), ("repository_uri", _OID_REPO), ("commit", _OID_COMMIT),
                     ("ref", _OID_REF), ("run", _OID_RUN)):
        try:
            out[key] = _utf8_value(cert.extensions.get_extension_for_oid(ObjectIdentifier(oid)).value)
        except Exception:   # noqa: BLE001 - an extension the certificate does not carry
            out[key] = None
    return out


class _SignerInRepository:
    """The workflow that signed is a file of the same repository (not a reusable workflow of someone else's)."""

    def __init__(self, repository: str):
        self.prefix = f"https://github.com/{repository}/.github/workflows/"

    def verify(self, cert) -> None:
        from sigstore.errors import VerificationError

        signer = identity(cert).get("signer") or ""
        if not signer.startswith(self.prefix):
            raise VerificationError(f"signed by {signer or 'an unknown workflow'}, not a workflow of "
                                    f"{self.prefix.removesuffix('/.github/workflows/').removeprefix('https://github.com/')}")


def _policy(repository: str):
    from sigstore.verify import policy

    return policy.AllOf([policy.OIDCIssuerV2(GITHUB_ISSUER),
                         policy.OIDCSourceRepositoryURI(f"https://github.com/{repository}"),
                         _SignerInRepository(repository)])


def _attestations(repository: str, digest: str, policy: fetch.Policy) -> list[dict]:
    from forgeyard.models import engineversions

    url = f"{engineversions.GITHUB_API}/repos/{repository}/attestations/{quote(digest, safe=':')}?per_page=30"
    try:
        data = fetch.get_bytes(url, policy, limit=ATTESTATIONS_MAX_BYTES,
                               headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    except fetch.FetchError as e:
        if e.status == 404:
            return []
        if e.status in (403, 429):
            raise SignatureError("cannot_verify", "GitHub's limit for anonymous requests was reached (60 an hour), so "
                                                  "the signature could not be fetched; try again later.") from None
        raise SignatureError("cannot_verify", f"Could not fetch the attestations from GitHub: {e}") from None
    try:
        body = json.loads(data)
    except ValueError:
        raise SignatureError("cannot_verify", "GitHub's answer for the attestations is not JSON.") from None
    found = body.get("attestations") if isinstance(body, dict) else None
    return [a for a in found or [] if isinstance(a, dict) and isinstance(a.get("bundle"), dict)]


def verify(engine: str, digest: str, *, what: str, policy: fetch.Policy, data_dir: Path) -> dict:
    """What was verified about one digest ("sha256:<hex>": an image index, or an archive's SHA-256) of a signed
    engine's project: {"status": "verified", …identity}. Raises SignatureError. `what` names it in messages."""
    from sigstore.models import Bundle
    from sigstore.verify import Verifier

    src = SOURCES[engine]
    repository = src["repository"]
    found = _attestations(repository, digest, policy)
    if not found:
        raise SignatureError("signature_missing", f"{repository} signs what it publishes, but GitHub has no attestation "
                                                  f"for {what} ({digest[:19]}…): it is not taken.")
    verifier = Verifier(trusted_root=trusted_root(policy, data_dir))
    want = digest.removeprefix("sha256:")
    problems: list[str] = []
    for att in found[:10]:
        try:
            bundle = Bundle.from_json(json.dumps(att["bundle"]))
            payload_type, payload = verifier.verify_dsse(bundle, _policy(repository))
            if payload_type != STATEMENT_TYPE:
                raise ValueError(f"the signed payload is {payload_type}, not an in-toto statement")
            statement = json.loads(payload)
            ptype = statement.get("predicateType")
            if ptype not in PREDICATES:
                raise ValueError(f"the statement is {ptype}, not build provenance")
            subjects = [s for s in statement.get("subject") or [] if isinstance(s, dict)]
            if not any((s.get("digest") or {}).get("sha256") == want for s in subjects):
                raise ValueError(f"the statement is about other artifacts, not {digest[:19]}…")
        except Exception as e:   # noqa: BLE001 - sigstore raises several kinds; each is a failed check
            problems.append(str(e)[:240] or type(e).__name__)
            continue
        ident = identity(bundle.signing_certificate)
        signer = ident.get("signer") or ""
        workflow = signer.removeprefix(f"https://github.com/{repository}/").split("@", 1)[0]
        entry = bundle.log_entry
        integrated = getattr(getattr(entry, "_inner", None), "integrated_time", None)
        return {"status": "verified", "source": src["kind"], "repository": repository, "workflow": workflow,
                "ref": ident.get("ref"), "commit": ident.get("commit"), "run": ident.get("run"),
                "predicate": ptype, "issuer": GITHUB_ISSUER,
                "log_index": getattr(getattr(entry, "_inner", None), "log_index", None),
                "signed_at": _iso(integrated)}
    raise SignatureError("signature_failed", f"The signature of {what} ({digest[:19]}…) did not verify: "
                                             f"{problems[0] if problems else 'no attestation could be read'}. "
                                             "It is not taken.")


def _iso(epoch) -> Optional[str]:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def payload_of(att: dict) -> dict:
    """The in-toto statement of an attestation, unverified (tests and diagnostics)."""
    return json.loads(base64.b64decode(att["bundle"]["dsseEnvelope"]["payload"]))
