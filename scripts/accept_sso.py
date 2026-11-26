#!/usr/bin/env python3
"""1.6.0 acceptance of single sign-on (#39): a running Forgeyard server against a REAL OpenID Connect provider
(Keycloak, Authentik, …), driven in a real browser (Playwright for Python, Chrome with --no-sandbox).

    FORGEYARD_PASSWORD=… python3 scripts/accept_sso.py --server https://<server>:7860 --ca forgeyard-ca.pem \\
        --user <owner> --issuer https://<idp>/realms/<realm> --client-id forgeyard \\
        --client-secret-file client-secret.txt --idp-ca idp-ca.pem --accounts-file accounts.json \\
        --role-claim groups --admin-values fy-admins --operator-values fy-operators \\
        --allowed-domain example.com --shots sso-shots --out sso.json

--user is the server's owner (an admin with a password and no authenticator app: the script signs in with the password
alone). The provider must have a client for Forgeyard whose redirect URI is the server's PUBLIC_URL + /sso/callback
(the run prints the one the server uses), the role claim in the ID token, and six test accounts. The accounts file is
JSON, one entry per part each account plays:

    {"plain":     {"username": "…", "password": "…"},   a person with no admin or operator value in the role claim
     "admin":     {"username": "…", "password": "…"},   a value of --admin-values in the role claim
     "operator":  {"username": "…", "password": "…"},   a value of --operator-values
     "automatic": {"username": "…", "password": "…"},   an e-mail address in --allowed-domain
     "outsider":  {"username": "…", "password": "…"},   a verified e-mail address in another domain
     "owner":     {"username": "…", "password": "…"}}   the account the owner links to theirs

None of them may have signed in to this server before (the run makes their accounts and removes them again). The
provider's sign-in page is filled as a username and password form (Keycloak's, or any page with a username or e-mail
field, a password field and a submit button; a page that asks for the username first is handled). --idp-form mock is
for oidc-provider-mock (a test provider without passwords: "username" is the subject to sign in as).

--idp-ca is the provider's CA when its certificate is not from a public CA: the operator gives the same file to the
server as SSO_CA_FILE; the script only checks that the provider's discovery document is served with a certificate it
verifies, and that the server has a CA file set.

The checks, numbered as in the report:

  1  the provider: --idp-ca verifies the provider (and the server has SSO_CA_FILE); Check (POST
     /api/identity-provider/check) reads its discovery document; it is saved with the role mapping (approve mode,
     no domains), and the sign-in page shows "Sign in with <name>"
  2  approve mode: the first sign-in of the plain account answers pending_approval, shows "Waiting for approval",
     leaves no code in the address bar and starts no session; the account is pending (role user); the owner approves
     it; the next sign-in works: role user, the session's auth_method sso
  3  role mapping: the admin and operator accounts, approved the same way, sign in as admin and operator
  4  automatic mode with --allowed-domain: a new account (automatic) is active at once; the outsider is refused with
     sso_domain_not_allowed and no account is made for it
  5  a disabled account: the plain account disabled by the owner: its session ends, and its next sign-in is refused
     with account_disabled
  6  linking: the owner, signed in with the password, links the owner account from My account (password, then a
     fresh sign-in at the provider); a sign-in through the provider is then the owner (admin, auth_method sso); the
     admin account (no password) may not unlink its only way in (last_way_in); the owner unlinks from My account
  7  two-factor exemption: with two-factor sign-in required of everyone (grace 0 days), a password session of an
     account without an authenticator reaches only the setup (403 mfa_enrolment_required, the setup page), while a
     session through the provider is not sent to it. The setting goes back to what it was right after (set and
     restored with the admin account's session through the provider, which the setting does not stop)
  8  the secret-key backup of the admin account (single sign-on only): once its sign-in is more than 5 minutes old,
     the backup is refused with fresh_sign_in_required; "Sign in again with <name>" in the backup dialog makes the
     session fresh, and the backup then downloads (the file is thrown away). Only when the key was already marked as
     backed up before the run: a download marks it backed up by that account, so the owner confirms it again
     afterwards; otherwise the download is a SKIP saying why
  9  API keys: a key the plain account made while signed in through the provider answers /v1/models; once the
     account is disabled (step 5) the key gets 401 account_disabled
  10 state reuse: a callback already used is refused (sso_state_invalid); a callback opened in another browser is
     refused (sso_state_invalid, audited as browser_mismatch), and the browser that started it cannot use it
     afterwards either (audited as state_invalid)

Step 8 waits for the admin account's sign-in of step 3 to be 5 minutes old, so a run takes about 6 minutes.

Before anything it refuses to run when the server already has an identity provider, unless --replace-provider
(then that provider's settings are put back at the end; one with a client secret also needs
--previous-client-secret-file, since the server never shows a stored secret). Everything it creates is removed or
put back at the end, also after a failure or Ctrl-C: the provider (deleted, or the previous one restored), the
accounts the sign-ins made, the throwaway password account (accept-<run>-user), the API key, the owner's link, the
two-factor setting and the backup record (re-confirmed by --user). The provider's own accounts are not changed.
Audit events stay. The three refused callbacks of step 10 count against the server's single sign-on back-off
(5 free failures since the server started; after that each further failure waits, doubling up to 5 minutes).

Nothing it prints or saves contains a password, the client secret, a session cookie, an authorization code or a
state: they are scrubbed from every message, and the screenshots are of Forgeyard's own pages.

It runs alone, not as a group of accept_all.py: it changes server-wide sign-in settings (the provider, the
two-factor requirement) for a few seconds to minutes and needs a provider set up for it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
import sys
import time
import traceback
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accept_all as A  # noqa: E402

TITLE = "single sign-on through a real OpenID Connect provider (1.6.0)"
PARTS = ("plain", "admin", "operator", "automatic", "outsider", "owner")
FRESH_SECONDS = 300        # a sign-in this recent stands in for a password (forgeyard/security/oidc.py)
IDP_TIMEOUT = 60_000       # ms: a round trip through the provider's sign-in page
PAGE_TIMEOUT = 45_000


# ------------------------------------------------------------------ options
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="https://127.0.0.1:7860", help="the server's address")
    ap.add_argument("--ca", "--cacert", dest="ca", help="the server's CA (forgeyard-ca.pem) when it uses Forgeyard's own CA")
    ap.add_argument("--user", default="owner", help="the server's owner (password from FORGEYARD_PASSWORD, or asked)")
    ap.add_argument("--issuer", required=True, help="the provider's issuer URL (its discovery document is under it)")
    ap.add_argument("--client-id", required=True, help="Forgeyard's client id at the provider")
    ap.add_argument("--client-secret-file", help="a file holding the client secret (left out: a public client)")
    ap.add_argument("--idp-ca", help="the provider's CA (PEM), the file the server has as SSO_CA_FILE")
    ap.add_argument("--accounts-file", required=True, help="JSON: the six test accounts at the provider (see above)")
    ap.add_argument("--role-claim", default="groups", help="the ID token claim the role mapping reads (default groups)")
    ap.add_argument("--admin-values", default="", help="comma-separated values of the claim that mean admin")
    ap.add_argument("--operator-values", default="", help="comma-separated values of the claim that mean operator")
    ap.add_argument("--allowed-domain", required=True, help="the e-mail domain of automatic mode (step 4)")
    ap.add_argument("--provider-name", help="the name the sign-in page shows (default accept-<run>-idp)")
    ap.add_argument("--idp-form", choices=("form", "mock"), default="form",
                    help="how the provider's sign-in page is filled: form (a username and password form, Keycloak's "
                    "among them) or mock (oidc-provider-mock, for trying the script locally)")
    ap.add_argument("--replace-provider", action="store_true",
                    help="run although the server has an identity provider (it is put back at the end)")
    ap.add_argument("--previous-client-secret-file", help="with --replace-provider: the replaced provider's client "
                    "secret, to put it back")
    ap.add_argument("--shots", help="save screenshots of Forgeyard's pages in this directory")
    ap.add_argument("--chrome", default="/opt/google/chrome/chrome", help="the Chrome to drive")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--install-playwright", action="store_true",
                    help="install Playwright for Python into --venv when it is missing")
    ap.add_argument("--venv", default=str(Path.home() / ".cache" / "forgeyard-accept-all"),
                    help="the virtual environment Playwright is installed into")
    ap.add_argument("--out", help="also write the report as JSON here")
    args = ap.parse_args(argv)
    args.server = args.server.rstrip("/")
    args.issuer = args.issuer.rstrip("/")
    if not args.admin_values.strip() or not args.operator_values.strip():
        ap.error("--admin-values and --operator-values are needed (step 3 checks both roles)")
    if args.previous_client_secret_file and not args.replace_provider:
        ap.error("--previous-client-secret-file goes with --replace-provider")
    for name in ("client_secret_file", "idp_ca", "accounts_file", "previous_client_secret_file"):
        path = getattr(args, name)
        if path and not Path(path).is_file():
            ap.error(f"--{name.replace('_', '-')}: no such file: {path}")
    try:
        args.accounts = load_accounts(Path(args.accounts_file).read_text(), mock=args.idp_form == "mock")
    except ValueError as e:
        ap.error(f"--accounts-file: {e}")
    try:
        args.domain = domain_of(args.allowed_domain)
    except ValueError as e:
        ap.error(f"--allowed-domain: {e}")
    # What accept_all's Run and start() read, for the parts of it this script does not have.
    for name in ("gpu_machine", "cpu_machine", "second_cpu_machine", "relay_machine", "relay_second_machine",
                 "engine_machine", "second_engine_machine", "cloud_provider"):
        setattr(args, name, None)
    args.tts_asr = "none"
    args.plan = [("sso", "")]
    return args


# ------------------------------------------------------------------ pure helpers (tests/test_accept_sso.py)
@dataclass(frozen=True)
class Account:
    part: str
    username: str
    password: str = ""


def load_accounts(text: str, mock: bool = False) -> dict[str, Account]:
    """The accounts file: {part: {"username", "password"}} for each of PARTS, six different usernames. The mock
    provider has no passwords."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"not JSON ({e})") from None
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object")
    missing = [p for p in PARTS if p not in data]
    if missing:
        raise ValueError(f"missing {', '.join(missing)} (needs {', '.join(PARTS)})")
    unknown = sorted(set(data) - set(PARTS))
    if unknown:
        raise ValueError(f"unknown part {unknown[0]!r} (the parts are {', '.join(PARTS)})")
    out = {}
    for part in PARTS:
        entry = data[part]
        if not isinstance(entry, dict) or not isinstance(entry.get("username"), str) or not entry["username"].strip():
            raise ValueError(f"{part}: needs a username")
        password = entry.get("password", "")
        if not isinstance(password, str) or (not mock and not password):
            raise ValueError(f"{part}: needs a password")
        out[part] = Account(part, entry["username"].strip(), password)
    names = [a.username.lower() for a in out.values()]
    if len(set(names)) != len(names):
        raise ValueError("each part needs its own account (a username is used twice)")
    return out


def domain_of(raw: str) -> str:
    d = raw.strip().lower().lstrip("@").rstrip(".")
    if not re.fullmatch(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", d):
        raise ValueError(f"{raw!r} is not a domain name (example.com)")
    return d


def values_of(raw: str) -> list[str]:
    return [v.strip() for v in raw.split(",") if v.strip()]


def role_mapping(admin_values: str, operator_values: str) -> dict[str, str]:
    """{claim value: role}; a value given for both roles is admin."""
    out = {v: "operator" for v in values_of(operator_values)}
    out.update({v: "admin" for v in values_of(admin_values)})
    return out


def provider_body(args, name: str, secret: Optional[str], automatic: bool) -> dict:
    """The PUT /api/identity-provider body (every field: a PUT sets them all). secret None keeps the stored one."""
    body = {"name": name, "issuer": args.issuer, "client_id": args.client_id, "scopes": "openid email profile",
            "role_claim": args.role_claim, "role_mapping": role_mapping(args.admin_values, args.operator_values),
            "new_accounts": "automatic" if automatic else "approve",
            "allowed_domains": [args.domain] if automatic else [], "enabled": True}
    if secret is not None:
        body["client_secret"] = secret
    return body


def restore_body(previous: dict, secret: Optional[str]) -> dict:
    """The PUT body that puts a provider (as GET /api/identity-provider shows it) back."""
    body = {k: previous[k] for k in ("name", "issuer", "client_id", "scopes", "role_claim", "role_mapping",
                                     "new_accounts", "allowed_domains", "enabled")}
    if secret is not None:
        body["client_secret"] = secret
    elif not previous.get("has_client_secret"):
        body["client_secret"] = ""
    return body


_QUERY_SECRETS = re.compile(r"(?i)\b(code|state|session_state|nonce|code_verifier|id_token|access_token|refresh_token|"
                            r"token|client_secret|password)=[^&\s\"'<>]+")
_COOKIES = re.compile(r"(?i)\b(forgeyard_session|forgeyard_sso|KEYCLOAK_[A-Z_]+|AUTH_SESSION_ID[A-Z_]*)=[^;\s\"']+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEYS = re.compile(r"\bfyk_[A-Za-z0-9_-]{6,}")


def scrub(text: str, secrets_: tuple[str, ...] = ()) -> str:
    """A message without the run's secrets: the given values, codes and states in URLs, cookies, bearer tokens and
    API keys."""
    out = str(text)
    for s in sorted({s for s in secrets_ if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(s, "***")
    out = _QUERY_SECRETS.sub(lambda m: f"{m.group(1)}=***", out)
    out = _COOKIES.sub(lambda m: f"{m.group(1)}=***", out)
    out = _BEARER.sub("Bearer ***", out)
    return _KEYS.sub("fyk_***", out)


def error_code(data) -> str:
    """The code of an error answer: {"detail": {"code"}} (the API) or {"error": {"code"}} (the gateway)."""
    if isinstance(data, dict):
        for k in ("detail", "error"):
            if isinstance(data.get(k), dict) and data[k].get("code"):
                return str(data[k]["code"])
    return ""


def retry_after(data) -> float:
    """The wait a 429 answer asks for (its detail's retry_after), 5 s when it names none."""
    d = data.get("detail") if isinstance(data, dict) else None
    try:
        return max(1.0, float((d or {}).get("retry_after") or 5)) if isinstance(d, dict) else 5.0
    except (TypeError, ValueError):
        return 5.0


def is_callback(url: str, server: str) -> bool:
    """The provider sending the browser back: the server's /sso/callback with a query."""
    return url.startswith(server + "/sso/callback?")


def seconds_until(iso: Optional[str], server_now) -> float:
    """How long until a server time (naive = UTC) from `server_now` (an aware datetime); <= 0 when past or None."""
    if not iso:
        return 0.0
    return (A.utc(iso) - server_now).total_seconds()


# ------------------------------------------------------------------ the run
@dataclass
class Outcome:
    status: int
    data: object
    page: object
    callback: str = ""

    @property
    def code(self) -> str:
        return error_code(self.data)


class Sso:
    def __init__(self, run: "A.Run", browser):
        self.run = run
        self.a = run.args
        self.browser = browser
        self.server = self.a.server
        self.accounts: dict[str, Account] = self.a.accounts
        self.secret = Path(self.a.client_secret_file).read_text().strip() if self.a.client_secret_file else ""
        self.passphrase = "Fy-" + secrets.token_urlsafe(18)
        self.secrets = (run.password, self.secret, self.passphrase, *(x.password for x in self.accounts.values()))
        self.name = self.a.provider_name or run.name("idp")
        self.shots = Path(self.a.shots) if self.a.shots else None
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.known_ids: set[int] = set()
        self.users: dict[str, dict] = {}         # part -> {"id", "username"} of accounts the run made
        self.contexts: list = []
        self.audit_from = 0

    # -- reporting
    def check(self, name: str, ok, detail="") -> bool:
        return self.run.check(name, ok, scrub(detail, self.secrets))

    def skip(self, name: str, reason: str) -> None:
        self.run.skip(name, scrub(reason, self.secrets))

    def shot(self, page, what: str) -> None:
        if not self.shots:
            return
        self.n += 1
        try:
            page.screenshot(path=str(self.shots / f"{self.n:02d}-{what}.png"), full_page=True)
        except Exception as e:  # noqa: BLE001 - a screenshot must not fail a check
            A.log(f"  screenshot {what}: {scrub(str(e).splitlines()[0], self.secrets)[:120]}")

    # -- the browser
    def context(self):
        ctx = self.browser.new_context(ignore_https_errors=True, locale="en-US", viewport={"width": 1440, "height": 900})
        ctx.set_default_timeout(PAGE_TIMEOUT)
        self.contexts.append(ctx)
        return ctx

    def api(self, ctx, method: str, path: str, body=None) -> tuple[int, object, dict]:
        """A call with this browser's cookies (its session): status, JSON, headers."""
        r = ctx.request.fetch(self.server + path, method=method, data=body,
                              headers={"X-Forgeyard": "1", "Accept": "application/json"}, fail_on_status_code=False)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001 - not JSON
            data = None
        return r.status, data, r.headers

    def me(self, ctx) -> tuple[int, dict]:
        status, data, _ = self.api(ctx, "GET", "/api/auth/me")
        return status, data if isinstance(data, dict) else {}

    def password_session(self, ctx, username: str, password: str) -> None:
        status, data, _ = self.api(ctx, "POST", "/api/auth/login", {"username": username, "password": password})
        if status != 200 or (isinstance(data, dict) and data.get("mfa_required")):
            raise A.Fail(f"signing in as {username} with the password: {status} {error_code(data) or 'two-factor'}")

    def idp(self, page, acct: Account) -> None:
        """Sign in at the provider's page the browser is on (or on its way to)."""
        page.wait_for_url(lambda u: not u.startswith(self.server), timeout=IDP_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        if self.a.idp_form == "mock":
            button = page.locator(f'button[name="sub"][value="{acct.username}"]')
            if button.count():
                button.first.click()
            else:
                page.fill("#subject-input", acct.username)
                page.locator("#subject-input ~ button[type=submit]").click()
            return
        fields = "input[name=username], input#username, input[type=email], input[name=uid], input[name=login]"
        page.wait_for_selector(f"{fields}, input[type=password]", state="visible", timeout=IDP_TIMEOUT)
        user = page.locator(fields).first
        if user.count() and user.is_visible() and user.is_editable():
            user.fill(acct.username)
        submit = "#kc-login, button[type=submit], input[type=submit]"
        pw = page.locator("input[type=password]").first
        if not (pw.count() and pw.is_visible()):
            page.locator(submit).first.click()      # a page that asks for the username first
            pw.wait_for(state="visible", timeout=IDP_TIMEOUT)
        pw.fill(acct.password)
        page.locator(submit).first.click()

    def round_trip(self, page, acct: Account, go: Callable[[], None], intercept: bool = False) -> Outcome:
        """go() leaves for the provider (a button); sign in there as `acct`; the answer of /api/auth/sso/finish that
        the callback page sends. intercept: the callback is not loaded (its address is returned, status 0)."""
        seen: list[str] = []

        def on_request(r):
            if r.is_navigation_request() and is_callback(r.url, self.server):
                seen.append(r.url)

        page.on("request", on_request)
        try:
            if intercept:
                # page.route() sees only the first address of a redirect chain, never the callback the provider
                # redirects to: so the provider's own answers are fetched here without following redirects, and the
                # one that sends the browser to the callback is kept and not passed on.
                stopped: list[str] = []

                def stop(route) -> None:
                    if not route.request.is_navigation_request():
                        route.continue_()
                        return
                    resp = route.fetch(max_redirects=0)
                    to = urljoin(route.request.url, resp.headers.get("location") or "")
                    if 300 <= resp.status < 400 and is_callback(to, self.server):
                        stopped.append(to)
                        route.fulfill(status=200, content_type="text/html", body="<p>stopped before the callback</p>")
                    else:
                        route.fulfill(response=resp)

                page.route(lambda u: not u.startswith(self.server), stop)
                go()
                self.idp(page, acct)
                deadline = time.monotonic() + IDP_TIMEOUT / 1000
                while not stopped and time.monotonic() < deadline:
                    page.wait_for_timeout(200)
                page.unroute_all(behavior="wait")
                if not stopped:
                    raise A.Fail(f"{acct.part}: the provider did not send the browser back ({self.where(page)})")
                return Outcome(0, None, page, stopped[-1])
            try:
                with page.expect_response(lambda r: r.url == self.server + "/api/auth/sso/finish",
                                          timeout=IDP_TIMEOUT) as info:
                    go()
                    self.idp(page, acct)
            except Exception as e:  # noqa: BLE001 - say where the browser stopped
                self.shot(page, f"{acct.part}-stuck")
                raise A.Fail(f"{acct.part}: no answer from the sign-in ({self.where(page)}): "
                             f"{str(e).splitlines()[0][:160]}") from None
            out = self.answer(info.value, page, seen[-1] if seen else "")
            return self.revisit(page, out.callback) if out.status == 429 and out.callback else out
        finally:
            page.remove_listener("request", on_request)

    def answer(self, resp, page, callback: str) -> Outcome:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = None
        page.wait_for_load_state("networkidle")
        return Outcome(resp.status, data, page, callback)

    def revisit(self, page, callback: str, tries: int = 4) -> Outcome:
        """Open a callback address again (a replay, the same one in another browser, or after the server's sign-in
        back-off answered 429: that answer does not use the state up, so it is sent again after the wait)."""
        for attempt in range(tries):
            with page.expect_response(lambda r: r.url == self.server + "/api/auth/sso/finish",
                                      timeout=PAGE_TIMEOUT) as info:
                page.goto(callback)
            out = self.answer(info.value, page, callback)
            if out.status != 429 or attempt == tries - 1:
                return out
            wait = min(float(retry_after(out.data)), 300.0)
            A.log(f"  the server's single sign-on back-off: waiting {wait:.0f} s")
            time.sleep(wait + 1)
        return out

    def where(self, page) -> str:
        try:
            u = urlsplit(page.url)
            return f"at {u.scheme}://{u.netloc}{u.path}, page title {page.title()!r}"
        except Exception:  # noqa: BLE001
            return "page unknown"

    def sign_in(self, ctx, acct: Account) -> Outcome:
        """From the sign-in page: Sign in with <name>, then the provider."""
        page = ctx.new_page()
        page.goto(self.server + "/login", wait_until="networkidle")
        button = page.get_by_role("button", name=f"Sign in with {self.name}")
        return self.round_trip(page, acct, button.click)

    # -- accounts
    def users_list(self) -> list[dict]:
        return self.run.admin.ok("GET", "/api/users")["users"]

    def user_named(self, username: str) -> Optional[dict]:
        return next((u for u in self.users_list() if u["username"] == username), None)

    def adopt(self, part: str, user: Optional[dict]) -> dict:
        """An account a sign-in made: removed at the end. One that was there before the run stops the run."""
        if user is None:
            raise A.Fail(f"{part}: no account found after the sign-in")
        if user["id"] in self.known_ids:
            raise A.Fail(f"{part}: the provider's account {self.accounts[part].username!r} is already linked to the "
                         f"Forgeyard account {user['username']!r}: use accounts that never signed in here")
        if part not in self.users:
            self.users[part] = {"id": user["id"], "username": user["username"]}
            self.run.cleanup.add(f"account {user['username']} (made by the {part} sign-in)", self._delete_user,
                                 user["id"])
        return user

    def _delete_user(self, uid: int) -> None:
        for k in self.run.admin.ok("GET", "/api/keys?all=true").get("keys") or []:
            if (k.get("user") or {}).get("id") == uid and k.get("status") == "active":
                self.run.admin.request("DELETE", f"/api/keys/{k['id']}")
        r = self.run.admin.request("DELETE", f"/api/users/{uid}")
        if r.status not in (200, 404):
            raise A.Fail(f"{r.status} {r.code()}")

    def approve(self, uid: int) -> None:
        self.run.admin.ok("PATCH", f"/api/users/{uid}", {"status": "active"})

    def pending_then_approved(self, part: str, want_role: str, ctx) -> Optional[dict]:
        """Steps 2 and 3: the first sign-in waits for approval, the owner approves, the next one works."""
        acct = self.accounts[part]
        n = "2" if part == "plain" else "3"
        first = self.sign_in(self.context(), acct)
        detail = first.data.get("detail", {}) if isinstance(first.data, dict) else {}
        username = str(detail.get("username") or "")
        st, _ = self.me(first.page.context)
        heading = first.page.get_by_text("Waiting for approval").count() > 0
        clean = first.page.url == self.server + "/sso/callback"
        self.shot(first.page, f"{part}-pending")
        ok = first.status == 403 and first.code == "pending_approval" and st == 401 and heading and clean
        self.check(f"{n} {part}: the first sign-in waits for approval, with no session",
                   ok, f"finish {first.status} {first.code}; /api/auth/me {st}; page "
                   + ("shows" if heading else "does NOT show") + " 'Waiting for approval'; address bar "
                   + ("without" if clean else "STILL WITH") + " the code")
        if first.code != "pending_approval" or not username:
            return None
        user = self.adopt(part, self.user_named(username))
        self.check(f"{n} {part}: the account is made pending, role {want_role} (from the role claim)",
                   user["status"] == "pending" and user["role"] == want_role,
                   f"{user['username']}: status {user['status']}, role {user['role']}, has_password {user['has_password']}")
        self.approve(user["id"])
        second = self.sign_in(ctx, acct)
        st, me = self.me(ctx)
        role = (me.get("user") or {}).get("role")
        method = (me.get("session") or {}).get("auth_method")
        landed = urlsplit(second.page.url).path
        self.shot(second.page, f"{part}-signed-in")
        self.check(f"{n} {part}: approved by the owner, the next sign-in works as {want_role}",
                   second.status == 200 and st == 200 and role == want_role and method == "sso" and landed != "/login"
                   and landed != "/sso/callback",
                   f"finish {second.status} {second.code}; role {role}; session auth_method {method}; landed on {landed}")
        return user

    # -- the steps
    def step_provider(self) -> None:
        a, c = self.a, self.run.admin
        if a.idp_ca:
            if a.issuer.startswith("https://"):
                tls = ssl.create_default_context(cafile=a.idp_ca)
                r = A.raw(a.issuer + "/.well-known/openid-configuration", tls=tls, timeout=20)
                got = (r.data or {}).get("issuer") if isinstance(r.data, dict) else None
                self.check("1 --idp-ca verifies the provider's certificate (its discovery document)",
                           r.status == 200 and got is not None, f"{r.status or r.error}; issuer {got}")
            else:
                self.skip("1 --idp-ca verifies the provider's certificate", "the issuer is not https")
        else:
            self.skip("1 --idp-ca verifies the provider's certificate", "no --idp-ca: a provider with a public CA")
        info = c.ok("GET", "/api/identity-provider")
        if a.idp_ca:
            self.check("1 the server has a CA file for the provider (SSO_CA_FILE)", info.get("ca_file") is True,
                       f"ca_file {info.get('ca_file')}")
        self.check("1 single sign-on is available on the server (Authlib installed)", info.get("available") is True,
                   f"available {info.get('available')}")
        A.log(f"  the server sends people back to {info.get('redirect_uri')} (register it at the provider)")
        r = c.request("POST", "/api/identity-provider/check", {"issuer": a.issuer})
        d = r.data if isinstance(r.data, dict) else {}
        self.check("1 Check reads the provider's discovery document", r.status == 200 and d.get("ok") is True,
                   f"{r.status}; ok {d.get('ok')}; issuer {d.get('issuer')}; algorithms {d.get('algorithms')}"
                   + (f"; {d.get('code')}: {d.get('problem')}" if not d.get("ok") else ""))
        if not d.get("ok"):
            raise A.Fail("the provider's discovery document cannot be read: nothing else can run")
        r = c.request("PUT", "/api/identity-provider", provider_body(a, self.name, self.secret, automatic=False))
        saved = (r.data or {}).get("provider") if isinstance(r.data, dict) else None
        if r.status != 200 or not saved:
            raise A.Fail(f"saving the provider: {r.status} {r.code()} {A.why(r)}")
        self.check("1 the provider is saved: approve mode, the role mapping, the secret stored",
                   saved["new_accounts"] == "approve" and saved["role_mapping"] == role_mapping(a.admin_values,
                                                                                               a.operator_values)
                   and saved["has_client_secret"] is bool(self.secret),
                   f"{saved['name']}: {saved['new_accounts']}, claim {saved['role_claim']}, "
                   f"{len(saved['role_mapping'])} values mapped, client secret {saved['has_client_secret']}")
        page = self.context().new_page()
        page.goto(self.server + "/login", wait_until="networkidle")
        shown = page.get_by_role("button", name=f"Sign in with {self.name}").count() == 1
        self.shot(page, "login-page")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(300)
        sw, cw = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        self.shot(page, "login-page-390")
        self.check(f"1 the sign-in page shows 'Sign in with {self.name}', fits 390 px", shown and sw <= cw + 1,
                   ("shown" if shown else "NOT shown") + f"; width {sw} of {cw} px at 390")
        page.context.close()

    def step_automatic(self) -> tuple[object, str]:
        """Step 4: returns the automatic account's browser and the callback address its sign-in used."""
        c = self.run.admin
        c.ok("PUT", "/api/identity-provider", provider_body(self.a, self.name, None, automatic=True))
        ctx = self.context()
        o = self.sign_in(ctx, self.accounts["automatic"])
        user = (o.data or {}).get("user") if isinstance(o.data, dict) else None
        if user:
            user = self.adopt("automatic", self.user_named(user["username"]))
        st, me = self.me(ctx)
        self.shot(o.page, "automatic-signed-in")
        self.check(f"4 automatic mode ({self.a.domain}): a new account is active at once",
                   o.status == 200 and user is not None and user["status"] == "active" and st == 200
                   and (me.get("session") or {}).get("auth_method") == "sso",
                   f"finish {o.status} {o.code}" + (f"; {user['username']}: {user['status']}, role {user['role']}"
                                                   if user else ""))
        before = {u["username"] for u in self.users_list()}
        out = self.sign_in(self.context(), self.accounts["outsider"])
        after = {u["username"] for u in self.users_list()}
        st2, _ = self.me(out.page.context)
        self.shot(out.page, "outsider-refused")
        self.check("4 the outsider (another domain) is refused with sso_domain_not_allowed, no account made",
                   out.status == 403 and out.code == "sso_domain_not_allowed" and after == before and st2 == 401,
                   f"finish {out.status} {out.code}; new accounts: {sorted(after - before) or 'none'}; /api/auth/me {st2}")
        for name in after - before:
            u = self.user_named(name)
            if u:
                self.adopt("outsider", u)
        return ctx, o.callback

    def step_disabled_and_key(self, ctx_plain) -> None:
        """Steps 5 and 9."""
        plain = self.users.get("plain")
        if not plain:
            self.skip("5 a disabled account is refused", "the plain account did not sign in (step 2)")
            return
        st, data, _ = self.api(ctx_plain, "POST", "/api/keys", {"name": self.run.name("sso-key")})
        key = (data or {}).get("key") if isinstance(data, dict) else None
        if st != 200 and st != 201 or not key:
            self.check("9 the plain account makes an API key while signed in through the provider", False,
                       f"{st} {error_code(data)}")
            key = None
        else:
            kid = data["api_key"]["id"]
            self.run.cleanup.add(f"API key {data['api_key']['name']}", self.run._revoke_key, self.run.admin, kid)
            r = A.raw(self.server + "/v1/models", headers={"Authorization": f"Bearer {key}"}, tls=self.run.tls)
            self.check("9 an API key of an account that signs in through the provider answers /v1/models",
                       r.status == 200, f"{r.status} {r.code()}")
        self.run.admin.ok("PATCH", f"/api/users/{plain['id']}", {"status": "disabled"})
        st, _ = self.me(ctx_plain)
        self.check("5 disabling the account ends its session through the provider", st == 401, f"/api/auth/me {st}")
        o = self.sign_in(self.context(), self.accounts["plain"])
        self.shot(o.page, "disabled-refused")
        st2, _ = self.me(o.page.context)
        self.check("5 the disabled account's sign-in through the provider is refused with account_disabled",
                   o.status == 403 and o.code == "account_disabled" and st2 == 401,
                   f"finish {o.status} {o.code}; /api/auth/me {st2}")
        if key:
            r = A.raw(self.server + "/v1/models", headers={"Authorization": f"Bearer {key}"}, tls=self.run.tls)
            self.check("9 once the account is disabled its API key gets 401 account_disabled",
                       r.status == 401 and r.code() == "account_disabled", f"{r.status} {r.code()}")

    def step_link(self, ctx_admin) -> None:
        a = self.a
        ctx = self.context()
        self.password_session(ctx, a.user, self.run.password)
        st, data, _ = self.api(ctx, "GET", "/api/auth/sso/identities")
        if (data or {}).get("identities"):
            self.skip("6 the owner links an account at the provider", "the owner's account is already linked")
            return
        page = ctx.new_page()
        page.goto(self.server + "/account", wait_until="networkidle")
        page.fill('input[name="sso-link-auth"]', self.run.password)

        o = self.round_trip(page, self.accounts["owner"],
                            lambda: page.get_by_role("button", name=f"Link {self.name}").click())
        st, data, _ = self.api(ctx, "GET", "/api/auth/sso/identities")
        ids = (data or {}).get("identities") or []
        item = self.run.cleanup.add("the owner's link to the provider", self._unlink_owner)
        self.shot(o.page, "owner-linked")
        self.check("6 the owner links the owner account from My account (password, then a fresh sign-in there)",
                   o.status == 200 and (o.data or {}).get("purpose") == "link" and len(ids) == 1,
                   f"finish {o.status} {o.code}; linked accounts {len(ids)}")
        if len(ids) != 1:
            return
        sso_ctx = self.context()
        o2 = self.sign_in(sso_ctx, self.accounts["owner"])
        st, me = self.me(sso_ctx)
        u = me.get("user") or {}
        self.shot(o2.page, "owner-through-provider")
        self.check("6 a sign-in through the provider is then the owner, still admin",
                   o2.status == 200 and u.get("username") == a.user and u.get("is_owner") is True
                   and u.get("role") == "admin" and (me.get("session") or {}).get("auth_method") == "sso",
                   f"finish {o2.status} {o2.code}; {u.get('username')}: owner {u.get('is_owner')}, role {u.get('role')}, "
                   f"auth_method {(me.get('session') or {}).get('auth_method')}")
        if ctx_admin is not None:
            st, data, _ = self.api(ctx_admin, "GET", "/api/auth/sso/identities")
            mine = (data or {}).get("identities") or []
            if mine:
                st, data, _ = self.api(ctx_admin, "POST", "/api/auth/sso/unlink", {"id": mine[0]["id"]})
                self.check("6 an account without a password may not unlink its only way in (last_way_in)",
                           st == 409 and error_code(data) == "last_way_in", f"{st} {error_code(data)}")
        page.goto(self.server + "/account", wait_until="networkidle")
        page.once("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Unlink").click()
        page.get_by_text(f"Your {self.name} account is unlinked.").wait_for(timeout=PAGE_TIMEOUT)
        st, data, _ = self.api(ctx, "GET", "/api/auth/sso/identities")
        left = (data or {}).get("identities") or []
        self.shot(page, "owner-unlinked")
        self.check("6 the owner (who has a password) unlinks from My account", not left, f"linked accounts {len(left)}")
        self.run.cleanup.now(item)

    def _unlink_owner(self) -> None:
        for i in self.run.admin.ok("GET", "/api/auth/sso/identities").get("identities") or []:
            self.run.admin.ok("POST", "/api/auth/sso/unlink", {"id": i["id"]})

    def step_mfa(self, ctx_admin, ctx_sso) -> None:
        if ctx_admin is None or ctx_sso is None:
            self.skip("7 two-factor exemption", "needs the admin (step 3) and automatic (step 4) sign-ins")
            return
        st, before, _ = self.api(ctx_admin, "GET", "/api/users/mfa-policy")
        if st != 200:
            raise A.Fail(f"reading the two-factor setting: {st} {error_code(before)}")
        prev = {"mfa_required": before["mfa_required"], "mfa_grace_days": before["mfa_grace_days"]}
        u = self.run.user()  # a password account without an authenticator (accept-<run>-user)
        ctx_pw = self.context()
        self.password_session(ctx_pw, u["name"], u["password"])

        def restore(via_admin_session: bool = True) -> None:
            if via_admin_session:
                st, data, _ = self.api(ctx_admin, "PUT", "/api/users/mfa-policy", prev)
                if st == 200:
                    return
            self.run.admin.ok("PUT", "/api/users/mfa-policy", prev)

        item = self.run.cleanup.add(f"the two-factor setting back to {prev['mfa_required']}, "
                                    f"{prev['mfa_grace_days']} days", restore)
        try:
            st, data, _ = self.api(ctx_admin, "PUT", "/api/users/mfa-policy",
                                   {"mfa_required": "everyone", "mfa_grace_days": 0})
            if st != 200:
                raise A.Fail(f"requiring two-factor sign-in: {st} {error_code(data)}")
            st1, d1, _ = self.api(ctx_pw, "GET", "/api/keys")
            page = ctx_pw.new_page()
            page.goto(self.server + "/machines", wait_until="networkidle")
            setup = page.get_by_text("Add two-factor sign-in to continue").count() > 0
            self.shot(page, "password-session-sent-to-setup")
            self.check("7 with two-factor required of everyone, a password session without one reaches only the setup",
                       st1 == 403 and error_code(d1) == "mfa_enrolment_required" and setup,
                       f"/api/keys {st1} {error_code(d1)}; the setup page " + ("shown" if setup else "NOT shown"))
            st2, d2, _ = self.api(ctx_sso, "GET", "/api/keys")
            _, me = self.me(ctx_sso)
            page2 = ctx_sso.new_page()
            page2.goto(self.server + "/machines", wait_until="networkidle")
            sent = page2.get_by_text("Add two-factor sign-in to continue").count() > 0
            self.shot(page2, "sso-session-not-sent")
            self.check("7 a session through the provider is not sent to the two-factor setup",
                       st2 == 200 and (me.get("user") or {}).get("mfa_enrolment") is None and not sent,
                       f"/api/keys {st2} {error_code(d2)}; mfa_enrolment {(me.get('user') or {}).get('mfa_enrolment')}; "
                       "the setup page " + ("SHOWN" if sent else "not shown"))
        finally:
            self.run.cleanup.now(item)
        now = self.run.admin.ok("GET", "/api/users/mfa-policy")
        self.check("7 the two-factor setting is back to what it was",
                   (now["mfa_required"], now["mfa_grace_days"]) == (prev["mfa_required"], prev["mfa_grace_days"]),
                   f"{now['mfa_required']}, {now['mfa_grace_days']} days")

    def step_backup(self, ctx_admin) -> None:
        if ctx_admin is None:
            self.skip("8 the secret-key backup of an account without a password", "the admin account did not sign in")
            return
        st, info, _ = self.api(ctx_admin, "GET", "/api/secret-key")
        if st != 200:
            raise A.Fail(f"GET /api/secret-key as the admin account: {st} {error_code(info)}")
        self.check("8 the admin account has no password and is linked", info.get("has_password") is False
                   and info.get("sso_linked") is True, f"has_password {info.get('has_password')}, "
                   f"sso_linked {info.get('sso_linked')}")
        backed_up = info.get("backed_up")
        # Wait until the sign-in of step 3 is no longer fresh (5 minutes after the provider's auth_time).
        while True:
            st, me, headers = self.api(ctx_admin, "GET", "/api/auth/me")
            server_now = parsedate_to_datetime(headers.get("date")) if headers.get("date") else None
            left = seconds_until((me or {}).get("session", {}).get("fresh_until"), server_now) if server_now else 0
            if left <= 0:
                break
            A.log(f"  waiting {left:.0f} s for the admin account's sign-in to be {FRESH_SECONDS // 60} minutes old")
            time.sleep(min(left + 2, 60))
        body = {"passphrase": self.passphrase, "passphrase_confirm": self.passphrase}
        st, data, _ = self.api(ctx_admin, "POST", "/api/secret-key/backup", body)
        self.check("8 the backup asks an account without a password to sign in again (fresh_sign_in_required)",
                   st == 400 and error_code(data) == "fresh_sign_in_required", f"{st} {error_code(data)}")
        if st == 200:
            raise A.Fail("the backup downloaded without a fresh sign-in")
        page = ctx_admin.new_page()
        page.goto(self.server + "/trust", wait_until="networkidle")
        page.get_by_role("button", name="Download a key backup…").click()
        o = self.round_trip(page, self.accounts["admin"],
                            lambda: page.get_by_role("button", name=f"Sign in again with {self.name}").click())
        st, me, headers = self.api(ctx_admin, "GET", "/api/auth/me")
        server_now = parsedate_to_datetime(headers["date"])
        fresh = seconds_until((me or {}).get("session", {}).get("fresh_until"), server_now)
        self.shot(o.page, "admin-signed-in-again")
        self.check("8 'Sign in again with …' in the backup dialog makes the session fresh",
                   o.status == 200 and (o.data or {}).get("purpose") == "reauth" and fresh > 0,
                   f"finish {o.status} {o.code}; fresh for {fresh:.0f} s more")
        if not backed_up:
            self.skip("8 the backup then downloads", "the secret key was not marked as backed up before the run: a "
                      "download would mark it backed up while nobody keeps the file (confirm the key's backup in "
                      "Setup health first to run this check)")
            return
        r = page.context.request.post(self.server + "/api/secret-key/backup", data=body,
                                      headers={"X-Forgeyard": "1"}, fail_on_status_code=False)
        size = len(r.body()) if r.status == 200 else 0
        self.run.cleanup.add(f"the secret key's backup record, confirmed again by {self.a.user}", self._reconfirm,
                             info.get("kid"))
        self.check("8 the backup then downloads (the file is thrown away)",
                   r.status == 200 and "octet-stream" in (r.headers.get("content-type") or "") and size > 0,
                   f"{r.status}; {size} bytes")

    def _reconfirm(self, kid: str) -> None:
        self.run.admin.ok("POST", "/api/advisor/secret-key-backup", {"kid": kid})

    def step_replay(self, auto_callback: str) -> None:
        if auto_callback:
            ctx = self.context()
            o = self.revisit(ctx.new_page(), auto_callback)
            st, _ = self.me(ctx)
            self.shot(o.page, "replay-refused")
            self.check("10 a callback already used is refused (sso_state_invalid), no session",
                       o.status == 400 and o.code == "sso_state_invalid" and st == 401,
                       f"finish {o.status} {o.code}; /api/auth/me {st}")
        else:
            self.skip("10 a callback already used is refused", "the automatic sign-in's callback was not seen")
        ctx_x = self.context()
        page_x = ctx_x.new_page()
        page_x.goto(self.server + "/login", wait_until="networkidle")
        got = self.round_trip(page_x, self.accounts["automatic"],
                              page_x.get_by_role("button", name=f"Sign in with {self.name}").click, intercept=True)
        ctx_y = self.context()
        o = self.revisit(ctx_y.new_page(), got.callback)
        st_y, _ = self.me(ctx_y)
        self.shot(o.page, "other-browser-refused")
        self.check("10 a callback opened in another browser is refused (sso_state_invalid), no session there",
                   o.status == 400 and o.code == "sso_state_invalid" and st_y == 401,
                   f"finish {o.status} {o.code}; /api/auth/me {st_y}")
        o2 = self.revisit(page_x, got.callback)
        st_x, _ = self.me(ctx_x)
        self.check("10 the browser that started it cannot use it afterwards (used up)",
                   o2.status == 400 and o2.code == "sso_state_invalid" and st_x == 401,
                   f"finish {o2.status} {o2.code}; /api/auth/me {st_x}")
        events = self.run.admin.ok("GET", "/api/audit?action=auth.sso_login_failed&limit=50")["events"]
        reasons = [(e.get("detail") or {}).get("reason") for e in events if e["id"] > self.audit_from]
        self.check("10 the refusals are audited (browser_mismatch, state_invalid)",
                   "browser_mismatch" in reasons and reasons.count("state_invalid") >= 1 + bool(auto_callback),
                   f"reasons since the run began: {sorted(set(r for r in reasons if r))}")


def preflight(run: "A.Run") -> None:
    """Before anything changes (exit code 2): the server's provider may be replaced, and --user is the owner."""
    a = run.args
    current = run.admin.ok("GET", "/api/identity-provider").get("provider")
    if current and not a.replace_provider:
        raise A.Refused(f"the server already has an identity provider ({current['name']}): run with "
                        "--replace-provider to replace it for the run (it is put back at the end)")
    if current and current.get("has_client_secret") and not a.previous_client_secret_file:
        raise A.Refused(f"{current['name']} has a client secret the server never shows: give it with "
                        "--previous-client-secret-file so that it can be put back")
    if not run.admin.ok("GET", "/api/auth/me")["user"].get("is_owner"):
        raise A.Refused(f"{a.user} is not the server's owner (step 6 links the owner's account)")


def restore_provider(run: "A.Run", previous: Optional[dict], secret: Optional[str]) -> None:
    if previous is None:
        r = run.admin.request("DELETE", "/api/identity-provider")
        if r.status not in (200, 404):
            raise A.Fail(f"{r.status} {r.code()}")
        return
    run.admin.ok("PUT", "/api/identity-provider", restore_body(previous, secret))


def exercise(run: "A.Run") -> None:
    a = run.args
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise A.Fail("Playwright for Python is not installed (run with --install-playwright)") from None
    current = run.admin.ok("GET", "/api/identity-provider").get("provider")
    previous_secret = (Path(a.previous_client_secret_file).read_text().strip()
                       if a.previous_client_secret_file else None)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=a.chrome if a.chrome and Path(a.chrome).exists() else None,
                                         headless=not a.headed, args=["--no-sandbox"])
        except Exception as e:  # noqa: BLE001
            raise A.Fail(f"no browser could be started ({str(e).splitlines()[0][:120]}); give one with --chrome") from None
        x = Sso(run, browser)
        x.known_ids = {u["id"] for u in x.users_list()}
        events = run.admin.ok("GET", "/api/audit?limit=1")["events"]
        x.audit_from = events[0]["id"] if events else 0
        run.cleanup.add("the identity provider " + (f"put back to {current['name']}" if current else "removed"),
                        restore_provider, run, current, previous_secret)
        try:
            x.step_provider()
            ctx_plain = x.context()
            x.pending_then_approved("plain", "user", ctx_plain)
            ctx_admin = x.context()
            admin = x.pending_then_approved("admin", "admin", ctx_admin)
            x.pending_then_approved("operator", "operator", x.context())
            ctx_auto, auto_callback = x.step_automatic()
            x.step_disabled_and_key(ctx_plain)
            x.step_link(ctx_admin if admin else None)
            x.step_mfa(ctx_admin if admin else None, ctx_auto if "automatic" in x.users else None)
            x.step_backup(ctx_admin if admin else None)
            x.step_replay(auto_callback)
        except A.Fail as e:
            raise A.Fail(scrub(str(e), x.secrets)) from None
        except Exception as e:  # noqa: BLE001 - a message without secrets, and where it happened
            frames = traceback.extract_tb(e.__traceback__)
            tb = next((f for f in reversed(frames) if f.filename == __file__), frames[-1])
            raise A.Fail(scrub(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''} "
                               f"(line {tb.lineno} in {tb.name})", x.secrets)) from None
        finally:
            for ctx in x.contexts:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
            browser.close()


def ensure_playwright(args) -> None:
    import importlib.util
    import shutil
    import subprocess

    if importlib.util.find_spec("playwright") is not None:
        return
    if not args.install_playwright or os.environ.get(A.REEXEC):
        raise A.Refused("Playwright for Python is not installed: run with --install-playwright (it goes into --venv)")
    venv = Path(args.venv)
    py = venv / "bin" / "python"
    A.log(f"playwright not installed here: putting {A.PLAYWRIGHT_SPEC} in a virtual environment in {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("uv"):
        if not py.exists():
            subprocess.run(["uv", "venv", "--quiet", str(venv)], check=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(py), A.PLAYWRIGHT_SPEC], check=True)
    else:
        if not py.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "--quiet", A.PLAYWRIGHT_SPEC], check=True)
    os.environ[A.REEXEC] = "1"
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        ensure_playwright(args)
    except A.Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    A.GROUPS["sso"] = A.Group("sso", TITLE, exercise)
    sign_in = A.start

    def start(run: "A.Run", password: str) -> None:  # accept_all's sign-in, then this script's own refusals
        sign_in(run, password)
        preflight(run)

    A.start = start
    try:
        return A.run_plan(args)
    finally:
        A.start = sign_in


if __name__ == "__main__":
    sys.exit(main())
