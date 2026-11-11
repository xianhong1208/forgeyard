# Reference

Every setting of the server and the agent, the command line, and where things are kept.

## Server settings

The server reads its settings from environment variables, or from a `.env` file in the folder it
starts in (`.env.example` lists them with comments, the common ones first). Restart the server
after a change.

The server checks its settings when it starts:

- **A value it cannot use stops the start** with the setting's name and the values it takes:
  `TLS`, `REGISTRATION`, `COOKIE_SECURE`, `GATEWAY_CLOUD_DEFAULT`, `GATEWAY_CLOUD_MAX`, a number
  that is not a number, and only one of `TLS_CERT_FILE` and `TLS_KEY_FILE`.
- **Anything surprising is logged as a warning** and shown to admins under
  [Setup health](#setup-health-checks): a name that is not a setting ("did you mean"), a setting
  that another one makes useless, and risky combinations.

### Common settings

Most installations set only some of these. Everything after this table is advanced: the defaults
are fine for almost everyone.

| Variable | Default | What it does |
|---|---|---|
| `HOST` | `127.0.0.1` | Where to listen. `0.0.0.0` opens it to the network, which a machine other than this one needs to join |
| `PORT` | `7860` | The port, for the web UI, the API, `/v1` and the agents |
| `PUBLIC_URL` | — | The address people and agents use, e.g. `https://192.168.1.20:7860`. Join commands, set-password links and the gateway address use it; its host is always accepted and is in the certificate. Empty: the address each browser opened Forgeyard with, which other machines cannot use when that was `127.0.0.1` |
| `DATA_DIR` | `data` | The database (SQLite), `secret.key`, `tls/` and the setup token. A relative path is relative to the folder the server starts in |
| `DATABASE_URL` | — | Empty: SQLite in `DATA_DIR`. PostgreSQL: `postgresql+psycopg://user:password@host:5432/forgeyard` (after `uv sync --extra postgres`) |
| `TLS` | `on` | `on`: HTTPS with Forgeyard's own CA (made in `data/tls/`) or your certificate. `off`: plain HTTP, only behind a proxy that handles HTTPS |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | Your own certificate and key: both or neither (one alone stops the start). Checked every 12 hours and reloaded when they changed. Ignored with `TLS=off` |
| `TLS_CA_FILE` | — | With your own certificate: the CA that issued it. Agents pin it and **Trust this server** offers it. Ignored without your own certificate |
| `REGISTRATION` | `off` | `approval`: people can ask for an account, which waits for an admin |
| `HF_ENDPOINT` | `https://huggingface.co` | Where Hugging Face models are inspected and downloaded from (a mirror can go here) |
| `EGRESS_PROXY` | — | Send provider requests through this proxy (`http://host:port`). A proxy from the environment is never used |

### Network and addresses

| Variable | Default | What it does |
|---|---|---|
| `ALLOWED_HOSTS` | — | More host names to answer to, comma-separated. `localhost` and IP addresses always work; any other name gets 421 |
| `TRUSTED_PROXIES` | — | Proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed, comma-separated IPs |

### HTTPS

| Variable | Default | What it does |
|---|---|---|
| `TLS_EXTRA_NAMES` | — | More names or IP addresses for Forgeyard's own server certificate, comma-separated. Ignored with your own certificate |

`TLS`, `TLS_CERT_FILE`, `TLS_KEY_FILE` and `TLS_CA_FILE` are under [Common settings](#common-settings).

### Data and secrets

| Variable | Default | What it does |
|---|---|---|
| `SECRET_KEY_FILE` | `data/secret.key` | The key that encrypts stored secrets. Back it up apart from the database |
| `FORGEYARD_SECRET_KEY` | — | The key itself (base64 of 32 bytes), instead of a file. Set one of the two: with both, this one is used and the file is ignored |

### Accounts and sign-in

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | Create the owner on the first start, instead of the setup page. Read only then: remove them afterwards |
| `FORGEYARD_SETUP_TOKEN` | — | Use this setup token instead of a random one (no use once setup is done) |
| `SESSION_IDLE_MINUTES` | `120` | Signed out after this long without a request |
| `SESSION_MAX_DAYS` | `7` | Signed out after this long in any case |
| `COOKIE_SECURE` | `auto` | `auto`: `Secure` cookies whenever the browser talks HTTPS; `true` or `false` to force. Any other value stops the start |
| `RATE_LIMIT_PER_MINUTE` | `600` | Requests per minute per address to the web UI's API, `/api` (0: no limit). The gateway, `/v1`, has per-key limits instead |
| `LOGIN_FREE_FAILURES` | `5` | Failed sign-ins per account before each further try waits (2 s, doubling, at most 5 min) |
| `LOGIN_IP_FAILURES` | `30` | Failed sign-ins per address within 15 minutes before that address waits |

### Machines

| Variable | Default | What it does |
|---|---|---|
| `AGENT_HEARTBEAT_SECONDS` | `10` | How often agents send a heartbeat |
| `AGENT_OFFLINE_SECONDS` | `30` | A machine with nothing heard for this long shows offline. Keep it at least 2.5 heartbeats long, or machines flap |

### Models

| Variable | Default | What it does |
|---|---|---|
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | Allow model sources on private addresses and plain http (a LAN mirror; agents need `allow_private_hosts` too) |
| `MODEL_URL_HOSTS` | — | Hosts allowed for `https://` GGUF sources, comma-separated. Only the first value: once an admin saves **Models → Model sources**, the database wins |
| `MODEL_LOCAL_DIRS` | — | Directories allowed for `local://` models, comma-separated. Only the first value, like `MODEL_URL_HOSTS` |
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
| `GATEWAY_CLOUD_MAX` | `off` | The most anyone may give their own key, also applied to existing keys on every request. Admins may give any key anything |
| `EGRESS_ALLOWED_HOSTS` | empty | Only these provider hosts, comma-separated (`*.example.org` for subdomains). Empty: any |
| `EGRESS_CA_FILE` | empty | A CA file to trust for providers on top of the system's (a company proxy that inspects TLS) |
| `PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | Let providers outside your network use private or loopback addresses and plain http. For testing with a stand-in only; link-local and metadata addresses stay refused |
| `PROVIDER_CONNECT_TIMEOUT` | `10` | Seconds to connect to a provider before trying the next target |
| `PROVIDER_READ_TIMEOUT` | `300` | Seconds a provider may take for its answer's headers or the next piece of a stream (a provider can set its own) |
| `PROVIDER_COOLDOWN_SECONDS` | `30` | A provider target that answered 429 or 5xx, or could not be reached, is tried last for this long (longer if its `Retry-After` says so, up to 5 minutes) |
| `PROVIDER_RETRY_AFTER_CAP` | `2` | When no other target is left, a `Retry-After` up to this many seconds is waited for once |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | Where **Refresh prices from OpenRouter** reads prices (only when an admin asks; no key is sent). Change it only to test against a stand-in |
| `BUDGET_DEFAULT_MAX_TOKENS` | `4096` | Budgets reserve a cloud request's estimated cost before it is sent. A request without `max_tokens` is estimated with an answer of this many tokens |

`EGRESS_PROXY` is under [Common settings](#common-settings). `GATEWAY_DEFAULT_*`, `GATEWAY_MAX_*`,
`GATEWAY_CLOUD_DEFAULT` and `GATEWAY_CLOUD_MAX` are only first values too: once an admin saves
**API keys → Limits for keys** or **Cloud for keys**, the database wins, and Setup health says so
when the environment still holds another value.

### Units, zero and empty

| Kind | How it is written |
|---|---|
| Durations | Seconds, unless the name says `MINUTES` or `DAYS`. A route's *Wait for a free local slot* is in milliseconds |
| Sizes | `_MIB` is mebibytes. `GATEWAY_MAX_BODY_MB` and the agent's `log_max_mb` are mebibytes too, despite the name. Sizes typed in forms take `8Gi` (GiB); `8G` and `8GB` are decimal (10⁹ bytes) |
| Money | US dollars in the web UI; micro-dollars (`_micros`) in the API; prices per million tokens |
| Rates | Per minute; `LOGIN_IP_FAILURES` counts within 15 minutes |

| Setting | `0` means | Empty means |
|---|---|---|
| `GATEWAY_DEFAULT_*`, `GATEWAY_MAX_*`, **Limits for keys** | No limit | No limit |
| A key's own requests or tokens per minute | Refused (at least 1) | The server's default |
| A budget (key, person, provider) | **$0: every cloud request it applies to is blocked** | No budget |
| `RATE_LIMIT_PER_MINUTE` | Off | — |
| A route's *Wait for a free local slot* | Spill at once | — |

The headers and error codes of `/v1` are listed under [The gateway (/v1)](#the-gateway-v1).

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

### Other variables the agent reads

These are not settings in `agent.toml`:

| Variable | What it does |
|---|---|
| `FORGEYARD_AGENT_CONFIG` | The settings file, instead of `/etc/forgeyard/agent.toml` (same as `--config PATH`) |
| `FORGEYARD_JOIN_TOKEN` | The one-time join token (same as `--join-token`) |
| `DOCKER_HOST`, `DOCKER_CONFIG`, `CONTAINER_HOST`, `XDG_RUNTIME_DIR` | Passed on to the `docker` or `podman` command, so a non-default engine socket or configuration works. Nothing else of the agent's environment reaches the engine |
| `ROCM_PATH`, `ROCM_HOME` | Where ROCm is installed, when it is not `/opt/rocm` (AMD cards) |
| `FORGEYARD_AGENT_SIMULATION` | `1` allows `--simulate-inventory FIXTURE`: the agent reports recorded hardware instead of its own. For development and tests only; the server marks such a machine as simulated |

## The gateway (/v1)

Programs call the models at `https://<server>:7860/v1` with an API key:
`Authorization: Bearer fyk_…`. The gateway takes no cookies and sends no CORS headers, so a web
page cannot use a signed-in browser against it.

### Endpoints

| Endpoint | What it serves | Cloud providers |
|---|---|---|
| `GET /v1/models` | The models this key may use. Leaves out a model that does not exist for the key (cloud-only, and the key's cloud setting is not **Allowed**), a model outside the key's model list, and a model whose deployments are all stopped (unless a provider the key may use can still serve it) | — |
| `GET /v1/models/{model}` | One model, in the same shape. 404 `model_not_found` for a model that is missing, hidden from this key or stopped; 403 `model_not_allowed` outside the key's model list | — |
| `POST /v1/chat/completions` | Chat models (vLLM, llama.cpp, provider chat models). `stream: true` streams server-sent events | yes |
| `POST /v1/completions` | The same models, with a plain `prompt` | yes |
| `POST /v1/embeddings` | Embedding models | yes |
| `POST /v1/rerank` | Rerankers (vLLM's rerank API) | no |
| `POST /v1/score` | Rerankers (vLLM's score API) | no |
| `POST /v1/audio/transcriptions` | Speech to text (faster-whisper), as `multipart/form-data` with a `model` field | no |
| `POST /v1/audio/translations` | Speech to English text, as above | no |

Any other path under `/v1` answers 404 `unknown_endpoint`.

What the gateway does with a request:

- It reads `model` from the body and sends the request to that model's route. A model served at
  another endpoint (an embedding model at `/v1/chat/completions`) answers 400 `wrong_endpoint`.
- It replaces `model` with the name the replica or the provider expects, adds the replica's internal
  key or the provider's key, and passes nothing else of yours on: not your API key, not your other
  headers.
- `max_tokens` (or `max_completion_tokens`) counts against the key's tokens per minute when the
  request arrives, and sets the most a request may cost under a budget.
- A streamed chat or completion gets a final usage chunk (`stream_options.include_usage`) so that
  tokens are counted. If you did not ask for it, the gateway removes it again.
- The body may be at most `GATEWAY_MAX_BODY_MB` (32 MB).
- If you close the connection, the gateway closes its request upstream at once, so the engine stops
  generating.

### Headers

| Header | Direction | Value |
|---|---|---|
| `Authorization` | request | `Bearer fyk_…`: your API key |
| `X-Forgeyard-Local-Only` | request | `1` (or `true`, `yes`, `on`): no cloud provider for this request. It never widens what the key may do |
| `X-Forgeyard-Target` | answer | Who answered: `local:<deployment>#<replica>` or `provider:<provider>/<model>`. Never a key |
| `X-Forgeyard-Replica` | answer | For a local answer: `<model>#<replica>` |
| `X-Forgeyard-Spilled` | answer | Why the request left the first tier: `unavailable`, `busy`, `refused`, `too_long` or `budget`. Missing when the first tier answered |
| `X-Forgeyard-Budget-Exhausted` | answer | The budgets that kept the request from a cloud provider, comma-separated: `key`, `person` or `provider:<name>`. Also on a successful answer that another target served |
| `Retry-After` | answer | Seconds to wait, on 429 `rate_limit_exceeded`, on 429 `provider_rate_limited` when the provider said, and on a replica's or provider's own 429 or 503 passed on as it was |

### Errors

Errors have OpenAI's shape, so every OpenAI client library reads them:

```json
{"error": {"message": "This API key was revoked.", "type": "authentication_error", "param": null, "code": "revoked_api_key"}}
```

`type` follows the status (`invalid_request_error`, `authentication_error`, `insufficient_quota`,
`permission_error`, `rate_limit_error`, `upstream_error`, `service_unavailable`). Handle errors by
`code`:

| Status | `code` | Means | What to do |
|---|---|---|---|
| 400 | `invalid_json` | The body is not JSON | Send JSON |
| 400 | `invalid_request` | No `model`, a body that is not an object, or audio not sent as `multipart/form-data` | Fix the request |
| 400 | `wrong_endpoint` | The model is served at another endpoint; the message names it | Use that endpoint |
| 400 | `context_length_exceeded` | Only on a route with **Spill a prompt that is too long**: the prompt does not fit the local context, and nothing else could take it | Shorten the prompt or lower `max_tokens` |
| 400 | `cloud_only_model` | Only a cloud provider serves the model, but the request has `X-Forgeyard-Local-Only` or the route is **Local only** | Drop the header, or use a local model |
| 401 | `missing_api_key` | No `Authorization: Bearer` header | Send the key |
| 401 | `invalid_api_key` | Not a Forgeyard key, or no such key | Check the key |
| 401 | `revoked_api_key` | The key was revoked | Create a new key |
| 401 | `expired_api_key` | The key is past its expiry | Create a new key |
| 401 | `account_disabled` | The key's owner is disabled or waiting for approval | Ask an admin |
| 402 | `budget_exhausted` | A monthly budget kept the request from every provider that could serve it, and nothing local could. The `budget` object says which and when it resets | Wait for the month to end, raise the budget, or use a local model. Do not retry |
| 402 | `budget_unpriced` | A budget applies, and the provider's model has no price, so its cost cannot be reserved | An admin sets its prices |
| 403 | `model_not_allowed` | The key is limited to other models | Use an allowed model, or change the key's models |
| 404 | `model_not_found` | No such model for this key: it does not exist, or it is cloud-only and the key's cloud setting is not **Allowed**. From `GET /v1/models/{model}`, also a stopped model | Check `GET /v1/models` |
| 404 | `unknown_endpoint` | The gateway has no such path | Use an endpoint above |
| 413 | `request_too_large` | The body is larger than `GATEWAY_MAX_BODY_MB` | Send less, or raise the setting |
| 421 | `host_not_allowed` | The server does not answer to the host name in the URL | Use its address, or add the name to `ALLOWED_HOSTS` |
| 429 | `rate_limit_exceeded` | Over the key's requests or tokens per minute | Wait `Retry-After` seconds |
| 429 | `provider_rate_limited` | Every cloud target that could serve it was rate limited | Wait `Retry-After` seconds, if given |
| 502 | `upstream_unreachable` | No replica could be connected to | Usually its agent or engine restarted; it recovers by itself. Check the deployment |
| 502 | `upstream_error` | The connection to the replica or provider broke while the answer was coming | Retry |
| 502 | `replica_refused_key` | The replica refused the gateway's internal key | Stop and start the deployment |
| 502 | `provider_auth_failed` | The provider refused Forgeyard's key for it. Never passed on as 401, which would make you doubt your own key | An admin fixes the provider's key |
| 502 | `provider_blocked` | The egress rules refused the provider's address before anything was sent | An admin checks the provider's base URL and `EGRESS_*` |
| 502 | `provider_redirect` | The provider answered with a redirect, which is never followed | An admin checks the provider's base URL |
| 502 | `provider_unreachable` | The provider could not be reached | Retry later |
| 502 | `provider_error` | The provider answered 5xx, or the request to it failed | Retry later |
| 503 | `no_ready_replica` | The model exists, but no replica is ready: downloading, starting, stopped, or its machine is offline. The message gives each replica's state | Wait, or start the deployment |
| 503 | `local_busy` | Every replica was at its *Concurrent requests* and nothing else could take the request. The gateway then sends it to a local replica anyway, where the engine queues it, so you should not normally see it | Retry |
| 503 | `no_target` | Every target of the route is disabled, or none serves this endpoint | An admin checks the route |
| 503 | `budget_unavailable` | This month's spend could not be read, so no cloud request is sent under a budget | Retry shortly |
| 504 | `upstream_timeout` | The replica gave no answer (or no next piece of a stream) within `GATEWAY_READ_TIMEOUT` | Lower `max_tokens`, or raise the setting |
| 504 | `provider_timeout` | The provider did not answer in time (`PROVIDER_READ_TIMEOUT`, or the provider's own timeout) | Retry later |

Two more cases:

- **An error from the engine or the provider itself** is passed on with its own status. A replica's
  answer is passed on unchanged (for example vLLM's 400 when a prompt is too long). A provider's
  other 4xx keeps its status and its `code`, with the provider's name before its message and anything that
  looks like a key removed. A replica's 429 or 503 is passed on when no other replica or tier could
  take the request.
- **A stream that breaks off** after it started cannot change its status any more. The gateway ends
  it with one last event, an error with the code `upstream_stream_broken`:
  `data: {"error": {…, "code": "upstream_stream_broken"}}`.

## Workload environment

A workload's replica starts with an empty environment. It gets only what its spec declares, plus:

| Variable | Value |
|---|---|
| `FORGEYARD_WORKLOAD` | The workload's name |
| `FORGEYARD_REPLICA` | The replica's id |
| `FORGEYARD_REPLICA_INDEX` | The replica's number, from 0 |
| `PORT`, `PORT_<NAME>` | For each declared port: a process gets the host port it must listen on; a container gets its own port (the engine maps the host port to it). `PORT` is the first one |
| `CUDA_VISIBLE_DEVICES`, `NVIDIA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER` | NVIDIA: the UUIDs of its cards only. A replica without GPUs sees none |
| `ROCR_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES` | AMD: its cards only |
| `PATH`, `HOME`, `LANG`, `USER`, `LOGNAME` | A process only: a basic `PATH`, its own directory as `HOME`, `LANG=C.UTF-8`, and the workload account |

A spec that sets a GPU variable (also `HIP_VISIBLE_DEVICES` and `GPU_DEVICE_ORDINAL`), `PORT`, or
any name that starts with `FORGEYARD_` or `PORT_` is refused. In a process's arguments and in
environment values, `${PORT}` and `${PORT_<NAME>}` become the port.

## Setup health checks

Admins see these under **Get started → Setup health**, as a count in the top bar (errors and
warnings) and as a banner on the page concerned. Users and operators see none of them. Every check
runs when the page asks, from the settings, the database and the agents' last reports. *Errors*
stay until fixed; an admin may snooze a *warning* or a *note* (`info` in the API) for 30 days, for themselves only. How
to use it: [Check the setup health](how-to.md#check-the-setup-health).

| Id | Severity | When |
|---|---|---|
| `setting-unknown` | warning | A name in `.env`, or one in the environment that starts like a setting (`GATEWAY_`, `PROVIDER_`, `EGRESS_`…) or is very close to one, is not a setting. It says which setting you probably meant |
| `tls-setting-ignored` | warning | `TLS_*` set with `TLS=off`; `TLS_CA_FILE` without your own certificate; `TLS_EXTRA_NAMES` with your own certificate |
| `secret-key-both` | warning | Both `FORGEYARD_SECRET_KEY` and `SECRET_KEY_FILE` are set |
| `heartbeat-flapping` | warning | `AGENT_OFFLINE_SECONDS` is under 2.5 × `AGENT_HEARTBEAT_SECONDS` |
| `private-hosts-allowed` | warning | `MODEL_ALLOW_PRIVATE_HOSTS` or `PROVIDER_ALLOW_PRIVATE_HOSTS` is on |
| `cookie-not-secure` | warning | `COOKIE_SECURE=false` while the server speaks HTTPS |
| `admin-password-left` | warning | `ADMIN_PASSWORD` is still set after the owner exists |
| `setup-token-left` | note | `FORGEYARD_SETUP_TOKEN` is still set after setup |
| `env-seed-ignored` | note | A first-value setting (`MODEL_URL_HOSTS`, `MODEL_LOCAL_DIRS`, `GATEWAY_DEFAULT_*`, `GATEWAY_MAX_*`, `GATEWAY_CLOUD_*`) differs from what an admin saved, which wins |
| `secret-key-backup` | warning | No admin has confirmed a backup of the current secret key. **I have a backup of key id …** ticks it off; it comes back when the key changes |
| `host-loopback` | error | `HOST` is a loopback address, but a machine joined from another address (and no proxy is in front) |
| `public-url-unset` | note | `PUBLIC_URL` is empty on a server other machines can reach. A warning when you opened the page as `127.0.0.1` or `localhost` |
| `certificate-name-missing` | error | Your own certificate does not name `PUBLIC_URL`'s host |
| `certificate-expiring` | warning | Your own certificate expires within 30 days, or Forgeyard's CA within 90. An error within 14 days |
| `registrations-pending` | note | People are waiting for approval |
| `agent-outdated` | warning | A machine's agent is older than the server. An error when its protocol is older |
| `gpus-shared-no-allowlist` | warning | Cards of a machine hold at least 1 GiB more than Forgeyard reserved there, and nobody chose its GPU access |
| `machine-no-models` | note | An online agent has no model cache, so no model is placed there. A warning when it has GPUs |
| `process-workloads-off` | note | An online agent refuses process workloads; the finding gives its reason |
| `model-cache-full` | warning | A machine's model cache disk has less than 10% free |
| `machine-simulated` | warning | A machine reports simulated hardware |
| `machine-offline-with-replicas` | warning | A machine has been offline for over 5 minutes with replicas placed on it |
| `replica-margin-low` | warning | A card with a running replica has less free than `VRAM_HEADROOM_MIB` (at least 256 MiB) |
| `deployment-waiting` | warning | A replica has waited for a machine for over 15 minutes |
| `extra-args-override` | warning | A deployment's extra engine arguments repeat a flag Forgeyard sets (memory, context, address, key) |
| `key-cloud-without-budget` | warning | A key may use the cloud (also through the server's default) and reaches a provider, but no budget applies: none on the key, its person or the provider |
| `cloud-model-unpriced` | error | A route's cloud model has no input or output price while any budget exists: its requests answer 402 `budget_unpriced` |
| `cloud-model-cost-unknown` | note | The same without budgets: its cost is unknown |
| `provider-no-key` | error | An enabled provider whose kind needs an API key has none |
| `provider-blocked-by-egress` | error | A provider's host is not in `EGRESS_ALLOWED_HOSTS` |
| `provider-inside-public-address` | warning | A provider marked as inside your network resolves only to public addresses |
| `cloud-default-open` | note | New keys get more than **Never**, or people may set their own keys to **Allowed**. A warning when a key also has no budget |
| `cloud-unreachable` | note | Routes have cloud targets, but no key may use the cloud |
| `route-cloud-before-local` | warning | A route's cloud tier comes before its first local tier |
| `route-cloud-never-used` | note | A **Local only** route has cloud targets, or a spill rule has no tier after the local one |
| `route-no-target` | error | No target of a route can answer: every one is disabled, or its provider is |
| `route-local-stopped` | warning | A route with local and cloud tiers whose local deployments are all stopped |
| `budget-zero` | note | A budget is $0, which blocks every cloud request it applies to |
| `prices-stale` | note | Prices from OpenRouter of a model a route uses are over 60 days old |
| `keys-idle-never-expire` | note | Keys that never expire were not used for 90 days |

## Command line

### The server

| Command | What it does |
|---|---|
| `uv run python main.py` | Start the server (same as `python -m forgeyard serve`, or `python -m forgeyard` alone). Database changes run on start |
| `python -m forgeyard secrets reset --yes` | The secret key is lost: make a new one and drop what it encrypted (stop the server first). Without `--yes` it only says what it would drop |
| `python -m forgeyard --version` | The version |

### The agent

Installed with the one-line install, the agent's command is `forgeyard` (in `/usr/local/bin`); from
a checkout it is `uv run --no-sync python -m forgeyard`. Every command takes the flags of the
[agent settings](#agent-settings), before or after the command's name, and `-v` (`--verbose`) for debug output.
`--fingerprint` is another name for `--ca-fingerprint`. In the agent's Docker image the command is
already `forgeyard agent`, so add only the rest (`… forgeyard-agent:<version> inventory`).

| Command | What it does |
|---|---|
| `forgeyard agent` | Run the agent (the service runs `forgeyard agent --config /etc/forgeyard/agent.toml`). With a join token and no credential yet, it joins first; `--rejoin` joins again as a new machine |
| `forgeyard agent init [--print] [--force]` | Write `/etc/forgeyard/agent.toml` from what is detected (GPUs, Docker or Podman, systemd, a workload account), and print it. `--print` only prints. Flags given here (`--server`, `--ca-fingerprint`, `--model-cache`…) go into the file |
| `forgeyard agent join --join-token fyj_…` | Join with the file's settings, save the credential and exit. Already joined: nothing to do; `--rejoin` joins again as a new machine |
| `forgeyard agent config [--get KEY]` | Every setting in effect and where it came from; `--get models.cache` prints one value |
| `forgeyard agent trust --server https://… [--ca-fingerprint …]` | Switch a joined machine to another address or CA, keeping its credential |
| `forgeyard agent inventory [--json] [--raw]` | What the agent detects (GPUs, CPU, memory), without joining |
| `forgeyard agent runtimes [--json]` | What it can run here (processes, containers, NVIDIA in containers), without joining |
| `forgeyard --version` | The agent's version |

### The install script

`curl … https://<server>/install.sh | sudo sh -s -- OPTIONS`:

| Option | What it does |
|---|---|
| `--join fyj_…` | The join token (first install). Also `--join-token` |
| `--ca-fingerprint sha256:…` | The server CA's fingerprint: checked before anything is downloaded, then pinned by the agent. Also `--fingerprint` |
| `--ca-file PEM` | Pin this CA file instead |
| `--server URL` | The server's address (default: the server the script came from) |
| `--name NAME`, `--label KEY=VALUE` | The machine's name and labels (first install) |
| `--model-cache DIR` | Where models go (default `/srv/forgeyard-models`) |
| `--workload-user USER` | The account process workloads run as (default `forgeyard-run`, created when missing) |
| `--no-start` | Install and join, but do not start the service |
| `--rejoin` | Join again as a new machine |
| `--uninstall [--purge]` | Remove the service and the program; `--purge` also deletes the settings and the credential |
| `-h`, `--help` | The options |

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
