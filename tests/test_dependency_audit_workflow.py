"""The dependency audit workflow (FY-DEPAUD-*): what it audits, when, and that it is information, not a gate."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _wf() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/dependency-audit.yml").read_text())


def test_fy_depaud_001_python_and_web_runtime_packages_on_every_pull_request():
    """FY-DEPAUD-001: on every pull request and push to dev and main, pip-audit reads uv.lock's runtime set (every
    extra, no dev group, hashed, so pip is never asked to resolve anything) at a pinned version, and npm audit the web
    UI's runtime packages; each job is bounded in time and reads only."""
    wf = _wf()
    on = wf.get("on", wf.get(True))   # PyYAML reads the key `on` as True
    assert on["pull_request"]["branches"] == ["main", "dev"] and on["push"]["branches"] == ["main", "dev"]
    assert wf["permissions"] == {"contents": "read"}
    py = " ".join(s.get("run", "") for s in wf["jobs"]["python"]["steps"])
    assert "uv export --locked --no-dev --all-extras --format requirements-txt -o requirements-audit.txt" in py
    assert "--no-hashes" not in py
    assert "pip-audit==" in py and "-r requirements-audit.txt --disable-pip" in py
    web = wf["jobs"]["web"]
    assert web["defaults"]["run"]["working-directory"] == "web"
    assert "npm audit --omit=dev" in " ".join(s.get("run", "") for s in web["steps"])
    assert all(j.get("timeout-minutes") for j in wf["jobs"].values())


def test_fy_depaud_002_it_is_its_own_workflow_not_a_ci_job():
    """FY-DEPAUD-002: the audit is a workflow of its own, so the required CI checks (backend, backend-postgres,
    frontend, agent-image) never wait on an advisory published against a package nobody changed."""
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert not any("audit" in name for name in ci["jobs"])
    assert _wf()["name"] == "Dependency audit"
