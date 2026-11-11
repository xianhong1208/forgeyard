# How-to

Tasks that come after the [quick start](quick-start.md), each on its own. The commands assume the
agent was installed with the one-line install (service `forgeyard-agent`, settings in
`/etc/forgeyard/agent.toml`); the Docker and manual variants are noted where they differ.

## Add an AMD machine

1. On the machine, install ROCm (6.0 or newer) and Docker. Check that ROCm sees the cards:

   ```bash
   amd-smi list
   ```

2. Add it like any other machine: **Machines** → **Add a machine** → **One-line install**, and
   run the command there.
3. Check what the agent sees:

   ```bash
   sudo forgeyard agent inventory
   ```

   It prints one line per card, with its `gfx` target (for example `gfx1100`, or `gfx1151` for a
   Strix Halo APU, marked *unified-memory*) and the ROCm version, then one line per collector.
   On a Strix Halo with a 96 GiB carve-out:

   ```text
   amd     0  AMD Radeon 8060S  arch=gfx1151  32.6 GiB free of 96.0 GiB  rocm=7.0.2 unified-memory
   ```

The agent reads AMD cards through the `amdsmi` library when it can import it (from its own Python,
or else from the copy ROCm ships in `/opt/rocm/share/amd_smi`), else the `amd-smi` command, else
`rocm-smi`. If the inventory says `amdsmi … not importable` or `failed to load` but `amd-smi`
works, nothing is missing: the same values come from the command.

What it shows for each card:

- **Name:** the card's market name. An APU's is often just "AMD Radeon Graphics"; then the GPU
  named in the CPU's model ("… w/ Radeon 8060S") is used.
- **ROCm version** (`rocm=` here, "ROCm" on the Machines page) where NVIDIA cards show their
  driver. The kernel driver's version is kept in the card's details.
- **Memory:** for an APU, the larger of the VRAM carve-out (set in the BIOS) and GTT, which is
  what `amd-smi monitor` shows too.
- **Key:** the card's UUID when it has a serial number, else its PCI address (APUs have no
  serial, so their UUID is the same on every machine). A machine that joined with Forgeyard
  1.1.0 or older keeps its GPU allowlist when the key changes.

Models run on AMD with the `vllm` recipe's ROCm image and with `llama-cpp`. A process workload gets
only its cards in `ROCR_VISIBLE_DEVICES`, and the groups that own `/dev/kfd` and its
`/dev/dri/renderD*` only when it has a GPU.

> AMD support has been checked on a real Strix Halo (Radeon 8060S, gfx1151) with ROCm 7.0.2: the
> inventory (through the `amdsmi` library, `amd-smi` and `rocm-smi`), joining, the GPU allowlist,
> and a model served through `/v1` with llama.cpp's Vulkan build as a custom recipe. Not checked on
> real hardware yet: the ROCm images of the `vllm` and `llama-cpp` recipes (whether they support
> gfx1151 is unknown), and discrete AMD cards (recorded data only). Report anything that looks wrong
> with the output of `forgeyard agent inventory --json --raw`.

## Add a CPU-only machine

Nothing special: run the one-line install there. Forgeyard shows the machine with its CPU and RAM,
and places CPU models on it:

- `llama-cpp` runs GGUF models on the CPU, as a container (with Docker) or, on x86_64 without
  Docker, as a program the agent downloads and checks against its SHA-256;
- `faster-whisper` runs speech to text on the CPU (with Docker).

When you deploy, choose **Runs on** → **CPU only**. Set **CPU threads** to the cores you want it to
use: more threads than free cores make llama.cpp slower, not faster.

## Run the agent in Docker

One `docker run` per machine, instead of the install script. The image is built from the Forgeyard
repository; it is not published yet.

**Machines** → **Add a machine** → **Docker** writes both commands for you: pick the machine's GPUs
(NVIDIA, AMD or CPU only) and fill in the form above the command. The command follows the form as
you type, **Copy** copies exactly what is shown, and the values are remembered in this browser for
the next machine:

- **Model directory on that machine** (default `/srv/forgeyard-models`): a disk with room for
  models. It goes on both sides of the `-v` and into `FORGEYARD_MODEL_CACHE`. An absolute path,
  without spaces, quotes or colons.
- **Machine name** (optional): how it is listed on **Machines**; empty means its host name. Letters,
  digits, `.`, `_` and `-`, up to 64 characters. Adds `FORGEYARD_NAME`.
- **Server address, as that machine reaches it** (default: this server's `PUBLIC_URL`): change it
  when the machine comes through an SSH tunnel (`https://127.0.0.1:7860`), a VPN or another LAN
  address. When that host is not in the server's certificate the page says so: add it to
  `TLS_EXTRA_NAMES` and restart the server, or use a name the certificate has.
- **Advanced → Port range** (`FORGEYARD_PORT_RANGE`, default `30000-32767`): the host ports for
  services and model replicas. Set it when only some ports reach the machine, for example
  `33000-33002` forwarded through an SSH tunnel.

A field the agent would reject shows its error instead of a command. The image tag is the server's
version. Like the page, the commands here start with `sudo`; as root, or when your account is in the
`docker` group, leave it out.

### Build the image

On the machine (or on another one, then `docker save` and `docker load`), in a Forgeyard checkout of
the server's version:

```bash
sudo docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .
```

### NVIDIA

Needs the NVIDIA Container Toolkit (`docker run --gpus all` works). With the form's defaults, the
page's command looks like this (with your server's address, fingerprint and join token):

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

Then:

```bash
sudo docker logs -f forgeyard-agent
```

It prints, among other lines:

```text
INFO forgeyard.agent: Joined https://192.168.1.20:7860 as 'gpu-01'; credential saved to /var/lib/forgeyard-agent/credential.json (mode 0600). Trusting only the CA sha256:3f9a….
INFO forgeyard.agent: Runtime container ok: docker 28.5.2; NVIDIA GPUs via gpus
INFO forgeyard.agent: Connected to https://192.168.1.20:7860 as 'gpu-01' (9 accelerators; heartbeat every 10 s).
```

### AMD

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  --device /dev/kfd --device /dev/dri \
  --group-add "$(getent group video | cut -d: -f3)" \
  --group-add "$(getent group render | cut -d: -f3)" \
  -v /opt/rocm:/opt/rocm:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

The host's ROCm is mounted read-only so the agent can use `amdsmi`, `amd-smi` and `rocm-smi` to
read the cards. The groups are given by their **number** on the host: Docker looks a group *name*
up in the image, which has no `render` group (`Unable to find group render`). On an older system
without a `render` group (`getent group render` prints nothing), leave that `--group-add` out.

To check what the agent sees before it joins, run its `inventory` command with the same devices
(the image's command already is `forgeyard agent`, so add only `inventory`):

```bash
sudo docker run --rm --device /dev/kfd --device /dev/dri \
  --group-add "$(getent group video | cut -d: -f3)" --group-add "$(getent group render | cut -d: -f3)" \
  -v /opt/rocm:/opt/rocm:ro \
  forgeyard-agent:1.1.0 inventory
```

### CPU only

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

### Why these options

- **`--network host`**: model replicas listen on the host's `127.0.0.1`, behind the agent's key
  guard, which listens on the replica's published port. Both only work if the agent shares the
  host's network. It also lets the agent publish ports on the machine's own address.
- **The model cache at the same path inside and outside** (`-v /srv/forgeyard-models:/srv/forgeyard-models`):
  the agent downloads models into it, then asks the host's Docker to start the engine container
  with the model mounted *by its path on the host*. Different paths would mount nothing.
- **`/var/run/docker.sock`**: models and container workloads run as containers on the host's Docker,
  next to the agent, not inside it. That socket gives full control of the host's Docker, which is
  why the agent's container is trusted like the agent itself.
- **The `forgeyard-agent` volume** keeps the machine's credential and the pinned CA. Keep it: the
  same `docker run` after a restart, an upgrade or `docker rm` brings the same machine back, and
  the join token is not used again (the log says so). Without it the machine has to join again.
- **`FORGEYARD_*` variables** instead of flags: they are the same settings as in
  [agent.toml](reference.md#agent-settings). You can also mount a file:
  `-v /etc/forgeyard/agent.toml:/etc/forgeyard/agent.toml:ro`.
- The agent runs as **root** inside the container: it needs the Docker socket and switches to the
  unprivileged account `forgeyard-run` for process workloads. Workloads never run as root.
- There is no systemd in the container, so process workloads are limited with rlimits (the image
  sets `FORGEYARD_LIMITS=rlimit`). Process workloads run inside the agent's container and stop
  with it; models and container workloads keep running on the host.

### Move an installed agent into Docker

A machine that already joined keeps its identity if the container gets its state directory:

1. Build the image (above).
2. Stop the old agent: `sudo systemctl disable --now forgeyard-agent` (installed), or stop the
   process you started. Its container workloads and models keep running.
3. Start the container with the old state directory and the same model cache, and **without** the
   join variables:

   ```bash
   sudo docker run -d --name forgeyard-agent --restart always --network host --gpus all \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /var/lib/forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     forgeyard-agent:1.1.0
   ```

4. `sudo docker logs -f forgeyard-agent` shows `Adopted replica …` for every running model: the new
   agent takes over the containers the old one started (they carry its id, which is in the state
   directory).

Process workloads the old agent started outside the container are not taken over: stop them from
the UI first.

## Add a cloud provider

Admins: open **Providers**, choose **Add a provider**, pick OpenAI, Anthropic, Google Gemini, Azure
OpenAI, OpenRouter or *OpenAI-compatible server*, paste the key, and **Test connection**. Add the
models you want, then put one in a route: open a deployment, choose **Routing and cloud spill**, add
a tier with the cloud model, and save. Give the keys that may use it a cloud setting other than
**Never** on **API keys**. [Cloud providers](cloud-providers.md#add-a-provider) has the steps for
each provider, and what each one's endpoint cannot do.

## Restrict GPUs on a shared machine

1. Open **Machines**, find the machine, choose **Change GPU access**.
2. Tick only the cards Forgeyard may use, for example card 4, and choose **Save**.

From then on:

- nothing Forgeyard runs is given another card, whatever a workload or deployment asks;
- the other cards show *not for Forgeyard*;
- memory is counted from what each allowed card reports **free**, so other people's use of the
  same card is respected.

A card in use cannot be taken off the list: stop what runs on it first. Every change is in the
audit log, with before and after.

To keep a model on one card next to other people's work, deploy it with **GPU sharing** →
**Share VRAM**: it then reserves only its estimate, and vLLM is told to use just that much (see
[vLLM does not start on a shared card](troubleshooting.md#vllm-does-not-start-on-a-shared-card)).

## Use your own certificate or a reverse proxy

**Your own certificate** (a company CA, or a public certificate for a real domain). In the server's
`.env`:

```bash
TLS_CERT_FILE=/etc/forgeyard/tls/fullchain.pem
TLS_KEY_FILE=/etc/forgeyard/tls/privkey.pem
TLS_CA_FILE=/etc/forgeyard/tls/company-ca.pem
```

`TLS_CA_FILE` is the CA that issued it; agents then pin that CA, and **Trust this server** offers it.
Leave it out for a publicly trusted certificate: join commands then carry no fingerprint and agents
use the system's trust store. The server reloads the files when they change (after a renewal).

**Behind a reverse proxy** that already handles HTTPS (nginx, Caddy):

```bash
TLS=off
HOST=127.0.0.1
PUBLIC_URL=https://forge.example.com
TRUSTED_PROXIES=127.0.0.1
```

The proxy must pass WebSockets (agents use one), must not buffer streamed answers, and must allow
long reads (600 s) and large uploads (32 MB, for audio). nginx:

```nginx
server {
    listen 443 ssl;
    server_name forge.example.com;
    ssl_certificate     /etc/letsencrypt/live/forge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/forge.example.com/privkey.pem;
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Caddy (it gets and renews the certificate by itself):

```caddyfile
forge.example.com {
    request_body {
        max_size 32MB
    }
    reverse_proxy 127.0.0.1:7860 {
        flush_interval -1
        transport http {
            read_timeout 600s
        }
    }
}
```

Agents that joined with Forgeyard's own CA switch once to the new certificate, keeping their
credential:

```bash
sudo forgeyard agent trust --server https://forge.example.com
```

(add `--ca-file company-ca.pem` for a company CA), then `sudo systemctl restart forgeyard-agent`.

## Run the server as a service

So that the server starts with the computer and restarts if it stops. As root, with the checkout in
`/opt/forgeyard-server` and its settings in `/opt/forgeyard-server/.env`:

```bash
sudo useradd --system --home-dir /opt/forgeyard-server --shell /usr/sbin/nologin forgeyard
sudo chown -R forgeyard: /opt/forgeyard-server
sudo tee /etc/systemd/system/forgeyard.service > /dev/null <<'EOF'
[Unit]
Description=Forgeyard server
Wants=network-online.target
After=network-online.target

[Service]
User=forgeyard
WorkingDirectory=/opt/forgeyard-server
ExecStart=/opt/forgeyard-server/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now forgeyard
```

The server reads `.env` from its working directory, keeps its data in `data/` there, and logs to
the journal: `journalctl -u forgeyard -f`.

## Upgrade the server and the agents

1. **Back up** the server first ([how](#back-up-and-restore)).
2. **The server**, in its folder:

   ```bash
   git pull
   cd web && npm ci && npm run build && cd ..
   uv sync
   ```

   Then restart it (`sudo systemctl restart forgeyard`). Database changes run by themselves on
   start. Read the CHANGELOG for anything that needs a step.
3. **The agents**, one by one. Running agents keep their workloads while they restart.
   - **Installed** with the one-line install: run it again, without `--join`. It installs the
     server's new version and keeps the settings and the credential:

     ```bash
     curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
       sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
       echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
       curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
       sudo sh -s -- --ca-fingerprint sha256:3f9a…
     ```

   - **Docker**: build the new image, then `docker rm -f forgeyard-agent` and the same `docker run`
     with the new tag. The volume keeps the machine.
   - **Manual**: `git pull`, `uv sync`, restart the agent.

The **Machines** page shows each agent's version. Upgrade agents soon after the server: a much
older agent may lack what a newer server asks for.

## Back up and restore

**What to back up on the server**, all in its `data/` folder unless you moved it:

| What | Why |
|---|---|
| `data/forgeyard.db` (or your PostgreSQL database) | everything Forgeyard knows: users, machines, models, deployments, keys, usage |
| `data/secret.key` | it encrypts stored secrets (Hugging Face tokens, replicas' keys, workload environment values). Keep it **apart** from the database backup |
| `data/tls/` | the CA every device and agent trusts. Without it, every device and agent has to trust a new one |
| `.env` | your settings |

A consistent copy of the SQLite database while the server runs:

```bash
uv run --no-sync python -c "import sqlite3; sqlite3.connect('data/forgeyard.db').backup(sqlite3.connect('/backup/forgeyard.db'))"
cp -a data/tls data/secret.key .env /backup/
```

PostgreSQL: `pg_dump` as usual.

**On each machine** (optional): `/var/lib/forgeyard-agent` (the credential; without it the machine
joins again with a new token) and `/etc/forgeyard/agent.toml`. The model cache can always be
downloaded again.

**Restore**: stop the server, put the files back in `data/` (and `.env`), start it. With a wrong or
missing `secret.key` the server refuses to start and says so, instead of losing the secrets. If the
key is really lost, stop the server and start over with:

```bash
uv run --no-sync python -m forgeyard secrets reset --yes
```

It makes a new key and drops what the old one encrypted: enter the Hugging Face tokens of gated
models again, model replicas restart with new internal keys, and workloads with lost environment
values have to be created again (their events say so).

## Move a model cache

For example from `/srv/forgeyard-models` to a bigger disk at `/data/forgeyard-models`:

1. Stop the deployments that run on this machine (each deployment's page → **Stop**).
2. Stop the agent and copy the cache (it uses relative links, so a copy works):

   ```bash
   sudo systemctl stop forgeyard-agent
   sudo rsync -a /srv/forgeyard-models/ /data/forgeyard-models/
   ```

3. In `/etc/forgeyard/agent.toml`, under `[models]`, set `cache = "/data/forgeyard-models"`, then:

   ```bash
   sudo forgeyard agent config --get models.cache
   sudo systemctl start forgeyard-agent
   ```

4. Start the deployments again. They start in seconds: nothing is downloaded again.
5. When everything runs, delete the old folder.

With Docker, change both sides of the cache volume (`-v /data/forgeyard-models:/data/forgeyard-models`)
and `FORGEYARD_MODEL_CACHE`, then recreate the container.

## Rotate keys

- **An API key**: create a new key on **API keys**, put it in your program, then **Revoke** the old
  one. A revoked key stops working on the next request.
- **A machine's credential**: revoke the machine, create a join command, and join again on the
  machine with `--rejoin`:

  ```bash
  sudo forgeyard agent join --rejoin --join-token fyj_…
  sudo systemctl restart forgeyard-agent
  ```

  It becomes a new machine on the page; remove the old entry.
- **The CA**: only when it must change (its key leaked, or you move to your own certificate).
  Every device trusts the new one again, and every agent runs `forgeyard agent trust` with the new
  fingerprint (shown on **Trust this server** for admins).
- **`secret.key`** cannot be rotated in place yet. Protect it like a password (mode 0600, apart from
  database backups).

## Revoke a machine

1. Stop or move what runs there: deployments placed only on that machine have nowhere to go.
2. **Machines** → the machine → **Revoke**. Its credential stops working, its agent is disconnected
   and exits (the installed service does not restart it), and its replicas are taken off the
   gateway.
3. **Remove** takes it off the list. To bring the machine back, join it again with a new token
   (`forgeyard agent join --rejoin --join-token fyj_…`).

Workloads still running on a revoked machine are not stopped by the server, which can no longer
reach the agent. Stop them on the machine: `docker ps --filter label=forgeyard.agent` lists the
containers.

## Uninstall the agent

On the machine, the same command as the install, with `--uninstall` instead of `--join`:

```bash
curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | sudo sh -s -- --uninstall
```

It stops and removes the service and `/opt/forgeyard`, and keeps `/etc/forgeyard/agent.toml`, the
credential and the model cache, so installing again brings the same machine back. Add `--purge` to
delete the settings and the credential as well (then remove the machine on the **Machines** page).
The model cache and the `forgeyard-run` account are never deleted: remove them yourself if you want.

By hand, without the server:

```bash
sudo systemctl disable --now forgeyard-agent
sudo rm -f /etc/systemd/system/forgeyard-agent.service /usr/local/bin/forgeyard
sudo rm -rf /opt/forgeyard
```
