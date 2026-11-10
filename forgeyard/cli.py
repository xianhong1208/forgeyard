"""Command line: `python -m forgeyard [serve]` runs the server, `python -m forgeyard agent ...` the agent.

    python -m forgeyard agent --server http://forge.lan:7860 --join-token fyj_...   # first start: join
    python -m forgeyard agent                                                      # later starts
    python -m forgeyard agent inventory [--json] [--raw]                           # what it detects
    python -m forgeyard agent runtimes [--workload-user nobody]                     # what it can run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from forgeyard import __version__


def _serve() -> int:
    import uvicorn

    from forgeyard.app import create_app
    from forgeyard.config import get_settings

    settings = get_settings()
    uvicorn.run(create_app(), host=settings.host, port=settings.port, proxy_headers=False)
    return 0


def _labels(values: list[str]) -> dict:
    out = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--label wants key=value, got {item!r}")
        out[key.strip()] = value.strip()
    return out


def _make_inventory(simulate: Optional[str]):
    from forgeyard.agent.inventory import Inventory, SimulatedInventory, SimulationNotAllowed

    if not simulate:
        return Inventory()
    try:
        inv = SimulatedInventory(simulate)
    except SimulationNotAllowed as e:
        raise SystemExit(f"error: {e}")
    except FileNotFoundError as e:
        raise SystemExit(f"error: {e}")
    logging.getLogger("forgeyard.agent").warning(
        "\n%s\nSIMULATED INVENTORY from %s: this agent reports made-up hardware.\n"
        "The server shows this machine as simulated. Never use this on a real deployment.\n%s",
        "!" * 72, inv.path, "!" * 72)
    return inv


def _human_size(n: Optional[int]) -> str:
    return "?" if n is None else f"{n / 1024**3:.1f} GiB"


def _inventory_cmd(args) -> int:
    inv = _make_inventory(args.simulate_inventory)
    data = inv.full(raw=args.raw)
    inv.close()
    report = {"agent_version": __version__, "simulated": inv.simulated, **data}
    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    for a in data["accelerators"]:
        mem = f"{_human_size(a.get('vram_free'))} free of {_human_size(a.get('vram_total'))}"
        flags = " unified-memory" if a.get("unified_memory") else ""
        print(f"{a['vendor']:<6} {a.get('index', 0):>2}  {a['model']}  arch={a.get('arch') or '?'}  {mem}"
              f"  driver={a.get('driver') or '-'}{flags}")
    for name, status in data["collectors"].items():
        state = "ok" if status.get("ok") else "nothing"
        why = f" ({status['reason']})" if status.get("reason") else ""
        print(f"[{name}] {state}: {status.get('count', 0)} via {status.get('source') or '-'}{why}")
    return 0


def _make_supervisor(args, state_dir: Path):
    """The workload side of the agent (M2): runtimes and the reconciler."""
    from forgeyard.agent import state
    from forgeyard.agent.workloads.container import ContainerRuntime
    from forgeyard.agent.workloads.ports import parse_range
    from forgeyard.agent.workloads.process import ProcessRuntime
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor

    store = RecordStore(state_dir)
    work_root = Path(args.work_dir) if args.work_dir else state.default_work_root(state_dir)
    log_max = max(1, int(args.log_max_mb)) * 1024**2
    runtimes = {
        "process": ProcessRuntime(store, work_root, workload_user=args.workload_user, limits=args.limits),
        "container": ContainerRuntime(store, agent_id=state.agent_id(state_dir), engine=args.container_engine,
                                      log_max_bytes=log_max),
    }
    models = None
    if getattr(args, "model_cache", None):
        from forgeyard.agent.models import ModelCache

        models = ModelCache(Path(args.model_cache), allow_private=bool(args.allow_private_model_hosts),
                            local_dirs=list(args.model_local_dir or []))
    return Supervisor(state_dir, runtimes, port_range=parse_range(args.port_range), log_max_bytes=log_max,
                      bind_address=args.bind_address or None, models=models)


def _describe_runtimes(info: dict) -> list[str]:
    lines = []
    p = info.get("process", {})
    if p.get("ok"):
        lines.append(f"process   ok: runs as {p.get('user')} (uid {p.get('uid')}); limits: {p.get('limits')}")
    else:
        lines.append(f"process   unavailable: {p.get('reason')}")
    c = info.get("container", {})
    if c.get("ok"):
        gpu = f"NVIDIA GPUs via {c.get('gpu_mode')}" if c.get("nvidia") else "no NVIDIA GPU support (no nvidia runtime or CDI)"
        lines.append(f"container ok: {c.get('engine')} {c.get('version') or ''}; {gpu}")
    else:
        lines.append(f"container unavailable: {c.get('reason')}")
    return lines


def _runtimes_cmd(args) -> int:
    import tempfile

    from forgeyard.agent import state

    state_dir = Path(args.state_dir) if args.state_dir else state.default_state_dir()
    if state_dir.exists():
        info = _make_supervisor(args, state_dir).runtime_info()
    else:  # only looking: never create the real state directory for it
        with tempfile.TemporaryDirectory(prefix="forgeyard-runtimes-") as tmp:
            info = _make_supervisor(args, Path(tmp)).runtime_info()
    if args.json:
        json.dump(info, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print("\n".join(_describe_runtimes(info)))
    return 0


def _agent_cmd(args) -> int:
    from forgeyard.agent import state
    from forgeyard.agent.client import EXIT_JOIN_FAILED, EXIT_USAGE, Agent, JoinError, join, normalise_server

    log = logging.getLogger("forgeyard.agent")
    state_dir = Path(args.state_dir) if args.state_dir else state.default_state_dir()
    try:
        saved = state.load(state_dir)
    except (ValueError, OSError) as e:
        log.error("Cannot read the agent state in %s: %s", state_dir, e)
        return EXIT_USAGE
    inventory = _make_inventory(args.simulate_inventory)
    try:
        server = normalise_server(args.server) if args.server else (saved or {}).get("server")
    except ValueError as e:
        log.error("%s", e)
        return EXIT_USAGE

    if args.join_token:
        if saved and not args.rejoin:
            log.error("This machine already joined %s as %r (state in %s). Start without --join-token, "
                      "or pass --rejoin to join again as a new machine.", saved.get("server"), saved.get("name"),
                      state_dir)
            return EXIT_USAGE
        if not server:
            log.error("--server is required to join.")
            return EXIT_USAGE
        try:
            joined = join(server, args.join_token, name=args.name)
        except JoinError as e:
            log.error("%s", e)
            return EXIT_JOIN_FAILED
        saved = {"server": server, "node_id": joined["node_id"], "name": joined["name"],
                 "credential": joined["credential"]}
        path = state.save(state_dir, saved)
        log.info("Joined %s as %r; credential saved to %s (mode 0600).", server, joined["name"], path)
    elif not saved:
        log.error("This machine has not joined yet. Create a join token on the Machines page and run:\n"
                  "    python -m forgeyard agent --server <url> --join-token <token>")
        return EXIT_USAGE
    elif server != saved.get("server"):
        log.info("Using --server %s (the credential was issued by %s).", server, saved.get("server"))

    try:
        supervisor = None if args.no_workloads else _make_supervisor(args, state_dir)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_USAGE
    if supervisor is not None:
        for line in _describe_runtimes(supervisor.runtime_info()):
            log.info("Runtime %s", line)
        cache = supervisor.model_cache_info()
        if cache is None:
            log.info("Models: not served here (start with --model-cache DIR to serve models).")
        elif cache.get("ok"):
            log.info("Models: cache in %s, %.1f GiB free, %d cached.", args.model_cache,
                     (cache.get("free_bytes") or 0) / 1024**3, len(cache.get("cached") or []))
        else:
            log.error("Models: %s", cache.get("reason"))
    agent = Agent(server, saved["credential"], inventory, labels=_labels(args.label), supervisor=supervisor)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, agent.stop)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            return await agent.run()
        finally:
            await asyncio.to_thread(inventory.close)

    return asyncio.run(main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgeyard", description="Forgeyard server and agent.")
    parser.add_argument("--version", action="version", version=f"forgeyard {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (the default)")

    agent = sub.add_parser("agent", help="run the agent on a machine that should join a server")
    agent.add_argument("--server", help="the server's address, e.g. http://192.168.1.20:7860")
    agent.add_argument("--join-token", help="one-time token from the Machines page (first start only)")
    agent.add_argument("--state-dir", help="where the machine credential is kept "
                                           "(default /var/lib/forgeyard-agent as root, else ~/.local/state/forgeyard-agent)")
    agent.add_argument("--name", help="machine name shown on the server (default: the host name)")
    agent.add_argument("--label", action="append", default=[], metavar="KEY=VALUE", help="a label; repeatable")
    agent.add_argument("--rejoin", action="store_true", help="join again even though a credential is saved")
    agent.add_argument("--simulate-inventory", metavar="FIXTURE",
                       help="report a fixture instead of real hardware (development only; needs "
                            "FORGEYARD_AGENT_SIMULATION=1)")
    _workload_args(agent)
    agent.add_argument("--no-workloads", action="store_true", help="only report hardware; run no workloads")
    agent.add_argument("-v", "--verbose", action="store_true")
    agent_sub = agent.add_subparsers(dest="agent_command")
    inv = agent_sub.add_parser("inventory", help="print what the agent detects, without joining")
    inv.add_argument("--json", action="store_true", help="machine-readable output")
    inv.add_argument("--raw", action="store_true", help="include what each tool returned (for bug reports)")
    inv.add_argument("--simulate-inventory", metavar="FIXTURE", help=argparse.SUPPRESS)
    rts = agent_sub.add_parser("runtimes", help="print which runtimes the agent can use here, without joining")
    rts.add_argument("--json", action="store_true", help="machine-readable output")
    _workload_args(rts, sub=True)
    return parser


def _workload_args(p: argparse.ArgumentParser, *, sub: bool = False) -> None:
    """The agent's workload options. On a subcommand they default to SUPPRESS, so the same option
    works before or after the subcommand name (argparse would otherwise reset it)."""

    def d(value):
        return argparse.SUPPRESS if sub else value

    p.add_argument("--workload-user", default=d(None), metavar="USER",
                   help="unprivileged user that process workloads run as when the agent is root "
                        "(required then: without it, process workloads are refused)")
    p.add_argument("--work-dir", default=d(None),
                   help="where each process replica gets its own directory (default: next to the state directory)")
    p.add_argument("--port-range", default=d("30000-32767"), metavar="LOW-HIGH",
                   help="host ports for services (default 30000-32767)")
    p.add_argument("--bind-address", default=d(None),
                   help="address container ports are published on (default: this machine's address towards "
                        "the server)")
    p.add_argument("--container-engine", default=d("auto"),
                   help="auto (docker, then podman), docker, podman, none, or a path")
    p.add_argument("--limits", choices=("auto", "systemd", "rlimit"), default=d("auto"),
                   help="process CPU/memory limits: a systemd scope (root on systemd hosts) or rlimits")
    p.add_argument("--log-max-mb", type=int, default=d(10), help="output kept per replica, in MB (default 10)")
    p.add_argument("--model-cache", default=d(None), metavar="DIR",
                   help="serve models: where models and engines are downloaded and kept, shared by every replica "
                        "on this machine (containers get their model mounted read-only from here)")
    p.add_argument("--allow-private-model-hosts", action="store_true", default=d(False),
                   help="allow model downloads from private addresses and plain http (a LAN mirror only)")
    p.add_argument("--model-local-dir", action="append", default=d([]), metavar="DIR",
                   help="a directory local:// models may come from on this machine; repeatable")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not verbose:
        logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.command == "agent":
        if args.agent_command == "inventory":
            return _inventory_cmd(args)
        if args.agent_command == "runtimes":
            return _runtimes_cmd(args)
        return _agent_cmd(args)
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
