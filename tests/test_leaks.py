"""The safety net for processes a test leaves running (FY-LEAK-*): see tests/leaks.py."""

import os
import subprocess
import sys
import time
from pathlib import Path

from tests import leaks


def _sleeper(cwd: Path, *argv: str) -> subprocess.Popen:
    # Its own session, like a process workload: it would outlive the test that started it.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv], cwd=cwd,
                            start_new_session=True)


def test_fy_leak_001_found_by_working_directory_or_command_line_and_killed(tmp_path):
    """FY-LEAK-001: a process working inside a test directory, and one naming it on its command line,
    are found, killed, and the directory removed; nothing else is touched."""
    d = leaks.world_dir()
    assert d.name.startswith("fy-test-") and (d / ".owner").read_text() == str(os.getpid())
    (d / "work").mkdir()
    by_cwd = _sleeper(d / "work")
    by_arg = _sleeper(tmp_path, str(d / "engine.py"))
    elsewhere = _sleeper(tmp_path)
    try:
        time.sleep(0.2)
        found = {pid for pid, _ in leaks.processes_in([d])}
        assert found == {by_cwd.pid, by_arg.pid}
        killed = leaks.cleanup(d)
        assert {pid for pid, _ in killed} == found and not d.exists()
        assert by_cwd.wait(5) and by_arg.wait(5)  # killed: a non-zero exit
        assert elsewhere.poll() is None
    finally:
        elsewhere.kill()
        elsewhere.wait()


def test_fy_leak_002_a_test_that_leaves_a_process_is_named(tmp_path):
    """FY-LEAK-002: what the per-test check does after each test: a process left in the test's own
    directory is found and killed, and described with its pid and command line for the failure."""
    p = _sleeper(tmp_path)
    time.sleep(0.2)
    found = leaks.check([tmp_path])
    assert [pid for pid, _ in found] == [p.pid] and p.wait(5)
    assert f"pid {p.pid}: " in leaks.describe(found) and "time.sleep(60)" in leaks.describe(found)
    assert leaks.check([tmp_path]) == []


def test_fy_leak_003_leftovers_of_gone_runs_but_never_a_live_runs(tmp_path):
    """FY-LEAK-003: at the start of a run, directories of runs that are gone count as leftovers (their
    .owner pid is dead, or they are older than an hour without one); a live run's never do."""
    def made(name: str, owner=None, age: float = 0.0) -> Path:
        d = tmp_path / f"fy-test-{name}"
        d.mkdir()
        if owner is not None:
            (d / ".owner").write_text(str(owner))
        if age:
            t = time.time() - age
            os.utime(d, (t, t))
        return d

    dead = subprocess.Popen(["true"])
    dead.wait()
    gone = made("gone", owner=dead.pid)
    live = made("live", owner=os.getpid())
    old = made("old", age=7200)
    new = made("new")
    stale = set(leaks.stale_dirs(str(tmp_path)))
    assert stale == {gone, old} and live not in stale and new not in stale
