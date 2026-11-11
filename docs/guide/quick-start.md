# Quick start

From nothing to your first API call in about ten minutes, plus the time a model takes to
download. You need:

- **A server**: any Linux, macOS or Windows (WSL) computer that the other machines can reach, with
  [uv](https://docs.astral.sh/uv/) (it gets Python 3.13 by itself when needed) and Node 22.12 or
  newer to build the web UI. It does not need a GPU.
- **A machine to run models on**: Linux on x86_64 or aarch64, with an NVIDIA GPU (driver 575 or
  newer, Docker with the NVIDIA Container Toolkit), an AMD GPU (ROCm and Docker), or only a CPU.
  It can be the same computer as the server.
- Internet access on that machine, for a few Python packages and the model.

The examples use `192.168.1.20` for the server's address. Use yours.

## 1. Install the server

On the server:

```bash
git clone https://github.com/xianhong1208/forgeyard.git
cd forgeyard
cd web && npm ci && npm run build && cd ..
uv sync
```

Start it, open to the network, with the address the other machines will use:

```bash
HOST=0.0.0.0 PUBLIC_URL=https://192.168.1.20:7860 uv run python main.py
```

It prints (among other lines):

```text
INFO forgeyard: Made Forgeyard's certificate authority in data/tls (fingerprint sha256:3f9a…).
INFO forgeyard: HTTPS with Forgeyard's own CA. CA fingerprint sha256:3f9a…
WARNING forgeyard:
================================================================
Forgeyard has no owner yet. Open https://192.168.1.20:7860/setup and enter this setup token:

    3xQ…

(also stored in data/secrets.json until setup is done)
================================================================
INFO:     Uvicorn running on https://0.0.0.0:7860 (Press CTRL+C to quit)
```

Keep it running. Everything Forgeyard stores goes into `data/` next to it: the database, the
certificate authority (`data/tls/`) and the key that encrypts secrets (`data/secret.key`). Back up
that folder ([how](how-to.md#back-up-and-restore)). To keep the server running after you log out,
see [Run the server as a service](how-to.md#run-the-server-as-a-service).

## 2. Create the owner and trust the CA

1. Open `https://192.168.1.20:7860/setup` in your browser.
2. The browser warns about the certificate. That is expected the first time: Forgeyard made its own
   certificate authority (CA), and your browser does not know it yet. Click past the warning once
   (Chrome: *Advanced* → *Proceed*; Firefox: *Advanced* → *Accept the risk*).
3. Paste the setup token from the log, choose a user name and a password (at least 12 characters),
   and create the **owner**. You are signed in.
4. Open **Trust this server** (in the account menu, top right) and compare its fingerprint with the
   one in the log. Download the CA and install it with the steps shown for your device. The warning
   is gone from then on. Do this once on each device you use Forgeyard from.

Forgeyard now shows **Get started**: a checklist of the next steps that ticks itself off.

## 3. Add a machine

1. Open **Machines** → **Add a machine** → **Create join command**.
2. Keep **One-line install** selected and copy the command. It looks like this:

   ```bash
   curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
     sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
     echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
     curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
     sudo sh -s -- --join fyj_… --ca-fingerprint sha256:3f9a…
   ```

   The token in it works **once**, within **15 minutes**.
3. Paste it into a terminal on the machine. If you are root, remove `sudo`.

It prints:

```text
forgeyard-ca.der: OK

==> Checking this machine
Linux x86_64, server https://192.168.1.20:7860, agent 1.2.0.

==> Checking the server's certificate authority
The CA matches sha256:3f9a….

==> Downloading the agent 1.2.0
forgeyard-1.2.0-py3-none-any.whl (335662 bytes, SHA-256 checked).
NVIDIA driver found: installing NVML support too.

==> Installing it in /opt/forgeyard/venv
Installed with uv (uv 0.9.24).
forgeyard 1.2.0, as /usr/local/bin/forgeyard (try: forgeyard agent config).

==> Account for process workloads
Created the system account 'forgeyard-run' (no password, no shell, no home).

==> Settings (/etc/forgeyard/agent.toml)
# Forgeyard agent settings, written by `forgeyard agent init` (1.2.0).
…
# GPUs: 2 x NVIDIA GeForce RTX 4090 (driver 580.95.05). Choose which ones Forgeyard may use on the Machines page.
…

==> Joining https://192.168.1.20:7860
INFO forgeyard.agent: Joined https://192.168.1.20:7860 as 'gpu-01'; credential saved to /var/lib/forgeyard-agent/credential.json (mode 0600). Trusting only the CA sha256:3f9a….

==> Service forgeyard-agent.service
● forgeyard-agent.service - Forgeyard agent
     Active: active (running)
…
Next, on https://192.168.1.20:7860:
  1. Machines: this machine shows as online within a few seconds.
  2. Change GPU access: choose which cards Forgeyard may use here.
  3. Models: deploy a model on it. The guide: https://192.168.1.20:7860/guide
```

What happened, in order:

1. The first three lines downloaded the server's CA **without** checking it (nothing secret is
   sent), turned it back into its binary form and compared its SHA-256 with the fingerprint in the
   command. A different CA, for example from a machine in between, stops everything here with
   `forgeyard-ca.der: FAILED`.
2. Only then was the installer downloaded, over a connection checked against that CA.
3. The installer downloaded the agent from your server (always the server's own version), checked
   it, and installed it with its own Python in `/opt/forgeyard`.
4. It created the account `forgeyard-run` that process workloads run as. Workloads never run as
   root.
5. It wrote `/etc/forgeyard/agent.toml` from what it found: GPUs, Docker, systemd. Every setting is
   explained in the file.
6. It traded the join token for this machine's own credential, kept in
   `/var/lib/forgeyard-agent` (mode 0600).
7. It installed and started the service `forgeyard-agent`, which starts with the machine and
   restarts if it stops.

Back on the Machines page, the machine appears within seconds, with its GPUs.

Other ways: the **Docker** tab runs the agent as a container
([Run the agent in Docker](how-to.md#run-the-agent-in-docker)); the **Manual** tab uses a Forgeyard
checkout and a config file. AMD and CPU-only machines work the same way
([AMD](how-to.md#add-an-amd-machine), [CPU only](how-to.md#add-a-cpu-only-machine)).

## 4. Choose the GPUs Forgeyard may use

By default Forgeyard may use every GPU of the machine. On a machine you share with others:

1. On **Machines**, find the machine and choose **Change GPU access**.
2. Tick the cards Forgeyard may use, and save.

Cards outside the list are never given to anything Forgeyard runs; the page marks them *not for
Forgeyard*. Even with all cards allowed, saving the choice once ticks this step off.

## 5. Add a recommended model

1. Open **Models** → **Catalog and recipes** → **Add a model**.
2. **Recommended** lists models known to work. Pick one that fits your machine:
   - a GPU with at least 8 GB free: **Qwen3 0.6B** (1.4 GB download);
   - only a CPU: **Qwen3 1.7B (GGUF, for the CPU)** (1.2 GB download, about 3 GB of RAM).
3. Forgeyard reads the model's details from Hugging Face without downloading it, and shows the
   memory one copy needs and the machines it **fits on right now**.
4. Choose **Add to the catalog**.

## 6. Deploy it

1. Choose **Deploy it** (or **Models** → **Deploy a model** and pick the model).
2. Give the deployment a name, for example `qwen`. It becomes the **model name** programs ask for.
3. Check **Runs on**: **GPU** for Qwen3 0.6B, **CPU only** for the GGUF model (*Deploy it* chooses
   it for you).
4. The page shows the estimate and where it will run. Choose **Deploy**.

The deployment page shows the download, then the engine starting. It is ready when its replica is
**healthy**. The first time, that takes as long as the download; later starts take seconds. Try it
right there with **Try it**, or in the **Playground**.

## 7. Create an API key

1. Open **API keys** → **New key**.
2. Give it a name, for example `laptop`, and choose **Create key**.
3. Copy the key (`fyk_…`) now: it is shown only once. Forgeyard keeps only a hash of it.

On the computer that will call the model, keep it in a variable and get the CA file:

```bash
export FORGEYARD_API_KEY=fyk_…
curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem
```

Check that it is the right CA: the fingerprint must be the one from step 2.

```bash
sed '/-----/d' forgeyard-ca.pem | base64 -d | sha256sum
```

## 8. Call it

### curl

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/models \
  -H "Authorization: Bearer $FORGEYARD_API_KEY"
```

It prints:

```json
{"object":"list","data":[{"id":"qwen","object":"model","created":1794314938,"owned_by":"forgeyard"}]}
```

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen", "messages": [{"role": "user", "content": "Say hello in five words. /no_think"}], "max_tokens": 100}'
```

It prints (shortened):

```json
{"choices":[{"finish_reason":"stop","index":0,"message":{"role":"assistant","content":"Hello, world!"}}],"model":"qwen","object":"chat.completion","usage":{"completion_tokens":7,"prompt_tokens":18,"total_tokens":25}}
```

`/no_think` asks Qwen3 to answer without thinking out loud first.

### Python, with the OpenAI SDK

```bash
pip install openai
```

Save as `hello.py`, next to `forgeyard-ca.pem`:

```python
import os
import ssl

import httpx
from openai import OpenAI

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
client = OpenAI(base_url="https://192.168.1.20:7860/v1", api_key=os.environ["FORGEYARD_API_KEY"],
                http_client=httpx.Client(verify=ca))
reply = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "Say hello in five words. /no_think"}],
    max_tokens=100,
)
print(reply.choices[0].message.content)
```

```bash
python hello.py
```

It prints:

```text
Hello, how are you?
```

Any program that speaks the OpenAI API works the same way: point it at
`https://192.168.1.20:7860/v1`, give it the key, and make it trust `forgeyard-ca.pem` (for many
programs, `SSL_CERT_FILE=forgeyard-ca.pem` is enough; see
[A program does not trust the certificate](troubleshooting.md#a-program-does-not-trust-the-certificate)
for what that variable replaces, and for Node.js).

## What next

- **Usage** shows requests, tokens and latency per key, model and person.
- Add people on **Users**: they get a one-time link to set their password. Roles are explained in
  [Concepts](concepts.md#roles).
- Add more machines the same way; replicas of one deployment spread over them, and the gateway
  balances between them.
- Something does not work? [Troubleshooting](troubleshooting.md).
