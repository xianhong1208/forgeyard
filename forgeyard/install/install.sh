#!/bin/sh
# Forgeyard agent installer, served by a Forgeyard server at /install.sh.
#
#   curl -fsS --cacert forgeyard-ca.pem https://<server>/install.sh | sudo sh -s -- \
#       --join fyj_... --ca-fingerprint sha256:...
#
# The Machines page shows the full command, which first checks the server's CA against the
# fingerprint. This script then:
#   1. checks the system (Linux, x86_64 or aarch64, root);
#   2. downloads the agent from the same server (its exact version) over HTTPS, verified against
#      the pinned CA, and checks the file's SHA-256;
#   3. installs it in its own Python environment (/opt/forgeyard/venv), with uv when it is there,
#      else with a Python 3.13+ already on the machine, else it installs uv first;
#   4. creates the account process workloads run as (forgeyard-run);
#   5. writes /etc/forgeyard/agent.toml with `forgeyard agent init` (kept when it exists);
#   6. joins the server with the one-time token (skipped when this machine already joined);
#   7. installs and starts the systemd service forgeyard-agent (KillMode=process: workloads keep
#      running when the agent restarts), or says how to run the agent without systemd.
#
# Running it again upgrades the agent to the server's version and keeps the config and the
# machine's credential. `--uninstall` removes the service and the program (add `--purge` to also
# delete the config and the credential). Everything is explained in the guide (<server>/guide).
set -eu

SERVER="@@SERVER@@"
VERSION="@@VERSION@@"
WHEEL="@@WHEEL@@"
WHEEL_SHA256="@@WHEEL_SHA256@@"

# Where things go. The environment can move them (the tests do; most people never need to).
HOME_DIR="${FORGEYARD_HOME:-/opt/forgeyard}"
CONFIG="${FORGEYARD_AGENT_CONFIG:-/etc/forgeyard/agent.toml}"
STATE_DIR="${FORGEYARD_AGENT_STATE_DIR:-/var/lib/forgeyard-agent}"
UNIT_DIR="${FORGEYARD_UNIT_DIR:-/etc/systemd/system}"
BIN_DIR="${FORGEYARD_BIN_DIR:-/usr/local/bin}"
SYSTEMD="${FORGEYARD_SYSTEMD:-auto}"
UNIT="forgeyard-agent.service"
VENV="$HOME_DIR/venv"

JOIN=""
FINGERPRINT=""
CA_FILE=""
NAME=""
LABELS=""
MODEL_CACHE=""
WORKLOAD_USER="forgeyard-run"
USER_GIVEN=0
START=1
UNINSTALL=0
PURGE=0
REJOIN=0

say() { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die() { printf 'forgeyard install: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Install the Forgeyard agent ($VERSION) from $SERVER.

  --join TOKEN            one-time join token from Machines -> Add a machine
  --ca-fingerprint FP     the server CA's fingerprint (sha256:...), from the same page
  --ca-file PEM           or pin this CA file instead (your own CA, a TLS proxy's)
  --server URL            the server's address (default: $SERVER)
  --name NAME             the machine's name on the server (default: the host name)
  --label KEY=VALUE       a label shown on the Machines page; repeatable
  --model-cache DIR       where models are downloaded (default /srv/forgeyard-models)
  --workload-user USER    the account process workloads run as (default forgeyard-run, created)
  --no-start              install and join, but do not start the service
  --rejoin                join again as a new machine, although this one already joined
  --uninstall             stop and remove the service and the program; keep the config,
                          the credential and the models
  --purge                 with --uninstall: also delete the config and the credential
  -h, --help              this text
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --join|--join-token) JOIN="${2:?--join needs a token}"; shift 2 ;;
        --ca-fingerprint|--fingerprint) FINGERPRINT="${2:?--ca-fingerprint needs a value}"; shift 2 ;;
        --ca-file) CA_FILE="${2:?--ca-file needs a path}"; shift 2 ;;
        --server) SERVER="${2:?--server needs an address}"; shift 2 ;;
        --name) NAME="${2:?--name needs a value}"; shift 2 ;;
        --label) LABELS="$LABELS ${2:?--label needs KEY=VALUE}"; shift 2 ;;
        --model-cache) MODEL_CACHE="${2:?--model-cache needs a directory}"; shift 2 ;;
        --workload-user) WORKLOAD_USER="${2:?--workload-user needs a name}"; USER_GIVEN=1; shift 2 ;;
        --no-start) START=0; shift ;;
        --rejoin) REJOIN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge) PURGE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }

use_systemd() {
    case "$SYSTEMD" in
        yes) return 0 ;;
        no) return 1 ;;
    esac
    [ -d /run/systemd/system ] && have systemctl
}

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
    [ "$(id -u)" = 0 ] || die "run it as root (sudo sh -s -- --uninstall)"
    step "Removing the Forgeyard agent"
    if use_systemd && [ -f "$UNIT_DIR/$UNIT" ]; then
        systemctl disable --now "$UNIT" || true
        rm -f "$UNIT_DIR/$UNIT"
        systemctl daemon-reload || true
        say "Stopped and removed the service $UNIT."
    fi
    case "$(readlink "$BIN_DIR/forgeyard" 2>/dev/null || true)" in
        "$HOME_DIR"/*) rm -f "$BIN_DIR/forgeyard" ;;
    esac
    rm -rf "$HOME_DIR"
    say "Removed the program ($HOME_DIR)."
    if [ "$PURGE" = 1 ]; then
        rm -rf "$STATE_DIR" "$STATE_DIR-work"
        rm -f "$CONFIG"
        rmdir "$(dirname "$CONFIG")" 2>/dev/null || true
        say "Deleted the credential ($STATE_DIR) and the config ($CONFIG)."
        say "Remove this machine on the server's Machines page too: its credential is gone."
    else
        say "Kept the config ($CONFIG) and the machine's credential ($STATE_DIR):"
        say "installing again brings the same machine back. --purge deletes them."
    fi
    say "Kept the model cache and the workload account. Workloads this agent started keep"
    say "running until you stop them on the server (or remove the machine there)."
    exit 0
fi

# ------------------------------------------------------------------ checks
step "Checking this machine"
[ "$(uname -s)" = Linux ] || die "the agent runs on Linux (this is $(uname -s))"
case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ;;
    *) die "unsupported CPU architecture $(uname -m): x86_64 and aarch64 are supported" ;;
esac
[ "$(id -u)" = 0 ] || die "run it as root: ... | sudo sh -s -- --join ... (it creates a service and an account)"
have curl || die "curl is needed"
case "$SERVER" in
    https://*) ;;
    *) die "the server address must start with https:// (got $SERVER)" ;;
esac
[ -z "$FINGERPRINT" ] || [ -z "$CA_FILE" ] || die "give --ca-fingerprint or --ca-file, not both"
say "Linux $(uname -m), server $SERVER, agent $VERSION."

WORK="$(mktemp -d "${TMPDIR:-/tmp}/forgeyard-install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
trap 'exit 1' INT TERM

# ------------------------------------------------------------------ trust
# The CA is fetched without checking (nothing secret is sent), then accepted only if its SHA-256
# matches the fingerprint from the Machines page. From then on every download is checked against it.
CURL_TLS=""
if [ -n "$FINGERPRINT" ]; then
    step "Checking the server's certificate authority"
    want="$(printf '%s' "$FINGERPRINT" | tr 'A-F' 'a-f' | sed 's/^sha256://; s/://g')"
    case "$want" in
        *[!0-9a-f]*|"") die "--ca-fingerprint is not a SHA-256 fingerprint: $FINGERPRINT" ;;
    esac
    [ "${#want}" = 64 ] || die "--ca-fingerprint is not a SHA-256 fingerprint: $FINGERPRINT"
    curl -fsSk "$SERVER/api/tls/ca.pem" -o "$WORK/ca.pem" || die "cannot download the CA from $SERVER"
    got="$(sed '/-----/d' "$WORK/ca.pem" | tr -d '\r\n' | base64 -d | sha256sum | cut -c1-64)"
    if [ "$got" != "$want" ]; then
        die "Refused: the CA $SERVER sent has fingerprint sha256:$got, not sha256:$want. Someone may be between this machine and the server, or the fingerprint is from another server."
    fi
    CURL_TLS="--cacert $WORK/ca.pem"
    say "The CA matches sha256:$want."
elif [ -n "$CA_FILE" ]; then
    [ -r "$CA_FILE" ] || die "cannot read $CA_FILE"
    CURL_TLS="--cacert $CA_FILE"
fi

fetch() {  # fetch URL FILE, verified against the pinned CA (or the system store)
    # shellcheck disable=SC2086 # CURL_TLS is empty or two words
    curl -fsS $CURL_TLS "$1" -o "$2"
}

# ------------------------------------------------------------------ the agent
step "Downloading the agent $VERSION"
fetch "$SERVER/install/$WHEEL" "$WORK/$WHEEL" || die "cannot download $SERVER/install/$WHEEL"
got="$(sha256sum "$WORK/$WHEEL" | cut -c1-64)"
[ "$got" = "$WHEEL_SHA256" ] || die "$WHEEL is damaged: SHA-256 $got, expected $WHEEL_SHA256"
say "$WHEEL ($(wc -c < "$WORK/$WHEEL" | tr -d ' ') bytes, SHA-256 checked)."

EXTRA=""
if [ -e /dev/nvidiactl ] || [ -e /proc/driver/nvidia/version ] || have nvidia-smi; then
    EXTRA="[nvidia]"
    say "NVIDIA driver found: installing NVML support too."
fi

python_ok() {  # a Python 3.13 or newer that can make a virtual environment
    "$1" -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1
}

step "Installing it in $VENV"
mkdir -p "$HOME_DIR"
UV=""
if have uv; then
    UV="$(command -v uv)"
elif [ -x "$HOME_DIR/bin/uv" ]; then
    UV="$HOME_DIR/bin/uv"
fi
PY=""
if [ -z "$UV" ]; then
    for candidate in python3.14 python3.13 python3; do
        if have "$candidate" && python_ok "$candidate"; then
            PY="$(command -v "$candidate")"
            break
        fi
    done
fi
if [ -z "$UV" ] && [ -z "$PY" ]; then
    say "No uv and no Python 3.13 here: installing uv into $HOME_DIR/bin (it brings its own Python)."
    curl -LsSf https://astral.sh/uv/install.sh -o "$WORK/uv-install.sh" || die "cannot download uv from astral.sh"
    env UV_INSTALL_DIR="$HOME_DIR/bin" UV_NO_MODIFY_PATH=1 sh "$WORK/uv-install.sh" >/dev/null || die "installing uv failed"
    UV="$HOME_DIR/bin/uv"
fi

cp "$WORK/$WHEEL" "$HOME_DIR/$WHEEL"
REQ="forgeyard$EXTRA @ file://$HOME_DIR/$WHEEL"
if [ -n "$UV" ]; then
    export UV_PYTHON_INSTALL_DIR="$HOME_DIR/python"
    if ! [ -x "$VENV/bin/python" ] || ! python_ok "$VENV/bin/python"; then
        "$UV" venv --quiet --python 3.13 "$VENV" || die "uv could not make a Python 3.13 environment"
    fi
    "$UV" pip install --quiet --python "$VENV/bin/python" --reinstall-package forgeyard "$REQ" \
        || die "installing the agent failed (it needs PyPI for its few dependencies)"
    say "Installed with uv ($("$UV" --version))."
else
    if ! [ -x "$VENV/bin/python" ] || ! python_ok "$VENV/bin/python"; then
        "$PY" -m venv "$VENV" || die "$PY could not make a virtual environment"
    fi
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps \
        "$HOME_DIR/$WHEEL" || die "installing the agent failed"
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "$REQ" \
        || die "installing the agent's dependencies failed (it needs PyPI)"
    say "Installed with $("$PY" --version 2>&1)."
fi
for old in "$HOME_DIR"/forgeyard-*.whl; do
    [ "$old" = "$HOME_DIR/$WHEEL" ] || rm -f "$old"
done
AGENT="$VENV/bin/python -m forgeyard agent --config $CONFIG"
installed="$("$VENV/bin/python" -m forgeyard --version)"
if mkdir -p "$BIN_DIR" 2>/dev/null && ln -sf "$VENV/bin/forgeyard" "$BIN_DIR/forgeyard" 2>/dev/null; then
    say "$installed, as $BIN_DIR/forgeyard (try: forgeyard agent config)."
else
    say "$installed, as $VENV/bin/forgeyard."
fi

# ------------------------------------------------------------------ account and config
step "Account for process workloads"
if [ "$USER_GIVEN" = 0 ] && [ -f "$CONFIG" ]; then
    # An upgrade: the account the existing settings name, not the default.
    WORKLOAD_USER="$("$VENV/bin/python" -m forgeyard agent config --config "$CONFIG" --get workloads.user 2>/dev/null || true)"
fi
if [ -z "$WORKLOAD_USER" ]; then
    say "The settings name no account (process workloads run as the agent's own user)."
elif id -u "$WORKLOAD_USER" >/dev/null 2>&1; then
    say "'$WORKLOAD_USER' exists."
elif have useradd; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$WORKLOAD_USER" \
        || die "could not create the account $WORKLOAD_USER"
    say "Created the system account '$WORKLOAD_USER' (no password, no shell, no home)."
elif have adduser; then
    adduser -S -D -H -s /sbin/nologin "$WORKLOAD_USER" || die "could not create the account $WORKLOAD_USER"
    say "Created the system account '$WORKLOAD_USER'."
else
    die "no useradd or adduser: create an unprivileged account and pass --workload-user"
fi

step "Settings ($CONFIG)"
if [ -f "$CONFIG" ]; then
    say "Keeping the existing file. To start over, delete it and run this again."
else
    set -- --config "$CONFIG" --server "$SERVER" --workload-user "$WORKLOAD_USER" --state-dir "$STATE_DIR"
    [ -z "$FINGERPRINT" ] || set -- "$@" --ca-fingerprint "$FINGERPRINT"
    [ -z "$CA_FILE" ] || set -- "$@" --ca-file "$CA_FILE"
    [ -z "$NAME" ] || set -- "$@" --name "$NAME"
    [ -z "$MODEL_CACHE" ] || set -- "$@" --model-cache "$MODEL_CACHE"
    for label in $LABELS; do
        set -- "$@" --label "$label"
    done
    "$VENV/bin/python" -m forgeyard agent init "$@" || die "writing $CONFIG failed"
fi
cache="$("$VENV/bin/python" -m forgeyard agent config --config "$CONFIG" --get models.cache)" \
    || die "$CONFIG has problems (above): fix them and run this again"
if [ -n "$cache" ]; then
    mkdir -p "$cache"
    chmod 755 "$cache"
fi

# ------------------------------------------------------------------ join
step "Joining $SERVER"
set -- join
[ -z "$JOIN" ] || set -- "$@" --join-token "$JOIN"
[ "$REJOIN" = 0 ] || set -- "$@" --rejoin
# shellcheck disable=SC2086 # AGENT is a command line with fixed, space-free parts
$AGENT "$@" || die "joining failed (see above). A join token works once and for 15 minutes: create a new one on the Machines page and run this again."

# ------------------------------------------------------------------ service
if use_systemd; then
    step "Service $UNIT"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/$UNIT" <<EOF
# Installed by $SERVER/install.sh. Running install.sh again rewrites it.
[Unit]
Description=Forgeyard agent
Documentation=$SERVER/guide
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=simple
ExecStart=$AGENT
Restart=always
RestartSec=5
# Exit code 3: the server refused the credential (the machine was revoked or removed). Restarting
# cannot fix that; join again instead.
RestartPreventExitStatus=3
# Stopping or restarting the agent must leave its workloads running: it adopts them when it
# starts again. So only the agent's own process is stopped, not the whole group. systemd logs
# that KillMode=process is unusual; for this service it is the point.
KillMode=process
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$UNIT" >/dev/null 2>&1 || systemctl enable "$UNIT"
    if [ "$START" = 1 ]; then
        systemctl restart "$UNIT"
        sleep 3
        systemctl --no-pager --lines=8 status "$UNIT" || true
    fi
    step "Done"
    if [ "$START" = 1 ]; then
        say "The agent runs as the service $UNIT and starts with the machine."
    else
        say "Installed but not started (--no-start). Start it with: systemctl start $UNIT"
    fi
    say "  Logs:     journalctl -u $UNIT -f"
    say "  Restart:  systemctl restart $UNIT   (workloads keep running)"
else
    step "Done (no systemd here)"
    say "This machine has no systemd, so nothing starts the agent by itself. Start it and keep it"
    say "running (in tmux or screen, or with your container's supervisor); it reconnects by itself:"
    say ""
    say "  $AGENT"
    say ""
    say "Or in the background with a log file:"
    say "  nohup $AGENT >> /var/log/forgeyard-agent.log 2>&1 &"
fi
say ""
say "Next, on $SERVER:"
say "  1. Machines: this machine shows as online within a few seconds."
say "  2. Change GPU access: choose which cards Forgeyard may use here."
say "  3. Models: deploy a model on it. The guide: $SERVER/guide"
say "Settings: $CONFIG (then restart the agent). Running this script again upgrades the agent."
