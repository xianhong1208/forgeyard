"""Command line: `python -m forgeyard [serve]` runs the server, `python -m forgeyard agent ...` the agent.

    python -m forgeyard agent --server https://forge.lan:7860 --join-token fyj_... \
        --ca-fingerprint sha256:...                                                # first start: join
    python -m forgeyard agent                                                      # later starts
    python -m forgeyard agent init [--server … --ca-fingerprint …]   # write /etc/forgeyard/agent.toml
    python -m forgeyard agent join --join-token fyj_...       # join with the config file's settings, then exit
    python -m forgeyard agent config                          # the settings in effect, and where each came from
    python -m forgeyard agent trust --server https://forge.lan:7860 --ca-fingerprint sha256:...
                                                   # an agent that joined over http: switch to https
    python -m forgeyard secrets reset --yes        # the secret key is lost: start over (see docs)
    python -m forgeyard secrets restore FILE [--data-dir DIR] [--force]
                                                   # put back a key backup downloaded from the console
    python -m forgeyard check-apps [--json]        # what the 1.5.3 upgrade removes (apps, /x/ key access), read only
    python -m forgeyard users reset-mfa NAME       # lost authenticator and recovery codes: remove the account's
                                                   # two-factor sign-in and end its sessions (audited, actor cli)
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
    from forgeyard.security import tls

    log = logging.getLogger("forgeyard")
    from pydantic import ValidationError

    from forgeyard.app import check_secret_key
    from forgeyard.config import get_settings
    from forgeyard.security.crypto import SecretKeyError

    try:
        get_settings()
    except ValidationError as e:
        for err in e.errors():
            where = ".".join(str(x) for x in err.get("loc") or ()).upper()
            log.error("Setting %s: %s", where or "?", err.get("msg", "").removeprefix("Value error, "))
        return 2
    try:
        check_secret_key()
    except SecretKeyError as e:
        log.error("%s", e)
        return 2
    from forgeyard.extensions import ExtensionError

    try:
        config = tls.uvicorn_config(create_app())
    except tls.TlsError as e:
        log.error("HTTPS: %s", e)
        return 2
    except ExtensionError as e:   # 1.6.1 (#190): one built for another version, or broken
        log.error("%s", e)
        return 2
    material = tls.live.material
    if material is not None and material.mode != "off":
        anchor = tls.trust_anchor_pem()
        log.info("HTTPS with %s.%s", "Forgeyard's own CA" if material.mode == "forgeyard-ca" else material.cert_file,
                 f" CA fingerprint {tls.fingerprint(anchor)}" if anchor else "")
    uvicorn.Server(config).run()
    return 0


def _labels(values: list[str]) -> dict:
    from forgeyard.agent.config import parse_labels

    try:
        return parse_labels(values)
    except ValueError as e:
        raise SystemExit(f"--label {e}")


def _settings(args, *, read: bool = True):
    """Merge the config file, the environment and the flags into `args` (see agent/config.py).
    None after logging the problems."""
    from forgeyard.agent import config

    try:
        return config.resolve(args, read=read)
    except config.ConfigError as e:
        logging.getLogger("forgeyard.agent").error("%s", e)
        return None


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
        driver = "rocm" if a["vendor"] == "amd" else "driver"  # AMD cards report the ROCm version
        print(f"{a['vendor']:<6} {a.get('index', 0):>2}  {a['model']}  arch={a.get('arch') or '?'}  {mem}"
              f"  {driver}={a.get('driver') or '-'}{flags}")
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
                                      log_max_bytes=log_max,
                                      pull_stall=60.0 * int(getattr(args, "pull_stall_minutes", None) or 5)),
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
        pulls = f"; image pulls through the {c['pulls']}" if c.get("pulls") else ""
        lines.append(f"container ok: {c.get('engine')} {c.get('version') or ''}; {gpu}{pulls}")
    else:
        lines.append(f"container unavailable: {c.get('reason')}")
    ports = info.get("ports") or {}
    if ports.get("low") is not None:
        lines.append(f"ports     {ports['low']}-{ports['high']} ({ports['high'] - ports['low'] + 1} host ports for services "
                     "and model replicas; two each behind the key guard)")
    return lines


def _runtimes_cmd(args) -> int:
    import tempfile

    from forgeyard.agent import state

    if _settings(args) is None:
        return 2
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


def _join(args, server: Optional[str], state_dir: Path) -> Optional[dict]:
    """Trade the join token for this machine's credential and save it (with the pinned CA)."""
    from forgeyard.agent import state, trust
    from forgeyard.agent.client import JoinError, join

    log = logging.getLogger("forgeyard.agent")
    if not server:
        log.error("A server address is needed to join: --server https://<server>:7860, or `server` in the config file.")
        return None
    try:
        ca_pem = trust.resolve(server, fingerprint=args.ca_fingerprint, ca_file=args.ca_file)
        joined = join(server, args.join_token, name=args.name,
                      ssl_context=trust.context(ca_pem) if server.startswith("https://") else None)
    except (trust.TrustError, JoinError) as e:
        log.error("%s", e)
        return None
    saved = {"server": server, "node_id": joined["node_id"], "name": joined["name"],
             "credential": joined["credential"], "ca_fingerprint": trust.fingerprint_of(ca_pem) if ca_pem else None}
    trust.save_ca(state_dir, ca_pem)
    path = state.save(state_dir, saved)
    log.info("Joined %s as %r; credential saved to %s (mode 0600).%s", server, joined["name"], path,
             f" Trusting only the CA {saved['ca_fingerprint']}." if ca_pem else "")
    return saved


def _pin_of(args) -> Optional[str]:
    """The fingerprint the settings pin (a file is read for its fingerprint), None without a pin."""
    from forgeyard.agent import trust

    if args.ca_fingerprint:
        return trust.normalise_fingerprint(args.ca_fingerprint)
    if args.ca_file:
        return trust.fingerprint_of(trust.read_ca_file(args.ca_file))
    return None


def _agent_cmd(args) -> int:
    from forgeyard.agent import state
    from forgeyard.agent.client import EXIT_JOIN_FAILED, EXIT_USAGE, Agent, normalise_server

    log = logging.getLogger("forgeyard.agent")
    settings = _settings(args)
    if settings is None:
        return EXIT_USAGE
    if args.verbose:  # -v, FORGEYARD_LOG_LEVEL=debug or log_level = "debug"
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("websockets").setLevel(logging.NOTSET)
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

    from forgeyard.agent import trust

    if args.join_token and (not saved or args.rejoin):
        saved = _join(args, server, state_dir)
        if saved is None:
            return EXIT_JOIN_FAILED
    elif not saved:
        log.error("This machine has not joined yet. Create a join token on the Machines page and run:\n"
                  "    forgeyard agent --server <url> --join-token <token> --ca-fingerprint <fingerprint>")
        return EXIT_USAGE
    else:
        # Joined before. The settings' server and CA were for joining; the state directory now
        # says which server and CA this machine trusts. A restart with the same settings (a
        # container's `--restart always`, a re-run install) must just start.
        if args.join_token:
            if settings.source("server") == "flag" and server != saved.get("server"):
                log.error("This machine already joined %s as %r (state in %s). To join %s as a new machine, "
                          "pass --rejoin.", saved.get("server"), saved.get("name"), state_dir, server)
                return EXIT_USAGE
            log.warning("This machine already joined %s as %r: the join token is not used (it expires by itself). "
                        "To join again as a new machine, pass --rejoin.", saved.get("server"), saved.get("name"))
        try:
            pin = _pin_of(args)
        except trust.TrustError as e:
            log.error("%s", e)
            return EXIT_USAGE
        if pin and pin != saved.get("ca_fingerprint"):
            source = settings.source("ca_fingerprint" if args.ca_fingerprint else "ca_file")
            if source == "flag":
                log.error("This machine already joined and trusts %s. To change which server or CA it trusts, run:\n"
                          "    forgeyard agent trust --server <https url> --ca-fingerprint <fingerprint>",
                          saved.get("ca_fingerprint") or "the system trust store")
                return EXIT_USAGE
            log.warning("The settings (%s) pin %s, but this machine joined trusting %s: keeping that. "
                        "`forgeyard agent trust` changes it.", source, pin,
                        saved.get("ca_fingerprint") or "the system trust store")
        if server != saved.get("server"):
            if settings.source("server") == "flag":
                log.info("Using --server %s (the credential was issued by %s).", server, saved.get("server"))
            else:
                log.warning("The settings (%s) say server %s, but this machine joined %s: using that. "
                            "`forgeyard agent trust --server …` changes it.", settings.source("server"), server,
                            saved.get("server"))
                server = saved.get("server")
    ca_pem = trust.load_ca(state_dir)
    if server.startswith("https://"):
        log.info("Verifying %s with %s.", server,
                 f"the pinned CA {trust.fingerprint_of(ca_pem)}" if ca_pem else "the system trust store")
    else:
        log.warning("Talking to %s over plain HTTP: the credential and workload secrets cross the network "
                    "unencrypted. Use https:// unless the server runs with TLS=off on a trusted network.", server)
    if settings.path is not None:
        log.info("Settings from %s.", settings.path)

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
            log.info("Models: not served here (set models.cache in the config file, or --model-cache DIR).")
        elif cache.get("ok"):
            log.info("Models: cache in %s, %.1f GiB free, %d cached.", args.model_cache,
                     (cache.get("free_bytes") or 0) / 1024**3, len(cache.get("cached") or []))
        else:
            log.error("Models: %s", cache.get("reason"))
    from forgeyard.agent import how_it_runs

    install = how_it_runs.for_agent(supervisor, credential=saved["credential"], server=server,
                                    config_path=str(settings.path) if settings.path else None, state_dir=str(state_dir))
    log.info("Runs as: %s%s.", install.get("method"), "" if install.get("known") else f" ({install.get('reason')})")
    agent = Agent(server, saved["credential"], inventory, labels=_labels(args.label), supervisor=supervisor,
                  ca_pem=ca_pem, install=install)

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


def _join_cmd(args) -> int:
    """`agent join`: join with the settings, save the credential and exit (install.sh, then the
    service starts the agent). Already joined: nothing to do, unless --rejoin."""
    from forgeyard.agent import state
    from forgeyard.agent.client import EXIT_JOIN_FAILED, EXIT_USAGE, normalise_server

    log = logging.getLogger("forgeyard.agent")
    if _settings(args) is None:
        return EXIT_USAGE
    state_dir = Path(args.state_dir) if args.state_dir else state.default_state_dir()
    try:
        saved = state.load(state_dir)
        server = normalise_server(args.server) if args.server else None
    except (ValueError, OSError) as e:
        log.error("%s", e)
        return EXIT_USAGE
    if saved and not args.rejoin:
        log.info("This machine already joined %s as %r (state in %s): keeping its credential. To join again as a "
                 "new machine, pass --rejoin.", saved.get("server"), saved.get("name"), state_dir)
        return 0
    if not args.join_token:
        log.error("A join token is needed: --join-token fyj_… (Machines → Add a machine), or FORGEYARD_JOIN_TOKEN.")
        return EXIT_USAGE
    return 0 if _join(args, server, state_dir) is not None else EXIT_JOIN_FAILED


def _trust_cmd(args) -> int:
    """Switch a joined agent to another address or CA (http -> https), keeping its credential."""
    from forgeyard.agent import state, trust
    from forgeyard.agent.client import EXIT_JOIN_FAILED, EXIT_USAGE, normalise_server

    log = logging.getLogger("forgeyard.agent")
    if _settings(args) is None:
        return EXIT_USAGE
    state_dir = Path(args.state_dir) if args.state_dir else state.default_state_dir()
    try:
        saved = state.load(state_dir)
    except (ValueError, OSError) as e:
        log.error("Cannot read the agent state in %s: %s", state_dir, e)
        return EXIT_USAGE
    if not saved:
        log.error("This machine has not joined yet (no credential in %s): use the join command instead.", state_dir)
        return EXIT_USAGE
    if not args.server:
        log.error("--server is required, e.g. --server https://forge.lan:7860")
        return EXIT_USAGE
    try:
        server = normalise_server(args.server)
        ca_pem = trust.resolve(server, fingerprint=args.ca_fingerprint, ca_file=args.ca_file)
    except (ValueError, trust.TrustError) as e:
        log.error("%s", e)
        return EXIT_JOIN_FAILED
    if server.startswith("http://"):
        log.warning("%s is plain HTTP: nothing is verified. Only for a server started with TLS=off.", server)
    before = saved.get("server")
    saved = {**saved, "server": server, "ca_fingerprint": trust.fingerprint_of(ca_pem) if ca_pem else None}
    trust.save_ca(state_dir, ca_pem)
    state.save(state_dir, saved)
    log.info("This machine (%r) now talks to %s%s; it was %s. Its credential is unchanged: restart the agent.",
             saved.get("name"), server, f", trusting only the CA {saved['ca_fingerprint']}" if ca_pem
             else (" with the system trust store" if server.startswith("https://") else ""), before)
    return 0


def _config_cmd(args) -> int:
    """`agent config`: the settings in effect and where each came from (exit 2 when they are wrong)."""
    from forgeyard.agent import config

    res = _settings(args)
    if res is None:
        return 2
    if args.get:
        if args.get not in config.BY_KEY:
            logging.getLogger("forgeyard.agent").error("No setting %r. Settings: %s", args.get, ", ".join(config.BY_KEY))
            return 2
        print(config.plain(res.values.get(args.get)))
        return 0
    print(config.describe(res))
    return 0


def _init_cmd(args) -> int:
    """`agent init`: write a config file from what is detected here, and print it."""
    from forgeyard.agent import config, setup

    log = logging.getLogger("forgeyard.agent")
    res = _settings(args, read=False)  # the flags and environment; the file is what we write
    if res is None:
        return 2
    path = Path(args.config) if args.config else config.DEFAULT_PATH
    values, notes, header = setup.detect(args, res)
    text = config.render(values, notes, header)
    try:
        config.read_text_check(text, path)
    except config.ConfigError as e:  # never write a file the agent would refuse
        log.error("%s", e)
        return 2
    if args.print_only:
        sys.stdout.write(text)
        return 0
    if path.exists() and not args.force:
        log.error("%s already exists. Pass --force to replace it, or --print to only see what would be written.", path)
        return 2
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o644)  # no secrets in it: the join token is never written
        tmp.replace(path)
    except OSError as e:
        log.error("Cannot write %s: %s", path, e.strerror or e)
        return 2
    sys.stdout.write(text)
    sys.stdout.flush()
    print(f"\nWrote {path}. Change it as you like, then restart the agent.", file=sys.stderr)
    return 0


def _read_passphrase(args) -> Optional[str]:
    """From --passphrase-file (its first line), else asked for without echo."""
    if args.passphrase_file:
        text = Path(args.passphrase_file).read_text(encoding="utf-8")
        return text.splitlines()[0] if text else ""
    import getpass

    try:
        return getpass.getpass("Backup passphrase: ")
    except (EOFError, KeyboardInterrupt):
        return None


def _restore_cmd(args) -> int:
    """`secrets restore FILE`: put back the secret key (and the CA) from a backup downloaded from the
    console. Run it with the server stopped. Overwrites nothing different unless --force, and then keeps
    the old files as <name>.replaced-<time>."""
    from forgeyard.config import get_settings
    from forgeyard.security import crypto, keybackup

    log = logging.getLogger("forgeyard")
    logging.getLogger("alembic").setLevel(logging.WARNING)  # imported along the way: its setup lines are noise here
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as e:
        log.error("Cannot read %s: %s", path, e.strerror or e)
        return 2
    try:
        header, _, _ = keybackup.read_header(data)
    except keybackup.BackupError as e:
        log.error("%s Nothing was written.", e)
        return 2
    passphrase = _read_passphrase(args)
    if not passphrase:
        log.error("No passphrase given. Nothing was written.")
        return 2
    try:
        contents = keybackup.open_backup(data, passphrase)
    except keybackup.BackupError as e:
        log.error("%s Nothing was written.", e)
        return 2
    if args.data_dir:
        data_dir = Path(args.data_dir)
        key_path = data_dir / "secret.key"
    else:
        settings = get_settings()
        data_dir, key_path = settings.data_dir, settings.secret_key_path
    try:
        plan = keybackup.plan(contents, key_path, data_dir / "tls", force=args.force)
    except keybackup.RestoreConflict as e:
        log.error("%s. Nothing was written. Is this the right data directory? To replace them (the old files are kept as "
                  "<name>.replaced-<time>), run it again with --force.", e)
        return 2
    for line in keybackup.apply(contents, plan):
        log.info("%s", line[0].upper() + line[1:] + ".")
    if contents.tls is None:
        log.info("The backup holds no certificate authority (the server it came from did not use Forgeyard's own CA).")
    if crypto.key_from_env() and not args.data_dir:
        log.warning("%s is set: the server uses it instead of %s. Remove it, or set it to the restored key.",
                    crypto.KEY_ENV, key_path)
    log.info("Restored key id %s (backup made %s). Start the server: it checks that this key belongs to the database.",
             contents.kid, header.get("created_at", "?"))
    return 0


def _secrets_cmd(args) -> int:
    from forgeyard.security import secrets_reset

    log = logging.getLogger("forgeyard")
    if args.secrets_command == "restore":
        return _restore_cmd(args)
    if args.secrets_command != "reset":
        log.error("Usage: python -m forgeyard secrets reset --yes | secrets restore FILE")
        return 2
    if not args.yes:
        log.error("This throws away every stored secret that cannot be read without the lost key: Hugging Face "
                  "tokens (enter them again), model replicas' keys (they restart with new ones) and workload "
                  "environment values (emptied). Stop the server first, then run it again with --yes.")
        return 2
    from forgeyard.security.crypto import SecretKeyError

    try:
        report = secrets_reset.reset()
    except (secrets_reset.NothingToReset, SecretKeyError) as e:
        log.error("%s", e)
        return 2
    log.info("New secret key %s in %s. %s", report["kid"], report["key_file"], report["summary"])
    return 0


def _users_cmd(args) -> int:
    """`users reset-mfa NAME`: the break-glass for someone (the owner too) who lost both the authenticator and the
    recovery codes, when no other admin can reset it in the console. Removes the account's authenticators, recovery
    codes and half-done sign-ins, ends its sessions and records `user.mfa_reset` with the actor `cli`. Shell access to
    the server host is what it trusts. It works with the server running (the same DATA_DIR / DATABASE_URL), needs no
    secret key, and touches nothing else: the person signs in with their password and adds an authenticator again."""
    import sqlalchemy as sa

    from forgeyard import audit
    from forgeyard.config import get_settings
    from forgeyard.db import User, get_engine, new_session
    from forgeyard.security import mfa, sessions

    log = logging.getLogger("forgeyard")
    if args.users_command != "reset-mfa":
        log.error("Usage: python -m forgeyard users reset-mfa USERNAME")
        return 2
    url = sa.engine.make_url(get_settings().db_url)
    if url.get_backend_name() == "sqlite" and not Path(url.database or "").is_file():
        log.error("There is no Forgeyard database at %s. Run this on the server host, with its DATA_DIR (or "
                  "DATABASE_URL).", url.database)
        return 2
    if not sa.inspect(get_engine()).has_table("mfa_factors"):
        log.error("This database has no two-factor sign-in yet (it was last used by a server older than 1.6.0).")
        return 2
    name = args.username.strip().lower()
    db = new_session()
    try:
        user = db.scalar(sa.select(User).where(User.username == name))
        if user is None:
            log.error("No account named %r.", name)
            return 2
        removed = mfa.remove_all(db, user.id)
        if not removed:
            log.info("%s has no two-factor sign-in. Nothing was changed.", user.username)
            return 0
        ended = sessions.revoke_all(db, user.id, "mfa_reset")
        audit.record(db, "user.mfa_reset", actor_name="cli", target=user.username, factors=removed,
                     sessions_ended=ended)
        db.commit()
    finally:
        db.close()
    log.info("Removed %d authenticator(s) and the recovery codes of %s, and ended %d session(s). They sign in with "
             "their password and can add an authenticator again in My account.", removed, name, ended)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forgeyard", description="Forgeyard server and agent.")
    parser.add_argument("--version", action="version", version=f"forgeyard {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (the default)")

    agent = sub.add_parser("agent", help="run the agent on a machine that should join a server",
                           description="Run the Forgeyard agent. Settings come from flags, then FORGEYARD_* "
                                       "environment variables, then the config file (/etc/forgeyard/agent.toml), "
                                       "then the defaults; see `forgeyard agent init`.")
    _agent_args(agent)
    agent.add_argument("--simulate-inventory", metavar="FIXTURE",
                       help="report a fixture instead of real hardware (development only; needs "
                            "FORGEYARD_AGENT_SIMULATION=1)")
    agent_sub = agent.add_subparsers(dest="agent_command")
    inv = agent_sub.add_parser("inventory", help="print what the agent detects, without joining")
    inv.add_argument("--json", action="store_true", help="machine-readable output")
    inv.add_argument("--raw", action="store_true", help="include what each tool returned (for bug reports)")
    inv.add_argument("--simulate-inventory", metavar="FIXTURE", help=argparse.SUPPRESS)
    rts = agent_sub.add_parser("runtimes", help="print which runtimes the agent can use here, without joining")
    rts.add_argument("--json", action="store_true", help="machine-readable output")
    _agent_args(rts, sub=True)
    init = agent_sub.add_parser("init", help="write a config file (default /etc/forgeyard/agent.toml) from what is "
                                             "detected on this machine, and print it")
    _agent_args(init, sub=True)
    init.add_argument("--print", dest="print_only", action="store_true", help="only print it, write nothing")
    init.add_argument("--force", action="store_true", help="replace an existing file")
    init.add_argument("--simulate-inventory", metavar="FIXTURE", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    jn = agent_sub.add_parser("join", help="join the server with the config file's settings and a join token, save "
                                           "the credential, and exit (already joined: nothing to do)")
    _agent_args(jn, sub=True)
    cfg = agent_sub.add_parser("config", help="print the settings in effect and where each came from (flag, "
                                              "environment, file, default)")
    _agent_args(cfg, sub=True)
    cfg.add_argument("--get", metavar="KEY", help="print only this setting's value, e.g. models.cache")
    tr = agent_sub.add_parser("trust", help="switch a joined machine to the server's https:// address and pin its "
                                            "CA, keeping the credential (agents that joined over http)")
    _agent_args(tr, sub=True)

    chk = sub.add_parser("check-apps", help="list what upgrading to 1.5.3 removes (apps, API keys' /x/ access), or "
                                            "removed, without changing anything (exit 1: something will be removed)")
    chk.add_argument("--json", action="store_true", help="machine-readable output")

    usr = sub.add_parser("users", help="accounts, from the server host")
    usr_sub = usr.add_subparsers(dest="users_command")
    rmfa = usr_sub.add_parser("reset-mfa", help="remove an account's two-factor sign-in (authenticators and recovery "
                                                "codes) and end its sessions, for someone who lost both; audited")
    rmfa.add_argument("username")

    sec = sub.add_parser("secrets", help="manage the key that encrypts stored secrets")
    sec_sub = sec.add_subparsers(dest="secrets_command")
    reset = sec_sub.add_parser("reset", help="the key is lost: make a new one and drop what it encrypted")
    reset.add_argument("--yes", action="store_true", help="really do it")
    restore = sec_sub.add_parser("restore", help="put back the secret key and the CA from a backup downloaded from the "
                                                 "console (asks for its passphrase; run it with the server stopped)")
    restore.add_argument("file", help="the backup file (forgeyard-key-backup-<key id>-<date>.fykey)")
    restore.add_argument("--data-dir", metavar="DIR",
                         help="the server's data directory (default: DATA_DIR, and FORGEYARD_SECRET_KEY_FILE for the key)")
    restore.add_argument("--force", action="store_true",
                         help="replace a different key or CA already there (the old files are kept as <name>.replaced-<time>)")
    restore.add_argument("--passphrase-file", metavar="PATH",
                         help="read the passphrase from this file's first line instead of asking (for scripts)")
    return parser


def _agent_args(p: argparse.ArgumentParser, *, sub: bool = False) -> None:
    """Every agent setting as a flag. Unset flags stay None so the environment and the config file
    can fill them (agent/config.py). On a subcommand they default to SUPPRESS, so the same option
    works before or after the subcommand name (argparse would otherwise reset it)."""

    def d(value=None):
        return argparse.SUPPRESS if sub else value

    p.add_argument("--config", default=d(), metavar="PATH",
                   help="the config file (default /etc/forgeyard/agent.toml when it exists; FORGEYARD_AGENT_CONFIG)")
    p.add_argument("--server", default=d(), help="the server's address, e.g. https://192.168.1.20:7860")
    p.add_argument("--join-token", default=d(), help="one-time token from the Machines page (first start only; "
                                                     "or FORGEYARD_JOIN_TOKEN)")
    p.add_argument("--state-dir", default=d(), help="where the machine credential is kept (default "
                                                    "/var/lib/forgeyard-agent as root, else ~/.local/state/forgeyard-agent)")
    p.add_argument("--name", default=d(), help="machine name shown on the server (default: the host name)")
    p.add_argument("--label", action="append", default=d(), metavar="KEY=VALUE", help="a label; repeatable")
    p.add_argument("--rejoin", action="store_true", default=d(), help="join again even though a credential is saved")
    p.add_argument("--ca-fingerprint", "--fingerprint", dest="ca_fingerprint", default=d(), metavar="SHA256",
                   help="pin the server's CA by its fingerprint (in the join command on the Machines page): only "
                        "certificates signed by that CA are accepted")
    p.add_argument("--ca-file", default=d(), metavar="PEM",
                   help="pin this CA certificate instead (your own CA, or a TLS proxy's)")
    p.add_argument("--workload-user", default=d(), metavar="USER",
                   help="unprivileged user that process workloads run as when the agent is root "
                        "(required then: without it, process workloads are refused)")
    p.add_argument("--work-dir", default=d(),
                   help="where each process replica gets its own directory (default: next to the state directory)")
    p.add_argument("--port-range", default=d(), metavar="LOW-HIGH",
                   help="host ports for services (default 30000-32767)")
    p.add_argument("--bind-address", default=d(),
                   help="address published ports listen on (default: this machine's address towards the server)")
    p.add_argument("--container-engine", default=d(),
                   help="auto (docker, then podman; the default), docker, podman, none, or a path")
    p.add_argument("--limits", choices=("auto", "systemd", "rlimit"), default=d(),
                   help="process CPU/memory limits: a systemd scope (root on systemd hosts) or rlimits (default auto)")
    p.add_argument("--log-max-mb", type=int, default=d(), help="output kept per replica, in MB (default 10)")
    p.add_argument("--pull-stall-minutes", type=int, default=d(), metavar="N",
                   help="an image pull fails when no new bytes arrive for N minutes (default 5); a slow pull that "
                        "keeps moving is never stopped")
    p.add_argument("--model-cache", default=d(), metavar="DIR",
                   help="serve models: where models and engines are downloaded and kept, shared by every replica "
                        "on this machine (containers get their model mounted read-only from here)")
    p.add_argument("--allow-private-model-hosts", action="store_true", default=d(),
                   help="allow model downloads from private addresses and plain http (a LAN mirror only)")
    p.add_argument("--model-local-dir", action="append", default=d(), metavar="DIR",
                   help="a directory local:// models may come from on this machine; repeatable")
    p.add_argument("--no-workloads", action="store_true", default=d(), help="only report hardware; run no workloads")
    p.add_argument("-v", "--verbose", action="store_true", default=d())


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] == ["agent", "agent"]:
        # The agent image's entrypoint is already `forgeyard agent`, so `docker run … IMAGE agent
        # inventory` arrives here as `agent agent inventory`. Accept it rather than fail.
        del argv[0]
    args = build_parser().parse_args(argv)
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)  # its debug lines only with -v (set later)
    if args.command == "agent":
        commands = {"inventory": _inventory_cmd, "runtimes": _runtimes_cmd, "trust": _trust_cmd, "init": _init_cmd,
                    "join": _join_cmd, "config": _config_cmd}
        return commands.get(args.agent_command, _agent_cmd)(args)
    if args.command == "secrets":
        return _secrets_cmd(args)
    if args.command == "users":
        return _users_cmd(args)
    if args.command == "check-apps":
        from forgeyard import apps_check

        return apps_check.run(as_json=args.json)
    return _serve()


if __name__ == "__main__":
    sys.exit(main())
