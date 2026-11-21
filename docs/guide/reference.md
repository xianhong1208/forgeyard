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
  that another one makes useless, risky combinations, and a
  [setting removed in 1.4](#removed-settings) that is still set, named with what replaces it. The
  one exception: a removed `SECRET_KEY_FILE` that would now mean another key file stops the start.

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
| `FORGEYARD_SECRET_KEY_FILE` | `data/secret.key` | The key that encrypts stored secrets (Hugging Face tokens, provider keys, replica keys, app environment values). Back it up apart from the database |
| `FORGEYARD_SECRET_KEY` | — | The key itself (base64 of 32 bytes), instead of a file. Set one of the two: with both, this one is used and the file is ignored |

### Accounts and sign-in

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | Create the owner on the first start, instead of the setup page. Read only then: remove them afterwards |
| `SESSION_IDLE_MINUTES` | `120` | Signed out after this long without a request |
| `SESSION_MAX_DAYS` | `7` | Signed out after this long in any case |
| `COOKIE_SECURE` | `auto` | `auto`: `Secure` cookies whenever the browser talks HTTPS; `true` or `false` to force. Any other value stops the start |
| `RATE_LIMIT_PER_MINUTE` | `600` | Requests per minute per address to the web UI's API, `/api` (0: no limit). The gateway, `/v1`, has per-key limits instead |
| `LOGIN_FREE_FAILURES` | `5` | Failed sign-ins per account before each further try waits (2 s, doubling, at most 5 min) |
| `LOGIN_IP_FAILURES` | `30` | Failed sign-ins per address within 15 minutes before that address waits |

### Machines

| Variable | Default | What it does |
|---|---|---|
| `AGENT_OFFLINE_SECONDS` | `30` | A machine with nothing heard for this long shows offline. Agents send a heartbeat every third of it (at least every 2 seconds) |
| `IMAGE_UPLOAD_MAX_GB` | `64` | The largest image file an admin may upload to a machine from its page (**Engine images** → **Upload an image**), in GiB. The file is streamed through the server to the agent, never stored |
| `AGENT_IMAGE` | — | The agent image in **Add a machine** → **Docker**. Empty: the published `docker.io/tonyhuang1101/forgeyard-agent` at this server's version, or a local build while none is published. Set a mirror or a private registry (`registry.example.org/forgeyard/forgeyard-agent`; the tag is still the server's version), or add `:TAG` or `@sha256:DIGEST` to pin one. Only a plain image reference is accepted |

### Models

| Variable | Default | What it does |
|---|---|---|
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | Allow model sources on private addresses and plain http (a LAN mirror; agents need `allow_private_hosts` too) |
| `MODEL_URL_HOSTS` | — | Hosts allowed for `https://` GGUF sources, comma-separated. Only the first value: once an admin saves **Models → Sources**, the database wins. Until then the panel marks it *from .env* |
| `MODEL_LOCAL_DIRS` | — | Directories allowed for `local://` models, comma-separated. Only the first value, like `MODEL_URL_HOSTS` |
| `GPU_CONTEXT_MIB` | `400` | GPU memory a replica's CUDA context takes on top of its own |
| `VRAM_HEADROOM_MIB` | `512` | Memory always left free on a card when placing replicas |

### Gateway

| Variable | Default | What it does |
|---|---|---|
| `GATEWAY_CONNECT_TIMEOUT` | `5` | Seconds to connect to a replica before trying the next one |
| `GATEWAY_READ_TIMEOUT` | `600` | Seconds a replica may take for a non-streamed answer's headers, or for each piece of a stream after its first (a stream's first bytes: the deployment's *Wait for the first answer*, 120 s by default, see [Retries and the wait for a first answer](#retries-and-the-wait-for-a-first-answer)) |
| `GATEWAY_MAX_BODY_MB` | `32` | The largest request body (an audio file for transcription is the big one). A deployment whose recipe has the *Largest request* parameter (`max_body_mb`, 64 by default for vLLM and llama.cpp) takes up to that instead |
| `GATEWAY_DEFAULT_RPM`, `GATEWAY_DEFAULT_TPM` | `0` | Requests and tokens per minute for keys without their own limit (0: none). Admins change them on **API keys** |
| `GATEWAY_MAX_RPM`, `GATEWAY_MAX_TPM` | `0` | The most anyone may give their own key (0: no cap) |

### Media in chat

| Variable | Default | What it does |
|---|---|---|
| `MEDIA_FETCH_MAX_MB` | `20` | The most the gateway downloads for one media URL (an `https://` image in a chat request) before it refuses it (413 `media_too_large`) |
| `MEDIA_ALLOW_PRIVATE_HOSTS` | `false` | Let media URLs point at private or loopback addresses (an image server on your LAN). Link-local and metadata addresses stay refused |

Media URLs also follow `EGRESS_PROXY`, `EGRESS_ALLOWED_HOSTS` and `EGRESS_CA_FILE` below. See
[Images, audio and video in chat](#images-audio-and-video-in-chat).

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
| `BUDGET_DEFAULT_MAX_TOKENS` | `4096` | Budgets reserve a cloud request's estimated cost before it is sent. A request without `max_tokens` is estimated with an answer of this many tokens |

`EGRESS_PROXY` is under [Common settings](#common-settings). `GATEWAY_DEFAULT_*`, `GATEWAY_MAX_*`,
`GATEWAY_CLOUD_DEFAULT` and `GATEWAY_CLOUD_MAX` are only first values too: once an admin saves
**API keys → Limits for keys** or **Cloud policy**, the database wins, and Setup health says so
when the environment still holds another value. Until an admin saves, those panels mark the values
that came from the environment as *from .env*.

### Removed settings

**Removed in 1.4.** These settings were deprecated in 1.3.1 and are no longer read. A server that
still has one set, in the environment or in `.env`, logs a warning at start that it is ignored and
what to use instead, and Setup health lists it (`setting-removed`) until you remove it.

- `SECRET_KEY_FILE`: use `FORGEYARD_SECRET_KEY_FILE`, the same value under its new name. If
  `SECRET_KEY_FILE` names a key file other than `data/secret.key` and neither
  `FORGEYARD_SECRET_KEY_FILE` nor `FORGEYARD_SECRET_KEY` is set, **the server refuses to start**
  instead of using (or making) a different key, and says to rename it. Nothing is changed until
  you do; `secrets reset` refuses too.
- `AGENT_HEARTBEAT_SECONDS`: agents send a heartbeat every third of `AGENT_OFFLINE_SECONDS`, at
  least every 2 seconds (10 seconds by default).
- `FORGEYARD_SETUP_TOKEN`: the server prints a random setup token (and keeps it in
  `data/secrets.json` until setup). For an unattended first start use `ADMIN_USERNAME` and
  `ADMIN_PASSWORD`.
- `USAGE_FLUSH_SECONDS`: usage counted in memory is written to the database every 5 seconds.
- `PROVIDER_COOLDOWN_SECONDS`: a provider target that answered 429 or 5xx, or could not be reached,
  is tried last for 30 seconds (longer if its `Retry-After` says so, up to 5 minutes).
- `PROVIDER_RETRY_AFTER_CAP`: when no other target is left, a `Retry-After` of up to 2 seconds is
  waited for once.
- `OPENROUTER_MODELS_URL`: **Refresh prices from OpenRouter** always reads OpenRouter's public list
  (only when an admin asks; no key is sent).

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
| `[workloads]` `user` | `FORGEYARD_WORKLOAD_USER` | `--workload-user` | `forgeyard-run` if it exists (as root) | The unprivileged account processes (apps and engines) run as when the agent runs as root. Default: forgeyard-run if that account exists (never created by the agent); without one, a root agent refuses process apps. |
| `[workloads]` `work_dir` | `FORGEYARD_WORK_DIR` | `--work-dir` | — | One directory per process replica. Default: next to the state directory (<state_dir>-work). |
| `[workloads]` `port_range` | `FORGEYARD_PORT_RANGE` | `--port-range` | `30000-32767` | Host ports for services and model replicas. |
| `[workloads]` `bind_address` | `FORGEYARD_BIND_ADDRESS` | `--bind-address` | — | The address published ports listen on. Default: this machine's address towards the server. |
| `[workloads]` `container_engine` | `FORGEYARD_CONTAINER_ENGINE` | `--container-engine` | `auto` | auto (docker, then podman), docker, podman, none, or the engine CLI's path. |
| `[workloads]` `limits` | `FORGEYARD_LIMITS` | `--limits` | `auto` | How processes are limited: systemd (a scope with CPU and memory caps; root on a systemd host), rlimit (no systemd, e.g. in a container), or auto. |
| `[workloads]` `log_max_mb` | `FORGEYARD_LOG_MAX_MB` | `--log-max-mb` | `10` | Output kept per replica, in MB. |
| `[workloads]` `pull_stall_minutes` | `FORGEYARD_PULL_STALL_MINUTES` | `--pull-stall-minutes` | `5` | An image pull fails when no new bytes arrive for this many minutes (three times as long while it unpacks). A slow pull that keeps moving is never stopped. |
| `[models]` `cache` | `FORGEYARD_MODEL_CACHE` | `--model-cache` | `/srv/forgeyard-models` (as root) | Where models and engines are downloaded and kept. Default: /srv/forgeyard-models as root, else ~/.local/share/forgeyard-models. Empty (""): this machine serves no models. In the Docker image it must be the same path inside and outside the container. |
| `[models]` `local_dirs` | `FORGEYARD_MODEL_LOCAL_DIRS` | `--model-local-dir` | — | Directories local:// models may come from on this machine. Environment: comma-separated. |
| `[models]` `allow_private_hosts` | `FORGEYARD_ALLOW_PRIVATE_MODEL_HOSTS` | `--allow-private-model-hosts` | `false` | Allow model downloads from private addresses and plain http (a LAN mirror only). |

**Two defaults depend on the machine** and match what `install.sh` sets up: the model cache is
`/srv/forgeyard-models` when the agent runs as root (`~/.local/share/forgeyard-models` otherwise),
and processes run as `forgeyard-run` when the agent runs as root and that account exists.
The agent never creates the account; `install.sh` and the agent image do. `forgeyard agent config`
shows these as `(default)`. To keep a machine from serving models, set the cache to an empty value
(`--model-cache ""`, `FORGEYARD_MODEL_CACHE=` or `cache = ""`).

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
| `POST /v1/audio/speech` | Text to speech (1.3): OpenAI's request, the audio back as it is made. See [Text to speech](#text-to-speech) | no |
| `GET /v1/audio/voices?model=<model>` | A speech model's voices, its default voice, the formats it makes, how it streams and its input cap | — |
| `POST /v1/audio/voices`, `DELETE /v1/audio/voices/{name}` | Uploading or deleting a voice (voice cloning): always 400 `voice_cloning_not_supported` in 1.3 | — |
| `POST /v1/images/generations` | Image generation (1.3): OpenAI's request, images back as base64 (`b64_json`) only. See [Images](#images) | no |
| `POST /v1/images/edits`, `POST /v1/images/variations` | Image edits and variations: not offered in 1.3, always 404 `endpoint_not_supported` | — |
| `POST /v1/videos` | Video generation (1.3): asks for a video, answers at once with the job. JSON, `multipart/form-data` (what OpenAI's SDK sends) or a URL-encoded form. See [Video](#video) | no |
| `GET /v1/videos` | The key's own videos, newest first: `limit` (1 to 100, 20), `after` (the last id seen), `order` (`desc` or `asc`) | — |
| `GET /v1/videos/{id}` | One video job: its status and progress | — |
| `GET /v1/videos/{id}/content` | The finished video, `video/mp4`, streamed from the replica that made it. `variant=video` only; `Range` requests answer 206 | — |
| `DELETE /v1/videos/{id}` | Cancels a job at work, or deletes a finished video, on its replica | — |
| `POST /v1/videos/sync`, `POST /v1/videos/{id}/remix` | Not offered: always 404 `endpoint_not_supported` | — |
| `POST /v1/vision/detect` | Object detection (1.4): one image, boxes, labels and scores back; a zero-shot detector (1.4.4) is told what to find with `labels`. See [Vision](#vision) | no |
| `POST /v1/vision/segment` | Segmentation with SAM 2 (1.4): one image and a point or a box, masks as PNGs back | no |
| `POST /v1/vision/depth` | Depth estimation (1.4): one image, a 16-bit depth map back | no |
| `POST /v1/vision/classify` | Image classification (1.4.4): one image, the best classes and their scores back; a zero-shot classifier (CLIP, SigLIP) is given the `labels` to choose from | no |
| `POST /v1/classify` | Text classification (1.4.4): texts, each one's label and probabilities back. See [Text classification](#text-classification) | no |

Any other path under `/v1` answers 404 `unknown_endpoint`.

What the gateway does with a request:

- It reads `model` from the body and sends the request to that model's route. A model served at
  another endpoint (an embedding model at `/v1/chat/completions`) answers 400 `wrong_endpoint`.
- It replaces `model` with the name the replica or the provider expects, adds the replica's internal
  key or the provider's key, and passes nothing else of yours on: not your API key, not your other
  headers.
- `max_tokens` (or `max_completion_tokens`) counts against the key's tokens per minute when the
  request arrives, and sets the most a request may cost under a budget. For text to speech, each
  character of `input` counts as one token instead (see [Text to speech](#text-to-speech)); an image
  request counts no tokens (see [Images](#images)).
- A streamed chat or completion gets a final usage chunk (`stream_options.include_usage`) so that
  tokens are counted. If you did not ask for it, the gateway removes it again.
- The body may be at most `GATEWAY_MAX_BODY_MB` (32 MB), or the model's own *Largest request*
  (`max_body_mb`, 64 MB by default for vLLM and llama.cpp deployments).
- If you close the connection, the gateway closes its request upstream at once, so the engine stops
  generating.

### Retries and the wait for a first answer

- **A request runs at most once.** The gateway tries another replica (or provider) only when the
  request provably never reached the first one: the connection could not be made (nothing listens,
  refused, `GATEWAY_CONNECT_TIMEOUT`), no connection was free, or the replica's key guard answered
  that its engine is not running (503 with `X-Forgeyard-Not-Taken`, from agents of 1.4.5 on). A
  connection that breaks once the request was sent, while it was being written or while the answer
  was awaited, is 502 `upstream_error` (`provider_error` for a provider) and is not tried anywhere
  else: the engine may have started on it, and a generation or a paid cloud request would run twice.
  Retry it yourself if that is safe. An agent older than 1.4.5 closes the connection when its engine
  is not running, which now reads as such a break: update agents to keep the failover in the few
  seconds before an agent reports a dead engine.
- **The first answer of a stream.** Once a streamed request (`stream: true`, or speech with a
  `stream_format`) was written to a replica, the replica has the deployment's *Wait for the first
  answer* (`first_answer_timeout`, **120 s** by default, 5 to 3,600) for the status line and the
  stream's first bytes. After that the request is closed, so the engine stops, and the answer is 504
  `first_answer_timeout`; it is never sent to another replica. Once a stream has started, each later
  piece may take up to `GATEWAY_READ_TIMEOUT` (600 s); a wait set above that is honoured for the first
  bytes. **A non-streamed answer is the whole answer, so the wait does not apply to it**: it keeps
  `GATEWAY_READ_TIMEOUT` (504 `upstream_timeout`), and long non-streamed generations and long
  transcriptions are not cut short. Image requests keep their deployment's own *Timeout*; video jobs
  their own; `first_answer_timeout` is refused for those deployments. It is under **Advanced** in the
  deploy and change forms; changing it restarts nothing.

### Images, audio and video in chat

`POST /v1/chat/completions` takes OpenAI's content parts. What the gateway accepts and what it sends
on:

| Part | Accepted as | Sent to the engine as |
|---|---|---|
| Text | `text`, `input_text`, `refusal` | unchanged |
| Image | `image_url` (`{"url": …, "detail"}` or a bare string), `input_image` | `image_url` with a `data:` URI |
| Audio | `input_audio` (`{"data": <base64>, "format"}`, or a `data:audio/…` URI), `audio_url` | `input_audio` with base64 `data` |
| Video | `video_url`, `input_video` | `video_url` with a `data:` URI |
| File (PDF) | `file` with `file_data` as a `data:` URI | unchanged, only to a recipe that takes files (no built-in one does) |

- A media URL may be a `data:` URI (base64, of the part's kind) or an `http(s)` URL. **The gateway
  fetches URLs itself** and sends the engine a `data:` URI: engines never fetch anything. It follows
  the egress rules: public addresses only (private ones with `MEDIA_ALLOW_PRIVATE_HOSTS`; link-local
  and metadata addresses never), at most 3 redirects, each checked again, `MEDIA_FETCH_MAX_MB` per
  item, 20 seconds per item, and the answer must be of the part's kind (`image/…` for an image).
  `file://` and every other scheme answer 400 `media_url_refused`.
- The request goes only to a deployment that takes what it carries: its model takes that medium
  (read from the model's `config.json`, or its projector file for GGUF), its recipe does, and it is
  within the deployment's *Images per request* (images and audio clips, 4 by default) and *Videos
  per request* (0 by default: video is off until enabled). Otherwise 400 `media_not_supported` or
  `too_many_media_items`.
- **Requests with media never go to a cloud provider** (nor to a provider inside your network), in
  1.3. A route served only by providers answers 400 `media_local_only`; on a mixed route they never
  spill.
- Usage counts the media items (`media_items` in `GET /api/usage`). Image tokens are in
  `prompt_tokens` as the engine reports them.

The deployment parameters, under **Advanced** in the deploy form (recipes `vllm` and `llama-cpp`):

| Parameter | Default | What it does |
|---|---|---|
| `images` (*Images per request*) | `4` | The most images, and audio clips, one request may carry; 0 turns them off. Passed to vLLM in `--limit-mm-per-prompt` |
| `videos` (*Videos per request*) | `0` | Video is off until this is 1 or more. On vLLM it reserves memory for a long video at startup |
| `video_fps` (*Video frames per second*, `llama-cpp` only) | `1` | Frames read from each second of a video (`--video-fps`), for models without a rate of their own; each frame costs about as much as an image. Qwen3-VL reads 2 frames a second whatever this says (llama.cpp b12242 and 1.0.0): a 6-second clip took 5,345 tokens on Qwen3-VL 2B. Give video deployments a context of 8,192 or more |
| `max_body_mb` (*Largest request*) | `64` | The largest request the gateway sends this deployment, in MiB (413 above) |
| `image_tokens` (*Largest image*) | `1024` | Each image is scaled down to at most this many tokens: about 1024 × 1024 pixels on Qwen3-VL. On `llama-cpp` through `--image-max-tokens`; on `vllm` through `--mm-processor-kwargs` `max_pixels`, for models whose image tokens grow with the resolution (Qwen-VL; Gemma takes a fixed number per image). Without it a 5 MB photo became about 4,000 tokens on llama.cpp (125 s on 8 CPU threads) and about 16,000 on vLLM, more than an 8,192-token context. A model added before this setting existed is not capped on vLLM until it is added again |

A recipe without these parameters gets 4 images or audio clips, no video and `GATEWAY_MAX_BODY_MB`.

### Text to speech

`POST /v1/audio/speech` takes OpenAI's request and answers with the audio itself (not JSON), passed
through as the engine makes it. Three engines serve it (recipes `vllm-omni-tts`, `llama-cpp` with a
TTS GGUF, `speaches-tts`); local deployments only, never a cloud provider.

| Field | What the gateway does |
|---|---|
| `model` | The speech model's name, as usual |
| `input` | Required, at most the deployment's *Longest input* (`max_input_chars`; 0 means a quarter of the context length for vLLM-Omni and llama.cpp, 1,024 at their default, and 4,096 for speaches). Longer: 400 `input_too_long` |
| `voice` | One of the model's voices (`GET /v1/audio/voices`), as a name or `{"id": …}`. OpenAI's names (`alloy`, `nova`, …) that the model does not have, or no voice, get the recipe's default voice or the first the engine lists; the answer names it in `X-Forgeyard-Voice` (`default` when the model has no voices of its own and speaks with its one voice) |
| `response_format` | `mp3`, `opus`, `aac`, `flac`, `wav` or `pcm`, of those the engine makes; others 400 `unsupported_response_format`. Left out: the engine's default (vLLM-Omni and llama.cpp `wav`, speaches `mp3`), not OpenAI's `mp3` |
| `stream_format` | `audio`: raw audio as it is made (a streamed WAV declares an unknown size). `sse`: OpenAI's `speech.audio.delta` / `speech.audio.done` events (vLLM-Omni only). Left out: the whole file. An engine that cannot: 400 `stream_format_not_supported`. `stream: true` means `stream_format: audio` |
| `speed` | 0.25 to 4, passed on (llama.cpp takes only 1.0, speaches 0.5 to 2 for Kokoro) |
| `instructions` | Passed on: vLLM-Omni Qwen3-TTS uses it (style and emotion), the others ignore it |
| `language`, `lang`, `sample_rate`, `seed`, … | Passed on. For llama.cpp the gateway sets `lang` (`zh`, `ja`, `ko`) from the text's script and `n_predict` (ten audio frames a character, at least 512, at most the context) when you leave them out |

**Voice cloning is not available in 1.3.** A reference recording (`ref_audio`, `ref_text`,
`speaker_ref_b64`, `speaker_embedding`, …), a voice given as audio data or a link, Qwen3-TTS's
`task_type: "Base"`, and uploading or deleting voices answer 400 `voice_cloning_not_supported`.
Nothing of it reaches an engine.

What is counted, per request that was answered (in `GET /api/usage` and on **Usage**):

| Unit | How |
|---|---|
| `characters` | The input's characters (Unicode code points), counted before sending |
| `media_ms` | The audio's length in milliseconds, read as it passes: `wav` from its header (or the bytes after it, when streamed), `pcm` from its size and the sample rate the answer states (`x-audio-sample-rate`, or `rate=` in its type; else the voice's own), `flac` from its STREAMINFO, `mp3` and `aac` by counting frames, `opus` from the last Ogg page; SSE from the size of the `pcm` deltas. Nothing for other answers |
| tokens | Only what the engine reports: vLLM-Omni's `x-vllm-omni-input-tokens` and `-output-tokens`, or the usage of `speech.audio.done` |

**Limits:** rpm as for every request; **tpm counts one token per character of `input`**, held when
the request arrives (a 15-character request needs 15 tokens of room). Budgets apply to cloud spend
only, and speech never goes to the cloud. A key's model list applies to speech and to the voice list.

`GET /v1/audio/voices?model=<model>` answers
`{"object": "list", "model", "voices": [{"id", "name", "language"?, "gender"?}], "default", "formats",
"default_format", "stream_formats", "max_input_chars"}`, asked of a ready replica and kept for 5
minutes. Voices uploaded to an engine are never listed.

### Images

`POST /v1/images/generations` takes OpenAI's request and answers OpenAI's JSON with the images as
base64. Two engines serve it (recipes `vllm-omni-image` for diffusers pipelines on NVIDIA,
`sd-cpp` for stable-diffusion.cpp on AMD with Vulkan, NVIDIA and the CPU); local deployments only,
never a cloud provider.

| Field | What the gateway does |
|---|---|
| `model` | The image model's name, as usual |
| `prompt` | Required, at most the deployment's *Longest prompt* (`max_prompt_chars`, 4,000). Longer: 400 `prompt_too_long`. Engine settings inside it (`<sd_cpp_extra_args>`) are refused: use the fields below |
| `n` | 1 to the deployment's *Images per request* (`max_images`, 4 by default); more: 400 `too_many_images`. Left out: 1 |
| `size` | `WIDTHxHEIGHT`, or `auto` or left out for the deployment's *Default size* (the model's own: 512x512 for Stable Diffusion 1.5, 1024x1024 for SDXL, Z-Image and FLUX). Each side at most *Largest side* (`max_side`, 0 = the model's own); larger: 400 `image_too_large`. Each side a multiple of 16 (diffusion transformers) or 64 (Stable Diffusion's UNet); else 400 `invalid_size` |
| `response_format` | Left out or `b64_json`. **`url` is refused** with 400 `response_format_not_supported`: Forgeyard keeps no copy of the images it makes, so there is no URL to give. Decode `data[].b64_json` |
| `output_format` | `png` (the default), `jpeg` or `webp`; others 400 `unsupported_output_format`. `output_compression` (0 to 100) for jpeg and webp |
| `num_inference_steps` | Denoising steps, at most the deployment's *Most steps* (`max_steps`, 50); more: 400 `too_many_steps`. Left out: the deployment's *Default steps*, else the model's own |
| `negative_prompt`, `guidance_scale`, `seed` | Passed on: as fields to vLLM-Omni, inside the prompt as stable-diffusion.cpp's `<sd_cpp_extra_args>` (built by the gateway). Without `seed` every request is a new image |
| `true_cfg_scale` | vLLM-Omni only (Qwen-Image); stable-diffusion.cpp ignores it |
| `quality`, `style`, `background`, `moderation`, `user` | Accepted and ignored |
| `stream` | Refused (400 `streaming_not_supported`): the answer comes when the images are made |

Nothing else reaches the engine: fields an engine would take but Forgeyard does not offer (vLLM-Omni's
`lora` with a path on the server, its `extra_params` and system prompts) are left out.

The answer: `{"created", "data": [{"b64_json"}], "output_format", "size"}`. The engines' own extras
(stable-diffusion.cpp's `generation_info` with the prompt and settings, vLLM-Omni's stage timings) are
not passed on. An engine's error comes back in OpenAI's shape with its status and the code
`engine_error`.

**The queue.** A deployment works on *Requests at once* (`concurrency`, 1) per replica; the others wait
in the gateway, first come first served, at most *Queue length* (`queue`, 8) of them. One more gets 429
`queue_full` with `Retry-After` (from how long its requests took lately). A request that waits longer
than the deployment's *Timeout* gets 503 `queue_timeout`; once a replica has it, the gateway waits up
to that *Timeout* (`timeout`: 600 s for vLLM-Omni, 1,800 s for stable-diffusion.cpp) for the images,
then 504 `upstream_timeout`. A client that gives up while waiting leaves the queue; its request never
reaches the engine.

**No prompt filtering.** The gateway checks the request's shape and the deployment's caps, not what the
prompt asks for. If a deployment needs a safety filter, put one in front of it.

What is counted, per request that was answered (in `GET /api/usage` and on **Usage**):

| Unit | How |
|---|---|
| `images` | The images in the answer |
| `kilopixels` | Their width x height / 1,000, read from each image's own header (PNG, JPEG, WebP), else the size asked |
| `busy_ms` | The time the replica worked on the request (from sending it to the last byte of the answer), times the GPUs the deployment holds (1 on a CPU): GPU time |
| tokens | None. A key's tokens per minute does not apply; its requests per minute does |

### Video

`POST /v1/videos` takes OpenAI's Videos request and answers at once with a **job**; the video is made on
one replica, which keeps it. Poll `GET /v1/videos/{id}` until `status` is `completed` or `failed`, then
fetch `GET /v1/videos/{id}/content`. One engine serves it (recipe `vllm-omni-video`, vLLM-Omni on
NVIDIA); local deployments only, never a cloud provider.

| Field | What the gateway does |
|---|---|
| `model` | The video model's name, as usual |
| `prompt` | Required, at most the deployment's *Longest prompt* (`max_prompt_chars`, 2,000). Longer: 400 `prompt_too_long` |
| `seconds` | A whole number of seconds, as a number or a string (`"5"`), 1 to the deployment's *Longest video* (`max_seconds`, 5); more: 400 `video_too_long`. Left out: *Default length* (`seconds`, 5). Frames are seconds x the frame rate, rounded to the counts the model makes (Wan: 4k + 1, so 5 s at 16 fps is 81 frames) |
| `size` | `WIDTHxHEIGHT`, or left out for *Default size* (the model's own: 832x480 for Wan2.1, 1280x704 for Wan2.2). Each side at most *Largest side* (`max_side`, 0 = the model's own long side), and no more pixels a frame than the default size scaled to that side; larger: 400 `video_too_large`. Sides are multiples of 16 (32 for Wan2.2-5B); else 400 `invalid_size` |
| `num_inference_steps` | Denoising steps, at most *Most steps* (`max_steps`, 50); more: 400 `too_many_steps`. Left out: *Default steps*, else the model's own |
| `negative_prompt`, `guidance_scale`, `seed` | Passed on |
| `input_reference` (and vLLM-Omni's `image_reference`, `video_reference`, `audio_reference`) | Refused with 400 `input_reference_not_supported`: text to video only in 1.3 |
| `quality`, `user` | Accepted and ignored |

Nothing else reaches the engine: the gateway sends only the prompt, the model, the size, the frames,
the frame rate and the fields above (never vLLM-Omni's `lora`, `extra_params` or frame interpolation
paths).

**The job** has OpenAI's shape: `{"id", "object": "video", "model", "status", "progress", "created_at",
"completed_at", "expires_at", "size", "seconds", "quality", "remixed_from_video_id", "error"}`.

- `id` is Forgeyard's own (`video_` and 32 random hex digits), never the engine's. Another key's id
  answers 404 `video_not_found`, exactly as an id that does not exist.
- `status` is `queued`, `in_progress`, `completed` or `failed`. `progress` is what the engine reports:
  vLLM-Omni says 0 until the video is made, then 100.
- `seconds` is a string, as in OpenAI's API. `expires_at` is set once the job ends: the deployment's
  *Keep results* later.
- `error` (`{"code", "message"}`) once `failed`:

| `error.code` | Means |
|---|---|
| `generation_failed` | The engine failed the video; its message follows |
| `timed_out` | Not done within the deployment's *Longest job* (`timeout`); the gateway cancelled it on the replica |
| `replica_restarted` | The replica's engine restarted: its jobs (kept in its memory) went with it |
| `replica_gone` | The replica was stopped, placed again or deleted |
| `video_lost` | The replica no longer knows the job (its engine restarted) |

The prompt is not stored. A job's row stays a week after its result expired, then goes.

**Content.** `GET /v1/videos/{id}/content` streams the MP4 from the replica that made it, as
`video/mp4` with `Content-Disposition: inline`. `variant=thumbnail` and `variant=spritesheet` answer 400
`variant_not_supported` (the engine makes the video only). A job not made yet answers 409
`video_not_ready`, a failed one 409 `video_failed`, an expired one 410 `video_expired`, and one whose
replica restarted or was stopped since 410 `video_lost`.

**Deleting.** `DELETE /v1/videos/{id}` cancels a job at work on its replica, or deletes a finished
video there, and removes the job: `{"id", "object": "video.deleted", "deleted": true}`.

**Jobs at once.** A replica makes one video at a time; the others wait on it. The gateway sends a job
to the ready replica with the fewest jobs. A deployment takes at most *Jobs at once* (`queue`, 8)
waiting or being made, and of those *Jobs per key* (`jobs_per_key`, 2) from one key (or one person in
the Playground). One more gets 429 `queue_full` or `too_many_jobs` with `Retry-After` (from how long
its videos took lately). The key's requests per minute count each job asked for; polls, lists and
downloads do not.

**Expiry.** A result can be fetched for *Keep results* (`keep_minutes`, 1,440); then the server deletes it
on the replica. The engine also deletes its own files an hour after that (`VLLM_OMNI_SERVER_STORAGE__FILE_TTL`),
and keeps them inside the replica's container, so a stopped or restarted replica leaves none behind.

The deployment parameters (recipe `vllm-omni-video`):

| Parameter | Default | What it does |
|---|---|---|
| `default_size` (*Default size*) | `auto` | The size of requests that give none; `auto` is the model's own |
| `seconds` (*Default length*) | `5` | The length of requests that give none |
| `max_seconds` (*Longest video*) | `5` | Longer requests: 400. The memory estimate is sized for it |
| `max_side` (*Largest side*, Advanced) | `0` | The widest or tallest video, in pixels; 0 is the model's own |
| `fps` (*Frames per second*, Advanced) | `0` | 0 is the model's own (16 for Wan2.1, 24 for Wan2.2) |
| `steps`, `max_steps` (Advanced) | `0`, `50` | Default steps (0: the model's own) and the most a request may ask |
| `queue` (*Jobs at once*, Advanced) | `8` | Jobs waiting or being made on the deployment |
| `jobs_per_key` (*Jobs per key*, Advanced) | `2` | Of those, one key's or one person's |
| `keep_minutes` (*Keep results*, Advanced) | `1440` | How long a finished video can be fetched |
| `timeout` (*Longest job*, Advanced) | `3600` | Seconds from asking; a job still at work then is cancelled and failed |
| `max_prompt_chars` (*Longest prompt*, Advanced) | `2000` | Longer prompts: 400 |
| `offload` (*Offload to host memory*, Advanced) | `none` | `model`: only the component at work on the card (`--enable-cpu-offload`); slower |

What is counted, **once per job, when it ends** (in `GET /api/usage` and on **Usage**):

| Unit | How |
|---|---|
| requests | One per job. A finished one counts as answered; a failed one as an error and nothing else; one deleted while at work as 499 (not an error). A refused request (400, 404, 429) is counted when it is refused |
| `media_ms` | The video's length in milliseconds, as the engine reports it (else frames / frames per second) |
| `busy_ms` | From when the job was seen in progress until it was done, times the GPUs the deployment holds: GPU time |
| tokens | None |

The Playground's **Video** panel uses `/api/playground/videos` (`POST`, `GET`), `GET
/api/playground/videos/options?model=<model>`, `GET` and `DELETE /api/playground/videos/{id}` and `GET
/api/playground/videos/{id}/content`: the same rules, as the signed-in person (the session cookie and
the console's `X-Forgeyard: 1` header), for that person's own jobs, counted as console. The jobs are kept
in the server's database (table `video_jobs`, migration `0014`).

### Vision

`POST /v1/vision/detect`, `/v1/vision/segment`, `/v1/vision/depth` (1.4) and `/v1/vision/classify` (1.4.4,
image classification, task `image-classify`) take one image for a
vision model and answer JSON. They are Forgeyard's own API (the OpenAI SDK has none): any HTTP client
works. One engine serves them, Forgeyard's own vision server (recipe `vision`, the image
`forgeyard-vision` pinned by digest: the CUDA build on NVIDIA, the CPU build elsewhere), with the
model from the machine's model cache. Local deployments only, never a cloud provider.

The request is `multipart/form-data` with `model`, the image as a file field `image` and the options
as fields, or JSON with `model`, `image` (base64, or a `data:` URI) and the options. PNG, JPEG or WebP,
at most 20 MiB and 40 megapixels. The gateway checks that there is a model and an image, changes only
`model`, and passes the rest to the replica as it came; a model of another task answers 400
`wrong_endpoint` (the message names its endpoint).

| Endpoint | Options | Answer |
|---|---|---|
| `detect` | `threshold` (0 to 1; 0.5, for a zero-shot detector 0.3, OWL 0.1), `max_detections` (1 to 1,000, 100); `labels` (1.4.4): what a zero-shot detector is to find, a list of names or a comma-separated string, at most 32 of at most 64 characters | `detections`: `[{"label", "score", "box": [x0, y0, x1, y1]}]`, best first, in the image's pixels; a zero-shot detector's `label` is one of the names sent, as sent |
| `segment` | `points` (`[[x, y], …]`, at most 16) with `labels` (1 inside, 0 outside; all 1 when left out), or `box` (`[x0, y0, x1, y1]`); `multimask` (`true`: three candidate masks) | `masks`: `[{"score", "area", "box", "png"}]`, best first; each mask a 1-bit PNG of the image's size, base64 |
| `depth` | none | `depth_png` (a 16-bit grey PNG of the image's size, base64, nearer is brighter), `min`, `max`, `unit` (relative depth, not metres) |
| `classify` | `top_k` (1 to 1,000; 5, a zero-shot classifier all its labels); `labels` (zero-shot classifiers only, and required there: the labels to choose from, as for `detect`); `template` (zero-shot: `This is a photo of {}.`, one `{}` where the label goes, at most 200 characters) | `labels`: `[{"label", "score"}]`, best first; `multi_label` (`true`: each score stands alone, a sigmoid, as SigLIP's and multi-label classifiers'; `false`: the scores share 1) |

Every answer also has `model`, `width`, `height` (the image's) and `ms` (the model's time). The vision
server's errors (`{"error": {"message", "code"}}`) come back with their status: 400 `invalid_image`,
`invalid_option`, `missing_prompt` (segment without a point or a box), `labels_required` (a zero-shot
detector without `labels`), `labels_not_supported` (`labels` for a detector or classifier with fixed classes; 1.4.4 for both), 413
`image_too_large`.

**Zero-shot detectors (1.4.4, #142)** (Grounding DINO, MM-Grounding-DINO and LLMDet, OWL-ViT, OWLv2) find
what each request names instead of a fixed list of classes: the catalog marks them
*Finds what you name* (`vision.zero_shot`). Send plain names, `labels: ["cat", "remote control"]` (or a form
field `labels=cat, remote control`); repeats and empty names are dropped. The vision server asks each family
its own way (Grounding DINO one lower-case prompt, "cat. remote control.", and each box takes the name whose
words score it highest; OWL one query per name), so a name comes back as it was sent. Their scores run lower
than a fixed detector's: start at 0.3 (Grounding DINO) or 0.1 to 0.2 (OWLv2). They need a vision image from
1.4.4 on, which the built-in recipe runs (it pins `forgeyard-vision` 1.4.4rc1); on a custom recipe that names an
older one, deploying one answers 422 `vision_image_too_old` (a local build is taken as it is).

What is counted, per request that was answered (in `GET /api/usage` and on **Usage**):

| Unit | How |
|---|---|
| `images` | 1: the image looked at |
| `kilopixels` | Its width x height / 1,000, from the answer |
| `busy_ms` | The time the replica worked on it |
| tokens | None. A key's tokens per minute does not apply; its requests per minute and its model list do |

A replica works on one image at a time and keeps the others waiting in order; more throughput is more
replicas. Which models are vision models, and the licence rule, are under [Built-in recipes](#built-in-recipes).

### Text classification

`POST /v1/classify` (1.4.4) takes texts for a text classification model and answers in vLLM's shape. It is
Forgeyard's own API (the OpenAI SDK has none). The engine is vLLM's pooling runner (recipe `vllm-pooling`);
the gateway sends the request to the replica's `/classify`. Local deployments only, never a cloud provider.

| Field | What |
|---|---|
| `model` | The model's name |
| `input` | A text, or a list of 1 to 2,048 texts; none of them empty. Token ids are refused |
| `truncate_prompt_tokens` | Optional: cut each text to this many tokens (at least 1), or -1 for the model's maximum. Without it, a text longer than the deployment's context is refused by the engine |
| `use_activation` | Optional, `true` or `false`: `false` gives the raw scores (logits) instead of probabilities |

The gateway sends only these fields (any other is dropped); `messages` (vLLM's chat form) answers 400. A
model of another kind answers 400 `wrong_endpoint` naming its endpoint (for a chat model, also how it
classifies with structured outputs).

The answer: `data` holds one entry per text, in order: `index`, `label` (the most likely), `probs` (every
label's probability, in the order of the model's `config.json`; a `multi_label_classification` model
scores each on its own, so they do not add up to 1) and `num_classes`; and `usage` with `prompt_tokens`.
The label names are on the catalog entry (`GET /api/models/<name>`, `classify.label_names`).

| Unit | How |
|---|---|
| tokens | `prompt_tokens` from the engine's answer, as for embeddings. A key's tokens per minute applies |

### Headers

| Header | Direction | Value |
|---|---|---|
| `Authorization` | request | `Bearer fyk_…`: your API key |
| `X-Forgeyard-Local-Only` | request | `1` (or `true`, `yes`, `on`): no cloud provider for this request. It never widens what the key may do |
| `X-Forgeyard-Target` | answer | Who answered: `local:<deployment>#<replica>` or `provider:<provider>/<model>`. Never a key |
| `X-Forgeyard-Replica` | answer | For a local answer: `<model>#<replica>` |
| `X-Forgeyard-Spilled` | answer | Why the request left the first tier: `unavailable`, `busy`, `refused`, `too_long` or `budget`. Missing when the first tier answered |
| `X-Forgeyard-Voice` | answer | Text to speech: the voice the gateway chose because the request named none, or an OpenAI voice the model does not have; `default` when the model has only its own voice |
| `X-Forgeyard-Dropped-Params` | answer | The request parameters Forgeyard left out on the way to the provider that answered (engine-only ones such as `top_k`, `chat_template_kwargs`; a model's own drop list), comma-separated. Also on that provider's error. Missing when nothing was left out ([structured output](cloud-providers.md#when-a-request-spills) is never left out: such a request skips that provider) |
| `X-Forgeyard-Budget-Exhausted` | answer | The budgets that kept the request from a cloud provider, comma-separated: `key`, `person` or `provider:<name>`. Also on a successful answer that another target served |
| `Retry-After` | answer | Seconds to wait, on 429 `rate_limit_exceeded`, on 429 `provider_rate_limited` when the provider said, on 503 `local_busy` (1), and on a replica's or provider's own 429 or 503 passed on as it was |

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
| 400 | `invalid_request` | No `model`, a body that is not an object, or audio not sent as `multipart/form-data`; for speech, no `input`, a `speed` outside 0.25 to 4, another `stream_format`, or `GET /v1/audio/voices` without `model` | Fix the request |
| 400 | `wrong_endpoint` | The model is served at another endpoint; the message names it | Use that endpoint |
| 400 | `context_length_exceeded` | Only on a route with **Spill a prompt that is too long**: the prompt does not fit the local context, and nothing else could take it | Shorten the prompt or lower `max_tokens` |
| 400 | `invalid_content_part` | A content part that is not an object with a `type`, a type the gateway does not take (`*_embeds`, unknown ones), or a media part without its URL | Use the parts listed under [Images, audio and video in chat](#images-audio-and-video-in-chat) |
| 400 | `invalid_media_data` | A `data:` URI that is not base64, or not of the part's kind (an image part with `data:text/plain`), or a `file` part without a `data:` URI | Send `data:<type>;base64,…` |
| 400 | `media_url_refused` | A media URL with another scheme than http(s) or `data:` (`file://`…), credentials, or a host that resolves to a private, loopback, link-local or metadata address, also after a redirect; a URL in `input_audio.data` | Send a public URL or a `data:` URI |
| 400 | `media_fetch_failed` | The gateway could not fetch a media URL: no answer, a status other than 200, more than 3 redirects, more than 20 seconds, or not of the part's kind | Check the URL, or send the file as a `data:` URI |
| 400 | `media_not_supported` | The model does not take that medium (images to a text model, video while *Videos per request* is 0); the message says what it takes | Use a model that takes it, or enable video on the deployment |
| 400 | `too_many_media_items` | More images (or audio clips, or videos) than the deployment's *Images per request* or *Videos per request* | Send fewer, or raise the parameter |
| 400 | `file_input_not_supported` | A `file` part (PDF) to a model whose recipe does not take files; the gateway does not convert files | Send the pages as images, or the text |
| 400 | `media_local_only` | A request with media to a model served only by providers | Use a local model |
| 400 | `input_too_long` | Text to speech: `input` is longer than the deployment's *Longest input* | Split the text, or raise `max_input_chars` |
| 400 | `unsupported_response_format` | Text to speech: a `response_format` that is not OpenAI's, or that the engine does not make; the message lists what it makes | Ask for one of those |
| 400 | `stream_format_not_supported` | Text to speech: `stream_format` the engine does not offer (`sse` on llama.cpp or speaches) | Leave it out, or use `audio` |
| 400 | `language_not_supported` | Text to speech: Chinese, Japanese or Korean text to speaches, which deletes those characters | Use a Qwen3-TTS deployment |
| 400 | `voice_cloning_not_supported` | Text to speech: a reference recording, a voice given as audio or a link, `task_type: "Base"`, or uploading a voice. Not in 1.3 | Use one of the model's voices |
| 400 | `response_format_not_supported` | Images: `response_format: url`. Images come back as base64 only | Leave it out, or ask for `b64_json` |
| 400 | `streaming_not_supported` | Images and vision: `stream: true` | Leave it out |
| 400 | `missing_image` | Vision: no file field `image`, or JSON without `image` | Send the image |
| 400 | `labels_required` | Vision (1.4.4): a zero-shot detector was not told what to find, or a zero-shot classifier not given its labels | Send `labels` |
| 400 | `labels_not_supported` | Vision (1.4.4): `labels` for a detector or classifier with fixed classes | Leave `labels` out, or use a zero-shot model |
| 400 | `too_many_images` | Images: `n` over the deployment's *Images per request* | Ask for fewer, or raise `max_images` |
| 400 | `image_too_large` | Images: a side over the deployment's *Largest side* | Ask for a smaller size, or raise `max_side` |
| 400 | `invalid_size` | Images: `size` is not `WIDTHxHEIGHT` (each side 64 to 8,192), or a side is not a multiple of what the model takes (16 or 64; the message says). Videos: the same, each side 64 to 2,048, multiples of 16 or 32 | Use a size like the one the message suggests |
| 400 | `unsupported_output_format` | Images: an `output_format` other than png, jpeg or webp, or one the engine does not make | Ask for png |
| 400 | `too_many_steps` | Images and videos: `num_inference_steps` over the deployment's *Most steps* | Ask for fewer, or raise `max_steps` |
| 400 | `prompt_too_long` | Images and videos: `prompt` or `negative_prompt` longer than the deployment's *Longest prompt* | Shorten it, or raise `max_prompt_chars` |
| 400 | `video_too_long` | Videos: `seconds` over the deployment's *Longest video* | Ask for fewer seconds, or raise `max_seconds` |
| 400 | `video_too_large` | Videos: a side over *Largest side*, or more pixels a frame than the default size scaled to it | Ask for a smaller size, or raise `max_side` |
| 400 | `input_reference_not_supported` | Videos: `input_reference` (or `image_reference`, `video_reference`, `audio_reference`): text to video only in 1.3 | Describe it in the prompt |
| 400 | `variant_not_supported` | Videos: `variant=thumbnail` or `spritesheet`; the engine makes the video only | Ask for `variant=video` |
| 4xx/5xx | `engine_error` | Images and videos: the engine refused or failed the request; its status and message are passed on | Read the message; an out-of-memory error means a smaller size or fewer images |
| 400 | `structured_output_not_supported` | The request asks for structured output (`response_format` JSON, `structured_outputs`, `guided_*`, `grammar`, `json_schema`, a strict tool) and every provider that serves the model would drop or ignore it. The message names them and the parameter | Leave the parameter out, or use a target that takes it (an OpenAI-compatible server, a local deployment) |
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
| 404 | `endpoint_not_supported` | `/v1/images/edits`, `/v1/images/variations`, `/v1/videos/sync` or `/v1/videos/{id}/remix`: not offered in 1.3 | Use `/v1/images/generations`, or a job at `/v1/videos` |
| 404 | `video_not_found` | Videos: no such video for this key (another key's id answers the same) | Check `GET /v1/videos` |
| 409 | `video_not_ready` | Videos: the content of a job still `queued` or `in_progress` | Poll until it is `completed` |
| 409 | `video_failed` | Videos: the content of a failed job | Read the job's `error`, and ask again |
| 410 | `video_expired` | Videos: the result is older than the deployment's *Keep results* and was deleted | Ask for it again |
| 410 | `video_lost` | Videos: the replica that made it restarted or was stopped, and its files went with it | Ask for it again |
| 413 | `request_too_large` | The body (with media URLs fetched and inlined) is larger than the model's *Largest request* or `GATEWAY_MAX_BODY_MB` | Send less or smaller images, or raise the parameter or the setting |
| 413 | `media_too_large` | A media URL's file is larger than `MEDIA_FETCH_MAX_MB` | Send a smaller file, or raise the setting |
| 421 | `host_not_allowed` | The server does not answer to the host name in the URL | Use its address, or add the name to `ALLOWED_HOSTS` |
| 429 | `rate_limit_exceeded` | Over the key's requests or tokens per minute | Wait `Retry-After` seconds |
| 429 | `provider_rate_limited` | Every cloud target that could serve it was rate limited | Wait `Retry-After` seconds, if given |
| 429 | `queue_full` | Images: every replica is busy and the deployment's queue is full. Videos: the deployment holds *Jobs at once* | Wait `Retry-After` seconds |
| 429 | `too_many_jobs` | Videos: this key (or person) already has *Jobs per key* videos waiting or being made on the deployment | Wait `Retry-After` seconds, or delete one |
| 502 | `upstream_unreachable` | No replica could be connected to, or every one's engine was not running | Usually its agent or engine restarted; it recovers by itself. Check the deployment |
| 502 | `upstream_error` | The connection to the replica broke after the request was sent, or while the answer was coming. It is not sent to another replica, which could run it twice | Retry if that is safe |
| 502 | `replica_refused_key` | The replica refused the gateway's internal key | Stop and start the deployment |
| 502 | `provider_auth_failed` | The provider refused Forgeyard's key for it. Never passed on as 401, which would make you doubt your own key | An admin fixes the provider's key |
| 502 | `provider_blocked` | The egress rules refused the provider's address before anything was sent | An admin checks the provider's base URL and `EGRESS_*` |
| 502 | `provider_redirect` | The provider answered with a redirect, which is never followed | An admin checks the provider's base URL |
| 502 | `provider_unreachable` | The provider could not be reached | Retry later |
| 502 | `provider_error` | The provider answered 5xx, or the request to it failed | Retry later |
| 503 | `no_ready_replica` | The model exists, but no replica is ready: downloading, starting, stopped, or its machine is offline. The message gives each replica's state | Wait, or start the deployment |
| 503 | `local_busy` | Every replica was at its *Concurrent requests* and nothing else could take the request. The gateway then sends it to a local replica anyway, where the engine queues it, so you should not normally see it. Carries `Retry-After: 1` | Retry after `Retry-After` seconds |
| 503 | `no_target` | Every target of the route is disabled, or none serves this endpoint | An admin checks the route |
| 503 | `budget_unavailable` | This month's spend could not be read, so no cloud request is sent under a budget | Retry shortly |
| 503 | `queue_timeout` | Images: the request waited longer than the deployment's *Timeout* for a free replica | Retry later, or add a replica |
| 504 | `first_answer_timeout` | Streamed requests only: the replica took the request but sent no status line and first bytes within the deployment's *Wait for the first answer* (120 s by default). Stopped, never sent to another replica | Retry later, or raise the deployment's wait (a long prompt takes long before its first token) |
| 504 | `upstream_timeout` | The replica gave no non-streamed answer, or no next piece of a stream, within `GATEWAY_READ_TIMEOUT`; for images, no answer within the deployment's *Timeout*; for a video, it did not take the job within 60 s | Lower `max_tokens`, or raise the setting; for images, a smaller size or fewer steps |
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

## Apps (deprecated)

Apps, the workloads that are not models (1.4, #88), left the console and are **removed in 1.5**, with a
migration that removes the ones left. Until then they keep working through the API, and say so:

| Deprecated | What |
|---|---|
| The apps part of `/api/workloads` | `POST /api/workloads`; and `GET`, `PUT …/gateway`, `POST …/stop`, `POST …/start`, `DELETE` and `GET …/replicas/<n>/logs` of an app; `GET /api/workloads` while it lists an app |
| `GET /api/workloads/templates` | The 1.3 templates |
| `/x/<name>/…` | The [passthrough](#the-passthrough-x), and the console's own `POST /api/playground/vision/<name>/<task>` to an exposed vision app |
| A key's `workloads` and `workload_list` | Still taken and returned by `/api/keys`, but the console no longer shows them, and `GET /api/keys` no longer lists the apps a key could name |

- **Headers.** Every answer of these, errors included, carries `Deprecation` (RFC 9745: the date it was
  announced, `@<epoch>`) and a `Link` with `rel="deprecation"` to this section. There is no `Sunset`
  header: 1.5 has no date yet.
- **Log.** The server logs a warning for each of the three kinds (apps, templates, `/x/`) at most once
  an hour, naming the request.
- **Setup health.** `apps-deprecated` (a warning) names the apps while any exists.

A model deployment's workload (runtime `model`) is not deprecated: it is how models run, and reading it
(`GET /api/workloads/<id>`, its logs) carries no header.

```http
HTTP/1.1 200 OK
Deprecation: @1794787200
Link: </guide/en/reference#apps-deprecated>; rel="deprecation"; type="text/html"
```

Instead: deploy what you run as a model under **Serving**; for a model that no built-in engine runs, add a
custom engine (**Models → Engines**, a recipe). Then delete each app with `DELETE /api/workloads/<id>`.
The [Playground's vision models](#vision) replace the 1.3 vision templates.

## The passthrough (/x/)

**Deprecated in 1.4, removed in 1.5** ([Apps (deprecated)](#apps-deprecated)). Programs call an exposed app at `https://<server>:7860/x/<name>/<path>?<query>` with an API key
whose **Apps** setting allows it. Like `/v1`, it takes `Authorization: Bearer fyk_…` only: no
cookies, no key in the query string, no CORS headers.

### Paths and methods

| What | Rule |
|---|---|
| Methods | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` |
| Prefix | `/x/<name>` is removed: `/x/<name>/a/b?c=1` reaches the app as `/a/b?c=1`, and `/x/<name>` alone as `/` |
| Path | Passed on byte for byte, as it came. Refused with 400 `invalid_path`, never normalised: `.` or `..` segments, an encoded `.`, `/` or `\` (`%2e`, `%2f`, `%5c`), a backslash, control characters (raw or encoded) and non-ASCII bytes |
| Query | Passed on as it came |
| Not passed on | WebSockets (`Upgrade` is removed), gRPC, HTTP/2 to the app |

### Request and answer headers

| Header | Direction | What happens |
|---|---|---|
| `Authorization` | request | Your API key is checked and removed; the app gets the replica's internal key, which the agent's key guard checks and removes |
| `Cookie`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `X-Forgeyard`, `X-Forgeyard-*` | request | Removed |
| `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` | request | Set fresh: the client's address, `https` or `http`, and the host it called |
| `X-Forwarded-Prefix` | request | `/x/<name>` |
| `X-Request-Id` | both | A new id per request, sent to the app and back to the client |
| Hop-by-hop headers (`Connection` and what it names, `Keep-Alive`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`, `Expect`, `Proxy-*`) | both | Removed |
| `Set-Cookie`, `Access-Control-*`, `Strict-Transport-Security`, `Clear-Site-Data`, `Alt-Svc`, `Service-Worker-Allowed`, `Report-To`, `NEL`, `Date`, `Server` | answer | Removed (the server sends its own `Date` and `Server`) |
| `Content-Security-Policy` | answer | Always `sandbox; default-src 'none'; frame-ancestors 'none'` |
| `X-Content-Type-Options`, `X-Frame-Options`, `Cache-Control`, `Cross-Origin-Resource-Policy`, `Referrer-Policy` | answer | Always `nosniff`, `DENY`, `no-store`, `same-origin` and `no-referrer` |
| `Location`, `Content-Location` | answer | A path, or an address of the app itself, is rewritten under `/x/<name>`; other sites and relative references stay. Redirects are never followed |
| `X-Forgeyard-Target` | answer | `workload:<name>#<replica>` |
| `Content-Encoding`, `Content-Length` and the rest | answer | Passed on: the body is relayed as raw bytes as it arrives (compressed bodies stay compressed; SSE and chunked answers stream) |

### Limits and timeouts

| Limit | Default | Set on |
|---|---|---|
| Largest request body | 32 MiB (413 `request_too_large` above) | the app: **Largest request (MiB)**, 1 to 1,024 |
| Wait for each piece of the answer | 600 s (504 `upstream_timeout` before the first byte) | the app: **Read timeout (s)**, 1 to 3,600 |
| Connecting to a replica | `GATEWAY_CONNECT_TIMEOUT` (5 s); a replica that cannot be reached is skipped for the next one | the server |
| Requests at once per replica | no limit (429 `workload_busy` when every replica is full) | the app |
| Requests at once per key | no limit (429 `key_busy`); in the Playground, per person | the app |
| Requests per minute | the key's own limit (429 `rate_limit_exceeded`); tokens per minute do not apply | the key |

Every 429 carries `Retry-After`. A client that goes away closes the request to the app at once.

**Usage** counts each request under the model `x:<name>`: requests, errors (an answer's own 5xx too),
limited requests, bytes in (request bodies) and out (answer bodies as sent), busy time (from sending
the request until the answer ended) and latency.

### Errors at /x/

Errors from the gateway have the same shape as at `/v1`. An answer from the app itself, also a
401 or a 5xx, is passed on as it is.

| Status | `code` | Means | What to do |
|---|---|---|---|
| 400 | `invalid_path` | The path has a dot segment, an encoded `.`, `/` or `\`, a backslash, a control character or non-ASCII bytes | Send the path without them |
| 401 | `missing_api_key`, `invalid_api_key`, `revoked_api_key`, `expired_api_key`, `account_disabled` | As at `/v1` | As at `/v1` |
| 403 | `workloads_not_allowed` | The key's **Apps** setting is *None* | Change the key's setting |
| 404 | `workload_not_found` | No such exposed app for this key: it does not exist, is not exposed, or is not on the key's list | Check the name and the key's list |
| 413 | `request_too_large` | The body is larger than the app's **Largest request** | Send less, or raise the limit |
| 429 | `rate_limit_exceeded` | Over the key's requests per minute | Wait `Retry-After` seconds |
| 429 | `workload_busy` | Every replica has as many requests in flight as **Requests at once per replica** | Wait `Retry-After` seconds, or add replicas |
| 429 | `key_busy` | The key has as many requests in flight to this app as **Requests at once per key** | Wait for one to finish |
| 502 | `upstream_unreachable` | No replica could be connected to (its program is down or restarting) | Check the app's output |
| 502 | `upstream_error` | The request to the replica failed | Retry |
| 502 | `replica_refused_key` | The replica's key guard refused the gateway's internal key | Stop and start the app |
| 503 | `no_ready_replica` | The app is stopped, or no replica is ready, or a replica is not behind the key guard yet; the message says which | Start it, wait, or update the agent |
| 504 | `upstream_timeout` | No answer within the app's **Read timeout** | Raise the timeout, or check the app |

An answer that breaks off after it started cannot change its status: the connection is closed before
the body is complete, so a client never takes a cut-off body for a whole one.

### From the Playground

Since 1.4 the console's Playground offers vision models only (in its one model picker; the Vision tab
is gone), and `GET /api/playground/vision/options` no longer lists apps (`workloads`). The route below
still reaches an exposed vision app until 1.5 (deprecated), the way 1.3's Vision tab did: through routes of its own,
never through `/x/`: they take the console's sign-in (its cookie, and on `POST` the `X-Forgeyard: 1`
header from the console's own origin), and `/x/` keeps taking API keys only. Anyone signed in may use
them. A vision app is an exposed service started with `--task detect`, `--task segment` or
`--task depth`, as every [template](#app-templates) is.

| Route | What it does |
|---|---|
| `GET /api/playground/vision/options` | `models`: the vision models (see [Vision](#vision)); `tasks`: the vision tasks |
| `POST /api/playground/vision/<name>/<task>` | The body (the image, as multipart) to the app's own `POST /v1/<task>`, nothing else: no other path, method or query |

The request then follows the `/x/` rules above: the same replicas and key guard, request and answer
headers (the cookie never reaches the app), the app's **Largest request**, **Requests at
once per replica** and **Requests at once per key** (counted per person), its timeouts, and the same
errors. Another task than the app's, or an app that is not an exposed vision app, is 404
`workload_not_found`; a task the vision server does not have is 404 `unknown_task`. Usage is counted
under `x:<name>` as *console*, for the person signed in.

## App templates

**Deprecated in 1.4, removed in 1.5.** The templates run `docker.io/tonyhuang1101/forgeyard-vision` at the server's version: the tag
`X.Y.Z` (CPU, linux/amd64 and linux/arm64) or `X.Y.Z-cuda` (torch with CUDA 13.0, linux/amd64, NVIDIA
driver 580 or newer). Built from `images/vision` in the repository, the dependencies pinned with hashes
and the models baked in at their revisions; it runs as an unprivileged user. `GET /api/workloads/templates`
lists the templates with the app spec each one makes.

| Endpoint | Takes | Returns |
|---|---|---|
| `GET /` | | `task`, `model`, `revision`, `licence`, `device` (`cpu` or `cuda`), `endpoint` |
| `GET /health` | | 200 once the model is loaded |
| `POST /v1/detect` | `image`; `threshold` (0.5), `max_detections` (100) | `detections`: `label`, `score`, `box` `[x0, y0, x1, y1]` in pixels |
| `POST /v1/segment` | `image`; `points` `[[x, y], …]` with `labels` (1 inside, 0 outside), or `box` `[x0, y0, x1, y1]`; `multimask` (true) | `masks`: `score`, `area`, `box`, `png` (a 1-bit PNG, base64), best first |
| `POST /v1/depth` | `image` | `depth_png` (a 16-bit PNG, base64, the image's size, nearer is brighter), `min`, `max`: relative depth, not metres |

An image is a multipart file field `image` (other fields as form fields, JSON values allowed) or a JSON
body `{"image": "<base64 or a data: URI>", …}`; PNG, JPEG or WebP, at most 20 MiB and 40 megapixels.
Every answer also has `model`, `revision`, `width`, `height` and `ms`. The server's errors are
`{"error": {"message", "code"}}` with 400 (`invalid_image`, `missing_image`, `missing_prompt`,
`invalid_option`, `invalid_json`) or 413 (`image_too_large`); through `/x/` they are passed on as they are.

## App environment

An app's replica starts with an empty environment. It gets only what its spec declares, plus:

| Variable | Value |
|---|---|
| `FORGEYARD_WORKLOAD` | The app's name |
| `FORGEYARD_REPLICA` | The replica's id |
| `FORGEYARD_REPLICA_INDEX` | The replica's number, from 0 |
| `PORT`, `PORT_<NAME>` | For each declared port: a process gets the host port it must listen on; a container gets its own port (the engine maps the host port to it). `PORT` is the first one |
| `CUDA_VISIBLE_DEVICES`, `NVIDIA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER` | NVIDIA: the UUIDs of its cards only. A replica without GPUs sees none |
| `ROCR_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES` | AMD: its cards only |
| `PATH`, `HOME`, `LANG`, `USER`, `LOGNAME` | A process only: a basic `PATH`, its own directory as `HOME`, `LANG=C.UTF-8`, and the app account |

A spec that sets a GPU variable (also `HIP_VISIBLE_DEVICES` and `GPU_DEVICE_ORDINAL`), `PORT`, or
any name that starts with `FORGEYARD_` or `PORT_` is refused. In a process's arguments and in
environment values, `${PORT}` and `${PORT_<NAME>}` become the port. In a process's arguments,
`${BIND_ADDRESS}` becomes the address it should listen on: `127.0.0.1` behind the agent's key guard
(an app exposed at `/x/`), else the agent's address.

## Change a deployment through the API

`PATCH /api/deployments/{id}`, for admins and operators, with a console session (the same requests
the **Edit** page makes; writes also send the `X-Forgeyard: 1` header). A field left out keeps its
value.

| Field | Value |
|---|---|
| `params` | The parameters to change: the recipe's own (`context`, `concurrency`…), `gpus`, `share` (`exclusive` or `vram`), `vram` (memory per device, e.g. `"20Gi"`), `extra_args` (a list) and `first_answer_timeout` (seconds, 5 to 3,600; not for image and video deployments; changing only it restarts nothing). Only the names sent change; a name sent as `null` goes back to the recipe's default (`vram`: back to the estimate) |
| `replicas` | 1 to 16 |
| `machine_id` | A machine's id pins the deployment there; `null` makes it automatic |
| `wait` | `true`: save even though it does not fit now; the restarted replicas wait for room |
| `dry_run` | `true`: only check. The answer is the same, without `deployment`, and nothing is saved |

The model, the recipe, the name and the served name cannot be changed (422). The answer:

| Field | What it is |
|---|---|
| `estimate` | The new estimate |
| `placement` | Where each replica would go, as for a deploy; `previous` marks a replica that stays on its machine, and `share_hint` is set when an exclusive model needs half its card or less |
| `changes` | `{name: {before, after}}` for each parameter, `replicas` and `machine_id` that changes; `{}` when nothing does |
| `restart` | `rolling` (one replica at a time), `all` (the only replica restarts), `none`, or `on_start` (stopped: the settings apply when it starts) |
| `restart_text` | The same in a sentence |
| `deployment` | The deployment after saving (not on a dry run) |

| Status | `code` | When |
|---|---|---|
| 409 | `does_not_fit` | The deployment runs and the new settings fit nowhere now; `machines` says why for each machine, as for a deploy. Nothing is saved |
| 409 | `recipe_gone` | Its recipe no longer exists |
| 422 | `invalid_params` | A parameter is unknown or out of its range |
| 422 | `cannot_estimate` | The new parameters cannot be estimated for this model |
| 404 | `not_found`, `machine_not_found` | No such deployment, or no such machine for `machine_id` |
| 403 | | Not an admin or an operator |

Each saved change is audited as `deployment.changed`, with the changes and the restart. `GET
/api/deployments/{id}` also gives `pinned` (the machine chosen, or `null`), `rollout` (while replicas
restart: `{generation, updated, total, next}`) and, per replica, `not_picked_up`: set when the replica
was placed on a connected machine whose agent has not reported it for 2 minutes
([A replica stays pending](troubleshooting.md#a-replica-stays-pending)), and (1.4)
`placement_note`: why the replica is on its machine when a machine that could not fetch what it needs
decided it, such as *cpu-01 could not reach huggingface.co; placed on cpu-02 instead*
([A machine cannot reach Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face)). The
deployment's `workload` (as in `GET /api/workloads/{id}`) names it back: `deployment: {id, name}`. In
`placement`, each machine and each placed replica lists `needs` (what it would download there:
`model`, `engine`, `image`) and, when that machine could not reach one of those sources in the last
hour, `unreachable`.

## A machine's model cache through the API

For a console session, like the other `/api` requests (writes also send the `X-Forgeyard: 1`
header). Everyone signed in can look; admins and operators can remove.

| Request | What it does |
|---|---|
| `GET /api/machines/{id}/cache` | The models in that machine's cache, as its agent last reported them |
| `POST /api/machines/{id}/cache/remove` with `{"key": "<key>"}` | Removes one entry: a revision, one GGUF file of it, a URL model or an engine |
| `POST /api/machines/{id}/cache/remove-partial` | Removes what unfinished downloads left |

The listing has `free_bytes` and `total_bytes` (the cache's disk), `used_bytes` (what the cache takes
on it, counting shared files once), `partial_bytes` and `partial_files`, `sizes` (false for an agent
older than 1.4, which lists only keys) and `remove_note` (why nothing can be removed now, or `null`).
`models` lists each entry:

| Field | What it is |
|---|---|
| `key` | What to send to remove it: `hub/models--<org>--<name>@<commit>` for a Hugging Face revision, `url/<id>` for a single file from a URL |
| `kind` | `hf` or `url` |
| `repo`, `revision`, `file` | The repository and commit, or the file name of a URL model |
| `bytes`, `files` | Its size and number of files. A file that two revisions share counts in both |
| `parts` | For a revision with GGUF files: each file (a split model once, with all its parts) with its own `key`, `bytes`, `catalog` and `used_by`. Removing the last one removes the revision |
| `catalog` | The catalog models it is |
| `used_by` | The deployments (or apps) on that machine that use it |

`engines` lists the engine archives (`key` `engine/<first 16 hex digits of the archive's SHA-256>`,
`name`, `bytes`, `used_by`). A removal answers `freed_bytes` (what was freed on the disk) and the
listing afterwards (`cache`).

| Status | `code` | When |
|---|---|---|
| 409 | `model_in_use` | A deployment on that machine uses it; `in_use` names them. The agent checks again before it deletes anything |
| 409 | `cache_busy` | A download of that model (or, for partial downloads, any download) is in progress there |
| 409 | `machine_offline`, `agent_too_old`, `agent_no_answer` | The agent is not connected, does not support removing (before 1.4), or did not answer within a minute |
| 409 | `cache_remove_failed` | The agent could not delete it; the message says why |
| 404 | `not_in_cache` | The machine does not report that key |
| 403 | | Not an admin or an operator |

Each removal is audited as `model.cache_removed` with the machine, the key, the model and
`freed_bytes`. Behind it is the agent protocol feature `cache_admin` (agents from 1.4): the agent
lists its cache with sizes, removes on request, and refuses while a replica there uses the files or
while one of their downloads runs.

## Download a model ahead through the API

For a console session, like the other `/api` requests (writes also send the `X-Forgeyard: 1`
header). Everyone signed in can look; admins and operators start and cancel downloads.

| Request | What it does |
|---|---|
| `GET /api/downloads/targets?model_id=<id>&recipe=<name>&engine=true` | Each machine, and whether it can take this model (and the recipe's engine) |
| `POST /api/downloads` with `{"model_id": <id>, "machine_ids": [<id>, …], "engine": true, "recipe": "<name>"}` | Starts a download on each machine that can take it; `engine` is on by default, and `recipe` defaults to the first recipe that serves the model |
| `GET /api/downloads?model_id=<id>&machine_id=<id>` | The downloads under way first, then the finished ones (the last 100) |
| `POST /api/downloads/{id}/cancel` | Cancels one under way; its agent stops it at once and keeps what is on disk |
| `PUT /api/machines/{id}/fetch-via` with `{"fetch_via": "auto"}` | Admins only: how that machine fetches models and engine archives: `auto` (directly, through the server after a network failure), `server` (always through the server) or `direct` (never through it). Its agent gets the new way at once |

Each machine in `targets` has a `state`: `ok`, `cached` (already there), `downloading`, `too_old` (an
agent before 1.4), `no_cache`, `offline`, `no_space` (the model plus a 1 GiB margin is more than the
cache's free disk) or `revoked`, with a `note` saying why; `need_bytes` and `free_bytes`; and, with
the engine, `variant` and `engine` (`kind` `archive` or `image`, `name`, `cached`), or `engine_note`
when no variant of the recipe runs there; and `via`, how the model would come (`direct`, `auto` or
`server`), with `from_machine` (1.4.1) naming the machine that would send it through the server, or null. The variant is the one the machine would run: a GPU variant
when it has an allowed card that the variant supports, else a CPU one, and one its runtime can run.

A download has `id`, `model`, `machine` (with `connected`), `file`, `recipe` (null: the model only),
`state` (`queued`, `downloading`, `done`, `failed` or `cancelled`), `download` (the progress, the same
shape as a deployment replica's, with `via: "server"` while the bytes come through the server),
`reason`, `via` (`direct`, `server` once it came through the server, or `machine` once the model came from
another machine's cache through the server, with `source: {id, name}`), `requested_by`,
`created_at`, `finished_at` and `waiting` (the machine is offline). Finished ones are kept 30 days, at
most 20 per machine.

| Status | `code` | When |
|---|---|---|
| 409 | `nothing_to_download` | No machine can take it; `skipped` lists each with its `state` and `reason` |
| 409 | `download_finished` | Cancel of a download that already ended |
| 422 | `local_model` | A local model is a directory on the machines: there is nothing to download |
| 422 | `recipe_not_compatible`, `bad_fetch_via` | The recipe does not serve the model; a `fetch_via` that is not one of the three |
| 404 | `not_found` | No such model, machine or download |
| 403 | | Not an admin or an operator (not an admin, for `fetch-via`) |

Audit events: `model.download_requested` (one per machine, with the model and the engine),
`model.download_cancelled`, `node.fetch_via_changed`, and `model.checksum_recorded` when the SHA-256 of
a URL model is recorded: a single GGUF file from a URL has no published checksum, so the first one an
agent computes after a complete download is kept in the catalog entry (`files[0].sha256` of the model's
metadata, with `sha256_from`, the machine, and `sha256_at`), and every later download must match it. A
different one reported later is logged and never replaces it.

Behind it is the agent protocol feature `prefetch` (agents and servers from 1.4). The server lists each
queued or downloading row in `desired.downloads`, beside the replicas: `{id, model, engine?, image?,
image_ids?}` (a Hugging Face model's block carries the server's token for gated repositories, kept in
memory only, as for replicas). The agent fetches it with the code a replica's download uses (the same
locks, resume from partial files, checksums and cancel; an image with the pull progress of 1.3.1),
starts nothing, runs at most two at a time and reports `status.downloads` (`{id, state, reason,
download}`); a download missing from the next list is cancelled. Migration `0015` adds the table
`model_downloads` and the column `nodes.fetch_via` (`auto` for every existing machine).

### Downloads through the server (the relay)

The agent protocol feature `relay` (agents and servers from 1.4). To an agent that lists it, on a
machine whose `fetch_via` is `auto` or `server`, every model block (Hugging Face or URL) and engine
archive block of a desired replica or download carries `relay: {path, mode}`: `mode` is `server`
(always through the server; set so, or `auto` with a network failure to that host remembered in the
last hour) or `auto` (directly first, through the server after a network failure). A `server` model
block carries no `token`. The agent then fetches from its own server, never from a host the server
names:

| Request | What it returns |
|---|---|
| `GET /agent/v1/relay/{replica\|download}/{id}/model/{file path}` | One file of a Hugging Face model |
| `GET /agent/v1/relay/{replica\|download}/{id}/model` | The file of a URL model |
| `GET /agent/v1/relay/{replica\|download}/{id}/engine` | The engine archive of a process variant |

`{id}` is the replica's or the download's id from the desired list. The request carries the machine
credential (`Authorization: Bearer …`) and at most one `Range: bytes=N-` (or `N-M`), which is passed
on to the source; the answer is the source's 200 or 206 with `Content-Length` and `Content-Range`,
streamed and stored nowhere. Browsers (any `Origin`) are refused.

The server serves only what the machine's **current** desired state names: a replica placed there
and wanted running, or a download to it that is queued or under way, and only a file that model
lists. It builds the source URL itself from the catalog entry or the recipe, with its own Hugging Face
token (or the deployment's) and its own model-source rules (`MODEL_ALLOW_PRIVATE_HOSTS`, the URL
hosts). It checks every 2 seconds that the relay is still wanted, and closes it when not.

| Status | `code` | When |
|---|---|---|
| 401 | `credential_refused` | No credential, or a revoked one |
| 403 | `relay_off` | The machine is set to `direct`, or its agent does not speak `relay` |
| 403 | `browser_refused` | The request has an `Origin` |
| 403 | `source_refused` | The source refused access (a gated model without a token that works) |
| 403 | `source_blocked` | The source's address is not allowed by the server's rules |
| 404 | `relay_not_wanted` | Nothing in the machine's desired state has that id, file or engine |
| 416 | `bad_range`, `range_not_satisfiable` | More than one range, or past the end |
| 429 | `relay_busy` | Two relays under way for that machine, or four in all; `Retry-After: 5` |
| 502 | `source_unreachable`, `source_failed` | The server could not reach the source, or it answered an error |

The agent stops on 401, 403 and 404 (with the server's words), waits out 429 without counting it as a
try, and retries the rest like a network error. Its progress then has `download.via: "server"`.

**From another machine** (the protocol feature `relay_source`, agents and servers from 1.4.1). Before the
server opens a model file's source, it asks another machine that has it. The candidates are the other
machines that are connected, whose agent lists `relay_source`, whose reported cache (`model_cache.cached`)
has the model's revision (a URL model: its key, listed in `model_cache.models` with the SHA-256 the server
recorded), and that are not marked for that file. A machine listing the GGUF file's own key comes first,
then the one with fewer sends under way, then the latest heartbeat. The request of the machine that asks
does not change.

The server sends the chosen machine `relay_send {request_id, path, item, offset, length}` over its
WebSocket. `item` describes the file by the digest the server holds: `{kind: "hf", repo, revision, file,
blob, size}` (`blob` is the SHA-256 of an LFS file, else its git blob id) or `{kind: "url", dir, sha256,
size}`. The agent looks it up only among the models it found on its own disk (the revision's marker lists
the file, its snapshot link points at that blob, a regular file of that size inside the cache; a URL model's
marker has that SHA-256), holds the model's files lock while it sends (a removal answers `busy`), and
POSTs bytes `offset` to `offset + length - 1`:

| Request | What it does |
|---|---|
| `POST /agent/v1/relay-send/{ticket}` | The bytes, with the machine credential and `Content-Length` = `length`; answered `{"ok": true, "bytes": n}` once all of them were passed on |

Every answer closes the connection (`Connection: close`), so a source whose send was ended early (the machine
that asked went away, the download was cancelled) stops writing at once.

It then answers `relay_sent {request_id, ok, code, reason, bytes}`, `code` being `not_found`, `busy` (two
sends under way), `corrupt` (a whole file it sent does not hash to its digest) or `failed`. The server
pipes the body into the asking machine's answer through a queue of at most 64 pieces; `Content-Length` and
`Content-Range` are its own, from the file's size. A machine that answers `not_found` or `busy`, or sends
nothing within 10 seconds (it is then not asked for that file for 10 minutes), is skipped for the next
one, then the source on the internet as above. A whole file is hashed on its way, and its last piece held
back until it matches: a machine whose copy does not match is not asked for that file for an hour, the
audit log records `node.relay_copy_refused` (the machine, the file, the machine that asked, both digests),
a deployment's events say so too, and the next request for that file from the machine that asked is
answered whole (200, ignoring its `Range`), so it starts the file over. At most two sends per machine.

| Status | `code` | When |
|---|---|---|
| 401 | `credential_refused` | No credential, or a revoked one |
| 403 | `browser_refused` | The request has an `Origin` |
| 404 | `send_not_found` | No send from this machine with that ticket (another machine's credential, a ticket that ended) |
| 409 | `send_claimed`, `send_ended` | A second POST for the ticket; the send ended before all its bytes were passed on |
| 400 | `send_length` | A `Content-Length` other than the length asked for |

Progress through the server then carries `download.source`, the sending machine's name (set by the server;
an agent cannot), and a download's `via` becomes `machine` with `source: {id, name}`. In the targets of
Download to…, `from_machine` names the machine that would send the model to a machine that gets it through
the server.

**Engine images** (the protocol feature `relay_images`, agents and servers from 1.4). To an agent that
also lists it, a desired replica (beside its `spec`) or download whose engine image is pinned by digest
and recorded in `forgeyard/recipes/images.lock.json` carries `image_relay: {path, mode}` with the same
modes; `path` is `/agent/v1/relay/{replica|download}/{id}/image`. A tag-only image never gets it.

| Request | What it returns |
|---|---|
| `GET /agent/v1/relay/{replica\|download}/{id}/image/manifest` | The recorded manifest of the machine's platform (from its agent's `platform.arch`), the registry's exact bytes, with `Docker-Content-Digest` and `Forgeyard-Platform` (`linux/amd64`) |
| `GET /agent/v1/relay/{replica\|download}/{id}/image/blobs/{digest}` | That manifest's config or one of its layers, by digest; `Range` as above |

The server fetches the manifest by its recorded digest and refuses it unless its bytes hash to it and
it names the recorded config; it keeps it in memory. Blobs come from the registry's API with an
anonymous pull token (Docker Hub, GHCR, quay.io and other registries that hand them out), through the
same egress rules as model files; the token is dropped when the registry redirects to its CDN. A whole
blob is hashed on its way through and its last piece is sent only when the hash matches. The agent
checks the manifest's digest against the recorded ids, every blob against its digest, the config's
content id against the recorded ones and its architecture against the machine's, then loads an
archive that holds an OCI image layout (`oci-layout`, `index.json`, `blobs/sha256/…`) and the classic
`manifest.json`, tags it `repo:tag` and runs the `image_ids` check on it. Its progress is a
`download` with `phase: "image"`, `layers`, `layers_done`, `via: "server"`, and `unpacking` while it loads.

| Status | `code` | When |
|---|---|---|
| 403 | `relay_off` | The machine is set to `direct`, or its agent does not speak `relay_images` |
| 404 | `relay_not_in_image` | The digest is not the config or a layer of that manifest |
| 409 | `relay_image_not_pinned` | The image is not pinned by digest with recorded ids: upload it instead |
| 409 | `relay_no_platform` | The image has no build for the machine's architecture |
| 502 | `source_failed` | The registry's manifest does not hash to the recorded digest, or names another config |

### Uploading an image file to a machine

`POST /api/machines/{id}/images` (admins). The body is the file `docker save` or `podman save` wrote,
gzip-compressed or not, with `Content-Length` (411 without it) and at most `IMAGE_UPLOAD_MAX_GB`
(413); `X-Filename` (URL-encoded) names it in the audit log. The server sends the agent `image_load
{request_id, path, size, name}` (protocol feature `image_load`); the agent fetches `GET
/agent/v1/image-upload/{ticket}` with its credential (once; another machine's credential gets 404),
streams it into `docker load` / `podman load`, and answers `image_loaded {request_id, ok, reason,
images: [{id, tags, content, os, architecture}]}`. The answer to the browser is `{ok, bytes, images}`,
each image with `matches: [{recipe, variant, image, how}]`: `how` is `checked` (its id or content id is
one recorded for that recipe's pinned image) or `tag` (a tag-only recipe image named like one of its
tags).

| Status | `code` | When |
|---|---|---|
| 409 | `machine_offline`, `agent_cannot_load` | The agent is not connected, or has no container engine or is older than 1.4 |
| 409 | `upload_busy` | An upload to that machine is under way (429: two in all) |
| 400 | `upload_short`, `upload_broken` | The body ended early or broke off |
| 504 | `agent_no_fetch`, `agent_stalled` | The agent did not start fetching within 30 s, or took nothing for 2 minutes |
| 422 | `image_load_failed` | The engine could not load it (the agent's words) |

Audited as `node.image_uploaded` (the file name, its size, `ok`, the images loaded or why not).

### Copying an image from another machine

`GET /api/machines/{id}/images` (admins) asks that machine's agent for the images in its engine (protocol
feature `image_save`, agents from 1.4.1: `image_list {request_id}`, answered by `image_listed {request_id, ok,
reason, images}`) and answers `{machine_id, images: [{id, tags, content, size, created, os, architecture,
matches}]}`, at most 256.

`POST /api/machines/{id}/images/copy` with `{"source_id": 2, "image": "sha256:…"}` (admins) copies one of them to
machine `id`. The server sends the source `image_save {request_id, path, image}`; its agent saves the image whose
id is `image` (one of its own: never a name from the server) under its own tags, with `docker save` (Podman:
`podman save --multi-image-archive` for several tags), and POSTs it, chunked, to `/agent/v1/image-send/{ticket}`
with its credential (once; another machine's credential gets 404, a second POST 409, a browser 403; every answer
closes the connection, so a source whose send was ended early stops at once). The server
pipes it into the target's `image_load` as for an upload (without `size`; the answer to the target is chunked and
never ends cleanly when the source broke off), and the source answers `image_saved {request_id, ok, reason, bytes,
image}`. The answer is `{ok, bytes, images, source: {id, name}, saved}`, each image with `matches` and
`same_as_source` (its content id, else its id, equals the source's; null when there is nothing to compare).

| Status | `code` | When |
|---|---|---|
| 422 | `bad_image`, `same_machine` | The image is not named by its id; the source is the target |
| 409 | `source_offline`, `source_cannot_save` | The source is not connected, or has no container engine or is older than 1.4.1 |
| 409 | `source_busy` | The source is already sending an image |
| 409 | `machine_offline`, `agent_cannot_load`, `upload_busy` | The target, as for an upload (429: two in all) |
| 409 | `image_save_failed` | The source could not save it (its words): not one of its images, or its engine failed |
| 504 | `source_no_send` | The source did not start sending within 2 minutes |
| 400 | `upload_broken` | The source's save broke off; nothing was loaded |
| 413 | `upload_too_large` | The image is larger than `IMAGE_UPLOAD_MAX_GB` |
| 422 | `image_load_failed` | The target's engine could not load it (the agent's words) |

Audited as `node.image_copied` (the target; `from_machine`, the image asked for, its size, `ok`, the images
loaded with their content ids, or why not). Refusals before a byte moved are not audited.

### A machine's engine images through the API

`GET /api/machines/{id}/engine-images` (everyone signed in, 1.4.2) asks that machine's agent for the images in
its engine (`image_list`, as above) and answers `{machine_id, listed, note, images, others, free_bytes,
total_bytes, can_remove, remove_note}`. `images` holds only the images a recipe names, largest first, each as
listed plus `matches`, `used_by` (the deployments on that machine whose replica runs it: `[{name, kind, id}]`),
`agent` (the agent runs from it) and `remove_note` (why it cannot be removed now, or null). `others` counts the
engine's other images. `free_bytes` / `total_bytes`: where the engine keeps its images, when the agent can see
that directory, else null. `listed` false with a `note` when the agent is offline, cannot list (no container
engine, older than 1.4.1) or failed.

`POST /api/machines/{id}/engine-images/remove` with `{"image": "sha256:…"}` (admins) removes one. The server lists
the machine's images again and refuses before asking the agent; then it sends `image_remove {request_id, image}`
(protocol feature `image_remove`, agents from 1.4.2). The agent removes only an image of its own listing, never
the image it runs from, one its replicas use or one any container of the engine uses; it runs `docker rmi` /
`podman rmi` without `-f` (by the image's tags when it has several) and answers `image_removed {request_id, ok,
code, reason, in_use, image, freed_bytes, free_bytes, total_bytes}`. A 1.4.2 agent's `image_listed`
also carries `agent_image` (the id of the image it runs from) and `free_bytes` / `total_bytes`. The answer is
`{ok, image, freed_bytes, free_bytes, total_bytes}`: `freed_bytes` is the image's size (at most that came back:
layers another image shares stay); `free_bytes` / `total_bytes` are the space after, when the agent can see it.

| Status | `code` | When |
|---|---|---|
| 422 | `bad_image` | The image is not named by its id |
| 409 | `machine_offline`, `agent_too_old` | The agent is not connected, or older than 1.4.2 |
| 404 | `image_not_found` | The machine does not list that id (any more) |
| 409 | `not_an_engine_image` | No recipe names the image: it is not Forgeyard's to remove |
| 409 | `agent_image` | The agent runs from that image |
| 409 | `image_in_use` | A deployment on that machine uses it (`in_use` names them), or a container of the engine does (the agent's words) |
| 409 | `image_remove_failed` | The engine did not remove it (its words) |
| 504 | `agent_no_answer` | The agent did not answer within 5 minutes |

Audited as `node.image_removed` (the image, its tags, the recipes that name it, `ok`, the bytes freed, or the
agent's `code` and reason) for every removal the agent was asked for; refusals before that are not audited.

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
| `setting-removed` | warning | A [setting removed in 1.4](#removed-settings) is still set, so it is ignored. The finding names what replaces it or what happens instead |
| `secret-key-both` | warning | Both `FORGEYARD_SECRET_KEY` and `FORGEYARD_SECRET_KEY_FILE` are set |
| `heartbeat-flapping` | warning | `AGENT_OFFLINE_SECONDS` is under 2.5 heartbeats (under 5 seconds: the shortest heartbeat is 2 seconds) |
| `private-hosts-allowed` | warning | `MODEL_ALLOW_PRIVATE_HOSTS`, `PROVIDER_ALLOW_PRIVATE_HOSTS` or `MEDIA_ALLOW_PRIVATE_HOSTS` is on |
| `cookie-not-secure` | warning | `COOKIE_SECURE=false` while the server speaks HTTPS |
| `admin-password-left` | warning | `ADMIN_PASSWORD` is still set after the owner exists |
| `env-seed-ignored` | note | A first-value setting (`MODEL_URL_HOSTS`, `MODEL_LOCAL_DIRS`, `GATEWAY_DEFAULT_*`, `GATEWAY_MAX_*`, `GATEWAY_CLOUD_*`) differs from what an admin saved, which wins |
| `secret-key-backup` | warning | No admin has confirmed a backup of the current secret key. **Download a backup…** (a passphrase-encrypted file, see [The key backup file](#the-key-backup-file)) or **I have a backup of key id …** ticks it off; it comes back when the key changes |
| `host-loopback` | error | `HOST` is a loopback address, but a machine joined from another address (and no proxy is in front) |
| `public-url-unset` | note | `PUBLIC_URL` is empty on a server other machines can reach. A warning when you opened the page as `127.0.0.1` or `localhost` |
| `certificate-name-missing` | error | Your own certificate does not name `PUBLIC_URL`'s host |
| `certificate-expiring` | warning | Your own certificate expires within 30 days, or Forgeyard's CA within 90. An error within 14 days |
| `registrations-pending` | note | People are waiting for approval |
| `agent-outdated` | warning | A machine's agent is older than the server. An error when its protocol is older |
| `gpus-shared-no-allowlist` | warning | Cards of a machine hold at least 1 GiB more than Forgeyard reserved there, and nobody chose its GPU access |
| `machine-no-models` | note | An online agent has no model cache, so no model is placed there. A warning when it has GPUs |
| `apps-deprecated` | warning | Apps (workloads that are not models) exist: they, their API and `/x/` are removed in 1.5 |
| `process-workloads-off` | note | An online agent refuses to run processes; the finding gives its reason |
| `model-cache-full` | warning | A machine's model cache disk has less than 10% free |
| `machine-simulated` | warning | A machine reports simulated hardware |
| `machine-offline-with-replicas` | warning | A machine has been offline for over 5 minutes with replicas placed on it |
| `machine-loopback-address` | warning | A machine that is not the server's host publishes its replicas on a loopback address (through a tunnel or a proxy), and its port range is not forwarded to the server's host ([what to do](troubleshooting.md#a-machine-publishes-its-replicas-on-a-loopback-address)) |
| `replica-margin-low` | warning | A card with a running replica has less free than `VRAM_HEADROOM_MIB` (at least 256 MiB) |
| `deployment-waiting` | warning | A replica has waited for a machine for over 15 minutes |
| `deployment-could-share` | note | A running exclusive replica reserves less than half of a card of 24 GiB or more: with Share VRAM the rest could hold other models |
| `extra-args-override` | warning | A deployment's extra engine arguments repeat a flag Forgeyard sets (memory, context, address, key) |
| `gated-model-no-token` | note | A deployment of a gated Hugging Face model has no token of its own, and the server has none. An error once its download was refused |
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
| `python -m forgeyard secrets restore FILE` | Put back the secret key and the CA from a key backup downloaded from the console (stop the server first). Asks for the passphrase. `--data-dir DIR`: another data folder; `--force`: replace a different key or CA already there (the old files are kept as `<name>.replaced-<time>`); `--passphrase-file PATH`: the passphrase from a file's first line. Exit 2, nothing written, on a wrong passphrase, a damaged file or a different key already there |
| `python -m forgeyard --version` | The version |

### The key backup file

**Download a backup…** (admins, 1.4.2) answers `POST /api/secret-key/backup` (JSON `password`,
`passphrase`, `passphrase_confirm`) with `forgeyard-key-backup-<key id>-<YYYYMMDD>.fykey`,
`Content-Disposition: attachment` and `Cache-Control: no-store`. The password is checked like a
sign-in (the same back-off; a wrong one answers 401 `wrong_password` and is audited as
`secret_key.backup_failed`); the passphrase needs 12 to 1024 characters, more than two different
ones, and must not be the password (422). `GET /api/secret-key` says what a backup holds. The file:

| Bytes | What |
|---|---|
| 0–7 | `FYKEYBK` and a zero byte |
| 8–9 | N, the header's length (big-endian) |
| 10 … 10+N | The header, UTF-8 JSON: `format` `forgeyard-key-backup`, `version` 1, `kdf` (`name` `argon2id`, `memory_kib` 65536, `iterations` 3, `lanes` 4, `salt`: 16 random bytes, base64), `cipher` (`name` `aes-256-gcm`, `nonce`: 12 random bytes, base64), `kid`, `created_at` |
| the rest | AES-256-GCM ciphertext, then its 16-byte tag |

The 32-byte encryption key is Argon2id (RFC 9106) of the passphrase, normalised to Unicode NFC and
encoded as UTF-8, with the header's salt and parameters. The associated data is every byte before
the ciphertext, so a changed header fails like a changed ciphertext. The plain text is JSON:
`kid`, `secret_key` (base64url, as in `secret.key`), `tls` (`{"ca.key": PEM, "ca.crt": PEM}`, or
`null` without Forgeyard's own CA), `created_at` and `server_version`. `secrets restore` reads
version 1 only, refuses KDF parameters below these or above 1 GiB of memory, 16 passes or 16 lanes,
and checks that the key matches its id and the CA's key matches its certificate before it writes
anything.

### The agent

Installed with the one-line install, the agent's command is `forgeyard` (in `/usr/local/bin`); from
a checkout it is `uv run --no-sync python -m forgeyard`. Every command takes the flags of the
[agent settings](#agent-settings), before or after the command's name, and `-v` (`--verbose`) for debug output.
`--fingerprint` is another name for `--ca-fingerprint`. In the agent's Docker image the command is
already `forgeyard agent`, so add only the rest (`… forgeyard-agent:<version> inventory`).

| Command | What it does |
|---|---|
| `forgeyard agent` | Run the agent (the service runs `forgeyard agent --config /etc/forgeyard/agent.toml`). With a join token and no credential yet, it joins first; `--rejoin` joins again as a new machine |
| `forgeyard agent init [--print] [--force]` | Write `/etc/forgeyard/agent.toml` from what is detected (GPUs, Docker or Podman, systemd, an app account), and print it. `--print` only prints. Flags given here (`--server`, `--ca-fingerprint`, `--model-cache`…) go into the file |
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
| `--workload-user USER` | The account processes run as (default `forgeyard-run`, created when missing) |
| `--no-start` | Install and join, but do not start the service |
| `--rejoin` | Join again as a new machine |
| `--uninstall [--purge]` | Remove the service and the program; `--purge` also deletes the settings and the credential |
| `-h`, `--help` | The options |

Run again, it upgrades the agent to the server's version and keeps the settings and the credential.
It needs Linux on x86_64 or aarch64, root, `curl`, and access to PyPI for the agent's few
dependencies. It uses `uv` when it is installed, else a Python 3.13 or newer that is already there,
else it installs `uv` into `/opt/forgeyard/bin`, which brings its own Python. Without systemd it
installs everything and prints how to start the agent.

What it downloads is checked (1.4.5): the agent and the list of its dependencies come from the server,
each checked against the SHA-256 the script names; the dependencies come from PyPI at the versions the
server's release was tested with, each file checked against its published hash (`--require-hashes`,
nothing else installed with them); and the `uv` it installs is a fixed release (0.9.24) downloaded from
GitHub and checked against its SHA-256, not an install script. A machine that reaches PyPI through a
mirror (`UV_INDEX_URL`, `PIP_INDEX_URL`) still gets the same files, or none.

## Built-in recipes

A recipe's variants are tried in order; the first one whose `when` matches a card (or the CPU) and
whose runtime works on the machine runs the replica. A deployment cannot pick a variant; a custom
recipe with only the variants you want can.

| Recipe | Variant | Runs on | Image or release |
|---|---|---|---|
| `vllm`, `vllm-pooling` | `cuda`, `cuda-12.9`, `rocm` | NVIDIA (compute capability 7.0+, driver 580+, or 575+ for `cuda-12.9`); AMD `gfx90a`, `gfx942`, `gfx950`, `gfx1100`, `gfx1101`, `gfx1150`, `gfx1151`, `gfx1200`, `gfx1201` | `vllm/vllm-openai:v0.32.0`, `-cu129`, `vllm/vllm-openai-rocm:v0.32.0` |
| `llama-cpp` | `cuda` | NVIDIA, compute capability 5.0+, driver 570+ | `ghcr.io/ggml-org/llama.cpp:server-cuda-v1.0.0-b12359-b1b770d` (CUDA 12.8) |
| | `rocm` | AMD `gfx908`, `gfx90a`, `gfx942`, `gfx1030`, `gfx1100`, `gfx1101`, `gfx1102`, `gfx1200`, `gfx1201` | `…:server-rocm-v1.0.0-b12359-b1b770d` (ROCm 7.2.1) |
| | `vulkan` | any other AMD GPU: APUs (`gfx1151`, `gfx1150`, `gfx1103`, …) and cards the ROCm image lacks. Only the card's render node, no `/dev/kfd` | `…:server-vulkan-v1.0.0-b12359-b1b770d` (Mesa RADV) |
| | `cpu`, `cpu-process` | x86_64 and aarch64 in a container; x86_64 as a process without Docker | `…:server-v1.0.0-b12359-b1b770d`; release `v1.0.0` (its archives are build `b12359`'s), `llama-b12359-bin-ubuntu-x64.tar.gz`, checked against its SHA-256 (glibc 2.34 or newer: Ubuntu 22.04 and later, Debian 12 and later) |
| `faster-whisper` | `cuda`, `cuda-12.4`, `cpu` | NVIDIA, CPU | `ghcr.io/speaches-ai/speaches:0.8.3-*` |
| `speaches-tts` (1.3) | `cpu` | CPU (in a container), on any machine | the same `ghcr.io/speaches-ai/speaches:0.8.3-cpu` image as faster-whisper. CPU only: Kokoro and Piper run on ONNX Runtime and make speech faster than it plays there; in the CUDA image Kokoro still used the CPU |
| `vllm-omni-tts` (1.3) | `cuda` | NVIDIA, compute capability 7.5+, driver 580+ (one GPU) | `vllm/vllm-omni:v0.32.0` (CUDA 13.0) |
| `vllm-omni-image` (1.3) | `cuda` | NVIDIA, compute capability 7.5+, driver 580+ (one GPU) | the same `vllm/vllm-omni:v0.32.0` |
| `vllm-omni-video` (1.3) | `cuda` | NVIDIA, compute capability 7.5+, driver 580+ (one GPU) | the same `vllm/vllm-omni:v0.32.0` |
| `sd-cpp` (1.3) | `cuda` | NVIDIA, compute capability 5.0+, driver 560+ | `ghcr.io/leejet/stable-diffusion.cpp:master-cuda` (build `3bad892`, CUDA 12.6) |
| | `vulkan` | every AMD GPU and APU. Only the card's render node, no `/dev/kfd` | `…:master-vulkan` (build `32d516c`, Mesa RADV) |
| | `cpu-process` | x86_64 as a process without Docker (glibc 2.39 or newer: Ubuntu 24.04, Debian 13) | release `master-1043-3be2378`, `sd-master-3be2378-bin-Linux-Ubuntu-24.04-x86_64.zip`, checked against its SHA-256. Needs a 1.3 agent (it unpacks `.zip`) |
| `vision` (1.4) | `cuda` | NVIDIA, compute capability 7.5+, driver 580+ (one GPU, shared by memory) | Forgeyard's own `docker.io/tonyhuang1101/forgeyard-vision:1.4.4rc1-cuda` (torch +cu130) |
| | `cpu` | x86_64 and aarch64 in a container, on any machine (also AMD ones: there is no ROCm build yet) | `…/forgeyard-vision:1.4.4rc1` (linux/amd64 and arm64) |

`vllm-pooling` serves a sequence-classification model (`…ForSequenceClassification`) as a reranker only
when it has one output, as vLLM decides: `id2label` in its `config.json` has one entry, or `num_labels` is 1
(1.4.3, #147). One with two labels or more is a text classifier (task `classify`, 1.4.4): inspecting or adding
it as a reranker answers 422 `cannot_inspect` with its labels. A reranker that is a language model
(Qwen3-Reranker) is not affected.

As a text classifier (1.4.4, #144), `vllm-pooling` takes an architecture vLLM 0.32.0 runs natively with a
classification head: `BertForSequenceClassification`, `RobertaForSequenceClassification`,
`XLMRobertaForSequenceClassification`, `ModernBertForSequenceClassification`,
`GteNewForSequenceClassification`, `GPT2ForSequenceClassification`, `JambaForSequenceClassification`,
`LlamaBidirectionalForSequenceClassification`. vLLM's pooling runner picks the classify task by itself for
them (no flag). Refused when added, with the reason (422 `cannot_inspect`): one output (a reranker), a
regression head, a zero-shot (NLI) model (labels such as entailment and contradiction) and any other
architecture (DeBERTa, DistilBERT, BART: the transformers engine in 1.5). A repository without a
`config.json` to read (GGUF, ONNX) answers 422 `not_a_text_classifier`. A model added as another kind
cannot become a text classifier (its labels were not read: add it again), and a text classifier cannot
become a reranker.

The context of a deployment that names none starts at the model's own maximum when that is shorter than
the recipe's default (1.4.4): a 512-token BERT classifier gets `--max-model-len 512`, not 8192. RoBERTa and
XLM-RoBERTa models count their positions after the padding index, so 514 position embeddings hold 512
tokens (BGE-M3: 8192).

`llama-cpp` serves text to speech too, with a TTS GGUF and its projector, on every variant above.

`llama-cpp` runs llama.cpp **1.0.0** (1.3.1), its first release with a semantic version. The release's
images and archives are those of the nightly build of the same commit, `b12359`, so the image tags
name the version, the build and the commit (`server-vulkan-v1.0.0-b12359-b1b770d`), and
`llama-server --version` says `1.0.0-dev (build 12359)`. Until 1.3.0 it ran build `b12242`. Every flag
the recipe passes is the same in both. Its *KV cache type* (`kv_cache`, under **Advanced**) is `f16`
by default; `f8_e4m3` (FP8, new in llama.cpp 1.0.0) stores the KV cache in one byte a value instead of
two, so the estimate's KV cache halves: for Qwen3-4B at a context of 8,192, 1.125 GiB a sequence
becomes 0.5625 GiB (576 MiB less a concurrent request). FP8 has kernels for the CPU, CUDA and ROCm;
llama.cpp has none for Vulkan, so leave it at `f16` there. Some architectures refuse it (MLA models
such as DeepSeek), and the replica then fails to start: set it back to `f16`.

stable-diffusion.cpp publishes its images only under tags that move with every build (`master-vulkan`,
`master-cuda`). The recipe pins the builds that were checked by digest; `scripts/pin_images.py` keeps
them (it reports that the tag moved on as information) unless it is run with `--move-rolling`. Its later
Linux releases are built on Ubuntu 26.04 (glibc 2.43), which is why the CPU process is an earlier build;
its OpenAI API is the same. `sd-server` has no API key: an agent without the key guard (before 1.1) gets
no `sd-cpp` replica. The first CUDA pin (build `465adf3`) crashed with Z-Image-Turbo on an A100
([#43](https://github.com/xianhong1208/forgeyard/issues/43)); build `3bad892` replaced it.

Every image is pinned by digest as well (`tag@sha256:…`); the tag is what the pages show.
`scripts/pin_images.py` records in `forgeyard/recipes/images.lock.json` the ids a correct copy of each
image can show (index, manifest and config digests, and a content id per platform), so a copy loaded
with `docker load` on a machine without registry access is checked and used:
[Offline machines](how-to.md#offline-machines).

A variant's fields: `name`, `when` (`vendor`: `nvidia`, `amd` or `cpu`; `arch`: AMD gfx targets or
CPU architectures; `compute_capability` and `driver`: comparisons such as `>=8.0`), `runtime`
(`container` or `process`), `image` and `entrypoint` (container), `engine` (`url`, `sha256`,
`binary`: a release the agent downloads) or `command` (an installed program) for a process, `args`
and `env` to replace the recipe's, and `amd_kfd` (AMD containers; `true` by default: `/dev/kfd` and
the card's render node; `false`: the render node only, for engines that use only Vulkan), and `unsupported` (1.3.1: parameter
values the variant cannot run, such as `{kv_cache: [f8_e4m3]}`; a deployment with one of them is never
placed on it, and the placement says why).

The `vision` recipe (1.4) runs Forgeyard's own vision server with `--task {task}` (the model's task,
a placeholder of its own) and `--model {model_path}`. Its image is pinned by digest like every other
engine's (1.4.1), so a loaded copy is checked by its recorded ids and a machine without registry access
gets it through the server. A server upgrade does not change it: when a release changes the vision server,
the pin moves to the new image in a later version. A custom recipe may still name the image of the
server's own version, tag only, with `forgeyard_image: vision` (or `vision-cuda`) instead of `image` (how
1.4.0's `vision` recipe named it); such an image is neither checked nor relayed. A model is a vision model when its
`config.json` names an `…ForObjectDetection` architecture (detect: RT-DETR, D-FINE, DETR without a timm
backbone…), SAM 2 or 2.1 (segment) or an `…ForDepthEstimation` one (depth: Depth Anything, DPT…). A
Hugging Face vision model is only added with a permissive licence (Apache-2.0, MIT, BSD): another
licence, or none, answers 422 `not_a_vision_model` (Depth Anything V2 Base and Large are CC-BY-NC-4.0,
Ultralytics' YOLO checkpoints AGPL-3.0). A zero-shot (open-vocabulary) detector, one of transformers'
zero-shot detection models known by its `model_type` or architecture (Grounding DINO, MM-Grounding-DINO and
LLMDet, OmDet-Turbo, OWL-ViT, OWLv2), is a detect model marked `zero_shot` (1.4.4, #142; 1.4.3 refused them,
and one added before 1.4.3 is known by the config the catalog kept, with no migration). OmDet-Turbo is refused
like DETR with a timm backbone: its transformers port loads its backbone through timm (422 `not_a_vision_model`
when added, or when one kept from before 1.4.3 is deployed). An image classifier (1.4.4, #141, task
`image-classify`) is a model whose `config.json` names an `…ForImageClassification` architecture (fixed classes:
ViT, ConvNeXt V2, DINOv2…; a timm checkpoint, `timm_wrapper`, is refused) or a CLIP-like one (marked `zero_shot`:
CLIP, SigLIP, SigLIP 2, ALIGN, AltCLIP, Chinese-CLIP, MetaCLIP 2), served as `vision-classify`. Zero-shot
detectors and image classifiers run on a vision image from 1.4.4 on: on a recipe whose `forgeyard-vision`
image is older, deploying or estimating one answers 422 `vision_image_too_old`, naming both versions (a
pre-release such as `1.4.4rc1` counts as the release it leads to, 1.4.4); an image of another name (a local
build) is taken as it is. OpenCLIP's own weights beside a CLIP repository's transformers ones (`open_clip_*`)
are neither read nor downloaded.
Estimate: the files plus 1 GiB on a GPU (and the CUDA context),
1.25 GiB of RAM on a CPU, measured with the three recommended models (peak 0.66 to 1.12 GiB on a CPU);
Grounding DINO and MM-Grounding-DINO add 512 MiB (their text encoder and 800-pixel features: peak 2.24 GiB on a
CPU with 0.64 GiB of files, 1.4.4).

A video recipe (1.3) has a `video` block: `request: vllm-omni` (vLLM-Omni's asynchronous jobs) and
`frames_step` (4: the engine makes k x 4 + 1 frames). It serves only the `video` task, as
`openai-videos`, and its `{keep_seconds}` placeholder is the deployment's `keep_minutes` and an hour.

## Container images

Each release publishes two images on Docker Hub, built by CI from the release's tag (and the vision
server's, `docker.io/tonyhuang1101/forgeyard-vision`, `X.Y.Z` and `X.Y.Z-cuda`, which the `vision` recipe
pins by digest):

| Image | What | Platforms |
|---|---|---|
| `docker.io/tonyhuang1101/forgeyard` | The server (`Dockerfile.server`) | linux/amd64, linux/arm64 |
| `docker.io/tonyhuang1101/forgeyard-agent` | The agent (`Dockerfile.agent`) | linux/amd64, linux/arm64 (AMD GPUs: amd64 only) |

- **Tags**: `1.4.4` (exactly that release), `1.4` (its newest patch release), `latest` (the newest
  release). A pre-release only gets its own version. Each image carries OCI labels (source,
  licence, version, revision), its build provenance and an SBOM.
- **The server image** runs `python main.py` as the account `forgeyard` (uid 10001), with
  `DATA_DIR=/data` (a volume), `HOST=0.0.0.0` and `PORT=7860` (exposed), and a health check on
  `/api/health`. [Run the server in Docker](how-to.md#run-the-server-in-docker).
- **The agent image** runs `forgeyard agent` behind `tini`, as root (it drives the host's Docker),
  with `FORGEYARD_LIMITS=rlimit`, the app account `forgeyard-run` (uid 990) and a volume at
  `/var/lib/forgeyard-agent`. [Run the agent in Docker](how-to.md#run-the-agent-in-docker).
- **Which agent image a server names**: [`AGENT_IMAGE`](#machines), else the published one at the
  server's version. When nothing is published (the namespace in `forgeyard/install/images.py` is
  empty) and `AGENT_IMAGE` is not set, it names a local build, `forgeyard-agent:<version>`, and
  **Add a machine** shows how to build it.

## Files and directories

On the server, in its folder (in the server image: in the `/data` volume, without `data/`):

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
