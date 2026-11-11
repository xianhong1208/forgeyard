"""Secrets encrypted at rest: the key, the stored values, the migration of plain-text rows, a lost
key and `secrets reset` (FY-SEC-*)."""

import base64
import json
import logging
import os
import stat

import pytest
from sqlalchemy import select, text

from forgeyard.config import get_settings
from forgeyard.db import Deployment, Replica, Workload, WorkloadEvent, get_engine, new_session, reset_engine
from forgeyard.security import crypto
from forgeyard.workloads import state as wstate
from tests.conftest import make_client
from tests.test_scheduler import MODELS, deploy, seed_catalog
from tests.workload_helpers import make_node

HF = "hf_secret_value_123"


def raw(sql: str, **params):
    with get_engine().connect() as conn:
        return conn.execute(text(sql), params).all()


def _deploy_with_secrets(owner):
    """A gated-model deployment (token + replica key) and a workload with an env value."""
    seed_catalog()
    a100 = make_node("a100", allowlist=[4], features=MODELS + ("replica_keys", "replica_guard"))
    deploy(owner, "chat", "qwen3-4b", "vllm", params={"context": 4096}, machine_id=a100, hf_token=HF)
    r = owner.post("/api/workloads", json={"name": "web", "kind": "service", "runtime": {"type": "process", "command": "true"},
                                           "env": {"API_TOKEN": "env-secret-456", "MODE": "plain"}})
    assert r.status_code == 201, r.text
    return a100


# ------------------------------------------------------------------ the box
def test_fy_sec_001_round_trip_purpose_tamper_wrong_key_and_plain_text():
    """FY-SEC-001: AES-GCM round trip; a value only opens in its own column (purpose); a changed
    byte, another key or a plain-text value is refused with a clear error."""
    box = crypto.Box(os.urandom(32))
    sealed = box.seal("hf_abc", "deployments.hf_token")
    assert sealed.startswith(f"fy1:{box.kid}:") and "hf_abc" not in sealed
    assert box.open(sealed, "deployments.hf_token") == "hf_abc"
    assert box.seal("hf_abc", "deployments.hf_token") != sealed  # a fresh nonce every time
    with pytest.raises(crypto.SecretError, match="cannot be decrypted"):
        box.open(sealed, "replicas.secret")  # copied into another column
    body = sealed.rsplit(":", 1)[1]
    flipped = sealed[: -len(body)] + ("A" if body[0] != "A" else "B") + body[1:]
    with pytest.raises(crypto.SecretError, match="cannot be decrypted"):
        box.open(flipped, "deployments.hf_token")
    other = crypto.Box(os.urandom(32))
    with pytest.raises(crypto.SecretError, match=f"encrypted with key {box.kid}, but the server has key {other.kid}"):
        other.open(sealed, "deployments.hf_token")
    with pytest.raises(crypto.SecretError, match="not encrypted"):
        box.open("hf_plain", "deployments.hf_token")


def test_fy_sec_002_key_file_is_made_0600_and_the_variable_wins(monkeypatch, tmp_path):
    """FY-SEC-002: the key file is made on first use (32 random bytes, base64, mode 0600), a looser
    mode is tightened, and FORGEYARD_SECRET_KEY is used instead without writing anything."""
    path = get_settings().secret_key_path
    assert path == get_settings().data_dir / "secret.key" and not path.exists()
    assert crypto.load_key(create=False) is None
    key = crypto.load_key(create=True)
    assert len(key) == 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert base64.urlsafe_b64decode(path.read_text().strip() + "==") == key
    os.chmod(path, 0o644)
    assert crypto.load_key(create=False) == key and stat.S_IMODE(path.stat().st_mode) == 0o600

    other = tmp_path / "elsewhere" / "fy.key"
    monkeypatch.setenv("SECRET_KEY_FILE", str(other))
    get_settings.cache_clear()
    assert crypto.load_key(create=True) != key and other.exists()  # SECRET_KEY_FILE moves it

    env_key = os.urandom(32)
    monkeypatch.setenv(crypto.KEY_ENV, base64.urlsafe_b64encode(env_key).decode())
    get_settings.cache_clear()
    other.unlink()
    assert crypto.load_key(create=True) == env_key and not other.exists()
    monkeypatch.setenv(crypto.KEY_ENV, "c2hvcnQ=")
    get_settings.cache_clear()
    with pytest.raises(crypto.SecretKeyError, match="32 bytes"):
        crypto.load_key(create=True)


# ------------------------------------------------------------------ in the database
def test_fy_sec_003_database_holds_ciphertext_code_and_agents_get_plain_values(owner):
    """FY-SEC-003: replica keys, Hugging Face tokens and workload env values are ciphertext in the
    database; the code (and the agent's desired state) sees the plain values; names stay readable."""
    a100 = _deploy_with_secrets(owner)
    token = raw("SELECT hf_token FROM deployments")[0][0]
    secret = raw("SELECT secret FROM replicas WHERE secret IS NOT NULL")[0][0]
    specs = [json.loads(s) if isinstance(s, str) else s for (s,) in raw("SELECT spec FROM workloads")]
    env = next(s["env"] for s in specs if s.get("env"))
    assert crypto.is_sealed(token) and HF not in token
    assert crypto.is_sealed(secret)
    assert set(env) == {"API_TOKEN", "MODE"} and all(crypto.is_sealed(v) for v in env.values())
    dump = json.dumps(raw("SELECT * FROM workloads"), default=str) + json.dumps(raw("SELECT * FROM deployments"), default=str)
    assert "env-secret-456" not in dump and HF not in dump
    kid = crypto.box().kid
    assert raw("SELECT value FROM server_settings WHERE key = 'secret_key'")[0][0] in ({"kid": kid}, json.dumps({"kid": kid}))

    db = new_session()
    try:
        assert db.scalar(select(Deployment)).hf_token == HF
        rep = db.scalar(select(Replica).where(Replica.secret.is_not(None)))
        assert len(rep.secret) == 43  # token_urlsafe(32), as before
        w = db.scalar(select(Workload).where(Workload.name == "web"))
        assert w.spec["env"] == {"API_TOKEN": "env-secret-456", "MODE": "plain"}
        msg = wstate.desired_message(db, a100, 1)
        assert msg["replicas"][0]["model"]["token"] == HF and msg["replicas"][0]["replica_key"]["value"] == rep.secret
    finally:
        db.close()
    # The owner still sees the env values of their workload (as before), only stored encrypted.
    web = next(x for x in owner.get("/api/workloads").json()["workloads"] if x["name"] == "web")
    assert owner.get(f"/api/workloads/{web['id']}").json()["workload"]["spec"]["env"]["API_TOKEN"] == "env-secret-456"


def test_fy_sec_004_migration_encrypts_plain_rows_and_the_downgrade_restores_them(owner):
    """FY-SEC-004: rows written in plain text before 0008 are encrypted by the upgrade (the only
    place plain text is read), the key id is recorded; the downgrade decrypts them again."""
    from alembic import command

    from forgeyard.migrate import alembic_config, upgrade

    _deploy_with_secrets(owner)
    db = new_session()
    plain = {"hf": HF, "secret": db.scalar(select(Replica.secret).where(Replica.secret.is_not(None)))}
    db.close()
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0007_preferences")
    assert raw("SELECT hf_token FROM deployments")[0][0] == HF  # decrypted again
    assert raw("SELECT secret FROM replicas WHERE secret IS NOT NULL")[0][0] == plain["secret"]
    assert raw("SELECT count(*) FROM server_settings WHERE key = 'secret_key'")[0][0] == 0
    # A row written in plain text by 1.0, after the downgrade:
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE deployments SET hf_token = 'hf_written_by_1_0'"))
    upgrade()
    token = raw("SELECT hf_token FROM deployments")[0][0]
    assert crypto.is_sealed(token)
    if get_engine().dialect.name == "sqlite":
        # No plain-text copy left behind in old pages: the migration rewrote the database.
        data_dir = get_settings().data_dir
        on_disk = b"".join(p.read_bytes() for p in data_dir.glob("forgeyard.db*"))
        assert b"hf_written_by_1_0" not in on_disk and b"env-secret-456" not in on_disk
        assert not crypto.scrub_after_migration
    specs = [json.loads(s) if isinstance(s, str) else s for (s,) in raw("SELECT spec FROM workloads")]
    assert all(crypto.is_sealed(v) for s in specs for v in (s.get("env") or {}).values())
    db = new_session()
    try:
        assert db.scalar(select(Deployment)).hf_token == "hf_written_by_1_0"
        assert db.scalar(select(Replica.secret).where(Replica.secret.is_not(None))) == plain["secret"]
        assert db.scalar(select(Workload).where(Workload.name == "web")).spec["env"]["API_TOKEN"] == "env-secret-456"
    finally:
        db.close()
    # After the migration a plain value is an error, never silently accepted.
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE deployments SET hf_token = 'hf_plain_again'"))
    db = new_session()
    try:
        with pytest.raises(crypto.SecretError, match="not encrypted"):
            db.scalar(select(Deployment))
    finally:
        db.close()


def test_fy_sec_005_a_missing_or_wrong_key_stops_the_server_clearly(owner, caplog):
    """FY-SEC-005: the database names the key it was encrypted with. Without the key file the
    server refuses to start (and never makes a new key over the old secrets); with another key
    too. The message says what to do."""
    from forgeyard.cli import main as cli_main

    _deploy_with_secrets(owner)
    path = get_settings().secret_key_path
    kid = crypto.box().kid
    backup = path.read_bytes()
    path.unlink()
    crypto.reset()
    reset_engine()
    with pytest.raises(crypto.SecretKeyError, match=f"database holds secrets encrypted with key {kid}"):
        with make_client():
            pass
    assert not path.exists()  # no new key was made
    caplog.set_level(logging.ERROR)
    assert cli_main(["serve"]) == 2
    assert "Restore the key file from your backup" in caplog.text and "secrets reset --yes" in caplog.text

    path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
    os.chmod(path, 0o600)
    crypto.reset()
    with pytest.raises(crypto.SecretKeyError, match=f"encrypted with key {kid}"):
        with make_client():
            pass

    path.write_bytes(backup)  # the backup is restored: everything is readable again
    crypto.reset()
    with make_client() as c:
        assert c.get("/api/health").status_code == 200
    db = new_session()
    try:
        assert db.scalar(select(Deployment)).hf_token == HF
    finally:
        db.close()


def test_fy_sec_006_secrets_reset_after_a_lost_key(owner, caplog):
    """FY-SEC-006: `secrets reset` needs --yes and a lost key; then it makes a new key, drops the
    Hugging Face tokens, places the model replicas again with a new generation (new keys), empties
    the lost env values and says so in the workload's events. The server starts again."""
    from forgeyard.cli import main as cli_main

    _deploy_with_secrets(owner)
    caplog.set_level(logging.INFO)
    assert cli_main(["secrets", "reset"]) == 2  # without --yes: it explains and touches nothing
    assert "Stop the server first, then run it again with --yes" in caplog.text
    assert cli_main(["secrets", "reset", "--yes"]) == 2  # the key is not lost
    assert "nothing is lost" in caplog.text
    db = new_session()
    gen_before = db.scalar(select(Workload).where(Workload.runtime == "model")).generation
    db.close()

    path = get_settings().secret_key_path
    path.unlink()
    crypto.reset()
    assert cli_main(["secrets", "reset", "--yes"]) == 0
    assert "Dropped 1 Hugging Face token(s), placed 1 model replica(s) again, emptied 2 workload" in caplog.text
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    crypto.reset()
    reset_engine()
    with make_client() as c:
        assert c.get("/api/health").status_code == 200
    db = new_session()
    try:
        assert db.scalar(select(Deployment)).hf_token is None
        model = db.scalar(select(Workload).where(Workload.runtime == "model"))
        assert model.generation == gen_before + 1
        assert all(r.generation == model.generation for r in model.replicas)
        web = db.scalar(select(Workload).where(Workload.name == "web"))
        assert web.spec["env"] == {"API_TOKEN": "", "MODE": ""}
        events = [e.message for e in db.scalars(select(WorkloadEvent).where(WorkloadEvent.kind == "secret_reset"))]
        assert any("API_TOKEN, MODE were lost" in m for m in events) and any("new internal keys" in m for m in events)
    finally:
        db.close()
