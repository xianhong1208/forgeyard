"""Command line: `python -m forgeyard [serve]` runs the server, `python -m forgeyard agent ...` the agent.

    python -m forgeyard agent --server http://forge.lan:7860 --join-token fyj_...   # first start: join
    python -m forgeyard agent                                                      # later starts
    python -m forgeyard agent inventory [--json] [--raw]                           # what it detects
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

    agent = Agent(server, saved["credential"], inventory, labels=_labels(args.label))

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
    agent.add_argument("-v", "--verbose", action="store_true")
    agent_sub = agent.add_subparsers(dest="agent_command")
    inv = agent_sub.add_parser("inventory", help="print what the agent detects, without joining")
    inv.add_argument("--json", action="store_true", help="machine-readable output")
    inv.add_argument("--raw", action="store_true", help="include what each tool returned (for bug reports)")
    inv.add_argument("--simulate-inventory", metavar="FIXTURE", help=argparse.SUPPRESS)
    return parser


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
        return _agent_cmd(args)
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
