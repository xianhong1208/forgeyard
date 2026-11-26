"""The OpenID Connect client side of single sign-on (1.6.0, #39): discovery, the authorization URL, the code exchange
and the ID token check.

- Authorization Code flow with PKCE (S256), `state` and `nonce` (RFC 9700). The verifier and the nonce never leave
  the server (forgeyard/security/sso.py keeps them, found by the hash of `state`).
- The provider's endpoints come from its discovery document, whose `issuer` must be exactly the configured one.
- The ID token: signed with RS256 or ES256 only (never `none`, never HMAC with the public key) by a key of the
  provider's JWKS; `iss` is the issuer, `aud` names the client id (and `azp` is the client id when there are several
  audiences), `exp` has not passed and `iat` is not in the future (60 s of clock skew either way), `nonce` is the one
  this sign-in sent, and `sub` is there.
- Every call to the provider: TLS verified against the system's CAs plus SSO_CA_FILE, no proxy from the
  environment, no redirects followed, 10 s timeouts. Plain http only for a provider on this host (localhost,
  127.0.0.1, ::1), like Google's own rule for redirect addresses.

Authlib (the token request and the client authentication) and joserfc (JWS) are imported only here, when a provider is used:
the `server` extra installs them. Without them `available()` is False and the console says so.
"""

from __future__ import annotations

import hmac
import ipaddress
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

import httpx

from forgeyard.config import get_settings

ALGORITHMS = ("RS256", "ES256")
LEEWAY = 60                 # seconds of clock skew accepted for exp and iat
FRESH_SECONDS = 300         # a sign-in at the provider this recent stands in for the password (max_age=0)
CACHE_SECONDS = 300         # discovery documents and key sets are fetched again after this long
TIMEOUT = 10.0
GOOGLE = "https://accounts.google.com"
# Tests hand the client a transport that plays the provider (httpx.MockTransport); None: the network.
transport: Optional[httpx.BaseTransport] = None


class OidcError(Exception):
    """The provider could not be used, or what it sent is not acceptable. `code` is the API's error code, `reason`
    the audit log's word for it."""

    def __init__(self, code: str, reason: str, message: str):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message


def clock() -> float:
    """Seconds since the epoch, for exp, iat and auth_time (tests move it)."""
    return time.time()


def available() -> bool:
    try:
        import authlib  # noqa: F401
        import joserfc  # noqa: F401
    except ImportError:
        return False
    return True


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_problem(url: str) -> Optional[str]:
    """Why `url` cannot be a provider address (issuer or endpoint), or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "is not a URL"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "must be an https:// address"
    if parts.query or parts.fragment or parts.username or parts.password:
        return "must not carry a query, a fragment or a user name"
    if parts.scheme == "http" and not _loopback(parts.hostname):
        return "must be an https:// address (plain http only for a provider on this host)"
    return None


def normalise_issuer(raw: str) -> str:
    """The issuer as typed, without spaces around. A trailing slash is kept: some providers publish one (Authentik),
    most do not (Keycloak, Entra ID, Google); discovery tells which (`discover`)."""
    return raw.strip()


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    extra = get_settings().sso_ca_file.strip()
    if extra:
        ctx.load_verify_locations(cafile=extra)
    return ctx


def _client_options() -> dict:
    options = {"verify": _ssl_context(), "trust_env": False, "follow_redirects": False,
               "timeout": httpx.Timeout(TIMEOUT)}
    if transport is not None:
        options["transport"] = transport
    return options


def _get_json(url: str, what: str) -> dict:
    try:
        with httpx.Client(**_client_options()) as c:
            r = c.get(url, headers={"Accept": "application/json"})
    except (httpx.HTTPError, ssl.SSLError, OSError) as e:
        raise OidcError("sso_provider_unreachable", f"{what}_unreachable",
                        f"Could not reach the identity provider's {what} at {url}: {e}") from None
    if r.status_code != 200:
        raise OidcError("sso_provider_unreachable", f"{what}_status",
                        f"The identity provider's {what} at {url} answered HTTP {r.status_code}.")
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise OidcError("sso_provider_unreachable", f"{what}_not_json",
                        f"The identity provider's {what} at {url} is not a JSON object.")
    return data


class _Cache:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        with self._lock:
            hit = self._items.get(key)
            return hit[1] if hit and hit[0] > time.monotonic() else None

    def put(self, key: str, value) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + CACHE_SECONDS, value)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


cache = _Cache()


def reset() -> None:
    cache.reset()


def discover(issuer: str, *, refresh: bool = False) -> dict:
    """The provider's discovery document, checked: its issuer is `issuer` (give or take a trailing slash; use
    meta["issuer"] from then on), and it names https endpoints for
    authorization, tokens and keys, and a signing algorithm Forgeyard accepts."""
    problem = url_problem(issuer)
    if problem:
        raise OidcError("sso_bad_issuer", "issuer_url", f"The issuer {problem}.")
    meta = None if refresh else cache.get("meta:" + issuer)
    if meta is not None:
        return meta
    meta = _get_json(issuer.rstrip("/") + "/.well-known/openid-configuration", "discovery document")
    # The issuer must be the one typed; a trailing slash typed or left out is forgiven, and the provider's own spelling
    # is what ID tokens are checked against (and what accounts are linked under).
    if not isinstance(meta.get("issuer"), str) or meta["issuer"].rstrip("/") != issuer.rstrip("/"):
        raise OidcError("sso_provider_unreachable", "discovery_issuer_mismatch",
                        f"The discovery document names the issuer {meta.get('issuer')!r}, not {issuer!r}. Type the "
                        "issuer exactly as the provider publishes it.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = meta.get(key)
        if not isinstance(value, str) or url_problem(value.split("?", 1)[0]):
            raise OidcError("sso_provider_unreachable", "discovery_endpoint",
                            f"The discovery document has no usable {key}.")
    algs = meta.get("id_token_signing_alg_values_supported")
    if isinstance(algs, list) and not set(algs) & set(ALGORITHMS):
        raise OidcError("sso_provider_unreachable", "discovery_algorithms",
                        f"The provider signs ID tokens with {', '.join(map(str, algs))}; Forgeyard accepts "
                        f"{' or '.join(ALGORITHMS)}.")
    types = meta.get("response_types_supported")
    if isinstance(types, list) and "code" not in types:
        raise OidcError("sso_provider_unreachable", "discovery_response_types",
                        "The provider does not offer the authorization code flow.")
    cache.put("meta:" + issuer, meta)
    return meta


def _key_set(jwks_uri: str, *, refresh: bool = False):
    from joserfc.jwk import KeySet

    keys = None if refresh else cache.get("jwks:" + jwks_uri)
    if keys is None:
        data = _get_json(jwks_uri, "key set")
        try:
            keys = KeySet.import_key_set(data)
        except Exception as e:  # a malformed key: say so, like any other answer we cannot use
            raise OidcError("sso_provider_unreachable", "jwks_invalid",
                            f"The identity provider's key set at {jwks_uri} cannot be read: {e}") from None
        cache.put("jwks:" + jwks_uri, keys)
    return keys


def code_challenge(verifier: str) -> str:
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    return create_s256_code_challenge(verifier)


def authorization_url(meta: dict, *, client_id: str, redirect_uri: str, scopes: str, state: str, nonce: str,
                      verifier: str, fresh: bool) -> str:
    """Where the browser goes to sign in. `fresh`: the provider must ask for the password again (max_age=0, plus
    prompt=login where the provider knows it: Google refuses that value)."""
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scopes,
              "state": state, "nonce": nonce, "code_challenge": code_challenge(verifier),
              "code_challenge_method": "S256"}
    if fresh:
        params["max_age"] = "0"
        if meta.get("issuer") != GOOGLE:
            params["prompt"] = "login"
    endpoint = meta["authorization_endpoint"]
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)


def _auth_method(meta: dict, secret: Optional[str]) -> str:
    if not secret:
        return "none"
    supported = meta.get("token_endpoint_auth_methods_supported")
    if isinstance(supported, list) and "client_secret_basic" not in supported and "client_secret_post" in supported:
        return "client_secret_post"
    return "client_secret_basic"


def exchange(meta: dict, *, client_id: str, client_secret: Optional[str], code: str, verifier: str,
             redirect_uri: str) -> dict:
    """The provider's token answer for `code` (with the PKCE verifier); it must carry an ID token. Authlib encodes the
    request and the client authentication; the request goes out on this module's own client (TLS, no proxy, no
    redirects), not Authlib's HTTP integration."""
    from authlib.oauth2.auth import ClientAuth
    from authlib.oauth2.rfc6749.parameters import prepare_token_request

    body = prepare_token_request("authorization_code", code=code, redirect_uri=redirect_uri, code_verifier=verifier)
    url, headers, body = ClientAuth(client_id, client_secret or None, _auth_method(meta, client_secret)).prepare(
        "POST", meta["token_endpoint"], {"Content-Type": "application/x-www-form-urlencoded",
                                         "Accept": "application/json"}, body)
    try:
        with httpx.Client(**_client_options()) as c:
            r = c.post(url, headers=headers, content=body)
    except (httpx.HTTPError, ssl.SSLError, OSError) as e:
        raise OidcError("sso_token_failed", "token_unreachable",
                        f"Could not get the sign-in from the identity provider: {e}") from None
    try:
        token = r.json()
    except ValueError:
        token = None
    if r.status_code != 200:
        error = token.get("error") if isinstance(token, dict) else None
        raise OidcError("sso_token_failed", "token_refused",
                        f"The identity provider refused the sign-in code (HTTP {r.status_code}"
                        + (f", {str(error)[:80]}" if error else "") + ").")
    if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
        raise OidcError("sso_token_failed", "no_id_token", "The identity provider sent no ID token. Is `openid` in the "
                                                          "scopes?")
    return token


def _invalid(reason: str, message: str) -> OidcError:
    return OidcError("sso_token_invalid", reason, message)


def _header(token: str) -> dict:
    import base64
    import json

    try:
        part = token.split(".", 1)[0]
        header = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, UnicodeDecodeError):
        return {}
    return header if isinstance(header, dict) else {}


def verify_id_token(id_token: str, *, meta: dict, client_id: str, nonce: str) -> dict:
    """The ID token's claims, after every check in the module's docstring; else OidcError (code sso_token_invalid,
    reason alg / signature / iss / aud / exp / iat / nonce / sub)."""
    from joserfc import jwt
    from joserfc.errors import BadSignatureError, InvalidKeyIdError, JoseError, MissingKeyError, \
        UnsupportedAlgorithmError

    def decode(keys):
        if _header(id_token).get("kid") is not None:
            return jwt.decode(id_token, keys, algorithms=list(ALGORITHMS))
        # No kid (allowed when a provider has one key): each published key in turn.
        failure: Exception = InvalidKeyIdError("The ID token names no key.")
        for key in keys.keys:
            try:
                return jwt.decode(id_token, key, algorithms=list(ALGORITHMS))
            except UnsupportedAlgorithmError:
                raise
            except (JoseError, ValueError) as e:
                failure = e
        raise failure

    try:
        try:
            token = decode(_key_set(meta["jwks_uri"]))
        except (InvalidKeyIdError, MissingKeyError, ValueError):
            token = decode(_key_set(meta["jwks_uri"], refresh=True))  # the provider may have rotated its keys
    except UnsupportedAlgorithmError:
        raise _invalid("alg", f"The ID token is not signed with {' or '.join(ALGORITHMS)}.") from None
    except BadSignatureError:
        raise _invalid("signature", "The ID token's signature is not the identity provider's.") from None
    except (InvalidKeyIdError, MissingKeyError, ValueError):
        raise _invalid("signature", "The ID token is signed with a key the identity provider does not publish.") from None
    except JoseError as e:
        raise _invalid("signature", f"The ID token cannot be read: {e}") from None
    claims = token.claims
    now = clock()
    if claims.get("iss") != meta["issuer"]:
        raise _invalid("iss", "The ID token comes from another issuer.")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in audiences:
        raise _invalid("aud", "The ID token is not for this client id.")
    if len(audiences) > 1 and claims.get("azp") not in (None, client_id):
        raise _invalid("aud", "The ID token was issued to another client.")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp + LEEWAY <= now:
        raise _invalid("exp", "The ID token has expired. Check this server's clock, then sign in again.")
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool) or iat - LEEWAY > now:
        raise _invalid("iat", "The ID token is dated in the future. Check this server's clock.")
    got = claims.get("nonce")
    if not isinstance(got, str) or not hmac.compare_digest(got.encode(), nonce.encode()):
        raise _invalid("nonce", "The ID token does not belong to this sign-in.")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise _invalid("sub", "The ID token names no account.")
    return dict(claims)


def fresh_until(claims: dict) -> Optional[float]:
    """Until when (epoch seconds) this sign-in counts as a fresh one, from the provider's auth_time; None when the
    provider did not say when the person signed in, or it was too long ago."""
    at = claims.get("auth_time")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None
    now = clock()
    if at - LEEWAY > now or now - at > FRESH_SECONDS:
        return None
    return min(at, now) + FRESH_SECONDS
