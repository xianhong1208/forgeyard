"""Migrations, the UI fallback and small app behaviour (FY-APP-*)."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from forgeyard import __version__
from forgeyard.db import Base, get_engine


def test_fy_app_001_migrations_match_the_models(client):
    """FY-APP-001: schema drift check (the app ran `upgrade head` on start)."""
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_fy_app_002_migrations_go_down_and_up(client):
    """FY-APP-002"""
    from alembic import command

    from forgeyard.migrate import alembic_config, upgrade

    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    upgrade()
    assert client.get("/api/auth/state").status_code == 200


def test_fy_app_003_unknown_api_route_is_json_404(client):
    """FY-APP-003: never the HTML shell for an API path."""
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    assert client.post("/api/nope").status_code == 404


def test_fy_app_004_ui_routes_fall_back_to_the_shell(client):
    """FY-APP-004"""
    r = client.get("/users/42")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert "text/html" in r.headers["content-type"] and r.headers["cache-control"] == "no-cache"


def test_fy_app_005_no_path_traversal(client):
    """FY-APP-005"""
    r = client.get("/..%2f..%2fpyproject.toml")
    assert "requires-python" not in r.text


def test_fy_app_006_health(client):
    """FY-APP-006"""
    assert client.get("/api/health").json() == {"ok": True, "version": __version__}
