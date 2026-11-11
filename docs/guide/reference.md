# Reference

Every setting of the server and the agent, the command line, and where things are kept.

## Server settings

The server reads its settings from environment variables, or from a `.env` file in the folder it
starts in (`.env.example` lists them with comments). Restart the server after a change.

### Network and addresses

| Variable | Default | What it does |
|---|---|---|
| `HOST` | `127.0.0.1` | Where to listen. `0.0.0.0` opens it to the network |
| `PORT` | `7860` | The port, for the web UI, the API, `/v1` and the agents |
| `PUBLIC_URL` | — | The address people and agents use, e.g. `https://192.168.1.20:7860`. Join commands, set-password links and the gateway address use it; its host is always accepted and is in the certificate |
| `ALLOWED_HOSTS` | — | More host names to answer to, comma-separated. `localhost` and IP addresses always work; any other name gets 421 |
| `TRUSTED_PROXIES` | — | Proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed, comma-separated IPs |

### HTTPS

| Variable | Default | What it does |
|---|---|---|
| `TLS` | `on` | `on`: HTTPS with Forgeyard's own CA (made in `data/tls/`) or your certificate. `off`: plain HTTP, only behind a proxy that handles HTTPS |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | Your own certificate and key (both or neither); reloaded when they change |
| `TLS_CA_FILE` | — | With your own certificate: the CA that issued it. Agents pin it and **Trust this server** offers it |
| `TLS_EXTRA_NAMES` | — | More names or IP addresses for Forgeyard's own server certificate, comma-separated |

### Data and secrets

| Variable | Default | What it does |
|---|---|---|
| `DATA_DIR` | `data` | The database (SQLite), `secret.key`, `tls/` and the setup token |
| `DATABASE_URL` | — | Empty: SQLite in `DATA_DIR`. PostgreSQL: `postgresql+psycopg://user:password@host:5432/forgeyard` (after `uv sync --extra postgres`) |
| `SECRET_KEY_FILE` | `data/secret.key` | The key that encrypts stored secrets. Back it up apart from the database |
| `FORGEYARD_SECRET_KEY` | — | The key itself (base64 of 32 bytes), instead of a file |

### Accounts and sign-in

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | Create the owner on the first start, instead of the setup page |
| `FORGEYARD_SETUP_TOKEN` | — | Use this setup token instead of a random one |
| `REGISTRATION` | `off` | `approval`: people can ask for an account, which waits for an admin |
| `SESSION_IDLE_MINUTES` | `120` | Signed out after this long without a request |
| `SESSION_MAX_DAYS` | `7` | Signed out after this long in any case |
| `COOKIE_SECURE` | `auto` | `auto`: `Secure` cookies whenever the browser talks HTTPS; `true` or `false` to force |
| `RATE_LIMIT_PER_MINUTE` | `600` | API requests per minute per address (0: no limit) |
| `LOGIN_FREE_FAILURES` | `5` | Failed sign-ins per account before each further try waits (2 s, doubling, at most 5 min) |
| `LOGIN_IP_FAILURES` | `30` | Failed sign-ins per address within 15 minutes before that address waits |

### Machines

| Variable | Default | What it does |
|---|---|---|
| `AGENT_HEARTBEAT_SECONDS` | `10` | How often agents send a heartbeat |
| `AGENT_OFFLINE_SECONDS` | `30` | A machine with nothing heard for this long shows offline |

### Models

| Variable | Default | What it does |
|---|---|---|
| `HF_ENDPOINT` | `https://huggingface.co` | Where Hugging Face models are inspected and downloaded from (a mirror can go here) |
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | Allow model sources on private addresses and plain http (a LAN mirror; agents need `allow_private_hosts` too) |
| `MODEL_URL_HOSTS` | — | Hosts allowed for `https://` GGUF sources, comma-separated (admins change it on **Models**) |
| `MODEL_LOCAL_DIRS` | — | Directories allowed for `local://` models, comma-separated (admins change it on **Models**) |
| `GPU_CONTEXT_MIB` | `400` | GPU memory a replica's CUDA context takes on top of its own |
| `VRAM_HEADROOM_MIB` | `512` | Memory always left free on a card when placing replicas |

### Gateway

| Variable | Default | What it does |
|---|---|---|
| `GATEWAY_CONNECT_TIMEOUT` | `5` | Seconds to connect to a replica before trying the next one |
| `GATEWAY_READ_TIMEOUT` | `600` | Seconds a replica may take for its answer's headers or the next piece of a stream |
| `GATEWAY_MAX_BODY_MB` | `32` | The largest request body (an audio file for transcription is the big one) |
| `GATEWAY_DEFAULT_RPM`, `GATEWAY_DEFAULT_TPM` | `0` | Requests and tokens per minute for keys without their own limit (0: none). Admins change them on **API keys** |
| `GATEWAY_MAX_RPM`, `GATEWAY_MAX_TPM` | `0` | The most anyone may give their own key (0: no cap) |
| `USAGE_FLUSH_SECONDS` | `5` | How often usage counted in memory is written to the database |

### Cloud providers

| Variable | Default | What it does |
|---|---|---|
| `GATEWAY_CLOUD_DEFAULT` | `off` | The cloud setting a new API key gets: `off`, `fallback` or `on`. Admins change it on **API keys** (stored in the database); existing keys keep theirs |
| `GATEWAY_CLOUD_MAX` | `off` | The most anyone may give their own key. Admins may give any key anything |
| `EGRESS_PROXY` | empty | Send provider requests through this proxy (`http://host:port`). A proxy from the environment is never used |
| `EGRESS_ALLOWED_HOSTS` | empty | Only these provider hosts, comma-separated (`*.example.org` for subdomains). Empty: any |
| `EGRESS_CA_FILE` | empty | A CA file to trust for providers on top of the system's (a company proxy that inspects TLS) |
| `PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | Let providers outside your network use private or loopback addresses and plain http. For testing with a stand-in only; link-local and metadata addresses stay refused |
| `PROVIDER_CONNECT_TIMEOUT` | `10` | Seconds to connect to a provider before trying the next target |
| `PROVIDER_READ_TIMEOUT` | `300` | Seconds a provider may take for its answer's headers or the next piece of a stream (a provider can set its own) |
| `PROVIDER_COOLDOWN_SECONDS` | `30` | A provider target that answered 429 or 5xx, or could not be reached, is tried last for this long (longer if its `Retry-After` says so, up to 5 minutes) |
| `PROVIDER_RETRY_AFTER_CAP` | `2` | When no other target is left, a `Retry-After` up to this many seconds is waited for once |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | Where **Refresh prices from OpenRouter** reads prices (only when an admin asks; no key is sent). Change it only to test against a stand-in |

Request header `X-Forgeyard-Local-Only: 1`: never a cloud target for this request. Answer headers:
`X-Forgeyard-Target` (`local:<deployment>#<replica>` or `provider:<provider>/<model>`),
`X-Forgeyard-Spilled` (`unavailable`, `busy`, `refused` or `too_long`) when the request left the
first tier, and `X-Forgeyard-Replica` for a local answer, as before.

## Agent settings

The agent reads `/etc/forgeyard/agent.toml` when it exists (another file with `--config PATH` or
`FORGEYARD_AGENT_CONFIG`), then `FORGEYARD_*` environment variables, then flags. **A flag wins over
the environment, the environment over the file, the file over the default.** Labels are merged key
by key in the same order.

`forgeyard agent init` writes a complete file from what it finds on the machine, every setting
explained. `forgeyard agent config` shows what is in effect and where each value came from, and
refuses a file with a mistake, naming it (an unknown setting, with the closest correct name; a
wrong type; a bad value).

| Setting (`agent.toml`) | Environment | Flag | Default | What it does |
|---|---|---|---|---|
| `server` | `FORGEYARD_SERVER` | `--server` | — | The server's address, as the Machines page shows it. |
| `ca_fingerprint` | `FORGEYARD_CA_FINGERPRINT` | `--ca-fingerprint` | — | Pin the server's certificate authority by its SHA-256 fingerprint (from the join command). Only certificates it signed are accepted. |
| `ca_file` | `FORGEYARD_CA_FILE` | `--ca-file` | — | Or pin this CA file instead (a company CA, a TLS proxy's). Not both. |
| `name` | `FORGEYARD_NAME` | `--name` | — | The machine's name on the server. Default: the host name. |
| `state_dir` | `FORGEYARD_AGENT_STATE_DIR` | `--state-dir` | — | Where the machine's credential, the pinned CA and the replicas' records are kept (mode 0700). Default: /var/lib/forgeyard-agent as root, else ~/.local/state/forgeyard-agent. |
| `log_level` | `FORGEYARD_LOG_LEVEL` | `-v` | `info` | info, or debug for every message (-v). |
| `labels` | `FORGEYARD_LABELS` | `--label` | — | Labels shown on the Machines page, e.g. { rack = "a" }. Environment: rack=a,room=2. Merged key by key: a flag wins over the environment, the environment over the file. |
| `[workloads]` `enabled` | `FORGEYARD_WORKLOADS` | `--no-workloads` | `true` | false: only report hardware, run nothing. |
| `[workloads]` `user` | `FORGEYARD_WORKLOAD_USER` | `--workload-user` | — | The unprivileged account process workloads run as when the agent runs as root (required then). |
| `[workloads]` `work_dir` | `FORGEYARD_WORK_DIR` | `--work-dir` | — | One directory per process replica. Default: next to the state directory (<state_dir>-work). |
| `[workloads]` `port_range` | `FORGEYARD_PORT_RANGE` | `--port-range` | `30000-32767` | Host ports for services and model replicas. |
| `[workloads]` `bind_address` | `FORGEYARD_BIND_ADDRESS` | `--bind-address` | — | The address published ports listen on. Default: this machine's address towards the server. |
| `[workloads]` `container_engine` | `FORGEYARD_CONTAINER_ENGINE` | `--container-engine` | `auto` | auto (docker, then podman), docker, podman, none, or the engine CLI's path. |
| `[workloads]` `limits` | `FORGEYARD_LIMITS` | `--limits` | `auto` | How process workloads are limited: systemd (a scope with CPU and memory caps; root on a systemd host), rlimit (no systemd, e.g. in a container), or auto. |
| `[workloads]` `log_max_mb` | `FORGEYARD_LOG_MAX_MB` | `--log-max-mb` | `10` | Output kept per replica, in MB. |
| `[models]` `cache` | `FORGEYARD_MODEL_CACHE` | `--model-cache` | — | Where models and engines are downloaded and kept. Without it this machine serves no models. In the Docker image it must be the same path inside and outside the container. |
| `[models]` `local_dirs` | `FORGEYARD_MODEL_LOCAL_DIRS` | `--model-local-dir` | — | Directories local:// models may come from on this machine. Environment: comma-separated. |
| `[models]` `allow_private_hosts` | `FORGEYARD_ALLOW_PRIVATE_MODEL_HOSTS` | `--allow-private-model-hosts` | `false` | Allow model downloads from private addresses and plain http (a LAN mirror only). |

`server`, `ca_fingerprint`, `ca_file` and `name` are used to join. Once the machine has joined, its
state directory says which server it talks to and which CA it trusts: a different value in the file
or the environment is reported and ignored, and `forgeyard agent trust` changes it.

A join token is never written to the file. Give it with `--join-token` or `FORGEYARD_JOIN_TOKEN`;
it is used once. When the machine has already joined, a token is ignored (and the log says so), so
the same command or container can be started again.

An example file:

```toml
server = "https://192.168.1.20:7860"
ca_fingerprint = "sha256:3f9a…"
name = "gpu-01"
labels = { rack = "a", room = "lab" }

[workloads]
user = "forgeyard-run"
port_range = "30000-32767"
container_engine = "docker"
limits = "auto"

[models]
cache = "/srv/forgeyard-models"
local_dirs = ["/data/models"]
```

## Command line

### The server

| Command | What it does |
|---|---|
| `uv run python main.py` | Start the server (same as `python -m forgeyard serve`). Database changes run on start |
| `python -m forgeyard secrets reset --yes` | The secret key is lost: make a new one and drop what it encrypted (stop the server first) |
| `python -m forgeyard --version` | The version |

### The agent

Installed with the one-line install, the agent's command is `forgeyard` (in `/usr/local/bin`); from
a checkout it is `uv run --no-sync python -m forgeyard`.

| Command | What it does |
|---|---|
| `forgeyard agent` | Run the agent (the service runs `forgeyard agent --config /etc/forgeyard/agent.toml`). With a join token and no credential yet, it joins first |
| `forgeyard agent init [--print] [--force]` | Write `/etc/forgeyard/agent.toml` from what is detected (GPUs, Docker or Podman, systemd, a workload account), and print it. `--print` only prints. Flags given here (`--server`, `--ca-fingerprint`, `--model-cache`…) go into the file |
| `forgeyard agent join --join-token fyj_…` | Join with the file's settings, save the credential and exit. Already joined: nothing to do; `--rejoin` joins again as a new machine |
| `forgeyard agent config [--get KEY]` | Every setting in effect and where it came from; `--get models.cache` prints one value |
| `forgeyard agent trust --server https://… [--ca-fingerprint …]` | Switch a joined machine to another address or CA, keeping its credential |
| `forgeyard agent inventory [--json] [--raw]` | What the agent detects (GPUs, CPU, memory), without joining |
| `forgeyard agent runtimes` | What it can run here (processes, containers, NVIDIA in containers), without joining |

### The install script

`curl … https://<server>/install.sh | sudo sh -s -- OPTIONS`:

| Option | What it does |
|---|---|
| `--join fyj_…` | The join token (first install) |
| `--ca-fingerprint sha256:…` | The server CA's fingerprint: checked before anything is downloaded, then pinned by the agent |
| `--ca-file PEM` | Pin this CA file instead |
| `--server URL` | The server's address (default: the server the script came from) |
| `--name NAME`, `--label KEY=VALUE` | The machine's name and labels (first install) |
| `--model-cache DIR` | Where models go (default `/srv/forgeyard-models`) |
| `--workload-user USER` | The account process workloads run as (default `forgeyard-run`, created when missing) |
| `--no-start` | Install and join, but do not start the service |
| `--rejoin` | Join again as a new machine |
| `--uninstall [--purge]` | Remove the service and the program; `--purge` also deletes the settings and the credential |

Run again, it upgrades the agent to the server's version and keeps the settings and the credential.
It needs Linux on x86_64 or aarch64, root, `curl`, and access to PyPI for the agent's few
dependencies. It uses `uv` when it is installed, else a Python 3.13 or newer that is already there,
else it installs `uv` into `/opt/forgeyard/bin`, which brings its own Python. Without systemd it
installs everything and prints how to start the agent.

## Files and directories

On the server, in its folder:

| Path | What |
|---|---|
| `.env` | Settings |
| `data/forgeyard.db` | The database (SQLite) |
| `data/secret.key` | The key that encrypts stored secrets (0600) |
| `data/tls/ca.crt`, `ca.key` | Forgeyard's certificate authority (the key 0600) |
| `data/tls/server.crt`, `server.key` | The server certificate, renewed by itself |
| `data/secrets.json` | The one-time setup token, until setup is done |

On a machine installed with the one-line install:

| Path | What |
|---|---|
| `/etc/forgeyard/agent.toml` | The agent's settings |
| `/var/lib/forgeyard-agent/` | The credential (`credential.json`, 0600), the pinned CA (`server-ca.pem`), the replicas' records and logs |
| `/var/lib/forgeyard-agent-work/` | One working directory per process replica |
| `/srv/forgeyard-models/` | The model cache |
| `/opt/forgeyard/` | The agent program: its Python environment and the downloaded wheel |
| `/usr/local/bin/forgeyard` | The `forgeyard` command |
| `/etc/systemd/system/forgeyard-agent.service` | The service (`KillMode=process`, `Restart=always`) |

## Ports

| Port | Where | What |
|---|---|---|
| `7860` | server | Web UI, API, `/v1`, the agents' WebSocket, `/install.sh` (one port, HTTPS) |
| `30000–32767` | machines | Published ports of services and model replicas (the agent's `port_range`); the server must be able to reach them |

Agents need no inbound port of their own: they connect to the server.
