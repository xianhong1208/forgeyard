"""The agent <-> server protocol, shared by both sides.

Transport: the agent dials out over WebSocket to /agent/v1/stream and authenticates with
`Authorization: Bearer <machine credential>`. Every message is one JSON object with the
protocol version `v` and a `type`:

agent -> server
  hello      first message on every connection: agent version, host, labels, whether the
             inventory is simulated, and the full inventory
  heartbeat  every 10 s: only the values that change (free/used memory, utilisation, temperature)
  inventory  the full inventory again, sent when the hardware changed

  status     (M2) replica states: after every desired, and whenever one changes. `full` lists
             every replica the agent knows. (M3) a replica may carry `download` progress, and the
             message `model_cache` (free space, the cached models) when that changed. (1.3.1) an
             image pull is reported as a `download` with phase "image" (see "pull_progress"). (1.4)
             `model_cache.engines` lists the engine archives unpacked there (first 16 hex digits of
             their SHA-256); older servers ignore it
  logs       (M2) answer to a logs_request: the last lines of a replica's output
  image_loaded (1.4) answer to image_load: ok or why not, and each image loaded (id, tags, content id)
  cache_removed (1.4) answer to cache_remove / cache_prune: ok, the bytes freed or why not, and the
             model cache as it is afterwards. (1.4) `model_cache` also lists each model with its size
             (`models`, `engines_detail`, `used_bytes`, `partial_bytes`); older servers ignore them
             (1.4, "prefetch") `status.downloads` reports each download asked for in `desired.downloads`:
             {id, state (queued | downloading | done | failed | cancelled), reason, download}

server -> agent
  welcome    answer to hello: machine id and name, heartbeat interval, offline threshold,
             and (M2) the features the server speaks
  desired    (M2) every replica this machine should run, with its full spec and generation; sent
             right after welcome and whenever it changes. The agent converges on it; it is
             idempotent, so a restart or reconnect just converges again. (1.4, "prefetch") beside the
             replicas, `downloads`: what to download into the model cache without starting anything
  logs_request (M2) the last N lines of one replica's output
  cache_remove / cache_prune (1.4, "cache_admin") remove one model, or the partial downloads, from
             the agent's model cache; answered by `cache_removed` (agent -> server)
  image_load (1.4, "image_load") an admin uploaded an image file to this machine: fetch it from `path`
             on the server and load it into the container engine; answered by `image_loaded`
  error      a refused message; the server closes the connection after it

HTTP (1.4, "relay"): GET /agent/v1/relay/{replica|download}/{id}/model[/{file path}] and …/engine, with
the machine credential as a Bearer token and an optional `Range`: the bytes of a model file or engine
archive the machine's desired state names, streamed from their source by the server, stored nowhere.
(1.4, "relay_images") …/image/manifest and …/image/blobs/<digest>: a pinned engine image's manifest for the
machine's platform, and its config and layers by digest. (1.4, "image_load") GET
/agent/v1/image-upload/<ticket>: the bytes of an image file an admin is uploading to this machine.

Models (M3) extend it the same way: an agent started with a model cache lists "models"; only
then does the server place model deployments there, and its desired replicas carry a rendered
`spec`, the `model` to fetch (with a token for gated repositories, never stored by the agent) and
an `engine` for process variants.

The gateway (M4) adds "replica_keys": a desired model replica then carries `replica_key` ({env,
value}), the internal key its engine must require. The agent keeps it in memory only, puts it in the
engine's environment (never on a command line) and sends it with the replica's health checks.
An agent that also lists "replica_guard" puts its key guard in front of every replica that has a key
(the engine on loopback, the guard on the published port, every path requiring the key); such an
agent gets a key for every model replica, with `env` null when the recipe's engine reads none.

An agent that lists "image_ids" can run an engine image loaded with `docker load` on a machine that
cannot reach the registry: a desired replica whose image is in forgeyard/recipes/images.lock.json then
carries `image_ids` ({ids, content}: the digests and content ids a correct copy can show), next to
`spec` and never inside it, which older agents validate with extra="forbid". They get no such key.

An agent that lists "workload_guard" (1.3) also guards plain service workloads exposed at /x/<name>:
their desired replica carries `replica_key` with `env` null and `strip` true, and its guard removes
the internal key before a request reaches the program. Exposed workloads are placed only on such
agents.

Workloads extend version 1 compatibly instead of bumping it. Each side lists its `features` (the
agent in hello, the server in welcome), and `desired` / `status` / `logs` flow only when both list
"workloads". An M1 agent (no features) keeps working with this server and is never sent work; an
M2 agent talking to an M1 server never sends status, which that server would refuse.

Close codes the agent acts on (4000-4999 are application codes):
  4001 revoked or removed: the credential is dead, stop
  4002 protocol version not supported: upgrade the agent, stop
  4003 a message the server could not accept: reconnect
  4008 the server heard nothing for the offline threshold: reconnect
  4009 another agent connected with the same credential: stop
  1012 the server is restarting: reconnect
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

STREAM_PATH = "/agent/v1/stream"
JOIN_PATH = "/agent/v1/join"

CLOSE_REVOKED = 4001
CLOSE_PROTOCOL = 4002
CLOSE_BAD_MESSAGE = 4003
CLOSE_TIMEOUT = 4008
CLOSE_REPLACED = 4009
CLOSE_RESTART = 1012

# The agent stops on these instead of reconnecting.
FATAL_CLOSE_CODES = {CLOSE_REVOKED, CLOSE_PROTOCOL, CLOSE_REPLACED}

# Largest single message the server accepts (a full inventory is a few KB).
MAX_MESSAGE_BYTES = 512 * 1024

VENDORS = ("nvidia", "amd", "cpu")

FEATURE_WORKLOADS = "workloads"
FEATURE_MODELS = "models"
# M4: the agent gives model replicas their internal key (environment only, memory only) and sends it
# with health checks. Only agents that list it are sent keys; replicas elsewhere run without one.
FEATURE_REPLICA_KEYS = "replica_keys"
# The agent guards each keyed replica's published port itself (agent/workloads/guard.py).
FEATURE_REPLICA_GUARD = "replica_guard"
# The agent checks a loaded image's tag against the recorded ids of a pinned image (workloads/images.py)
# before it runs it without a pull. Only agents that list it are sent the ids.
FEATURE_IMAGE_IDS = "image_ids"
# 1.3: the agent unpacks process engines published as .zip archives (stable-diffusion.cpp's releases),
# not only .tar.gz. A process variant whose engine is a .zip goes only to agents that list it.
FEATURE_ENGINE_ZIP = "engine_zip"
# 1.3: the agent's guard also fronts exposed workloads (/x/<name>) and strips the internal key from
# what it passes on (`replica_key.strip`). Exposed workloads are placed only on agents that list it.
FEATURE_WORKLOAD_GUARD = "workload_guard"
# 1.3.1: the server shows image pull progress. Listed by the server only: an agent then reports a pull
# like a download (`download.phase` "image", with `layers` / `layers_done` and `unpacking`, and the
# reason "pulling the image: …"). To an older server, which records every new reason as an event, the
# agent says only "pulling the image".
FEATURE_PULL_PROGRESS = "pull_progress"
# 1.4: the agent lists the models in its cache with their sizes (`model_cache.models`, `engines_detail`,
# `used_bytes`, `partial_bytes`) and removes one on request: `cache_remove {request_id, key}` or
# `cache_prune {request_id}` (the partial downloads), answered by `cache_removed {request_id, ok,
# freed_bytes, code, reason, in_use, model_cache}`. Listed by both sides; only an agent that lists it is
# asked. The agent refuses ("in_use") while a replica there uses the files, and ("busy") while one of
# their downloads runs.
FEATURE_CACHE_ADMIN = "cache_admin"
# 1.4 ("Download to…", issue #35): the server lists downloads beside the replicas in `desired`:
# `downloads: [{id, model, engine?, image?, image_ids?}]` (a Hugging Face model's `token` inside `model`, as
# for replicas: memory only). The agent fetches each into its model cache with the code a replica uses (the
# same locks, resume, checksums and cancel; an image with the 1.3.1 pull), starts nothing, and reports
# `status.downloads: [{id, state, reason, download}]`. A download missing from the next list is cancelled.
# Listed by both sides; only an agent that lists it is sent downloads, and only to a server that lists it
# does the agent report them.
FEATURE_PREFETCH = "prefetch"
# 1.4 (issue #35, D1/D3): the server relays model files and engine archives to a machine that cannot reach
# their source, storing nothing. To an agent that lists it, on a machine whose `fetch_via` is "auto" or
# "server", a desired replica's or download's `model` and `engine` blocks carry `relay: {path, mode}`: the
# path on the server (RELAY_PATH/<replica|download>/<id>/<model|engine>; a Hugging Face file is
# `<path>/<file path>`) and the mode, "server" (always through the server: the model block then carries no
# `token`, the server adds its own) or "auto" (directly first, through the server after a network failure).
# The agent GETs `<its own server URL><path>` with its machine credential and the CA it pins, sending only
# `Range`; the server serves only what that machine's desired state names, from the source the catalog or
# recipe gives (never a URL from the agent). The agent's resume, retries, cancel and checks are unchanged.
# Progress through the server says `download.via` "server". Listed by both sides.
FEATURE_RELAY = "relay"
RELAY_PATH = "/agent/v1/relay"
# 1.4 (issue #35 PR 4, D4): engine images through the server too. To an agent that lists it (and "relay"), a
# desired replica's or download's *pinned* image (one in forgeyard/recipes/images.lock.json) gets
# `image_relay: {path, mode}` beside `spec` (RELAY_PATH/<replica|download>/<id>/image), with the modes of
# "relay". The agent GETs `<path>/manifest` (its platform's manifest, which must hash to a recorded manifest
# digest) and `<path>/blobs/<digest>` (the config, then each layer, resumable, each checked against its
# digest), builds an archive `docker load` / `podman load` accept, loads it and runs the "image_ids" check on
# what it loaded. The server serves only the digests that manifest names. Tag-only images never get it.
# Progress says `download.via` "server". Listed by both sides.
FEATURE_RELAY_IMAGES = "relay_images"
# 1.4 (issue #35 PR 4): an admin uploads an image file (`docker save`, optionally gzip) to a machine from the
# console. The server sends `image_load {request_id, path, size, name}`; the agent GETs `<its server><path>`
# (IMAGE_UPLOAD_PATH/<ticket>, with its credential), streams the body into `docker load` / `podman load`
# and answers `image_loaded {request_id, ok, reason, images: [{id, tags, content, os, architecture}]}`. The
# server keeps nothing but a small buffer. Listed by both sides; only an agent that lists it is asked.
FEATURE_IMAGE_LOAD = "image_load"
IMAGE_UPLOAD_PATH = "/agent/v1/image-upload"
SERVER_FEATURES = (FEATURE_WORKLOADS, FEATURE_MODELS, FEATURE_REPLICA_KEYS, FEATURE_PULL_PROGRESS,
                   FEATURE_CACHE_ADMIN, FEATURE_PREFETCH, FEATURE_RELAY, FEATURE_RELAY_IMAGES, FEATURE_IMAGE_LOAD)
# How a machine fetches models and engines (nodes.fetch_via, 1.4): directly first and through the server after
# a network failure; always through the server; never through the server.
FETCH_VIA = ("auto", "server", "direct")
# What an agent reports of a download (1.4, prefetch). "queued": waiting for another download on that machine.
DOWNLOAD_STATES = ("queued", "downloading", "done", "failed", "cancelled")
RUNTIMES = ("process", "container")

# What the agent reports per replica. Server-only states: "stopping" (asked, not confirmed yet).
REPLICA_STATES = ("pending", "starting", "running", "healthy", "unhealthy", "backoff", "exited", "failed",
                  "stopped")
# Largest log tail one answer carries (the message must stay under MAX_MESSAGE_BYTES).
MAX_LOG_CHARS = 200_000
