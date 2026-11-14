"""The process runtime: a replica is a process group on the host.

- **Never as root by default.** When the agent runs as root, processes run as the unprivileged
  `--workload-user`; without one the runtime is unavailable and says so. An agent that is not
  root runs processes as its own user and cannot switch.
- **Its own process group** (a new session), so stopping a replica signals everything it
  started and nothing else, and a Ctrl-C in the agent's terminal never reaches it.
- **Limits:** on a systemd host with a root agent, a transient scope (cgroup) caps CPU and
  memory. Otherwise only rlimits: no core dumps, and an address-space cap for memory on
  workloads without GPUs. CPU is then not capped (placement still counts it).
- **Adoption:** the pid, its kernel start time and the boot id are saved, so a restarted agent
  recognises its processes (and never a stranger that reused the pid). A small `sh` wrapper
  writes the exit code to a file, because an adopted process is no longer the agent's child.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from forgeyard.agent.workloads import logs, plan
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.runtime import Observation, StartError
from forgeyard.workloads.spec import ProcessRuntime as ProcessSpec, WorkloadSpec

log = logging.getLogger("forgeyard.agent")

EXIT_FILE = ".forgeyard-exit"
LIMIT_MODES = ("auto", "systemd", "rlimit")


def boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def proc_stat(pid: int) -> Optional[tuple[str, int, int]]:
    """(state, pgid, starttime) of a pid, or None if there is no such process."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    rest = raw.rsplit(")", 1)[-1].split()
    try:
        return rest[0], int(rest[2]), int(rest[19])
    except (IndexError, ValueError):
        return None


def is_ours(pid: Optional[int], start: Optional[int], boot: Optional[str]) -> bool:
    """The process we started is still alive: same pid, same start time, same boot."""
    if not pid or start is None or (boot and boot != boot_id()):
        return False
    st = proc_stat(pid)
    return st is not None and st[0] != "Z" and st[2] == start


def marked_members(rec_id: str, pgid: Optional[int], leader_start: Optional[int] = None,
                   boot: Optional[str] = None) -> list[int]:
    """Processes of a replica whose group leader is gone: in its process group *and* known to be
    its own, so a reused group id can never point at someone else.

    Its own: the process carries the replica's FORGEYARD_REPLICA marker in its environment. When
    that cannot be read (a root agent in a container without CAP_SYS_PTRACE may not read another
    user's /proc/<pid>/environ), the group itself is the proof, as long as its id was not reused:
    Linux never hands out a pid that is still a living group's id, so while members of the group
    remain, the id is still the one the replica's leader created. Only if a *different* process now
    has the group's id as its pid (the old group died out and the number came back) is nothing
    taken without the marker."""
    if not pgid:
        return []
    if boot and boot != boot_id():
        return []
    marker = f"FORGEYARD_REPLICA={rec_id}".encode()
    leader = proc_stat(pgid)
    reused = leader is not None and leader[0] != "Z" and leader_start is not None and leader[2] != leader_start
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        st = proc_stat(int(entry.name))
        if st is None or st[1] != pgid or st[0] == "Z":
            continue
        try:
            marked = marker in (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            marked = None  # unreadable: decide by the group
        if marked or (marked is None and leader_start is not None and not reused):
            out.append(int(entry.name))
    return out


def find_leader(rec_id: str) -> Optional[tuple[int, int, int]]:
    """A replica's group leader found by its marker: (pid, pgid, start time). For the moment
    between starting a process and saving its pid, should the agent die right then."""
    marker = f"FORGEYARD_REPLICA={rec_id}".encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        st = proc_stat(pid)
        if st is None or st[1] != pid or st[0] == "Z":
            continue
        try:
            if marker in (entry / "environ").read_bytes().split(b"\0"):
                return pid, pid, st[2]
        except OSError:
            continue
    return None


def _address(hexaddr: str) -> str:
    """An address from /proc/net/tcp{,6} (little-endian 32-bit words) as text."""
    import ipaddress

    raw = bytes.fromhex(hexaddr)
    raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return str(ipaddress.ip_address(raw))


def beyond_loopback(ports: list[int], proc_net: Path = Path("/proc/net")) -> list[str]:
    """Listening sockets on these ports bound to anything but loopback ("0.0.0.0:31001"). A guarded
    engine must listen on 127.0.0.1 only; a recipe that ignores {host} would be reachable around
    the agent's key guard."""
    import ipaddress

    wanted, out = set(ports), []
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc_net / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 4 or cols[3] != "0A":  # 0A: LISTEN
                continue
            hexaddr, _, hexport = cols[1].partition(":")
            try:
                port, addr = int(hexport, 16), _address(hexaddr)
            except ValueError:
                continue
            if port not in wanted:
                continue
            ip = ipaddress.ip_address(addr)
            mapped = getattr(ip, "ipv4_mapped", None)
            if not (ip.is_loopback or (mapped is not None and mapped.is_loopback)):
                out.append(f"{addr}:{port}" if ":" not in addr else f"[{addr}]:{port}")
    return sorted(set(out))


# `limits = auto` asks systemd for a throwaway scope instead of looking for its files: an agent in a
# container can see the host's /run/systemd and systemd-run and still fail with "Failed to connect to
# bus" (A14 of the settings audit). Only a root agent probes (a scope needs root).
SYSTEMD_PROBE = ["systemd-run", "--scope", "--quiet", "--collect", "--", "true"]
SYSTEMD_PROBE_TIMEOUT = 10.0


def systemd_running(run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                    which: Callable[[str], Optional[str]] = shutil.which) -> bool:
    """Whether a systemd scope really works here: systemd-run exists and starting a scope succeeds."""
    if which("systemd-run") is None:
        return False
    try:
        done = run(SYSTEMD_PROBE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=SYSTEMD_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        log.info("systemd-run cannot start a scope here (%s): process workloads get rlimits only.",
                 (done.stderr or b"").decode("utf-8", "replace").strip()[:200] or f"exit {done.returncode}")
        return False
    return True


class ProcessRuntime:
    name = "process"

    def __init__(self, store: RecordStore, work_root: Path, *, workload_user: Optional[str] = None,
                 limits: str = "auto", euid: Callable[[], int] = os.geteuid,
                 systemd: Callable[[], bool] = systemd_running):
        self.store = store
        self.work_root = Path(work_root)
        self.children: dict[str, subprocess.Popen] = {}
        self._euid = euid
        self._info = self._resolve(workload_user, limits, systemd)

    # ------------------------------------------------------------------ who runs it
    def _resolve(self, user: Optional[str], limits: str, systemd: Callable[[], bool]) -> dict:
        if limits not in LIMIT_MODES:
            return {"ok": False, "reason": f"--limits must be one of {', '.join(LIMIT_MODES)}"}
        me = self._euid()
        if me == 0:
            if not user:
                return {"ok": False, "reason": "the agent runs as root and no --workload-user is set; "
                                               "process workloads are refused rather than run as root"}
            try:
                pw = pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
            except (KeyError, ValueError):
                return {"ok": False, "reason": f"--workload-user {user!r} does not exist on this machine"}
            if pw.pw_uid == 0:
                return {"ok": False, "reason": "--workload-user must not be root"}
            self.uid, self.gid, self.user, self.switch = pw.pw_uid, pw.pw_gid, pw.pw_name, True
        else:
            try:
                name = pwd.getpwuid(me).pw_name
            except KeyError:
                name = str(me)
            if user and user not in (name, str(me)):
                return {"ok": False, "reason": f"the agent runs as {name}, not root, so it can only run processes "
                                               f"as {name}; drop --workload-user or start the agent as root"}
            self.uid, self.gid, self.user, self.switch = me, os.getegid(), name, False
        mode = "rlimit"
        if limits in ("auto", "systemd") and me == 0 and systemd():
            mode = "systemd"
        elif limits == "systemd":
            return {"ok": False, "reason": "--limits systemd needs a root agent on a host running systemd"}
        self.limits = mode
        return {"ok": True, "user": self.user, "uid": self.uid, "limits": mode,
                "cpu_limit": mode == "systemd",
                "memory_limit": "cgroup" if mode == "systemd" else "address space (workloads without GPUs)"}

    def info(self) -> dict:
        return dict(self._info)

    # ------------------------------------------------------------------ paths
    def home(self, rec: Record) -> Path:
        return self.work_root / rec.id

    def _prepare_dirs(self, rec: Record) -> tuple[Path, Path]:
        self.work_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.work_root, 0o711)  # the workload user may enter its own directory, not list others
        home = self.home(rec)
        home.mkdir(exist_ok=True)
        os.chmod(home, 0o700)
        if self.switch:
            os.chown(home, self.uid, self.gid)
        log_dir = self.store.log_dir(rec.id)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        return home, log_dir

    @staticmethod
    def _device_gids(gpus: list[dict]) -> list[int]:
        """AMD GPUs need the groups owning /dev/kfd and /dev/dri/renderD*; without GPUs the
        workload gets no extra group at all, so the kernel itself keeps it off AMD devices."""
        if not any(g.get("vendor") == "amd" for g in gpus):
            return []
        gids = set()
        for dev in [Path("/dev/kfd"), *sorted(Path("/dev/dri").glob("renderD*"))]:
            try:
                gids.add(dev.stat().st_gid)
            except OSError:
                continue
        return sorted(g for g in gids if g != 0)

    # ------------------------------------------------------------------ lifecycle
    def start(self, rec: Record, address: str, secret: Optional[dict[str, str]] = None) -> None:
        if not self._info.get("ok"):
            raise StartError(self._info.get("reason") or "the process runtime is not available")
        spec = WorkloadSpec.model_validate(rec.spec)
        rt = spec.runtime
        assert isinstance(rt, ProcessSpec)
        home, log_dir = self._prepare_dirs(rec)
        extra = None
        if rec.model is not None:
            if not rec.paths:
                raise StartError("the model is not in the cache")
            # Behind the guard the engine binds loopback only; the guard holds the published address.
            bind = plan.LOOPBACK if plan.guarded(rec.ports) else address
            extra = plan.model_values(rec.paths, container=False, model=rec.model, bind=bind)
        env = plan.process_env(spec.env, rec.gpus, rec.ports, home=str(home), user=self.user,
                               replica_id=rec.id, workload=rec.workload, ordinal=rec.ordinal, extra=extra,
                               secret=secret)
        command = rec.engine_path or rt.command
        exe = command if "/" in command else shutil.which(command, path=env.get("PATH", plan.BASIC_PATH))
        if not exe or not os.path.isfile(exe):
            raise StartError(f"command not found: {rt.command}")
        # 1.3: ${BIND_ADDRESS} in a plain process's arguments too: loopback behind the key guard (a workload
        # exposed at /x/), else the agent's own address. (Its environment values stay as declared.)
        bind_value = {"BIND_ADDRESS": plan.LOOPBACK if plan.guarded(rec.ports) else address}
        args = plan.substitute(rt.args, env, extra if extra is not None else bind_value)
        exit_file = home / EXIT_FILE
        exit_file.unlink(missing_ok=True)
        cwd = rt.workdir or str(home)
        if not os.path.isdir(cwd):
            raise StartError(f"workdir does not exist: {cwd}")
        inner = plan.process_argv(exe, args, str(exit_file),
                                  plan.ulimits(spec.resources.memory, rec.gpus) if self.limits == "rlimit" else ["-c 0"])
        groups = self._device_gids(rec.gpus)
        kwargs: dict = {}
        if self.limits == "systemd":
            argv = plan.systemd_argv(f"forgeyard-{rec.id[:12]}-{rec.generation}-{rec.attempt + rec.restarts}",
                                     cpu=spec.resources.cpu, memory=spec.resources.memory, uid=self.uid,
                                     gid=self.gid, groups=groups, inner=inner)
        else:
            argv = inner
            if self.switch:
                kwargs = {"user": self.uid, "group": self.gid, "extra_groups": groups}
        fd = os.open(log_dir / logs.LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fd,
                                    stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                    umask=0o077, **kwargs)
        except OSError as e:
            raise StartError(f"could not start {rt.command}: {e.strerror or e}") from None
        finally:
            os.close(fd)
        self.children[rec.id] = proc
        rec.pid = proc.pid
        rec.pgid = proc.pid  # a new session: the process leads its own group
        st = proc_stat(proc.pid)
        rec.proc_start = st[2] if st else None
        rec.boot_id = boot_id()

    def _exit_code_file(self, rec: Record) -> Optional[int]:
        try:
            return int((self.home(rec) / EXIT_FILE).read_text().strip())
        except (OSError, ValueError):
            return None

    def observe(self, rec: Record) -> Observation:
        proc = self.children.get(rec.id)
        if proc is not None and proc.pid == rec.pid:
            code = proc.poll()
            if code is None:
                return Observation(alive=True)
            self.children.pop(rec.id, None)
            if code < 0:  # the wrapper itself was killed by a signal
                code = 128 - code
            return Observation(alive=False, exit_code=code)
        if is_ours(rec.pid, rec.proc_start, rec.boot_id):
            return Observation(alive=True)
        code = self._exit_code_file(rec)
        return Observation(alive=False, exit_code=code, missing=code is None)

    def _signal(self, rec: Record, sig: int) -> bool:
        """Signal the replica's processes. True if any were there."""
        proc = self.children.get(rec.id)
        if proc is not None:
            proc.poll()  # reap a finished leader, so its group can be seen empty
        if is_ours(rec.pid, rec.proc_start, rec.boot_id) or (proc is not None and proc.returncode is None):
            try:
                os.killpg(rec.pgid or rec.pid, sig)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                log.warning("Not allowed to signal replica %s (pgid %s).", rec.id, rec.pgid)
                return False
        hit = False
        for pid in marked_members(rec.id, rec.pgid, rec.proc_start, rec.boot_id):
            try:
                os.kill(pid, sig)
                hit = True
            except (ProcessLookupError, PermissionError):
                continue
        return hit

    def stop(self, rec: Record, grace: float) -> None:
        if not self._signal(rec, signal.SIGTERM):
            self._reap(rec)
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            time.sleep(0.1)
            if not self._signal(rec, 0):
                self._reap(rec)
                return
        self._signal(rec, signal.SIGKILL)
        end = time.monotonic() + 5
        while time.monotonic() < end and self._signal(rec, 0):
            time.sleep(0.05)
        self._reap(rec)

    def _reap(self, rec: Record) -> None:
        proc = self.children.pop(rec.id, None)
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def cleanup(self, rec: Record) -> None:
        """After the main process ended: anything it left behind in its group goes too (an engine
        whose wrapper was killed would otherwise keep the port, and its successor could not start)."""
        for pid in marked_members(rec.id, rec.pgid, rec.proc_start, rec.boot_id):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def remove(self, rec: Record) -> None:
        shutil.rmtree(self.home(rec), ignore_errors=True)
        shutil.rmtree(self.store.log_dir(rec.id), ignore_errors=True)

    def tail(self, rec: Record, lines: int) -> tuple[str, bool]:
        return logs.tail(self.store.log_dir(rec.id), lines)

    def trim(self, rec: Record, max_bytes: int) -> None:
        logs.trim(self.store.log_dir(rec.id), max_bytes)

    def owned(self) -> dict[str, dict]:
        return {}  # records are the source of truth for processes

