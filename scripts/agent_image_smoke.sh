#!/usr/bin/env bash
# Smoke test of the agent image (CI, after `docker build -f Dockerfile.agent -t IMAGE .`):
#   1. what is inside: the CLI, the docker CLI, the workload account, the version, numeric groups;
#   2. inventory (also as `IMAGE agent inventory`) and init in simulation (no GPU on the runner);
#   3. a real join: a Forgeyard server on this host, the image started the way the guide says
#      (host network, FORGEYARD_* variables, a state volume), the machine online; then a restart
#      of the container with the same command line keeps the machine (no second join).
#
#   scripts/agent_image_smoke.sh forgeyard-agent:ci
set -euo pipefail

IMAGE="${1:?usage: $0 IMAGE}"
PORT="${PORT:-7899}"
WORK="$(mktemp -d)"
NAME="fy-smoke-agent-$$"
VOLUME="fy-smoke-state-$$"
SERVER_PID=""

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
    [ -z "$SERVER_PID" ] || kill "$SERVER_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

ok() { printf 'ok   %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; [ -f "$WORK/server.log" ] && tail -n 40 "$WORK/server.log" >&2; exit 1; }

want_version="$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+)\"", open("forgeyard/__init__.py").read()).group(1))')"

# ------------------------------------------------------------------ 1. contents
docker run --rm "$IMAGE" --help | grep -q "forgeyard agent" || fail "--help"
ok "forgeyard agent --help"
got="$(docker run --rm --entrypoint forgeyard "$IMAGE" --version)"
[ "$got" = "forgeyard $want_version" ] || fail "version: $got, expected forgeyard $want_version"
ok "$got"
docker run --rm --entrypoint docker "$IMAGE" --version | grep -q "Docker version" || fail "docker CLI"
ok "docker CLI present"
docker run --rm --entrypoint id "$IMAGE" forgeyard-run | grep -q "uid=990" || fail "forgeyard-run account"
ok "forgeyard-run account"
docker run --rm --entrypoint python "$IMAGE" -c "import pynvml" || fail "nvidia-ml-py"
ok "nvidia-ml-py importable"
# What the host's libamd_smi.so dlopen()s (ROCm 7.0): without it amdsmi reads no ASIC information.
docker run --rm --entrypoint sh "$IMAGE" -c 'ldconfig -p | grep -q "libdrm.so.2 " && ldconfig -p | grep -q "libdrm_amdgpu.so.1 "' \
    || fail "libdrm in ldconfig"
docker run --rm --entrypoint python "$IMAGE" -c 'import ctypes; ctypes.CDLL("libdrm.so.2"); ctypes.CDLL("libdrm_amdgpu.so")' \
    || fail "dlopen libdrm.so.2 / libdrm_amdgpu.so"
ok "libdrm.so.2 and libdrm_amdgpu.so load (for amdsmi on AMD hosts)"
# The AMD command gives the host's video and render groups by number: a name is looked up in the
# image's /etc/group, which has no "render" group.
docker run --rm --group-add 4242 --entrypoint id "$IMAGE" | grep -q "4242" || fail "--group-add with a numeric gid"
ok "--group-add with a numeric gid (the AMD command)"

# ------------------------------------------------------------------ 2. inventory and init
docker run --rm -e FORGEYARD_AGENT_SIMULATION=1 "$IMAGE" inventory --json --simulate-inventory nvidia-a100x8 \
    > "$WORK/inv.json"
python3 - "$WORK/inv.json" <<'PY' || fail "inventory --json"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["simulated"] is True and sum(a["vendor"] == "nvidia" for a in d["accelerators"]) == 8, d
PY
ok "inventory --json (simulated 8 x A100)"
# The entrypoint already is `forgeyard agent`; a redundant `agent` before the subcommand works too.
docker run --rm -e FORGEYARD_AGENT_SIMULATION=1 "$IMAGE" agent inventory --simulate-inventory amd-gfx1151-apu \
    | grep -q "arch=gfx1151 .* rocm=" || fail "IMAGE agent inventory"
ok "IMAGE agent inventory (a redundant 'agent')"
docker run --rm -e FORGEYARD_AGENT_SIMULATION=1 "$IMAGE" init --print --simulate-inventory nvidia-a100x8 \
    --server https://192.168.1.20:7860 --ca-fingerprint "sha256:$(printf 'ab%.0s' $(seq 32))" > "$WORK/agent.toml"
grep -q '^limits = "rlimit"' "$WORK/agent.toml" || fail "init: limits"
grep -q '^user = "forgeyard-run"' "$WORK/agent.toml" || fail "init: workload user"
grep -q '^server = "https://192.168.1.20:7860"' "$WORK/agent.toml" || fail "init: server"
grep -q '8 x NVIDIA A100' "$WORK/agent.toml" || fail "init: GPUs"
ok "init --print"
docker run --rm -v "$WORK/agent.toml:/etc/forgeyard/agent.toml:ro" "$IMAGE" config > "$WORK/config.txt"
grep -q "^workloads.limits .* rlimit .*(env FORGEYARD_LIMITS)" "$WORK/config.txt" || fail "config: env over file"
grep -q "^server .*192.168.1.20.*(file)" "$WORK/config.txt" || fail "config: file"
ok "config: the file, the image's environment over it"

# ------------------------------------------------------------------ 3. a real join
uv sync --locked --quiet
DATA_DIR="$WORK/data" HOST=127.0.0.1 PORT="$PORT" ADMIN_USERNAME=smoke ADMIN_PASSWORD=quiet-meadow-4821 \
    uv run --no-sync python main.py > "$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 60); do
    [ -f "$WORK/data/tls/ca.crt" ] && curl -fsS --cacert "$WORK/data/tls/ca.crt" "https://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
    sleep 1
done
CA="$WORK/data/tls/ca.crt"
api() { curl -fsS --cacert "$CA" -b "$WORK/jar" -c "$WORK/jar" -H "X-Forgeyard: 1" -H "Content-Type: application/json" "$@"; }
api -X POST "https://127.0.0.1:$PORT/api/auth/login" -d '{"username":"smoke","password":"quiet-meadow-4821"}' >/dev/null \
    || fail "server sign-in"
api -X POST "https://127.0.0.1:$PORT/api/machines/join-tokens" > "$WORK/token.json" || fail "join token"
TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$WORK/token.json")"
FP="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ca_fingerprint"])' "$WORK/token.json")"
ok "server up, join token issued"

run_agent() {
    docker run -d --name "$NAME" --network host \
        -v /var/run/docker.sock:/var/run/docker.sock -v "$VOLUME:/var/lib/forgeyard-agent" \
        -e FORGEYARD_SERVER="https://127.0.0.1:$PORT" -e FORGEYARD_CA_FINGERPRINT="$FP" \
        -e FORGEYARD_JOIN_TOKEN="$TOKEN" -e FORGEYARD_NAME=smoke-agent "$IMAGE" >/dev/null
}

online() {  # prints the machine id once smoke-agent is online
    api "https://127.0.0.1:$PORT/api/machines" | python3 -c '
import json, sys
for m in json.load(sys.stdin)["machines"]:
    if m["name"] == "smoke-agent" and m["status"] == "online":
        print(m["id"]); break'
}

wait_online() {
    for _ in $(seq 40); do
        id="$(online)"
        [ -n "$id" ] && { echo "$id"; return 0; }
        sleep 1
    done
    docker logs "$NAME" >&2 || true
    return 1
}

run_agent
first="$(wait_online)" || fail "the agent container did not come online"
ok "joined over wss:// with the pinned CA, online as machine $first"
docker logs "$NAME" 2>&1 | grep -q "Runtime container ok: docker" || { docker logs "$NAME" >&2; fail "container runtime through the socket"; }
ok "container runtime: the host's Docker through the socket"

docker rm -f "$NAME" >/dev/null
sleep 2
run_agent   # the same command again (what --restart always does): the token is not used twice
second="$(wait_online)" || fail "the agent did not come back after a restart"
[ "$first" = "$second" ] || fail "a restart joined again as machine $second (was $first)"
docker logs "$NAME" 2>&1 | grep -q "join token is not used" || fail "restart: expected the 'already joined' notice"
count="$(api "https://127.0.0.1:$PORT/api/machines" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["machines"]))')"
[ "$count" = 1 ] || fail "$count machines after a restart, expected 1"
ok "recreated with the same command: same machine ($second), credential kept in the volume"
echo "agent image smoke test passed"
