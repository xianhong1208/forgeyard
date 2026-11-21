"""The DCO workflow checks contributors' pull requests, not the release pull requests between dev and main."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _job():
    wf = yaml.safe_load((ROOT / ".github/workflows/dco.yml").read_text())
    return wf["jobs"]["dco"]


def test_fy_dco_001_release_pull_requests_are_not_checked_again():
    """FY-DCO-001: the dco job skips dev → main and main → dev from this repository (each commit was checked on
    its own pull request), and still runs for every other pull request, including a fork's branch named dev."""
    cond = " ".join(_job()["if"].split())
    assert "github.event.pull_request.head.repo.full_name != github.repository ||" in cond
    assert "(github.event.pull_request.head.ref == 'dev' && github.event.pull_request.base.ref == 'main')" in cond
    assert "(github.event.pull_request.head.ref == 'main' && github.event.pull_request.base.ref == 'dev')" in cond
    assert cond.split("||", 1)[1].strip().startswith("!((")
    steps = " ".join(s.get("run", "") for s in _job()["steps"])
    assert 'scripts/check_dco.sh "$BASE" "$HEAD"' in steps
