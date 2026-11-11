#!/usr/bin/env bash
# Smoke test of the server image (CI, after `docker build -f Dockerfile.server -t IMAGE .`):
#   1. what is inside: not root, the version, the built web UI, the user guide, the labels;
#   2. a first start the way the guide says (a volume at /data, PUBLIC_URL, -p): HTTPS from the
#      server's own CA, the setup token printed in the log, setup finished through the API, the
#      HEALTHCHECK healthy, the install script served;
#   3. the container removed and started again on the same volume (an upgrade does exactly this):
#      the owner, the CA, secret.key and the data are still there, and no new setup token.
#
#   scripts/server_image_smoke.sh forgeyard:ci
set -euo pipefail

IMAGE="${1:?usage: $0 IMAGE}"
PORT="${PORT:-7898}"
WORK="$(mktemp -d)"
NAME="fy-smoke-server-$$"
VOLUME="fy-smoke-data-$$"
URL="https://127.0.0.1:$PORT"
PASSWORD="amber-harbor-7316"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

ok() { printf 'ok   %s\n' "$*"; }
fail() {
    printf 'FAIL %s\n' "$*" >&2
    docker logs "$NAME" 2>&1 | tail -n 40 >&2 || true
    exit 1
}

want_version="$(python3 -c 'import re; print(re.search(r"__version__ = \"(.+)\"", open("forgeyard/__init__.py").read()).group(1))')"

# ------------------------------------------------------------------ 1. contents
uid="$(docker run --rm --entrypoint id "$IMAGE" -u)"
[ "$uid" != 0 ] || fail "the server runs as root"
ok "runs as uid $uid, not root"
got="$(docker run --rm "$IMAGE" python main.py --version)"
[ "$got" = "forgeyard $want_version" ] || fail "version: $got, expected forgeyard $want_version"
ok "$got"
docker run --rm "$IMAGE" sh -c 'test -f forgeyard/static/index.html && test -f docs/guide/how-to.md && test -f docs/guide/zh-TW/how-to.md' \
    || fail "the built web UI or the user guide is missing"
ok "web UI and user guide in the image"
docker inspect -f '{{json .Config.Labels}}' "$IMAGE" > "$WORK/labels.json"
python3 - "$WORK/labels.json" <<'PY' || fail "OCI labels: $(cat "$WORK/labels.json")"
import json, sys
labels = json.load(open(sys.argv[1]))
assert labels["org.opencontainers.image.source"] == "https://github.com/xianhong1208/forgeyard", labels
assert labels["org.opencontainers.image.licenses"] == "Apache-2.0", labels
assert labels["org.opencontainers.image.version"] and labels["org.opencontainers.image.revision"], labels
PY
ok "OCI labels (source, licence, version, revision)"
docker inspect -f '{{json .Config.Healthcheck}}' "$IMAGE" > "$WORK/healthcheck.json"
grep -q "forgeyard.install.healthcheck" "$WORK/healthcheck.json" || fail "no HEALTHCHECK"
ok "HEALTHCHECK defined"

# ------------------------------------------------------------------ 2. first start
run_server() {
    docker run -d --name "$NAME" --hostname forgeyard-smoke -p "127.0.0.1:$PORT:7860" \
        -v "$VOLUME:/data" -e PUBLIC_URL="$URL" "$IMAGE" >/dev/null
}

# Copies the CA out of the volume once the server answers over HTTPS with it.
wait_up() {
    for _ in $(seq 60); do
        if docker exec "$NAME" cat /data/tls/ca.crt > "$WORK/ca.crt" 2>/dev/null \
            && curl -fsS --cacert "$WORK/ca.crt" "$URL/api/health" > "$WORK/health.json" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_healthy() {
    for _ in $(seq 60); do
        [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME")" = healthy ] && return 0
        sleep 1
    done
    return 1
}

# Responses go to a file before grep: with pipefail, `curl | grep -q` fails when grep stops early.
api() { curl -fsS --cacert "$WORK/ca.crt" -b "$WORK/jar" -c "$WORK/jar" -H "X-Forgeyard: 1" -H "Content-Type: application/json" "$@"; }

run_server
wait_up || fail "the server did not answer over HTTPS with its own CA"
grep -q "\"version\":\"$want_version\"" "$WORK/health.json" || fail "/api/health: $(cat "$WORK/health.json")"
ok "HTTPS on :$PORT with the CA from the volume, /api/health answers"

docker logs "$NAME" > "$WORK/first.log" 2>&1
TOKEN="$(python3 - "$WORK/first.log" <<'PY'
import re, sys
m = re.search(r"enter this setup token:\s+(\S+)", open(sys.argv[1]).read())
print(m.group(1) if m else "")
PY
)"
[ -n "$TOKEN" ] || { cat "$WORK/first.log" >&2; fail "no setup token in the log"; }
grep -q "Open $URL/setup" "$WORK/first.log" || fail "the log does not point at PUBLIC_URL/setup"
ok "setup token printed in the log, with $URL/setup"

api "$URL/api/auth/state" > "$WORK/state.json"
grep -q '"needs_setup":true' "$WORK/state.json" || fail "needs_setup before setup"
api -X POST "$URL/api/auth/setup" \
    -d "{\"setup_token\":\"$TOKEN\",\"username\":\"smoke\",\"password\":\"$PASSWORD\"}" >/dev/null || fail "setup"
api "$URL/api/auth/me" > "$WORK/me.json"
grep -q '"username":"smoke"' "$WORK/me.json" || fail "signed in after setup"
api -X POST "$URL/api/machines/join-tokens" > "$WORK/join.json" || fail "join token"
ok "setup done through the API, owner signed in, a join token issued"

python3 - "$WORK/join.json" "$URL" <<'PY' || fail "join token commands: $(head -c 600 "$WORK/join.json")"
import json, sys
j, url = json.load(open(sys.argv[1])), sys.argv[2]
assert j["server_url"] == url, j["server_url"]
assert j["ca_fingerprint"].startswith("sha256:"), j
assert j["agent_image"]["reference"] in j["docker_commands"]["nvidia"], j["agent_image"]
PY
fingerprint="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ca_fingerprint"])' "$WORK/join.json")"
curl -fsS --cacert "$WORK/ca.crt" "$URL/install.sh" > "$WORK/install.sh" || fail "install.sh"
grep -q "$URL" "$WORK/install.sh" || fail "install.sh does not name $URL"
ok "the Machines page's commands use PUBLIC_URL; install.sh served"

wait_healthy || fail "HEALTHCHECK: $(docker inspect -f '{{json .State.Health}}' "$NAME")"
ok "HEALTHCHECK: healthy"

docker exec "$NAME" sh -c 'stat -c "%a %U" /data/secret.key /data/tls/ca.key' > "$WORK/perms.txt"
[ "$(sort -u "$WORK/perms.txt")" = "600 forgeyard" ] || fail "secret.key / ca.key: $(cat "$WORK/perms.txt")"
key_sum="$(docker exec "$NAME" sha256sum /data/secret.key | cut -d' ' -f1)"
ok "secret.key and tls/ca.key: mode 600, owned by the server's account"

# ------------------------------------------------------------------ 3. a new container, same volume
docker rm -f "$NAME" >/dev/null
rm -f "$WORK/jar"
run_server
wait_up || fail "the server did not come back on the same volume"
[ "$(docker exec "$NAME" sha256sum /data/secret.key | cut -d' ' -f1)" = "$key_sum" ] || fail "secret.key changed"
api "$URL/api/auth/state" > "$WORK/state.json"
grep -q '"needs_setup":false' "$WORK/state.json" || fail "needs setup again after a restart"
api -X POST "$URL/api/auth/login" -d "{\"username\":\"smoke\",\"password\":\"$PASSWORD\"}" >/dev/null \
    || fail "sign-in after a restart"
api -X POST "$URL/api/machines/join-tokens" > "$WORK/join2.json" || fail "join token after a restart"
again="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ca_fingerprint"])' "$WORK/join2.json")"
[ "$again" = "$fingerprint" ] || fail "the CA changed: $again, was $fingerprint"
docker logs "$NAME" > "$WORK/second.log" 2>&1
if grep -q "setup token" "$WORK/second.log"; then fail "a setup token was printed again"; fi
wait_healthy || fail "HEALTHCHECK after a restart"
ok "new container on the same volume: owner, CA ($fingerprint), secret.key kept; healthy"
echo "server image smoke test passed"
