"""scripts/accept_sso.py, the 1.6.0 acceptance of single sign-on against a real OpenID Connect provider: its pure parts
(FY-ASSO-*). The run itself needs a server, a provider and a browser; these cover its options, the accounts file, the
provider settings it saves and puts back, and how it keeps secrets out of what it prints."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

ACCOUNTS = {p: {"username": f"{p}-x", "password": f"pw-{p}-1234"}
            for p in ("plain", "admin", "operator", "automatic", "outsider", "owner")}


@pytest.fixture(scope="module")
def mods():
    loaded = {}
    sys.path.insert(0, str(SCRIPTS))
    try:
        for name in ("accept_all", "accept_sso"):
            spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            loaded[name] = mod
        yield loaded["accept_all"], loaded["accept_sso"]
    finally:
        for name in ("accept_all", "accept_sso", "accept_scale", "replica_port"):
            sys.modules.pop(name, None)
        sys.path.remove(str(SCRIPTS))


@pytest.fixture
def files(tmp_path):
    acc = tmp_path / "accounts.json"
    acc.write_text(json.dumps(ACCOUNTS))
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret-value\n")
    return {"accounts": str(acc), "secret": str(secret), "dir": tmp_path}


def base(files, *extra):
    return ["--server", "https://h:7860/", "--issuer", "https://idp.example/realms/r/", "--client-id", "fy",
            "--accounts-file", files["accounts"], "--admin-values", "fy-admins", "--operator-values", "fy-ops",
            "--allowed-domain", "@Example.COM.", *extra]


def test_fy_asso_001_options_and_refusals(mods, files):
    """FY-ASSO-001: the options and their defaults (role claim groups, the form filler, the owner as --user, the trailing
    slashes of the server and the issuer dropped, the domain normalised); the run is the script's own `sso` group, not
    one of accept_all.py's; what is missing or does not go together is refused before anything runs."""
    acc, s = mods
    a = s.parse_args(base(files))
    assert a.server == "https://h:7860" and a.issuer == "https://idp.example/realms/r"
    assert (a.role_claim, a.idp_form, a.user, a.domain, a.replace_provider) == ("groups", "form", "owner", "example.com",
                                                                                 False)
    assert a.plan == [("sso", "")] and a.cloud_provider is None and a.gpu_machine is None
    assert set(a.accounts) == set(ACCOUNTS) and a.accounts["owner"].username == "owner-x"
    assert "sso" not in acc.GROUPS   # standalone: a regression run of accept_all.py does not change the sign-in setup
    for bad in (["--admin-values", ""], ["--operator-values", " "], ["--previous-client-secret-file", files["secret"]],
                ["--client-secret-file", str(files["dir"] / "nope")], ["--allowed-domain", "not a domain"],
                ["--idp-ca", str(files["dir"] / "nope.pem")], ["--idp-form", "saml"]):
        with pytest.raises(SystemExit) as e:
            s.parse_args(base(files, *bad))
        assert e.value.code == 2, bad
    for missing in ("--issuer", "--client-id", "--accounts-file", "--allowed-domain"):
        argv = base(files)
        i = argv.index(missing)
        with pytest.raises(SystemExit):
            s.parse_args(argv[:i] + argv[i + 2:])
    ok = s.parse_args(base(files, "--replace-provider", "--previous-client-secret-file", files["secret"]))
    assert ok.replace_provider and ok.previous_client_secret_file == files["secret"]


def test_fy_asso_002_accounts_file(mods):
    """FY-ASSO-002: the accounts file needs all six parts, each with its own username and (except for the mock
    provider) a password; anything else is refused with what is wrong."""
    _, s = mods
    got = s.load_accounts(json.dumps(ACCOUNTS))
    assert [a.part for a in got.values()] == list(s.PARTS) and got["admin"].password == "pw-admin-1234"
    no_pw = {p: {"username": v["username"]} for p, v in ACCOUNTS.items()}
    assert s.load_accounts(json.dumps(no_pw), mock=True)["plain"].password == ""
    cases = {
        "not JSON": "{",
        "a JSON object": "[]",
        "missing owner": json.dumps({k: v for k, v in ACCOUNTS.items() if k != "owner"}),
        "unknown part 'extra'": json.dumps({**ACCOUNTS, "extra": {"username": "e", "password": "p"}}),
        "plain: needs a username": json.dumps({**ACCOUNTS, "plain": {"username": " ", "password": "p"}}),
        "admin: needs a password": json.dumps({**ACCOUNTS, "admin": {"username": "a"}}),
        "used twice": json.dumps({**ACCOUNTS, "owner": {"username": "PLAIN-x", "password": "p"}}),
    }
    for want, text in cases.items():
        with pytest.raises(ValueError, match=want):
            s.load_accounts(text)


def test_fy_asso_003_provider_settings(mods, files):
    """FY-ASSO-003: the provider the run saves: every field (a PUT sets them all), the role mapping from both lists (a
    value in both is admin), approve mode with no domains, automatic mode with the one domain, the client secret only
    when one is given (None keeps the stored one); and the body that puts a replaced provider back (its secret only
    when given, a provider without one stays without)."""
    _, s = mods
    a = s.parse_args(base(files, "--admin-values", "fy-admins, both", "--operator-values", "fy-ops,both"))
    assert s.role_mapping(a.admin_values, a.operator_values) == {"fy-admins": "admin", "both": "admin",
                                                                 "fy-ops": "operator"}
    first = s.provider_body(a, "accept-ab12-idp", "sec", automatic=False)
    assert first == {"name": "accept-ab12-idp", "issuer": "https://idp.example/realms/r", "client_id": "fy",
                     "scopes": "openid email profile", "role_claim": "groups",
                     "role_mapping": {"fy-ops": "operator", "both": "admin", "fy-admins": "admin"},
                     "new_accounts": "approve", "allowed_domains": [], "enabled": True, "client_secret": "sec"}
    auto = s.provider_body(a, "n", None, automatic=True)
    assert auto["new_accounts"] == "automatic" and auto["allowed_domains"] == ["example.com"]
    assert "client_secret" not in auto
    prev = {"id": 3, "kind": "oidc", "name": "Company", "issuer": "https://kc", "client_id": "c", "scopes": "openid email",
            "role_claim": None, "role_mapping": {}, "new_accounts": "approve", "allowed_domains": ["corp.example"],
            "enabled": False, "has_client_secret": True, "linked_accounts": 4}
    back = s.restore_body(prev, "old-secret")
    assert back == {"name": "Company", "issuer": "https://kc", "client_id": "c", "scopes": "openid email",
                    "role_claim": None, "role_mapping": {}, "new_accounts": "approve",
                    "allowed_domains": ["corp.example"], "enabled": False, "client_secret": "old-secret"}
    assert s.restore_body({**prev, "has_client_secret": False}, None)["client_secret"] == ""


def test_fy_asso_004_nothing_secret_is_printed(mods):
    """FY-ASSO-004: a message the run prints or saves loses the run's passwords and client secret, the code and state
    of a callback address, session and provider cookies, bearer tokens and API keys; the rest stays readable."""
    _, s = mods
    msg = ("Timeout waiting for http://h:7887/sso/callback?code=AbC123xyz&state=Zz9-_q&session_state=k1 after typing "
           "hunter2-Pass into the form; cookie forgeyard_session=s.e.c; forgeyard_sso=b1; KEYCLOAK_IDENTITY=eyJabc; "
           "Authorization: Bearer fyk_abcdefghijkl; id token Bearer eyJhbGciOiJSUzI1NiJ9.e30.sig; key fyk_zyxwvutsrq; client_secret=topsecret")
    out = s.scrub(msg, ("hunter2-Pass", "", "abc"))
    for secret in ("AbC123xyz", "Zz9-_q", "k1&", "hunter2-Pass", "s.e.c", "b1;", "eyJabc", "fyk_abcdefghijkl",
                   "fyk_zyxwvutsrq", "topsecret", "eyJhbGciOiJSUzI1NiJ9"):
        assert secret not in out, secret
    assert "http://h:7887/sso/callback?code=***&state=***" in out and "Timeout waiting for" in out
    assert s.scrub("nothing here", ("hunter2-Pass",)) == "nothing here"


def test_fy_asso_005_answers_callbacks_and_times(mods):
    """FY-ASSO-005: the error code of an API or gateway answer; the wait a 429 asks for (the back-off); what counts as the provider sending the browser back
    (the server's own /sso/callback with a query, not another host's or the bare page); how long until a server time
    (naive = UTC), from the server's own clock."""
    _, s = mods
    assert s.error_code({"detail": {"code": "sso_state_invalid", "message": "…"}}) == "sso_state_invalid"
    assert s.error_code({"error": {"code": "account_disabled"}}) == "account_disabled"
    assert s.error_code({"detail": "plain text"}) == "" and s.error_code(None) == ""
    srv = "http://127.0.0.1:7887"
    assert s.is_callback(srv + "/sso/callback?code=x&state=y", srv)
    assert not s.is_callback(srv + "/sso/callback", srv)
    assert not s.is_callback("http://127.0.0.1:7887.evil/sso/callback?code=x", srv + "/x")
    assert not s.is_callback("http://idp/sso/callback?code=x", srv)
    now = datetime(2026, 11, 26, 2, 0, 0, tzinfo=timezone.utc)
    assert s.seconds_until("2026-11-26T02:04:30", now) == 270
    assert s.seconds_until("2026-11-26T01:59:00+00:00", now) == -60
    assert s.seconds_until(None, now) == 0
    assert s.retry_after({"detail": {"code": "rate_limited", "retry_after": "8"}}) == 8
    assert s.retry_after({"detail": {"code": "rate_limited"}}) == 5 and s.retry_after(None) == 5
    assert s.retry_after({"detail": {"retry_after": "soon"}}) == 5 and s.retry_after({"detail": {"retry_after": 0}}) == 5
    assert s.retry_after({"detail": {"retry_after": "0.2"}}) == 1


def test_fy_asso_006_main_runs_the_group(mods, files, monkeypatch):
    """FY-ASSO-006: main() runs accept_all's plan (sign-in, report, cleanup, exit code) with the script's own group, and
    refuses (exit 2) when Playwright is missing and --install-playwright was not given."""
    acc, s = mods
    seen = {}
    monkeypatch.setattr(acc, "run_plan", lambda args: seen.setdefault("plan", args.plan) and 0)
    monkeypatch.setattr(s, "ensure_playwright", lambda args: None)
    assert s.main(base(files)) == 0 and seen["plan"] == [("sso", "")]
    assert acc.GROUPS["sso"].fn is s.exercise and acc.GROUPS["sso"].needs == ()
    del acc.GROUPS["sso"]
    monkeypatch.undo()
    import importlib.util as iu

    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec", lambda name, *a: None if name == "playwright" else real(name, *a))
    monkeypatch.delenv(acc.REEXEC, raising=False)
    assert s.main(base(files)) == 2 and "sso" not in acc.GROUPS
