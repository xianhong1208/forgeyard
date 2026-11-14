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

`llama-cpp` picks one of two images for each card:

| Card | `llama-cpp` runs | Why |
|---|---|---|
| Instinct (`gfx908`, `gfx90a`, `gfx942`, MI300A included) and the discrete Radeon cards its ROCm image is built for (`gfx1030`, `gfx1100`, `gfx1101`, `gfx1102`, `gfx1200`, `gfx1201`) | **ROCm** (variant `rocm`, a 7.7 GB image) | Instinct cards have no Vulkan |
| Every other AMD GPU: APUs with unified memory (Strix Halo `gfx1151`, Strix Point `gfx1150`, Phoenix `gfx1103`, …) and discrete cards the ROCm image lacks (`gfx1031`, `gfx1032`, …) | **Vulkan** (variant `vulkan`, a 0.3 GB image with Mesa's RADV driver) | No ROCm needed in the image; the path used on a real Strix Halo |

A Vulkan container gets only its card's render node (`/dev/dri/renderD*`) and the group that owns
it, not `/dev/kfd`. Its memory is checked against the same pool as for ROCm: for an APU, the larger
of the carve-out and GTT. The deployment's events say which variant it got (`… (vulkan; reserves
…)`).

A deployment cannot choose the variant itself. To run a card with the other one, add a custom
recipe (**Models**, *Recipes*, **Add a custom recipe**): a copy of `llama-cpp` under another name, with only the variant you
want under `variants`.

**From a custom `llama-cpp-vulkan` recipe.** Before 1.3, Vulkan needed a custom recipe; the built-in
one replaces it. A deployment keeps the recipe it was made with, so deploy the model again with
`llama-cpp`, then delete the old deployment and, once nothing uses it, the custom recipe.

**A machine that cannot reach `ghcr.io`** gets the image as a file. On a machine that can, pull it
by its tag and check that it is the pinned one (the `DIGEST` of that tag must be the recipe's,
`sha256:28a56d03…`):

```bash
sudo docker pull ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242
sudo docker images --digests ghcr.io/ggml-org/llama.cpp
sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 | gzip > llama-vulkan.tar.gz
```

Copy the file over and load it there with `gunzip -c llama-vulkan.tar.gz | sudo docker load`. The
recipe names the image as `tag@sha256:digest`, and Docker looks it up by the digest. With Docker's
classic image store (`docker info` shows a storage driver such as `overlay2`), a loaded image keeps
its tag but not the digest it was pulled by (`docker images --digests` shows `<none>`): `docker run`
does not find it, tries to pull it from `ghcr.io`, and the replica fails with that error. With the
containerd image store (`driver-type: io.containerd.snapshotter.v1` in `docker info`), check
`docker images --digests` after loading. Where the digest is missing, use a custom recipe whose
image is the loaded tag without the digest; Docker then finds it locally and does not pull.

> AMD support has been checked on a real Strix Halo (Radeon 8060S, gfx1151) with ROCm 7.0.2: the
> inventory (through the `amdsmi` library, `amd-smi` and `rocm-smi`), joining, the GPU allowlist,
> and a model served through `/v1` with llama.cpp's Vulkan build as a custom recipe (build b11977).
> Not checked on real hardware yet: the built-in Vulkan variant (b12242), the ROCm images of the
> `vllm` and `llama-cpp` recipes, and discrete AMD cards (recorded data only). Report anything that
> looks wrong with the output of `forgeyard agent inventory --json --raw`.

## Add a CPU-only machine

Nothing special: run the one-line install there. Forgeyard shows the machine with its CPU and RAM,
and places CPU models on it:

- `llama-cpp` runs GGUF models on the CPU, as a container (with Docker) or, on x86_64 without
  Docker, as a program the agent downloads and checks against its SHA-256;
- `faster-whisper` runs speech to text on the CPU (with Docker).

When you deploy, choose **Runs on** → **CPU only**. Set **CPU threads** to the cores you want it to
use: more threads than free cores make llama.cpp slower, not faster.

## Run the agent in Docker

One `docker run` per machine, instead of the install script. Each release publishes the agent image
on Docker Hub as `docker.io/tonyhuang1101/forgeyard-agent`, for linux/amd64 and linux/arm64 (AMD GPUs
need amd64: ROCm is x86_64 only), tagged with the release's version. The command pulls the one that
matches your server.

**Machines** → **Add a machine** → **Docker** writes the command for you: pick the machine's GPUs
(NVIDIA, AMD or CPU only) and fill in the form above the command. The command follows the form as
you type, **Copy** copies exactly what is shown, and the values are remembered in this browser for
the next machine:

- **Model directory on that machine** (default `/srv/forgeyard-models`): a disk with room for
  models. It goes on both sides of the `-v` and into `FORGEYARD_MODEL_CACHE`. An absolute path,
  without spaces, quotes or colons.
- **Machine name** (optional): how it is listed on **Machines**; empty means its host name. Letters,
  digits, `.`, `_` and `-`, up to 64 characters. Adds `FORGEYARD_NAME`.
- **Server address, as that machine reaches it** (default: this server's `PUBLIC_URL`, or, while
  `PUBLIC_URL` is empty, the address you opened this page with): change it
  when the machine reaches the server by another address: through a VPN, a forwarded port
  (`https://127.0.0.1:7860`) or another LAN address. When that host is not in the server's certificate the page says so: add it to
  `TLS_EXTRA_NAMES` and restart the server, or use a name the certificate has.
- **Advanced → Port range** (`FORGEYARD_PORT_RANGE`, default `30000-32767`): the host ports for
  services and model replicas. Set it when only some ports reach the machine, for example
  `40000-40099` opened in a firewall. The server must be able to reach these ports.

A field the agent would reject shows its error instead of a command. The image's tag is the
server's own version, so agent and server always match. An admin can point the command at a mirror,
a private registry or a digest with [`AGENT_IMAGE`](reference.md#machines). Like the page, the
commands here start with `sudo`; as root, or when your account is in the `docker` group, leave it out.

A machine that cannot reach Docker Hub gets the image as a file, or builds it:
[Offline machines, or build it yourself](#offline-machines-or-build-it-yourself).

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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0 inventory
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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
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

### Offline machines, or build it yourself

The page shows these under **Advanced / offline**. A machine that cannot reach Docker Hub (no
internet access, or a DNS filter) gets the image as a file. On a machine that can, pull it and save
it:

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.2.0 && \
  sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.2.0 | gzip > forgeyard-agent.tar.gz
```

Copy `forgeyard-agent.tar.gz` to the new machine (`scp`, a USB disk) and load it there:

```bash
gunzip -c forgeyard-agent.tar.gz | sudo docker load
```

When the two machines differ (x86_64 and arm64), add `--platform linux/arm64` or
`--platform linux/amd64` to `docker pull`. Then run the page's command as it is: Docker finds the
image locally and does not pull it. With a digest in `AGENT_IMAGE`, put the tag on the command's last
line instead of the digest: a loaded image keeps its tag, not the digest it was pulled by (the page
says so too).

Or build the image on that machine, in a Forgeyard checkout of the server's version, under the same
name:

```bash
sudo docker build -f Dockerfile.agent -t docker.io/tonyhuang1101/forgeyard-agent:1.2.0 .
```

A server that names no published image ([Container images](reference.md#container-images)) puts a
local build, `forgeyard-agent:1.2.0`, in the command, and the page shows that `docker build` as its
first step.

### Move an installed agent into Docker

A machine that already joined keeps its identity if the container gets its state directory:

1. Pull the image: `sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.2.0` (or bring it
   over, as above).
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
     docker.io/tonyhuang1101/forgeyard-agent:1.2.0
   ```

4. `sudo docker logs -f forgeyard-agent` shows `Adopted replica …` for every running model: the new
   agent takes over the containers the old one started (they carry its id, which is in the state
   directory).

Process workloads the old agent started outside the container are not taken over: stop them from
the UI first.

## Offline machines

A machine that cannot reach the registry (no internet access, or a DNS filter that blocks `ghcr.io`
or Docker Hub) runs the built-in recipes from engine images you carry over as files. The recipe and
the deployment stay as they are.

1. **Find the image.** On **Models → Catalog and recipes**, open **Machine without registry access:
   load the image** on the recipe: it lists the image of each container variant with the commands
   below filled in. A machine gets the first variant whose `when` matches its card
   ([Built-in recipes](reference.md#built-in-recipes)): llama.cpp runs an AMD APU (gfx1151, gfx1150,
   …) on `vulkan`, for example. A replica on the deployment's page shows the image it runs.
2. **On a machine that can reach the registry**, pull the image by its digest, give it its tag, and
   save it. For llama.cpp's Vulkan variant (the page has the commands with its digest):

   ```bash
   sudo docker pull ghcr.io/ggml-org/llama.cpp@sha256:<digest> && \
     sudo docker tag ghcr.io/ggml-org/llama.cpp@sha256:<digest> ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 && \
     sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 | gzip > llama.cpp-server-vulkan-b12242.tar.gz
   ```

   Pulled by the digest, the file holds exactly the pinned image; `docker load` keeps the tag, which
   is how the agent finds it. When the two machines differ (x86_64 and arm64), add `--platform
   linux/amd64` or `--platform linux/arm64` to `docker pull`.
3. **Copy the file** to the offline machine (`scp`, a USB disk) and load it there (Podman: `podman
   load`):

   ```bash
   gunzip -c llama.cpp-server-vulkan-b12242.tar.gz | sudo docker load
   ```

4. **Deploy** as usual.

**How Forgeyard checks the loaded image.** Recipes pin images as `tag@sha256:…`, and Docker finds such
a reference by the digest only. A loaded copy often lacks it: Docker's classic image store keeps the
tag but drops the registry digest, and its containerd store can give the copy an id of its own. So
when the digest is missing but the tag is there, the agent compares that image with the ids recorded
for the pinned image (`forgeyard/recipes/images.lock.json`): the registry's digests (the index, and
each platform's manifest and config), or, when the id is one of the store's own, its content (the
layers, platform, entrypoint, command, environment, user, working directory and stop signal). A
match runs by its local image id, with no pull. Any other image under that tag is never run: the
agent asks the registry for the pinned image instead. So the built-in recipes work offline as they
are, with no custom recipe that names an image by tag only.

Ids are recorded for every image of the built-in recipes, and for a custom recipe that names one of
them exactly. Another image pinned by digest cannot be checked: on an offline machine, name it by its
tag only in your custom recipe. The check needs an agent from this release or later; an older one
tries to pull.

## Run a service or a task

Admins and operators run their own programs on the machines as **workloads**: open **Workloads** →
**New workload**, fill in the form, and choose **Create and start**.

1. **What:** **Service** (runs until you stop it; restarted when it exits) or **Task** (runs to the
   end). A **Name**: `a-z`, `0-9` and `-`, unique.
2. **Run:** **Container** (an **Image**, for example `python:3.13-slim`, with an optional
   **Entrypoint**) or **Process** (a **Command** on the basic `PATH` or an absolute path, run by the
   agent as its workload account). **Arguments** go one per line, without shell quoting.
3. **Where:** a **Machine**, or *Any online machine with room*; **GPUs**: *None*, *How many* (and
   a **Vendor**), or *These cards* on a chosen machine; **CPU cores**, **Memory** (for example `4Gi`)
   and **Replicas** (1 to 16, each on one machine).
4. **Environment:** the variables it needs. It starts from an empty environment; the variables
   Forgeyard adds are in the [reference](reference.md#workload-environment). Values are stored
   encrypted.
5. **Ports and health** (a service): **Add port** for each port it listens on. The agent picks a host
   port from its range and shows the address on the workload's page. A process listens on `$PORT`
   (or `${PORT_<NAME>}` in its arguments); a container gives its own port, which the host port maps
   to. **Health check**: *None*, *HTTP* (a **Path**) or *TCP*. **Restart**: *Always*, *On failure* or
   *Never*.
6. **Limits and retries** (a task): **Max duration (s)** stops and fails it when it runs longer;
   **Retries** after a non-zero exit, with a **Retry backoff (s)** that doubles each time, up to 5
   minutes.

*Spec that will be sent* shows the JSON the form makes. The workload's page shows each replica, its machine, GPUs and address, the
events, and the last lines of its output. **Stop** stops every replica (SIGTERM, then SIGKILL after
10 s) and frees its GPUs; **Start** places them again; **Delete** also removes its history and
output.

Isolation, in short: a process runs as the unprivileged workload account with only its own cards
visible; a container gets only its cards, never `--privileged`. See
[Workloads](concepts.md#workloads).

## Expose a workload at /x/

Call a service's own HTTP API through the gateway, with an API key. Admins and operators do this.

1. **The service listens on loopback.** Behind the agent's key guard a process must listen on
   `127.0.0.1`: give it `${BIND_ADDRESS}` and `${PORT}` in its arguments, which become `127.0.0.1`
   and its port. A container listens on its own port as usual (the agent publishes it on loopback).
   A process that listens on another address is stopped, so it is never reachable around the guard.
2. **Expose it.** In **New workload**, tick **Expose at** `/x/<name>` under **Gateway**; or, on a
   running workload's page, **Gateway** → **Expose** → **Save**: its replicas restart behind the
   guard. **Limits and timeouts** (collapsed) holds the **Port** (default: the first),
   **Read timeout (s)** (600), **Largest request (MiB)** (32), **Requests at once per replica** and
   **Requests at once per key** (no limit by default).
3. **Give a key access.** On **API keys**, a key's **Workloads at /x/**: *Listed workloads* (tick
   it) or *Every exposed workload*. New keys have *None*.
4. **Call it.** The workload's page shows its address and a `curl` call. The prefix is stripped:
   `/x/detector/v1/detect` reaches the service as `/v1/detect`.

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/detector/v1/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F image=@street.jpg
```

A service that builds links or redirects can read `X-Forwarded-Prefix` (`/x/<name>`), or set its root
path. For example, a FastAPI service run with `python3 -m uvicorn` as a process, with the
**Environment** variable `ROOT_PATH=/x/echo`:

```python
# app.py: a service behind /x/<name>. The gateway strips the prefix, so routes start at /;
# root_path makes the docs and redirects use the public prefix.
import os
from fastapi import FastAPI, Request

app = FastAPI(root_path=os.environ.get("ROOT_PATH", ""))

@app.post("/v1/echo")
async def echo(request: Request):
    return {"bytes": len(await request.body())}
```

and these **Arguments**:

```text
-m
uvicorn
app:app
--host
${BIND_ADDRESS}
--port
${PORT}
```

Turning **Expose** off closes `/x/<name>` at once; the guard stays in front of the replicas until the
workload is restarted. What passes and what is removed is in the
[reference](reference.md#the-passthrough-x).

## Start a workload from a template

**Workloads** → **Start from a template** offers ready-made vision APIs. Each runs Forgeyard's own
vision server image and is exposed at `/x/<name>` straight away:

| Template | Model (licence) | Call |
|---|---|---|
| **Object detection** | RT-DETRv2 ResNet-18, `PekingU/rtdetr_v2_r18vd` (Apache-2.0) | `POST /x/detector/v1/detect`: boxes, labels and scores |
| **Segmentation** | SAM 2.1 Hiera tiny, `facebook/sam2.1-hiera-tiny` (Apache-2.0) | `POST /x/segmenter/v1/segment` with a point or a box: masks |
| **Depth estimation** | Depth Anything V2 Small, `depth-anything/Depth-Anything-V2-Small-hf` (Apache-2.0) | `POST /x/depth/v1/depth`: a 16-bit depth PNG |

1. Choose **On an NVIDIA GPU** (the image's CUDA build, one card shared by memory, 3 GiB) or **On the
   CPU** (the CPU build, no GPU). The form opens filled in: the image, the task, the port, the health
   check and **Expose at** `/x/<name>`. Change anything, for example the name or the machine, then
   **Create and start**.
2. Allow an API key to call it (**API keys** → the key's **Workloads at /x/**).
3. Send an image as a multipart file `image` (or JSON `{"image": "<base64>"}`):

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/detector/v1/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg -F threshold=0.5
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/segmenter/v1/segment \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg -F 'points=[[320, 240]]'
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/depth/v1/depth \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg
```

Only permissively licensed models are offered: not Ultralytics YOLOv8 or YOLO11 (AGPL-3.0), and only the
Small Depth Anything V2 (Base and Large are CC-BY-NC-4.0). The models are inside the image, at pinned
revisions, so a template needs no network once its image is on the machine. For a machine that cannot
reach Docker Hub, copy the image (the CUDA build needs NVIDIA driver 580 or newer; without a usable GPU
it runs on the CPU):

```bash
docker pull docker.io/tonyhuang1101/forgeyard-vision:1.3.0-cuda
docker save docker.io/tonyhuang1101/forgeyard-vision:1.3.0-cuda | gzip > forgeyard-vision.tar.gz
# on the machine without the network:
gunzip -c forgeyard-vision.tar.gz | sudo docker load
```

On AMD machines use **On the CPU**: the image has no ROCm build. What each endpoint takes and returns
is in the [reference](reference.md#workload-templates). To try one without an API key, see
[Try a vision workload in the Playground](#try-a-vision-workload-in-the-playground).

## Try a vision workload in the Playground

**Playground → Vision** sends a picture to an exposed vision workload (one started from a
[template](#start-a-workload-from-a-template), or any exposed service started with `--task detect`,
`--task segment` or `--task depth`), as you, without an API key. Everyone signed in may use it; the
requests follow the workload's limits and count as `console` under `x:<name>` in **Usage**.

1. Pick the **Workload**: each is listed with its task and model. With none exposed yet, the tab says
   so; admins and operators get a link to **Start from a template**.
2. Drop a PNG, JPEG or WebP picture on the box, or **Choose an image** (at most 20 MiB, or the
   workload's **Largest request** if that is smaller).
3. Then, by task:
   - **Object detection:** **Detect objects** draws a box with the label and score of everything the
     model found; **Score at least** hides the weaker ones without asking again.
   - **Segmentation:** click a point on something, or drag a box around it. The best mask is laid over
     the picture; the buttons above it switch between the masks the model offers. Click or drag again
     for another.
   - **Depth estimation:** **Estimate depth** shows the depth map beside the photo, brighter or warmer
     where it is nearer, in **Colour** or **Grey**. The depth is relative, not metres.

Under the result: which replica answered, the picture's size and the time the model and the whole request
took. A busy, stopped or too-small workload is said in plain words; nothing reaches `/x/` with your
console sign-in, which takes API keys only. The routes are in the
[reference](reference.md#from-the-playground).

## Send images to a model

1. **Add a model that reads images.** On **Models → Add a model → Recommended**, the **Vision** group
   has Qwen3-VL 2B, 4B and 8B for vLLM, and Qwen3-VL as GGUF with its projector file for llama.cpp
   (a CPU, or an AMD APU). Any model works whose catalog entry says *Images*: the catalog reads it
   from the model's `config.json`. For a GGUF repository, choose its **Projector** file too (F16 is
   chosen when there is one; *None* makes it text only).
2. **Deploy it** as usual. *Images per request* (4), *Videos per request* (0, off) and *Largest
   request* (64 MiB) are under **Advanced**; the defaults work. The estimate shows the vision
   encoder (vLLM) or the projector (llama.cpp) as parts of its own.
3. **Call it** with an image as a `data:` URI or an `https://` URL:

   ```python
   import base64

   with open("photo.jpg", "rb") as f:
       photo = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
   client.chat.completions.create(model="qwen3-vl-8b", messages=[{"role": "user", "content": [
       {"type": "text", "text": "What is in this picture?"},
       {"type": "image_url", "image_url": {"url": photo}},
   ]}])
   ```

   An `https://` URL is downloaded by the gateway (public addresses only, at most
   `MEDIA_FETCH_MAX_MB` per image) and passed on as data. An image server on your LAN needs
   `MEDIA_ALLOW_PRIVATE_HOSTS=true` on the server.
4. **Try it in the Playground:** the image button next to the message box attaches images.

**Video:** set *Videos per request* to 1 or more under **Advanced** and send a `video_url` part.
On vLLM this makes the engine reserve memory for a long video when it starts, which leaves less for
the KV cache: lower the context if it does not start. On llama.cpp, give the deployment a context of
8,192 or more: Qwen3-VL reads a video at its own 2 frames a second (*Video frames per second* applies
to other models), and a 6-second clip took 5,345 tokens.

**A custom llama.cpp recipe** (another build, for example) reads images once it passes the
projector and says what it takes:

```yaml
optional_args:
  - if_model: mmproj
    args: [--mmproj, "{mmproj_file}"]
media: {inputs: [image, audio, video], files: false}
```

## Turn text into speech

1. **Add a speech model.** On **Models → Add a model → Recommended**, the **Text to speech** group
   has:
   - Qwen3-TTS 0.6B or 1.7B CustomVoice for an NVIDIA GPU (vLLM-Omni): Chinese and English, nine
     voices. About 10 and 12 GiB of GPU memory.
   - Qwen3-TTS 1.7B as GGUF for llama.cpp: Chinese and English on an AMD APU or the CPU; it has one
     voice of its own.
   - Kokoro 82M (speaches) and Chatterbox Turbo (llama.cpp): small English voices for a CPU.

   A model from **Search Hugging Face** works when it is tagged *text-to-speech* and a recipe serves
   its format: safetensors Qwen3-TTS for vLLM-Omni, a TTS GGUF with its projector (`mmproj-*.gguf`)
   for llama.cpp, an ONNX Kokoro or Piper repository for speaches.
2. **Deploy it** as usual. *Longest input* is under **Advanced**; its default works.
3. **Call it** with the OpenAI SDK:

   ```python
   with client.audio.speech.with_streaming_response.create(
       model="qwen3-tts-1-7b", voice="vivian", input="今天天氣很好。", response_format="wav",
   ) as response:
       response.stream_to_file("speech.wav")
   ```

   `GET /v1/audio/voices?model=qwen3-tts-1-7b` lists the voices and the formats the model makes.
   For audio as it is made, add `extra_body={"stream_format": "audio"}` with `pcm` or `wav`.
4. **Try it in the Playground:** a speech model gets a **Speak** box with its voices, formats and a
   player.

**Which format:** vLLM-Omni makes wav, pcm, flac, mp3 and opus; llama.cpp wav and pcm; speaches
mp3, flac, wav and pcm. Without `response_format` you get the engine's default (wav, or mp3 from
speaches), not OpenAI's mp3. `pcm` is 16-bit mono at the rate the answer states
(`x-audio-sample-rate`, or `rate=` in its `Content-Type`).

**On an AMD APU (Vulkan):** Qwen3-TTS can give slightly different audio for the same request with
llama.cpp's cooperative matrices on (ggml-org/llama.cpp#32050). If its speech sounds wrong there, add
a custom recipe whose vulkan variant sets `GGML_VK_DISABLE_COOPMAT: "1"` in its `env`.

## Generate images

1. **Add an image model.** On **Models → Add a model → Recommended**, the **Images** group has:
   - Z-Image-Turbo, Stable Diffusion XL and FLUX.1-schnell for an NVIDIA GPU (vLLM-Omni). About
     30, 17 and 42 GiB of GPU memory with the default caps; FLUX.1-schnell is gated (accept its terms on
     Hugging Face and give a token).
   - Z-Image-Turbo and FLUX.1-schnell as GGUF for stable-diffusion.cpp: an AMD GPU or APU (Vulkan) or
     an NVIDIA GPU (CUDA), about 18 and 19 GiB at 1024x1024, or the CPU (slowly).
   - Stable Diffusion 1.5 as GGUF for the CPU: small and old (512x512), to check that images work.

   A model from **Search Hugging Face** works when it is tagged *text-to-image* and is a diffusers
   repository (vLLM-Omni) or GGUF / safetensors files (stable-diffusion.cpp; choose the model file, and
   the text encoder and VAE files beside it are found by their names). The catalog shows its licence;
   check it before you use the images.
2. **Deploy it** as usual. *Images per request* and *Default size* are on the form; the largest side,
   steps, queue, timeout and offloading are under **Advanced**. Their defaults work.
3. **Call it** with the OpenAI SDK:

   ```python
   import base64

   out = client.images.generate(model="z-image-turbo", prompt="A lighthouse on a rocky coast at sunset",
                                size="1024x1024", n=1)
   with open("lighthouse.png", "wb") as f:
       f.write(base64.b64decode(out.data[0].b64_json))
   ```

   Steps, a negative prompt and a seed go in `extra_body`:
   `extra_body={"num_inference_steps": 8, "negative_prompt": "blurry", "seed": 7}`.
4. **Try it in the Playground:** an image model gets an **Image** tab with a prompt, the sizes the
   deployment takes and how many images, and a download link under each.

**How long:** seconds per image on an A100; minutes on a CPU. Measured on 16 to 32 cores of a busy
x86-64 machine: Stable Diffusion 1.5 at 512x512 with 20 steps about 100 s; Z-Image-Turbo (Q4_0) with 8
steps about 2 minutes at 512x512 and 8.5 minutes at 1024x1024. Raise *Timeout* under **Advanced** for slow
machines or large sizes.

**The model's own settings** (Z-Image-Turbo needs CFG 1 and 8 steps) live in the catalog entry's
engine arguments for stable-diffusion.cpp (`--cfg-scale 1 --steps 8`); the recommended entries carry
them. vLLM-Omni reads them from the model itself.

**On an offline AMD machine:** load the `sd-cpp` recipe's Vulkan image with `docker load` (see
[Offline machines](#offline-machines)). For the model, deploy it once on a machine that can reach
Hugging Face, then copy its directory from that machine's model cache
(`hub/models--<org>--<name>`, with `tar` or `rsync -a`: it uses relative links) into the same place in
the offline machine's cache. The deployment there finds it cached and downloads nothing.

## Generate videos

1. **Add a video model.** On **Models → Add a model → Recommended**, the **Video** group has
   Wan2.1-T2V-1.3B (832x480 at 16 fps, about 22 GiB of GPU memory with the default caps) and
   Wan2.2-TI2V-5B (1280x704 at 24 fps, about 33 GiB; its estimate is not yet measured), both Apache-2.0
   and for an NVIDIA GPU (vLLM-Omni). A model from **Search Hugging Face** works when it is a diffusers
   repository whose `model_index.json` names a text-to-video pipeline (`WanPipeline`).
2. **Deploy it** as usual. *Default size*, *Default length* and *Longest video* are on the form; the
   largest side, frames per second, steps, jobs at once, jobs per key, how long results are kept, the
   longest job and offloading are under **Advanced**. Their defaults work.
3. **Call it** with the OpenAI SDK: ask for the video, poll it, then download it.

   ```python
   import time

   video = client.videos.create(model="wan2.1-t2v-1.3b", prompt="A red kite over a green hill, slow camera pan",
                                seconds="5", size="832x480")
   while video.status in ("queued", "in_progress"):
       time.sleep(5)
       video = client.videos.retrieve(video.id)
   if video.status == "completed":
       client.videos.download_content(video.id).write_to_file("kite.mp4")
   else:
       print(video.error.code, video.error.message)
   ```

   `seconds` is a string, as in OpenAI's API. Give `size` as the model takes it: OpenAI's own sizes
   (`1280x720`, `720x1280`) are larger than Wan2.1-1.3B allows. Steps, a negative prompt and a seed go in
   `extra_body`: `extra_body={"num_inference_steps": 30, "negative_prompt": "blurry", "seed": 7}`.
   `client.videos.list()` lists your key's videos and `client.videos.delete(video.id)` cancels one at
   work or deletes a finished one.

   With curl, as a form:

   ```bash
   curl https://192.168.1.20:7860/v1/videos -H "Authorization: Bearer $FORGEYARD_KEY" \
     -F model=wan2.1-t2v-1.3b -F prompt="A red kite over a green hill" -F seconds=5 -F size=832x480
   curl https://192.168.1.20:7860/v1/videos/<id> -H "Authorization: Bearer $FORGEYARD_KEY"
   curl https://192.168.1.20:7860/v1/videos/<id>/content -H "Authorization: Bearer $FORGEYARD_KEY" -o kite.mp4
   ```

4. **Try it in the Playground:** a video model gets a **Video** panel with a prompt, the sizes and
   lengths the deployment takes, the job's progress, and the finished video playing in the page with a
   download link. Your earlier videos with that model are listed under it.

**How long:** minutes per video, one at a time per replica; the others wait their turn on it.
Raise *Longest job* under **Advanced** for long or large videos.

## Add a cloud provider

Admins: open **Providers**, choose **Add a provider**, pick OpenAI, Anthropic, Google Gemini, Azure
OpenAI, OpenRouter or *OpenAI-compatible server*, paste the key, and **Test connection**. Add the
models you want, then put one in a route: open a deployment, choose **Routing and cloud spill**, add
a tier with the cloud model, and save. Give the keys that may use it a cloud setting other than
**Never** on **API keys**. [Cloud providers](cloud-providers.md#add-a-provider) has the steps for
each provider, and what each one's endpoint cannot do.

## Track and limit cloud spend

Admins, once providers are in use:

1. **Set prices.** On **Providers**, choose **Refresh prices from OpenRouter**. Check the suggested
   matches, tick the rows to take, and choose **Apply ticked**. For a model OpenRouter does not list,
   open its provider, choose **Prices** on the model, type the prices from the provider's pricing
   page (US dollars per million tokens), and **Save prices**. A cloud model without a price is counted
   *without a price*, and is refused under any budget.
2. **Watch the cost.** **Usage** → **Cost** shows the cost per day, key, person, model or provider,
   and how much was served locally.
3. **Set budgets**, per calendar month (UTC), in US dollars:
   - a key: **API keys** → the key's **Limits** → **Monthly budget (USD)** → **Save limits**. An
     admin's value on someone else's key is locked for its owner; **Unlock budget** removes it;
   - a person: **Usage** → **Cost** → **Budgets this month**, with **Everyone** selected → **Set
     budget**;
   - a provider: its page → **Monthly budget** → **Save budget**.
4. **Watch the alerts.** At 80% and 100% of a budget, the **Audit log** gets one event
   (`budget.warning`, `budget.exhausted`), and the pages show a warning. There is no e-mail.

How cost is worked out, how a budget reserves money and what happens when it runs out:
[Cost and prices](cloud-providers.md#cost-and-prices) and [Budgets](cloud-providers.md#budgets).

## Restrict GPUs on a shared machine

1. Open **Machines**, find the machine, choose **Change GPU access**.
2. Tick only the cards Forgeyard may use, for example cards 2 and 3, and choose **Save**.

From then on:

- nothing Forgeyard runs is given another card, whatever a workload or deployment asks;
- the other cards show *not for Forgeyard*;
- memory is counted from what each allowed card reports **free**, so what other programs use on the
  same card is respected.

A card in use cannot be taken off the list: stop what runs on it first. Every change is in the
audit log, with before and after.

To keep a model on one card next to other programs, deploy it with **GPU sharing** →
**Share VRAM**: it then reserves only its estimate, and vLLM is told to use just that much (see
[vLLM does not start on a shared card](troubleshooting.md#vllm-does-not-start-on-a-shared-card)).

## Add people

Admins, on **Users**:

1. **Add user**: a **Username**, an optional **Email (optional)**, and a **Role** (`user`,
   `operator` or `admin`; see [Roles](concepts.md#roles)). Choose **Create and get link**.
2. Send them the one-time link. It sets their password, works once, and expires in 72 hours. You never
   see their password.

Later, from the same list: **Password link** gives a new link (24 hours) to someone who forgot their
password; **Sign out** ends all their sessions; **Disable** stops them signing in and stops their API
keys; **Delete** removes them. The owner cannot be disabled, demoted or deleted.

To let people ask for an account themselves, start the server with `REGISTRATION=approval`. The
sign-in page then offers to request an account, and requests wait on **Users** until an admin chooses
**Approve** or **Reject**.

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
use the system's trust store. The server checks the files every 12 hours and switches to a renewed certificate without a restart;
restart it to switch at once.

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

## Run the server in Docker

Each release also publishes the server, as `docker.io/tonyhuang1101/forgeyard` for linux/amd64 and linux/arm64.
It needs only Docker: no checkout, uv or Node. The server runs as an unprivileged account (uid
10001) and keeps everything it stores in a volume at `/data`:

```bash
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.2.0
sudo docker logs forgeyard
```

The log is the one in the [quick start](quick-start.md#1-install-the-server), with the one-time
setup token; continue with [Create the owner and trust the CA](quick-start.md#2-create-the-owner-and-trust-the-ca).
To read the token again later: `sudo docker logs forgeyard 2>&1 | grep -A 2 "setup token"`.

- **`PUBLIC_URL`**: the address people and agents use, with the port you publish. Inside the
  container the server sees only the container's own addresses, so this is how the host's address
  gets into the server certificate and into the commands on **Machines**. More names or addresses:
  `-e TLS_EXTRA_NAMES=forge.example.com,192.168.1.21`.
- **`-p 7860:7860`**: the image listens on port 7860 on all the container's addresses
  (`HOST=0.0.0.0`). Publish another port if you like, e.g. `-p 443:7860` with
  `PUBLIC_URL=https://192.168.1.20`.
- **`--hostname forgeyard`**: a fixed host name. Without it every new container has another one,
  and the server certificate is made again on start. That is harmless (browsers and agents trust
  the CA, not the certificate), only noisier.
- **The volume** holds the database, `secret.key`, `secrets.json` and `tls/`: keep it when you
  upgrade. A folder on the host works too, owned by uid 10001:
  `sudo install -d -o 10001 -g 10001 -m 700 /srv/forgeyard-data`, then `-v /srv/forgeyard-data:/data`.
- **Every other setting** is an `-e` ([Server settings](reference.md#server-settings)), or
  `--env-file` with a `.env` file. `ADMIN_USERNAME` and `ADMIN_PASSWORD` create the owner on the
  first start instead of the setup token. For PostgreSQL set `DATABASE_URL`: the image has the
  driver.
- **Health**: the image has a health check, so `docker ps` shows `healthy` once `/api/health`
  answers. It asks over the container's own loopback without checking the certificate: the request
  never leaves the container and carries nothing secret.

**Back up** the volume. Stop the server for a consistent copy of the database:

```bash
sudo docker stop forgeyard
sudo docker run --rm --user 0 -v forgeyard-data:/data:ro -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.2.0 tar czf /backup/forgeyard-data.tar.gz -C /data .
sudo docker start forgeyard
```

Restore into a new, empty volume with the same image (tar runs as root, so the files keep their
owner, the server's account):

```bash
sudo docker run --rm --user 0 -v forgeyard-data:/data -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.2.0 tar xzf /backup/forgeyard-data.tar.gz -C /data
```

As with any backup, keep `secret.key` apart from the database ([why](#back-up-and-restore)).

**Upgrade** by tag. Back up first, then pull the new version and start the same command with it; the
volume keeps everything, and database changes run on start:

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard:1.2.1
sudo docker rm -f forgeyard
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.2.1
```

`1.2.1` is exactly that release, `1.2` follows its newest patch release and `latest` the newest
release. Prefer an exact version: the agents' commands use the server's version, so upgrade them
right after it ([how](#upgrade-the-server-and-the-agents)).

**Pin a digest** when a tag must never change under you. Look it up:

```bash
sudo docker buildx imagetools inspect docker.io/tonyhuang1101/forgeyard:1.2.0
```

It prints, among other lines, `Digest: sha256:…`: run `docker.io/tonyhuang1101/forgeyard@sha256:…` instead of the tag. For
the agents, set `AGENT_IMAGE=docker.io/tonyhuang1101/forgeyard-agent@sha256:…` on the server: **Machines** then puts that
digest in every agent's command. Each image also carries its build provenance and an SBOM
(`docker buildx imagetools inspect --format '{{ json .SBOM }}' …`).

**Build it yourself**, from a checkout: `sudo docker build -f Dockerfile.server -t forgeyard:1.2.0 .`

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
   start. Read the CHANGELOG for anything that needs a step. In Docker: pull the new tag and start
   the same `docker run` with it ([how](#run-the-server-in-docker)).
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

   - **Docker**: `docker pull` the image of the server's new version (the **Machines** page names
     it), then `docker rm -f forgeyard-agent` and the same `docker run` with the new tag. The
     volume keeps the machine.
   - **Manual**: `git pull`, `uv sync`, restart the agent.

The **Machines** page shows each agent's version. Upgrade agents soon after the server: a much
older agent may lack what a newer server asks for.

## Check the setup health

Forgeyard checks its own setup for settings and combinations that are broken, risky or useless:
a misspelled setting, a key that may spend on cloud providers with no budget, a shared machine whose
GPU access nobody chose, an agent older than the server, a route that tries the cloud before its
local tier. Admins see the results in three places:

- **Get started → Setup health** lists every finding, with what to do and a link to where it is
  fixed.
- **A count in the top bar** shows errors and warnings. It disappears when there are none.
- **A banner on the page concerned** (Machines, API keys, Providers, Models, a route, a
  deployment, Users) shows that page's findings.

Users and operators see none of it.

- An **error** is broken or unsafe now. It stays until it is fixed.
- A **warning** is likely to cost money, leak or break. A **note** is useless or surprising. You can
  **Snooze 30 days** either one: it is hidden for you only, and comes back after 30 days if it still
  applies. **Show snoozed** lists them, with **Show again**.

Findings are worked out when the page asks, from the settings, the database and the agents' last
reports, so a fix shows within a minute (or at once on the next page). Settings problems are also
written to the server's log when it starts, each line beginning with `Settings:`. The full list is
in the reference: [Setup health checks](reference.md#setup-health-checks).

## Check a server end to end

After an upgrade, or before you rely on a new release, `scripts/accept_all.py` checks every feature
on your own server and machines: one command, a PASS, FAIL or SKIP line for each check (a SKIP says
why), and a summary at the end. It uses the API with an admin account, the official OpenAI SDK for
`/v1`, and a browser for the web console.

Run it from a Forgeyard checkout, on a computer that reaches the server (and, for the key guard
checks, the machines' replica ports), with the server's CA from **Trust this server**:

```bash
FORGEYARD_PASSWORD='…' python3 scripts/accept_all.py --server https://192.168.1.20:7860 \
  --ca forgeyard-ca.pem --user admin \
  --gpu-machine gpu-01 --cpu-machine cpu-01 --second-cpu-machine cpu-02 \
  --cloud-provider openai --out accept-all.json --shots accept-shots --install-playwright
```

Every machine and the provider are optional: leave one out and the checks that need it are skipped,
saying so. The groups, in the order they run:

| Group | What it checks |
|---|---|
| `server` | HTTPS with the CA, security headers, a foreign host name (421), the CSRF rule, sign-in back-off on a throwaway account, `/guide`, `/trust`, `/install.sh` |
| `machines` | each machine is online with its hardware; its GPU allowlist is enforced; join commands pin the CA |
| `workloads` | a process service, tasks (ok, retries, `max_duration`), a container task when the machine has Docker or Podman; on the GPU machine, that a workload sees only its card |
| `models` | deploys small models and waits for them: Qwen3-1.7B GGUF on each CPU machine; a chat, an embedding, a rerank and a speech model on the GPU machine (each skipped if its estimate does not fit); the estimate on the deployment page; **Try it** |
| `gateway` | `/v1`: chat, streaming, embeddings, rerank, transcription; key allowlists, limits and revoking; the key guard on every replica port; balancing between the two CPU machines; the Playground; usage |
| `cloud` | with `--cloud-provider`: spilling, keys set to **Never**, local only, the cloud maximum, cost, and budgets with their alerts |
| `passthrough` | a small HTTP app exposed at `/x/`: as a process on the CPU machine, in a container on the GPU machine. Keys (none, listed), the path as sent, refused paths, credentials and forwarded headers replaced, the forced sandbox, redirects, streaming, the body limit, rpm, concurrency, the read timeout, the replica's port behind the key guard, usage |
| `console` | every main page as an admin and as a plain user, at 1440 and 390 px: it loads, names itself in the tab, has no errors and no sideways scroll |

`--only gateway,cloud` or `--skip console` choose groups; `gateway` and `cloud` deploy the models
they need even when `models` is not chosen. The exit code is 0 when nothing failed, 1 when a check
failed, 2 when the run could not start.

What to know before you run it:

- **It cleans up after itself.** Everything it creates is named `accept-<run>-…`: deployments,
  workloads, routes, API keys, a throwaway user, and catalog entries it had to add. It removes them
  at the end, also after a failure or **Ctrl-C**. Usage rows and audit events of the run stay.
- **One request reaches the cloud provider,** a fixed short prompt, so it costs a fraction of a
  cent. The provider's monthly budget is changed for a few seconds and put back; the other budget
  checks use the run's own keys and user.
- **Five failed sign-ins** on the throwaway user: the server allows 30 failures per address in 15
  minutes, so wait between runs if you run it many times in a row.
- **Time:** about 15 to 20 minutes when the machines already have the models (3 minutes without a GPU
  machine); downloads come on top.
- `--kill-local` also checks failover by killing the engine of the CPU replica that runs on the
  computer where you run the script (one of the two CPU machines).
- The console checks need Playwright for Python and Chrome (`--chrome`, default
  `/opt/google/chrome/chrome`); `--install-playwright` puts Playwright, like the OpenAI SDK, in a
  virtual environment of the script's own (`--venv`). Without it, the console group is skipped.

## Back up and restore

**What to back up on the server**, all in its `data/` folder unless you moved it (in Docker: the
`/data` volume, [backed up like this](#run-the-server-in-docker)):

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

Once `secret.key` is backed up, tick **I have a backup of key id …** under **Get started → Setup
health**. Until an admin does, Setup health reminds every admin, and it reminds them again when
the key changes. The key itself never leaves the server: the page shows only its id.

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
