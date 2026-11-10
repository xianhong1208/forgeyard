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
             message `model_cache` (free space, the cached models) when that changed
  logs       (M2) answer to a logs_request: the last lines of a replica's output

server -> agent
  welcome    answer to hello: machine id and name, heartbeat interval, offline threshold,
             and (M2) the features the server speaks
  desired    (M2) every replica this machine should run, with its full spec and generation; sent
             right after welcome and whenever it changes. The agent converges on it; it is
             idempotent, so a restart or reconnect just converges again
  logs_request (M2) the last N lines of one replica's output
  error      a refused message; the server closes the connection after it

Models (M3) extend it the same way: an agent started with a model cache lists "models"; only
then does the server place model deployments there, and its desired replicas carry a rendered
`spec`, the `model` to fetch (with a token for gated repositories, never stored by the agent) and
an `engine` for process variants.

The gateway (M4) adds "replica_keys": a desired model replica then carries `replica_key` ({env,
value}), the internal key its engine must require. The agent keeps it in memory only, puts it in the
engine's environment (never on a command line) and sends it with the replica's health checks.

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
SERVER_FEATURES = (FEATURE_WORKLOADS, FEATURE_MODELS, FEATURE_REPLICA_KEYS)
RUNTIMES = ("process", "container")

# What the agent reports per replica. Server-only states: "stopping" (asked, not confirmed yet).
REPLICA_STATES = ("pending", "starting", "running", "healthy", "unhealthy", "backoff", "exited", "failed",
                  "stopped")
# Largest log tail one answer carries (the message must stay under MAX_MESSAGE_BYTES).
MAX_LOG_CHARS = 200_000
