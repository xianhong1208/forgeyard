"""A test run that hangs in CI leaves its stacks in the log instead of being cancelled at the job's limit with
no log at all (#286)."""
import re
import shlex
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
# GitHub's own limit for a job without timeout-minutes.
DEFAULT_JOB_MINUTES = 360
# What a job does before its pytest step (checkout, Python, uv, uv sync, ruff) takes well under a minute; the
# step's limit must leave at least this much of the job's limit, or the job is cancelled first and the log is lost.
SETUP_MARGIN_S = 3 * 60


def _seconds(duration: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", duration)
    assert m, f"not a timeout duration: {duration}"
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def _pytest_commands():
    """(workflow, job, job limit in s, command tokens) for every command in a run step that starts pytest."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(path.read_text())
        for job_name, job in wf["jobs"].items():
            limit = job.get("timeout-minutes", DEFAULT_JOB_MINUTES) * 60
            for step in job.get("steps", []):
                script = step.get("run", "").replace("\\\n", " ")
                for line in script.splitlines():
                    if "pytest" not in line:  # other lines may be parts of a multi-line quoted string
                        continue
                    tokens = shlex.split(line)
                    if "pytest" in tokens:
                        found.append((path.name, job_name, limit, tokens))
    return found


def test_fy_cihang_001_every_ci_pytest_run_dumps_its_stacks_before_the_job_limit():
    """FY-CIHANG-001: every pytest command in .github/workflows runs as `timeout --signal=ABRT --kill-after=… <limit>
    <python> -X faulthandler -m pytest`, inside the job's own limit with room for the job's setup. On a hang, SIGABRT
    makes faulthandler print every thread's stack and the step fails (a failed step keeps its log; a job cancelled at
    timeout-minutes does not). -X faulthandler, not only pytest's own handler: pytest turns its handler off before
    interpreter exit, where a non-daemon thread left behind waits. The timeout goes to python itself (inside
    `uv run`), so the signal does not depend on uv forwarding it."""
    commands = _pytest_commands()
    jobs = {(wf, job) for wf, job, _, _ in commands}
    assert {("ci.yml", "backend"), ("ci.yml", "backend-postgres"), ("ci.yml", "vision-image")} <= jobs
    for wf, job, job_limit, tokens in commands:
        where = f"{wf} {job}: {' '.join(tokens)}"
        assert "timeout" in tokens, where
        t = tokens.index("timeout")
        options, rest = [], tokens[t + 1:]
        while rest and rest[0].startswith("--"):
            options.append(rest.pop(0))
        assert "--signal=ABRT" in options, where
        kill_after = [o.split("=", 1)[1] for o in options if o.startswith("--kill-after=")]
        assert len(kill_after) == 1, where
        step_limit, python, *args = rest
        assert re.search(r"(^|/)python3?$", python), where
        assert args[:4] == ["-X", "faulthandler", "-m", "pytest"], where
        assert _seconds(step_limit) + _seconds(kill_after[0]) + SETUP_MARGIN_S <= job_limit, where


def test_fy_cihang_002_a_single_hung_test_still_fails_on_its_own_first():
    """FY-CIHANG-002: the per-test limit (pytest-timeout, thread method: it prints every thread's stack and names
    the test) stays, and is shorter than the CI step's limit, so a hang inside one test is still reported as that
    test; the step's limit catches what pytest-timeout does not cover (collection, session hooks, interpreter exit)."""
    ini = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert ini["timeout_method"] == "thread"
    backend = [c for c in _pytest_commands() if c[:2] in {("ci.yml", "backend"), ("ci.yml", "backend-postgres")}]
    assert len(backend) == 2
    for _, _, _, tokens in backend:
        rest = [x for x in tokens[tokens.index("timeout") + 1:] if not x.startswith("--")]
        assert 0 < int(ini["timeout"]) < _seconds(rest[0])
