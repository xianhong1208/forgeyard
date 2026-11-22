"""The agent <-> server protocol, shared by both sides.

Transport: the agent dials out over WebSocket to /agent/v1/stream and authenticates with
`Authorization: Bearer <machine credential>`. Every message is one JSON object with the
protocol version `v` and a `type`:

agent -> server
  hello      first message on every connection: agent version, host, labels, whether the
             inventory is simulated, and the full inventory. (1.4.2) `runtime_info.ports` ({low, high}):
             the host port range replicas are published on, which placement counts; older servers drop it.
             (1.4.2) `install`: how the agent runs (agent/how_it_runs.py: its container's `docker run`
             options, install.sh, or a checkout), for its update command; no secrets. Older servers ignore it.
             (1.4.5) `runtime_info.address`: the address replicas are published on (what the gateway dials), and
             `runtime_info.connection` ({address, port}): the agent's own end of this connection, which the
             server compares with the peer it sees (same: the agent runs on the server's host) and does not
             keep. Older servers drop both
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
  relay_sent (1.4.1) answer to relay_send: how the send of a cached model file ended (ok, or not_found |
             busy | corrupt | failed, and why)
  image_listed (1.4.1) answer to image_list: the images in the agent's container engine
  image_saved (1.4.1) answer to image_save: how the send of an image ended, and which image it was
  image_removed (1.4.2) answer to image_remove: removed (the image, the bytes freed, the space after) or why not
  cert_request (1.4.5, "guard_tls") a CSR for the key the agent's replica guards serve TLS with
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
  relay_send (1.4.1, "relay_source") another machine needs a model file this agent has in its cache: send it
             to `path` on the server; answered by `relay_sent`
  image_list / image_save (1.4.1, "image_save") list the images in the container engine; `docker save` one
             of them to `path` on the server (an admin copies it to another machine); answered by
             `image_listed` / `image_saved`
  image_remove (1.4.2, "image_remove") remove one engine image by its id (an admin asked); answered by
             `image_removed`
  certificate (1.4.5, "guard_tls") answer to cert_request: the signed certificate chain, or why not
  error      a refused message; the server closes the connection after it

HTTP (1.4, "relay"): GET /agent/v1/relay/{replica|download}/{id}/model[/{file path}] and …/engine, with
the machine credential as a Bearer token and an optional `Range`: the bytes of a model file or engine
archive the machine's desired state names, streamed from their source by the server, stored nowhere.
(1.4, "relay_images") …/image/manifest and …/image/blobs/<digest>: a pinned engine image's manifest for the
machine's platform, and its config and layers by digest. (1.4, "image_load") GET
/agent/v1/image-upload/<ticket>: the bytes of an image file an admin is uploading to this machine.
(1.4.1, "relay_source") POST /agent/v1/relay-send/<ticket>: the bytes of a cached model file, from the machine
that has it, which the server pipes into another machine's relay GET. (1.4.1, "image_save") POST
/agent/v1/image-send/<ticket>: an image `docker save` writes, chunked, which the server pipes into another
machine's image-upload GET.

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

Mixed versions (1.4.5, #170): each side ignores a message type it does not know (AGENT_MESSAGES on the server,
SERVER_MESSAGES on the agent), with one warning per type and connection, and the server leaves out an accelerator
whose `vendor` it does not know (warning, and an "unknown vendor" detection note on the machine) instead of refusing
the hello. A known message that does not validate, or one sent without the feature it needs, is still refused
(4003).

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

# 1.4.5 (#170): the message types each side knows. A side ignores a type it does not know, with a warning, so an
# agent newer than its server (or older) keeps working: what it adds is gated by `features` anyway. The same holds
# for a card of a `vendor` outside VENDORS: the server leaves that card out and keeps the rest of the hello.
AGENT_MESSAGES = ("hello", "heartbeat", "inventory", "status", "logs", "cache_removed", "image_loaded", "relay_sent",
                  "image_listed", "image_saved", "image_removed", "cert_request")
SERVER_MESSAGES = ("welcome", "error", "desired", "logs_request", "cache_remove", "cache_prune", "image_load",
                   "relay_send", "image_list", "image_save", "image_remove", "certificate")

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
# 1.4.1 (issue #104): a machine that gets its files through the server gets a model file from another machine
# that has it in its cache, still through the server, before the internet. An agent that lists it may be asked
# `relay_send {request_id, path, item, offset, length}`: `item` names one verified file of its cache ({kind: "hf",
# repo, revision, file, blob, size} or {kind: "url", dir, sha256, size}), which the agent looks up only among what
# it found on its own disk. It POSTs bytes offset..offset+length-1 of it to `<its own server><path>`
# (RELAY_SEND_PATH/<ticket>, its machine credential, the CA it pins, Content-Length = length), and answers
# `relay_sent {request_id, ok, code, reason, bytes}` (code not_found | busy | corrupt | failed). The server
# pipes the body into the other machine's relay GET (unchanged), hashing whole files on the way; a source whose
# bytes do not match is not asked for that file again for an hour. Listed by both sides; only an agent that
# lists it is asked, at most 2 sends at a time.
FEATURE_RELAY_SOURCE = "relay_source"
RELAY_SEND_PATH = "/agent/v1/relay-send"
# 1.4.1 (issue #104, Q4): an admin copies an engine image from one machine to another. To an agent that lists it
# (it has a container engine), the server sends `image_list {request_id}`, answered by `image_listed {request_id,
# ok, reason, images: [{id, tags, content, size, created, os, architecture}]}`, and `image_save {request_id, path,
# image}`: the agent saves the image whose id is `image` (one of its own: it never saves a name the server sends)
# with its own tags, `docker save` / `podman save`, and POSTs the archive, chunked, to `<its own server><path>`
# (IMAGE_SEND_PATH/<ticket>, its credential, the CA it pins); it answers `image_saved {request_id, ok, reason,
# bytes, image}`. The server pipes the body into the other machine's "image_load" (unchanged), keeping nothing.
# Listed by both sides; only an agent that lists it is asked.
FEATURE_IMAGE_SAVE = "image_save"
IMAGE_SEND_PATH = "/agent/v1/image-send"
# 1.4.2 (issue #123): an admin removes an engine image a machine no longer uses. To an agent that lists it (it has a
# container engine), `image_listed` also carries `agent_image` (the id of the image the agent runs from, when it runs
# in a container of that engine) and `free_bytes` / `total_bytes` (where the engine keeps its images, when the agent
# can see it). The server sends `image_remove {request_id, image}`, `image` an id from that listing; the agent removes
# only an image of its own listing, never its own image, never one a replica there uses or any container of the
# engine uses (refused first; then `docker rmi` / `podman rmi` without -f, by the image's own tags when it has
# several), and answers `image_removed {request_id, ok, code, reason, in_use, image, freed_bytes,
# free_bytes, total_bytes}` (code not_found | agent_image | in_use | failed). The server offers only images a recipe
# names. Listed by both sides; only an agent that lists it is asked.
FEATURE_IMAGE_REMOVE = "image_remove"
# 1.4.5 (issue #163): TLS from the gateway to every replica. An agent that lists it (it has the `cryptography`
# package) serves its key guards with TLS once the server lists it too, with a certificate Forgeyard's CA signs:
#   - the welcome carries `replica_tls: {name, ca}`: the machine's binding name (the only name in its certificate,
#     security/replica_tls.py `binding_name`) and the SHA-256 fingerprint of the CA that signs it;
#   - the agent makes a new EC P-256 key (it never leaves the machine) and sends `cert_request {request_id, csr}`
#     when it has no certificate for that name and CA, or one with fewer than RENEW_BEFORE_DAYS left (checked at
#     every welcome and every hour); the server answers `certificate {request_id, ok, chain, name, not_after, ca}`
#     (the leaf, then the CA) or `{ok: false, code, reason}` (code too_often | refused | failed). The server takes
#     only the CSR's public key: the name and everything else in the certificate are its own;
#   - hello's `runtime_info.tls` ({serial, not_after, name, ca}) says which certificate the guards serve now.
# The gateway then dials that machine's replicas at https:// and accepts only a chain to Forgeyard's CA naming
# that machine's binding name. An agent without it (older than 1.4.5) is dialled over plain HTTP, and the setup
# advisor says so ("traffic to this machine is not encrypted: update the agent"). A server without it gets plain
# HTTP from every agent: the agent's guards serve TLS only when the server listed it in the welcome.
FEATURE_GUARD_TLS = "guard_tls"
SERVER_FEATURES = (FEATURE_WORKLOADS, FEATURE_MODELS, FEATURE_REPLICA_KEYS, FEATURE_PULL_PROGRESS,
                   FEATURE_CACHE_ADMIN, FEATURE_PREFETCH, FEATURE_RELAY, FEATURE_RELAY_IMAGES, FEATURE_IMAGE_LOAD,
                   FEATURE_RELAY_SOURCE, FEATURE_IMAGE_SAVE, FEATURE_IMAGE_REMOVE, FEATURE_GUARD_TLS)
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
