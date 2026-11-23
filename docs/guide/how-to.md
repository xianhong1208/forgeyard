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
  what `amd-smi monitor` shows too. GTT is the machine's own RAM, so when the card reports GTT,
  Forgeyard counts the card and the RAM as one pool (1.4.5): what a model on the GPU reserves is
  not promised again to a CPU model, and the card never offers more than the RAM has free. A
  carve-out is memory Linux does not see, so with it the two are counted apart.
- **Key:** the card's UUID when it has a serial number, else its PCI address (APUs have no
  serial, so their UUID is the same on every machine). A machine that joined with Forgeyard
  1.1.0 or older keeps its GPU allowlist when the key changes.

Models run on AMD with the `vllm` recipe's ROCm image and with `llama-cpp`. A process (an app or an engine) gets
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
engine (**Models → Engines → Add a custom engine**, *Start from a built-in engine*: `llama-cpp`): the copy
gets another name; keep only the variant you want under `variants` and save. A custom engine's card
has **Edit** and **Delete** (refused while a deployment uses it).

**From a custom `llama-cpp-vulkan` recipe.** Before 1.3, Vulkan needed a custom recipe; the built-in
one replaces it. A deployment keeps the recipe it was made with, so deploy the model again with
`llama-cpp`, then delete the old deployment and, once nothing uses it, the custom recipe.

**A machine that cannot reach `ghcr.io`** gets the image as a file. On a machine that can, pull it
by its tag and check that it is the pinned one (the `DIGEST` of that tag must be the recipe's,
`sha256:784bb87c…`):

```bash
sudo docker pull ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d
sudo docker images --digests ghcr.io/ggml-org/llama.cpp
sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d | gzip > llama-vulkan.tar.gz
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
> and models served through `/v1` with llama.cpp's Vulkan build, first as a custom recipe (build
> b11977), then with the built-in Vulkan variant (build b12242) from an image loaded from a file. Not
> checked on real hardware yet: the built-in Vulkan variant on llama.cpp 1.0.0, the ROCm images of the
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
  `40000-40099` opened in a firewall. The server must be able to reach these ports. Each port an app
  publishes takes one port of the range, and a model replica (or an app exposed at `/x/`) takes two: one
  for the agent's key guard and one for the engine behind it. Forgeyard counts them, so a range of 3
  ports holds one model: a machine whose range is full is skipped, and a replica with nowhere else to go
  waits ([No free host port](troubleshooting.md#a-replica-waits-no-free-host-port)).

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
  $(for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]*; do [ -e "$d" ] && printf -- '--device %s ' "$d"; done) \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  docker.io/tonyhuang1101/forgeyard-agent:1.5.2
```

The `$(for d in …)` line passes each NVIDIA device node of the host (`/dev/nvidiactl`, `/dev/nvidia-uvm`,
`/dev/nvidia-uvm-tools`, `/dev/nvidia-modeset` and every `/dev/nvidiaN`) with `--device` as well, skipping one the host
does not have. Docker accepts them next to `--gpus all`, and it matters: the devices `--gpus` gives are set up behind
Docker's and systemd's back, and the host's next `systemctl daemon-reload` takes them away from new processes in the
container, while the ones given with `--device` stay
([New processes on a machine cannot open its GPUs](troubleshooting.md#new-processes-on-a-machine-cannot-open-its-gpus)).
If `/dev/nvidia-uvm` is not there yet (no CUDA program has run on the host since it started), run
`sudo nvidia-modprobe -u -c=0` first. With CDI set up (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`)
you can use `--device nvidia.com/gpu=all` instead of both: CDI declares the devices, so they stay too.

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
  docker.io/tonyhuang1101/forgeyard-agent:1.5.2
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
  docker.io/tonyhuang1101/forgeyard-agent:1.5.2 inventory
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
  docker.io/tonyhuang1101/forgeyard-agent:1.5.2
```

### Why these options

- **`--network host`**: model replicas listen on the host's `127.0.0.1`, behind the agent's key
  guard, which listens on the replica's published port. Both only work if the agent shares the
  host's network. It also lets the agent publish ports on the machine's own address.
- **The model cache at the same path inside and outside** (`-v /srv/forgeyard-models:/srv/forgeyard-models`):
  the agent downloads models into it, then asks the host's Docker to start the engine container
  with the model mounted *by its path on the host*. Different paths would mount nothing.
- **`/var/run/docker.sock`**: models and container apps run as containers on the host's Docker,
  next to the agent, not inside it. That socket gives full control of the host's Docker, which is
  why the agent's container is trusted like the agent itself.
- **The `forgeyard-agent` volume** keeps the machine's credential and the pinned CA. Keep it: the
  same `docker run` after a restart, an upgrade or `docker rm` brings the same machine back, and
  the join token is not used again (the log says so). Without it the machine has to join again.
- **`FORGEYARD_*` variables** instead of flags: they are the same settings as in
  [agent.toml](reference.md#agent-settings). You can also mount a file:
  `-v /etc/forgeyard/agent.toml:/etc/forgeyard/agent.toml:ro`.
- The agent runs as **root** inside the container: it needs the Docker socket and switches to the
  unprivileged account `forgeyard-run` for processes. Apps and engines never run as root.
- There is no systemd in the container, so processes are limited with rlimits (the image
  sets `FORGEYARD_LIMITS=rlimit`). Processes run inside the agent's container and stop
  with it; models and apps in containers keep running on the host.

### Offline machines, or build it yourself

The page shows these under **Advanced / offline**. A machine that cannot reach Docker Hub (no
internet access, or a DNS filter) gets the image as a file. On a machine that can, pull it and save
it:

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.2 && \
  sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.5.2 | gzip > forgeyard-agent.tar.gz
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
sudo docker build -f Dockerfile.agent -t docker.io/tonyhuang1101/forgeyard-agent:1.5.2 .
```

A server that names no published image ([Container images](reference.md#container-images)) puts a
local build, `forgeyard-agent:1.5.2`, in the command, and the page shows that `docker build` as its
first step.

### Move an installed agent into Docker

A machine that already joined keeps its identity if the container gets its state directory:

1. Pull the image: `sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.2` (or bring it
   over, as above).
2. Stop the old agent: `sudo systemctl disable --now forgeyard-agent` (installed), or stop the
   process you started. Its models and apps in containers keep running.
3. Start the container with the old state directory and the same model cache, and **without** the
   join variables:

   ```bash
   sudo docker run -d --name forgeyard-agent --restart always --network host --gpus all \
     $(for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]*; do [ -e "$d" ] && printf -- '--device %s ' "$d"; done) \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /var/lib/forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     docker.io/tonyhuang1101/forgeyard-agent:1.5.2
   ```

4. `sudo docker logs -f forgeyard-agent` shows `Adopted replica …` for every running model: the new
   agent takes over the containers the old one started (they carry its id, which is in the state
   directory).

Processes the old agent started outside the container are not taken over: stop them from
the UI first.

## Run a machine without internet access

A machine with no internet access of its own (behind a firewall, on a DNS filter, or reached only through
an SSH tunnel) runs models like any other machine: it needs to reach only the server. Models, engine
archives and engine images come through the server, or from another machine that already has them, and
the agent checks every file as it checks a direct download. This section goes through it in order, from
joining to a deployed model, and links to the sections with the details. In the examples, `amd-01` has no
internet access and `gpu-01` has.

Give it an agent of the server's version. Downloads through the server need 1.4; sending files to other
machines, 1.4.1; the update command, counting free ports and removing engine images, 1.4.2. The machine's
page says when its agent is too old for something.

### Join it

1. **The agent reaches the server, and nothing else.** It connects out, over HTTPS, to the server's
   address (port 7860 unless you changed it), and everything it downloads later comes over that
   connection. Add the machine as usual: **Machines** → **Add a machine**. On a machine that cannot reach
   Docker Hub, bring the agent image as a file
   ([Offline machines, or build it yourself](#offline-machines-or-build-it-yourself)).
2. **The server reaches the machine's port range.** Model replicas listen on host ports of the agent's
   port range (`FORGEYARD_PORT_RANGE`, default `30000-32767`), and the gateway connects to them. Open the
   range in the firewall, or choose a small range and forward it.
3. **Through an SSH tunnel**, one connection carries both. From the server's host, `-R` brings the
   server's port to the machine and each `-L` brings one port of the range back. With the range
   `33000-33002`:

   ```bash
   ssh -N -R 7860:127.0.0.1:7860 \
     -L 33000:127.0.0.1:33000 -L 33001:127.0.0.1:33001 -L 33002:127.0.0.1:33002 \
     USER@amd-01
   ```

   Then add the machine with **Server address, as that machine reaches it** set to
   `https://127.0.0.1:7860` and **Advanced → Port range** set to `33000-33002`
   ([the form](#run-the-agent-in-docker)).

Placement counts the free ports of the range. A model replica takes two (the agent's key guard and the
engine behind it), so three ports hold one model; a machine whose range is full is skipped, and a replica
with nowhere else to go waits ([No free host port](troubleshooting.md#a-replica-waits-no-free-host-port)).

### Choose its internet access

On the machine's page (admins), under **Models in cache**, set **Internet access for downloads**
([details](#give-an-offline-machine-models-through-the-server)):

- **Always through the server**: for a machine with no internet access at all, like `amd-01`. Every
  model file, engine archive and pinned engine image comes through the server. The agent never tries the
  source first, so nothing waits for a failure, and the machine is never sent a Hugging Face token.
- **Auto** (the default): for a machine that usually reaches the internet. It downloads directly. Once a
  source cannot be reached from the machine (the name does not resolve, the connection is refused, reset
  or times out, the network is unreachable), the rest of that download, and the next ones from that host
  or registry for an hour, come through the server. An HTTP error (a missing file, a refused token) or a
  checksum mismatch never switches: the server would get the same answer.
- **Direct only**: never through the server. Such a machine that cannot reach a source is placed last for
  an hour ([A machine cannot reach Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face)).

Every byte that comes through the server crosses its network twice, so keep the machines that reach the
internet on **Auto** or **Direct only**.

### Get models onto it

Deploy as usual, or fill the machine's model cache ahead with **Download to…**, which fetches the engine
too ([Download a model before deploying it](#download-a-model-before-deploying-it)). Either way each file
comes through the server, from one of two places:

- **another machine's cache**: when a machine whose agent is 1.4.1 or later (`gpu-01`) has the model, it
  sends the file to the server, which passes it on. That machine also connects out only; it opens no port.
  The internet is used only when no machine can send the file
  ([From another machine](#give-an-offline-machine-models-through-the-server));
- **the internet**: the server downloads the file from Hugging Face or the model's URL and passes it on. A
  gated model needs the server's Hugging Face token (**Models → Sources**) or the deployment's own.

Which one it was shows in several places:

- the progress, on the deployment's page, under the model in the catalog and on the machine's page:
  *Downloading the model via the server: … of …* from the internet, *Downloading the model via the
  server, from gpu-01: … of …* from that machine's cache, then *Downloaded … via the server* or
  *Downloaded … via the server, from gpu-01* when it is done;
- **Download to…**, before you start: *via the server*, or *via the server, from gpu-01*, on the
  machine's line;
- the downloads API: `via` is `direct`, `server` or `machine`, with the sending machine in `source`
  ([the reference](reference.md#download-a-model-ahead-through-the-api));
- the server's log, one line per file: *Relayed … to amd-01: …*, or *… from gpu-01 to amd-01: …*.

Engine archives (llama.cpp's process variant on a CPU machine, for example) always come from the internet
through the server: agents delete an archive once it is unpacked, so no machine has one to send.

### Get engine images onto it

- **Pinned images come through the server by themselves.** Every built-in recipe pins its images by
  digest, Forgeyard's own vision image too (since 1.4.1). The server reads the image for the machine's
  platform from its registry and passes its layers on; the agent checks each one, loads the image and runs
  it only when it matches the recorded ids. The progress says *Pulling the image via the server: … of …,
  … of … layers* ([details](#give-an-offline-machine-models-through-the-server)). So a vision model
  ([Deploy an object-detection model](#deploy-an-object-detection-model)) deploys on such a machine as it
  does anywhere.
- **An image named by a tag only** (a custom recipe's, or one you built) has no recorded digest to check
  it against, so it cannot come through the server. Put it there from the machine's page →
  **Engine images**:
  - **Upload an image**: a `docker save` file from your computer
    ([Upload an engine image to a machine](#upload-an-engine-image-to-a-machine));
  - **Copy from another machine**: an image that `gpu-01`'s container engine already has, with no file
    ([Copy an engine image from another machine](#copy-an-engine-image-from-another-machine)).
- **Remove old images** on the same page when recipes move to newer ones: **Remove** works on an image
  no deployment there uses ([Engine images](#engine-images)).
- An agent older than 1.4 gets none of this: carry the image over as a file and `docker load` it
  ([Offline machines](#offline-machines)).

### Update its agent

After a server upgrade, the machine's page shows **Update the agent** with the complete command
([Update an agent](#update-an-agent)). For a machine set to **Always through the server**, the command
carries the agent image instead of pulling it. Run its first step on a machine that can reach Docker Hub
(replace `USER`, and the host name if you reach the machine by another):

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.2 && \
sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.5.2 | gzip | ssh USER@amd-01 'gunzip -c | sudo docker load'
```

Then run its second step on `amd-01`: the `docker rm -f` and the same `docker run` on the loaded image.
No `ssh` route to the machine? Save the image to a file and carry it
([Offline machines, or build it yourself](#offline-machines-or-build-it-yourself)). An agent before 1.4.2
does not report how it runs: update it that way by hand once, and its page shows the command from then on.

### When something goes wrong

- A download or an image *via the server* fails: each message, and what to do, is under *Through the
  server, it still fails* in
  [A machine cannot reach Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face).
- *Cannot be placed: amd-01: no free port: 3 of 3 in use (host ports 33000-33002)*: the port range is full
  ([No free host port](troubleshooting.md#a-replica-waits-no-free-host-port)).
- *Relay of … to amd-01 cut short: the source broke off (…)* in the server's log: the source (a host on the
  internet, a registry, or `machine gpu-01`) stopped in the middle of a file. The agent asks for the rest
  again by itself and the download goes on from what is on disk; look further only when it keeps
  happening with the same source (that network, or that machine's agent log).
- A replica says its image is not on the machine:
  [A replica fails: the image is not on this machine](troubleshooting.md#a-replica-fails-the-image-is-not-on-this-machine).
- The machine shows offline: the tunnel or the firewall between it and the server
  ([An agent shows offline](troubleshooting.md#an-agent-shows-offline)).

## Offline machines

For the whole path, from joining such a machine to a deployed model, see
[Run a machine without internet access](#run-a-machine-without-internet-access).

A machine that cannot reach the registry (no internet access, or a DNS filter that blocks `ghcr.io`
or Docker Hub) runs the built-in recipes from engine images you carry over as files. The recipe and
the deployment stay as they are.

Since 1.4 there are two easier ways, with a 1.4 agent: the built-in recipes' images (all pinned) come
**through the server** by themselves, layer by layer
([Give an offline machine models through the server](#give-an-offline-machine-models-through-the-server)),
and any image file can be **uploaded** from the machine's page
([Upload an engine image to a machine](#upload-an-engine-image-to-a-machine)). The steps below still
work, for an older agent or without the server.

1. **Find the image.** On **Models → Engines**, open **Machine without registry access:
   load the image** on the recipe: it lists the image of each container variant with the commands
   below filled in. A machine gets the first variant whose `when` matches its card
   ([Built-in recipes](reference.md#built-in-recipes)): llama.cpp runs an AMD APU (gfx1151, gfx1150,
   …) on `vulkan`, for example. A replica on the deployment's page shows the image it runs.
2. **On a machine that can reach the registry**, pull the image by its digest, give it its tag, and
   save it. For llama.cpp's Vulkan variant (the page has the commands with its digest):

   ```bash
   sudo docker pull ghcr.io/ggml-org/llama.cpp@sha256:<digest> && \
     sudo docker tag ghcr.io/ggml-org/llama.cpp@sha256:<digest> ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d && \
     sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d | gzip > llama.cpp-server-vulkan-v1.0.0-b12359-b1b770d.tar.gz
   ```

   Pulled by the digest, the file holds exactly the pinned image; `docker load` keeps the tag, which
   is how the agent finds it. When the two machines differ (x86_64 and arm64), add `--platform
   linux/amd64` or `--platform linux/arm64` to `docker pull`.
3. **Copy the file** to the offline machine (`scp`, a USB disk) and load it there (Podman: `podman
   load`):

   ```bash
   gunzip -c llama.cpp-server-vulkan-v1.0.0-b12359-b1b770d.tar.gz | sudo docker load
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
tag only in your custom recipe. The check needs an agent of 1.3 or later; an older one
tries to pull.

## Deploy an object-detection model

Object detection, segmentation and depth are model kinds of their own (1.4): you add them to the
catalog (**Models**) and deploy them from **Serving** like any other model. They run on Forgeyard's own vision
server (recipe `vision`): the CUDA build on an NVIDIA GPU (one card shared by memory), the CPU build on
any machine, AMD ones included. The model comes from the machine's model cache, so it is downloaded once
and an offline machine works once its cache has it.

1. **Models → Add a model → Recommended**: *Object detection, segmentation and depth* lists RT-DETRv2 R18
   (detection, the 80 COCO classes), SAM 2.1 Hiera tiny (segmentation) and Depth Anything V2 Small
   (depth), all Apache-2.0. Or search Hugging Face with the task **Object detection**, **Segmentation**
   or **Depth estimation**: any transformers detection model (RT-DETR, D-FINE, DETR without a timm
   backbone…), SAM 2 or 2.1, or depth model (Depth Anything, DPT…) with a permissive licence (Apache-2.0,
   MIT, BSD). Another licence, or none, cannot be added. Since 1.4.4 the list also has three detectors
   that **find what you name** (Grounding DINO tiny, MM-Grounding-DINO tiny, OWLv2 base): no fixed classes,
   each request says what to look for (see below). They need the vision image of 1.4.4 or later.
2. **Deploy**: the suggestion is the CPU (about 1.3 GiB of RAM, a fraction of a second to a second and a
   half per image on four threads) or an NVIDIA GPU (the files plus about 1.5 GiB on a shared card).
3. The deployment shows up under **Serving → Vision**, with its path. **Try it** on its page
   sends a test image; the **Playground** (the model under *Vision* in its picker) takes your own pictures.

Call it with an API key at `/v1/vision/detect` (or `/segment`, `/depth`), the model's name in `model`
and the picture as a file field `image`. A detector that finds what you name also takes `labels`, the
names to look for (a comma-separated field, or a JSON list), and refuses a request without them (400
`labels_required`):

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=grounding-dino-tiny -F image=@cats.jpg -F 'labels=cat, remote control' -F threshold=0.3
```

The other kinds:

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=rtdetr-v2-r18 -F image=@street.jpg -F threshold=0.5
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/segment \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=sam2.1-hiera-tiny -F image=@street.jpg -F 'box=[40, 40, 320, 240]'
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/depth \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=depth-anything-v2-small -F image=@street.jpg
```

From Python (vision is Forgeyard's own API: the OpenAI SDK has none, any HTTP client works):

```python
import base64
import os
import ssl

import httpx

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
key = os.environ["FORGEYARD_API_KEY"]
with httpx.Client(base_url="https://192.168.1.20:7860/v1", verify=ca, timeout=120,
                  headers={"Authorization": f"Bearer {key}"}) as fy:
    with open("street.jpg", "rb") as f:
        r = fy.post("/vision/detect", data={"model": "rtdetr-v2-r18", "threshold": "0.5"}, files={"image": f})
    for d in r.json()["detections"]:
        print(d["label"], round(d["score"], 2), d["box"])  # box: [x0, y0, x1, y1] in pixels

    with open("street.jpg", "rb") as f:
        r = fy.post("/vision/depth", data={"model": "depth-anything-v2-small"}, files={"image": f})
    with open("depth.png", "wb") as out:
        out.write(base64.b64decode(r.json()["depth_png"]))  # 16-bit grey, nearer is brighter
```

The answers: `detections` (label, score, box), `masks` (score, area, box and a 1-bit PNG each, best
first) or `depth_png` (with `min` and `max`); each with the image's `width` and `height`. Everything they
take is in the [reference](reference.md#vision). Usage counts one image per request and its
kilopixels; a key's model list and requests per minute apply as for any model.

A machine that cannot reach Docker Hub gets the vision image through the server, like every pinned engine
image ([Offline machines](#offline-machines)); or load it there (Models → Engines → `vision` → *Machine
without registry access* gives the commands).

## Classify images

Image classification (1.4.4) is a model kind of its own, *Image classification* (task `image-classify`),
answered at `/v1/vision/classify` by the same vision server; it is not text classification (`/v1/classify`).
Two sorts of model:

- **Fixed classes:** ViT, ConvNeXt V2, DINOv2 classifiers… (an `…ForImageClassification` model) know their
  own list, often the 1,000 ImageNet classes, and answer the best of them.
- **Zero-shot:** CLIP and SigLIP 2 sort a picture into the labels you send with each request (`labels`),
  each read as "This is a photo of …" (`template` changes that). SigLIP scores each label on its own
  (`multi_label: true`: several can fit, or none); CLIP shares 1 between them.

1. **Models → Add a model → Recommended → Image classification** lists SigLIP 2 base and LAION's CLIP
   ViT-B/32 (zero-shot), ViT base, ConvNeXt V2 tiny and DINOv2 small (ImageNet classes), Apache-2.0 or MIT.
   Or search Hugging Face with the task **Image classification**.
2. **Deploy** it like the other vision models: a CPU is enough (0.1 to 0.4 s per picture on four threads).
   It needs the vision image of 1.4.4 or later.
3. Call it:

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=vit-base-patch16-224 -F image=@cats.jpg -F top_k=3
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=siglip2-base -F image=@cats.jpg -F 'labels=cats on a sofa, a dog, an invoice'
```

The answer: `labels`, best first, each `{"label", "score"}`, and `multi_label`. A zero-shot classifier
without `labels` answers 400 `labels_required`; a classifier with fixed classes given `labels` answers 400
`labels_not_supported`.

## Try vision in the Playground

The Playground's vision panel sends a picture to a vision model ([deployed like any other](#deploy-an-object-detection-model)),
as you, without an API key. Everyone signed in may use it; the requests count as `console` under the
model in **Usage**. There is no Vision tab since 1.4: vision models are in the Playground's one model
picker, and 1.3's address (`/playground?tab=vision`) opens the first one. Apps made from the 1.3
templates are not offered here (apps left the console in 1.4 and are
[removed in 1.5](reference.md#apps-deprecated)).

1. Open the **Model** picker and choose a model under **Vision**: each is listed with its task. Only
   ready models are listed; with none at all, the Playground says so and admins and operators get a link
   to the catalog.
2. Drop a PNG, JPEG or WebP picture on the box, or **Choose an image** (at most 20 MiB).
3. Then, by task:
   - **Object detection:** **Detect objects** draws a box with the label and score of everything the
     model found; **Score at least** hides the weaker ones without asking again. A detector that finds
     what you name (1.4.4) first asks **Find these**: type the names, separated by commas (*cat, remote
     control*), then **Find**; the slider starts at 0.3, as these models score lower.
   - **Segmentation:** click a point on something, or drag a box around it. The best mask is laid over
     the picture; the buttons above it switch between the masks the model offers. Click or drag again
     for another.
   - **Depth estimation:** **Estimate depth** shows the depth map beside the photo, brighter or warmer
     where it is nearer, in **Colour** or **Grey**. The depth is relative, not metres.
   - **Image classification (1.4.4):** **Classify** lists the best classes with a bar for each score. A
     zero-shot classifier first asks for **Labels**, separated by commas (*a cat, a dog, a screenshot*).

Under the result: which replica answered, the picture's size and the time the model and the whole request
took. A stopped model, or a picture it cannot read, is said in plain words. The console sends it to
`/api/playground/vision/<task>`, the same as `/v1/vision/<task>` for the signed-in person
([reference](reference.md#vision)).

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
4. **Try it in the Playground:** an image model (under *Images* in the picker) gets an **Image** panel with a prompt, the sizes the
   deployment takes and how many images, and a download link under each.

**How long:** seconds per image on an A100; minutes on a CPU. Measured on 16 to 32 cores of a busy
x86-64 machine: Stable Diffusion 1.5 at 512x512 with 20 steps about 100 s; Z-Image-Turbo (Q4_0) with 8
steps about 2 minutes at 512x512 and 8.5 minutes at 1024x1024. Raise *Timeout* under **Advanced** for slow
machines or large sizes.

**The model's own settings** (Z-Image-Turbo needs CFG 1 and 8 steps) live in the catalog entry's
engine arguments for stable-diffusion.cpp (`--cfg-scale 1 --steps 8`); the recommended entries carry
them. vLLM-Omni reads them from the model itself.

**On an offline AMD machine:** with a 1.4 agent, the `sd-cpp` recipe's Vulkan image (pinned) and the
model come through the server, the model from another machine's cache when one has it
([Run a machine without internet access](#run-a-machine-without-internet-access)). With an older agent,
load the image with `docker load` ([Offline machines](#offline-machines)); for the model, deploy it once on
a machine that can reach Hugging Face, then copy its directory from that machine's model cache
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

## Classify text

Text classification (1.4.4) labels texts with a small, fast model: sentiment, the language, toxicity. It
runs on vLLM's pooling runner (recipe `vllm-pooling`) on a GPU, one card shared by memory.

1. **Models → Add a model → Recommended**: *Text classification* lists a multilingual sentiment model (1 to
   5 stars), a language detector (20 languages, Chinese among them) and a toxicity model (6 labels, each
   scored on its own), MIT or Apache-2.0. Or search Hugging Face with the task **Text classification**: a
   BERT, RoBERTa, XLM-RoBERTa or ModernBERT classifier with two labels or more can be added. The catalog
   shows its labels; a reranker (one output), a zero-shot (NLI) model and DeBERTa or DistilBERT classifiers
   are refused with the reason.
2. **Deploy** it. The context starts at the model's own maximum when that is shorter than the engine's
   default (512 tokens for these BERT-sized models). About 2 GiB on a shared card.
3. It shows up under **Serving → Text classification**. **Try it** on its page sends one sentence; the
   **Playground** takes your own texts, one per line, and shows every label's probability.

Call it with an API key at `/v1/classify` (Forgeyard's own API, vLLM's shape: the OpenAI SDK has none):

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "bert-multilingual-sentiment", "input": ["Great phone, fast delivery.", "Broke after a day."]}'
```

```json
{"id": "classify-…", "object": "list", "created": 1795000000, "model": "bert-multilingual-sentiment",
 "data": [{"index": 0, "label": "5 stars", "probs": [0.01, 0.01, 0.04, 0.26, 0.68], "num_classes": 5},
          {"index": 1, "label": "1 star", "probs": [0.81, 0.14, 0.03, 0.01, 0.01], "num_classes": 5}],
 "usage": {"prompt_tokens": 19, "total_tokens": 19, "completion_tokens": 0}}
```

From Python, with any HTTP client:

```python
import os
import ssl

import httpx

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
r = httpx.post("https://192.168.1.20:7860/v1/classify", verify=ca,
               headers={"Authorization": f"Bearer {os.environ['FORGEYARD_API_KEY']}"},
               json={"model": "xlm-roberta-language-detection", "input": ["今天天氣很好", "Bonjour à tous"]})
for d in r.json()["data"]:
    print(d["label"], round(max(d["probs"]), 3))
```

`truncate_prompt_tokens` (a number of tokens, or -1 for the model's maximum) cuts longer texts instead of
refusing them; up to 2,048 texts per request.

### Classify with a chat model, into labels you choose

A chat model classifies into any labels with structured outputs: it can only answer one of them. On vLLM
the shortest form is `structured_outputs.choice`:

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen3-4b", "temperature": 0,
       "chat_template_kwargs": {"enable_thinking": false},
       "messages": [{"role": "user", "content": "Which department should handle this? 我的發票金額開錯了"}],
       "structured_outputs": {"choice": ["billing", "shipping", "technical", "other"]}}'
```

The answer's `content` is one of the four. A JSON schema with an `enum` does the same on vLLM, llama.cpp
and cloud providers (`response_format: {"type": "json_schema", …}`). It is slower and costlier than a
classifier, but needs no new model, and labels can change per request. Zero-shot classifiers (NLI models
with your labels in the request, on the CPU too) come with the transformers engine in 1.5 (#144).

## Use gated Hugging Face models

Some models (Llama, Gemma 3, FLUX.1-schnell…) are *gated*: Hugging Face serves them only to an
account that accepted their licence. Forgeyard needs a token of such an account to inspect them and
to download them, and one token for the whole server is enough:

1. On huggingface.co, sign in, open the model's page and accept its licence. Then, under
   **Settings → Access Tokens**, make a token with *Read* access.
2. Admins: open **Models → Sources** and paste it into **Hugging Face token**
   and choose **Save token**. It is stored encrypted with the server's secret key and never shown
   again, only its last four characters.
3. Add and deploy the model as usual. Inspecting it uses the server's token, and the agent that
   downloads it gets the token with the download (it never stores it).

A deployment can still bring its own token: **Deploy a model → Advanced → Hugging Face token**. It
wins over the server's for that deployment only, and deployments made before 1.3.1 keep theirs. The
same goes for the token field in **Add a model**, which is used for that one inspection.
**Remove token** takes the server's token away; deployments with their own keep working.

A token is only ever sent to Hugging Face (`HF_ENDPOINT`), never to an `https://` GGUF host. Setup
health says when a deployment of a gated model has no token to use (`gated-model-no-token`).

## Add a cloud provider

Admins: open **Cloud → Providers**, choose **Add a provider**, pick OpenAI, Anthropic, Google
Gemini, Azure OpenAI, OpenRouter or *OpenAI-compatible server*, paste the key, and **Test
connection**. Add the models you want, then use one: open a deployment, and under **Cloud fallback**
add a cloud tier with the cloud model and save; or serve it under a name of its own with **Cloud →
Cloud models → New cloud model**. Give the keys that may use it a cloud setting other than
**Never** on **API keys**, or choose a [cloud policy](#choose-the-cloud-policy-for-keys).
[Cloud providers](cloud-providers.md#add-a-provider) has the steps for each provider, and what each
one's endpoint cannot do.

**Outside your network (cloud)** is ticked for you: always for OpenAI, Anthropic, Gemini, Azure and
OpenRouter, and for an *OpenAI-compatible server* when its base URL is a public address (or a name
that resolves to one). A private address (`10.x`, `172.16–31.x`, `192.168.x`, `100.64–127.x`,
loopback, link-local, IPv6 `fc00::/7`) or a name such as `gpu-box`, `*.local`, `*.lan` or
`*.internal` leaves it unticked. The form says why; change the tick if it is wrong. When the name
does not resolve from the server, the box stays unticked and the form says it could not tell.

## Choose the cloud policy for keys

Admins: **API keys → Cloud policy** decides what new keys get and how far people may open their own
keys to cloud providers. Pick one and choose **Save cloud policy**:

| Policy | New keys get | People may set their own keys to |
|---|---|---|
| **No cloud** (the default) | Never | Never |
| **People may opt in to spill** | Never | Only to spill |
| **Spill by default** | Only to spill | Only to spill |
| **Cloud allowed** | Only to spill | Allowed |
| **Custom** | your choice | your choice |

*Only to spill* sends a request to a cloud tier only when the local deployment is unavailable, busy or
answers 429 or 503; *Allowed* also uses cloud tiers placed before local ones. Changing the policy
changes no existing key's own setting, but a lower maximum applies at once to keys people set
higher. Admins may give any key any setting, which locks it for its owner. Budgets still apply:
[Track and limit cloud spend](#track-and-limit-cloud-spend).

When `GATEWAY_CLOUD_DEFAULT` or `GATEWAY_CLOUD_MAX` is set and nobody has saved a policy yet, the
panel marks the values as *from .env*. Saving replaces them for good.

## Track and limit cloud spend

Admins, once providers are in use:

1. **Set prices.** On **Cloud → Providers**, choose **Refresh prices from OpenRouter**. Check the suggested
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

## Change a deployment

On the deployment's page, choose **Edit** (admins and operators). You can change:

- **GPU sharing**: *Exclusive* keeps each card to one replica; *Share VRAM* reserves only the
  estimate, so other models fit in the rest of the card, sharing its compute
  ([GPU sharing](concepts.md#gpu-sharing));
- **GPUs per replica**, **Context length**, **Concurrent requests** and the recipe's other
  parameters, the memory override and the extra engine arguments;
- **Replicas**: more are added without touching the running ones; fewer remove the
  highest-numbered ones;
- **Machine**: *Automatic* (Forgeyard chooses) or *Pinned to* one machine. Pinning it to another
  machine moves its replicas there; going back to *Automatic* moves nothing;
- (1.5.2) **Engine version**: the built-in engine or a copy of it made with **Try this version**
  ([Move a deployment to another engine version](#move-a-deployment-to-another-engine-version)).

The model and the names stay: deploy a new one to change them. Before you save, the page
shows the new estimate, where each replica would go and what saving does. A change that does not fit
is refused like a deploy ([A deployment does not fit](troubleshooting.md#a-deployment-does-not-fit)).
A stopped deployment is only saved: the new settings apply when it starts.

Saving restarts the replicas on the new settings:

- **several replicas** restart one at a time: the next one only once the one before it is ready on
  the new settings, so the model keeps answering. The page shows *updating: 1 of 2 on the new
  settings* until all are done;
- **one replica** stops and starts again: the model does not answer until the new one is ready. Before
  you save, the page says about how long (1.5.2; its last start, else the other deployments of its
  engine, else a typical start) and how to avoid it. To avoid it, raise **Replicas** to 2 in the same
  change when there is room for two: the new replica starts first, then the old one restarts, and you
  can lower it again afterwards;
- changing only the number of replicas, or going back to *Automatic*, restarts nothing.

A replica that restarts (after an edit, or **Stop** then **Start**) goes back to the machine it ran on
when that machine still takes it, rather than the best fit elsewhere: that machine already has the
model and the engine. When it no longer has room, or is offline, the replica goes where it fits. The
server keeps this in memory: a replica still waiting for room when the server restarts is placed by
the usual rules.

From a script: `PATCH /api/deployments/{id}` ([reference](reference.md#change-a-deployment-through-the-api)).

## See how long a model takes to start

Each time a replica of a deployment becomes ready, the deployment's events say how long that start
took and where the time went, such as *Replica 0 on gpu-01: ready in 74.2 s (model 0.3 s, image
0.1 s, engine 73.8 s)*:

- `model`: checking the model's files in the machine's model cache, or downloading them (and a
  process engine's archive);
- `waiting`: waiting for another replica to finish loading on a GPU they share;
- `image`: checking the engine's image, or pulling it;
- `engine`: from launching the engine to its first healthy answer.

The parts add up to the total. The same start is `last_start` in `GET /api/deployments/{id}` and in
the list ([reference](reference.md#change-a-deployment-through-the-api)), and
`forgeyard_replica_start_seconds` in `/metrics` ([Metrics](reference.md#metrics)). With an agent older
than 1.5.0 the server can only measure the engine's part, and the event says *measured by the
server*: [update the agent](#update-an-agent) to see the rest.

While a replica starts, a 1.5.0 agent checks its health every second instead of every *interval* of
its recipe (10 s for vLLM), so a model that is ready is used within a second, not up to 10 s later.
It does so only during the recipe's start period, where failed checks do not count, and only until
the first healthy answer; afterwards the checks run at the recipe's interval, as before, so a replica
is never called unhealthy sooner than before.

### Keep vLLM's compile cache between starts

At every start vLLM compiles the model (torch.compile, and Inductor and Triton compile its kernels)
into a directory inside its container, which would go with the container. Since 1.5.0 the `vllm` and
`vllm-pooling` recipes keep that directory on the machine (`engine_cache: {env: VLLM_CACHE_ROOT,
enabled: true}`), so only a replica's first start compiles; its next starts, a wake after
[sleeping](#let-a-deployment-sleep-when-nobody-uses-it) among them, load what it compiled. Measured on
an A100 with Qwen3-0.6B: the first start took 118 s either way, and a restart 114 s without the cache
and 57 s with it, the same answers both ways; the directory held 44 MiB. You see it in a deployment's
events: the `engine` part of *ready in …* after a restart.

Nothing needs to be done to use it. To start without it (to rule it out while chasing a problem, say),
add an engine in **Models → Engines** → **Add an engine**, start from the built-in `vllm`, name it (for
example `vllm-nocache`), change its `engine_cache` line to `enabled: false` and deploy with it. Turning
it on or off never restarts a running replica: it applies from the replica's next start.

Each replica gets a directory of its own on its machine, `<model cache>/engine-cache/<key>`, mounted
writable at `/forgeyard/engine-cache` with `VLLM_CACHE_ROOT` pointing at it; no other deployment or
replica uses it. vLLM keeps what it compiles under a hash of the model, the settings that change the
graph, its own code and the compiler, so a changed parameter compiles anew instead of loading a wrong
graph. The directory goes when the deployment is deleted (or its recipe stops keeping one), and a
replica that starts with a new image (a new recipe version) loses its older directory. It needs a
1.5.0 agent with a model cache and a container engine; process variants, and replicas on older agents,
start as before. An image that does not run as root cannot write to the directory.

## Let a deployment sleep when nobody uses it

A model that is used now and then (one you try in the evening, a big model for a weekly job) can give its GPU
memory back while nobody calls it ([Sleep when idle](concepts.md#sleep-when-idle)). It is off unless you turn it
on, per deployment.

1. Turn it on in the form: **Serving → Deploy a model**, or **Edit** on a deployment's page. Under *When not in use*
   tick **Sleep when idle** and keep or change **Idle minutes** (30 by default, 1 to 1,440); under **Advanced**,
   **Wake wait** is how long a request waits for it to wake (300 seconds by default, 10 to 1,800). Beside the
   setting the form says how long the last start took (on the deploy form: the last start of another deployment of
   the same model and engine), which is about how long the first request after a sleep waits. Saving only these
   restarts nothing. From a script: `PATCH /api/deployments/{id}` with
   `{"params": {"sleep_when_idle": true, "idle_minutes": 30}}` (admins and operators; leave `idle_minutes` out for
   30 minutes; add `"wake_wait": 120` for requests to wait at most 2 minutes for it to wake instead of 5); a deploy
   takes the same parameters.
2. The deployment's page has a *When not in use* card: the setting, what it is doing now (*sleeps in 18 min unless a
   request comes*, or what keeps it awake: a request in flight, a replica starting…), the last request, the last
   start time and how often it was woken in the last 24 hours.
3. After the idle time without a request its replicas stop. **Serving** shows it **Asleep** (a moon), with *holds
   nothing* and the machine it wakes on; its page says since when, and its events say *Asleep: no requests for 30
   min; frees …*. **Machines** names it under the machine it would run on (*qwen3-4b asleep here (14.4 GiB on GPU
   #4 when awake)*): that memory is free for others meanwhile. The model stays in `/v1/models`; the machine keeps
   the model files and the engine image.
4. The next request wakes it: it waits until the model is ready, up to the wake wait, and is answered then (a key
   that may use the route's cloud tier is served there meanwhile). Its events say *Woken by a request (key …)*. Or
   wake it yourself with **Wake now** on its page or in the **Playground**, where an asleep model stays in the
   picker with the moon (`POST /api/deployments/{id}/start`); requests that arrive while it starts wait for it.
   Until it is ready Serving shows **Waking** with the time since the wake, and the Playground, which simply sends
   your message, says *Waking the model (about 74 s)…* while it waits. If it cannot wake (no room, its machine
   offline, its engine failing) requests get 503 `cannot_wake` with the reason.
5. **Stop** on an asleep deployment stops it for good: it leaves `/v1/models` until someone starts it.

The setup advisor helps with both directions: *had no requests for 3 days: let it sleep when idle?* for a
deployment that holds GPU memory nobody uses, and *woke 11 times in a day: raise its idle time* for one that
sleeps between the requests it gets and takes over a minute to start ([Setup health](reference.md#setup-health-checks)).

Choose the idle time by how long the model takes to start: a llama.cpp model is ready in seconds, a large vLLM
model can take minutes, so give that one an idle time well above its start (the default 30 minutes is). While it
sleeps its memory is free for others: on a shared card something else may take it, and the model then starts on
another machine that has room, or waits.

To check sleeping and waking on your own machines, run `scripts/accept_scale.py`
([Check sleep, waking and automatic replicas](#check-sleep-waking-and-automatic-replicas)).

## When a machine stays offline

A machine is *offline* once nothing has been heard from it for 30 seconds (`AGENT_OFFLINE_SECONDS`). From then on:

1. **At once:** the gateway no longer sends requests to its replicas. A deployment with replicas on other
   machines keeps answering from those; one with no other ready replica answers 503 `no_ready_replica`, saying
   *the machine is offline* (or spills to its cloud fallback). Only right after the server itself starts, when
   every machine shows offline until its agent reconnects (seconds), a machine not heard from yet keeps its
   replicas for those 30 seconds: their engines still answer meanwhile.
2. **After 10 minutes** (`AGENT_OFFLINE_MOVE_SECONDS`, counted from the last heartbeat): each replica of a
   deployment that is not pinned to that machine is placed again on a machine that fits, by the deployment's own
   rules (spread over machines, enough memory, the estimate as it is now). Its page shows *Replica 0: moved:
   gpu-01 offline for 10 minutes; placing it on another machine (gpu-02 fits)*, then where it was placed. It is the
   same replica, never a second copy: the deployment keeps its number of replicas.
   - A deployment **pinned** to the machine keeps its replicas there: they wait for it (*waiting for gpu-01 to
     come online (the deployment is pinned to it)*).
   - When **no other machine fits**, the replica stays and waits too, and moves on a later check once one does.
3. **When the machine comes back**, its agent gets its work list without the replicas that moved, and stops their
   engines. Replicas that waited start serving again. Nothing moves back by itself: to put a replica back, edit
   the deployment's machine.

A machine back within the 10 minutes (a reboot, an agent update, a short network drop) keeps everything; the time
starts again from its next heartbeat. After the server itself restarts, every machine shows offline until its
agent reconnects, so the 10 minutes count from the server's start. Set `AGENT_OFFLINE_MOVE_SECONDS=0` to never
move replicas. Apps (deprecated) follow the same rules, except that a task stays where it is: it may still be
running there, and running it again elsewhere could do its work twice.

## Let the number of replicas follow the load

A deployment that is busy at some hours and quiet at others can run more replicas only while it needs them
([Automatic replicas](concepts.md#automatic-replicas)). It is off unless you turn it on, per deployment.

1. Open the deployment on **Serving**, choose **Edit** (or deploy a new one), and set **Replicas** to **Automatic,
   by load**. Set the fewest and the most (1 and 2 unless you change them; up to 16). Choose **Save**: nothing
   restarts. A number outside the new bounds is brought into them at once (a replica more, or fewer).
   - From a script: `PATCH /api/deployments/{id}` with
     `{"params": {"autoscale": true, "min_replicas": 1, "max_replicas": 4}}`
     ([reference](reference.md#automatic-replicas)).
2. Set *Concurrent requests* to what one replica should take at once (the recipe's default is a good start): the
   load is counted against it, at 80 % unless you change *Target load per replica* under **Advanced**.
3. Watch the deployment's page: under **Replicas** it says *Automatic, 1 to 4 · 2 now*, the load against the target,
   a replica being drained and why one more does not fit; its events list every decision.

Before you rely on it:

- **Room:** a replica is added only where it fits now. On a shared card, keep enough memory free, or pin the
  deployment to a machine that has it. Two replicas of one deployment go on different machines when they can.
- **Starting takes time:** the new replica helps only once it is ready (seconds for llama.cpp, a minute or more for
  vLLM). Meanwhile requests wait in the line, and a key with a cloud tier may spill to it, as before.
- **Removing waits for streams:** the replica removed takes no new requests and finishes the ones it runs, however
  long, for up to 30 minutes; then it is removed anyway.
- To go back to a fixed number, set **Replicas** to **Fixed number** (`{"params": {"autoscale": null}}` and a
  `replicas` number from a script); the deployment keeps the replicas it has until you change the number.

To see a replica added under load and drained on your own machines, run `scripts/accept_scale.py`
([Check sleep, waking and automatic replicas](#check-sleep-waking-and-automatic-replicas)).

## Restrict GPUs on a shared machine

1. Open **Machines**, open the machine, choose **Change GPU access**.
2. Tick only the cards Forgeyard may use, for example cards 2 and 3, and choose **Save**.

From then on:

- nothing Forgeyard runs is given another card, whatever an app or deployment asks;
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

1. **Add user**: a **Username** and a **Role** (`user`, `operator` or `admin`; see
   [Roles](concepts.md#roles)). Choose **Create and get link**.
2. Send them the one-time link. It sets their password, works once, and expires in 72 hours. You never
   see their password.

Later, from the same list: **Password link** gives a new link (24 hours) to someone who forgot their
password; **Sign out** ends all their sessions; **Disable** stops them signing in and stops their API
keys; **Delete** removes them. The owner cannot be disabled, demoted or deleted.

To let people ask for an account themselves, start the server with `REGISTRATION=approval`. The
sign-in page then offers to request an account, and requests wait on **Users** until an admin chooses
**Approve** or **Reject**.

## Use the console in Traditional Chinese

The console speaks English and Traditional Chinese (1.5.1). The first time a browser opens it, it follows the
browser's language: any Chinese setting gives Traditional Chinese, anything else English. To change it, open the
account menu (your name, top right) and choose **English** or **繁體中文** under **Language**; on a phone the
same switch is at the end of the menu. The page changes at once, without reloading.

- The choice is kept **in this browser**, not in your account: another browser or computer starts from its own
  language again. Nothing is stored on the server, and nothing changes for API clients or scripts.
- It is **the guide's choice too**: the language links at the top of a guide page and the switch in the console
  set the same thing, and the console's help links open the guide in it.
- Numbers, sizes, times and dates follow the language (*41 秒前*, *2026年11月23日*).
- The server keeps writing its messages in English. The console translates the common ones by their code (for
  example `model_waking`, `cannot_wake`, `budget_exhausted`) and names each **Setup health** finding's kind in
  Chinese, with the server's own words (which name the machines and models) under it. A message without a
  translation is shown as the server sent it.
- Every page is translated: the frame (menus, sign-in, Get started and Setup health), **Machines**, **Serving**,
  **Models**, **Cloud**, **Playground**, **API keys**, **Usage**, **Users**, the **Audit log**, your account and
  **Trust this server**. Names you choose (machines, models, keys), what engines and agents print, and the API
  itself stay as they are.

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
  docker.io/tonyhuang1101/forgeyard:1.5.2
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
  docker.io/tonyhuang1101/forgeyard:1.5.2 tar czf /backup/forgeyard-data.tar.gz -C /data .
sudo docker start forgeyard
```

Restore into a new, empty volume with the same image (tar runs as root, so the files keep their
owner, the server's account):

```bash
sudo docker run --rm --user 0 -v forgeyard-data:/data -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.5.2 tar xzf /backup/forgeyard-data.tar.gz -C /data
```

As with any backup, keep `secret.key` apart from the database ([why](#back-up-and-restore)).

**Upgrade** by tag. Back up first, then pull the new version and start the same command with it; the
volume keeps everything, and database changes run on start:

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard:1.6.0
sudo docker rm -f forgeyard
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.6.0
```

`1.6.0` is exactly that release, `1.6` follows its newest patch release and `latest` the newest
release. Prefer an exact version: the agents' commands use the server's version, so upgrade them
right after it ([how](#upgrade-the-server-and-the-agents)).

**Pin a digest** when a tag must never change under you. Look it up:

```bash
sudo docker buildx imagetools inspect docker.io/tonyhuang1101/forgeyard:1.5.2
```

It prints, among other lines, `Digest: sha256:…`: run `docker.io/tonyhuang1101/forgeyard@sha256:…` instead of the tag. For
the agents, set `AGENT_IMAGE=docker.io/tonyhuang1101/forgeyard-agent@sha256:…` on the server: **Machines** then puts that
digest in every agent's command. Each image also carries its build provenance and an SBOM
(`docker buildx imagetools inspect --format '{{ json .SBOM }}' …`).

**Build it yourself**, from a checkout: `sudo docker build -f Dockerfile.server -t forgeyard:1.5.2 .`

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
   uv sync --extra server
   ```

   Then restart it (`sudo systemctl restart forgeyard`). Database changes run by themselves on
   start. Read the CHANGELOG for anything that needs a step. In Docker: pull the new tag and start
   the same `docker run` with it ([how](#run-the-server-in-docker)).

   **To 1.4:** seven settings deprecated in 1.3.1 are no longer read
   ([the list](reference.md#removed-settings)). Before you restart, rename `SECRET_KEY_FILE` to
   `FORGEYARD_SECRET_KEY_FILE` if you set it (otherwise the server refuses to start), and remove
   the others; Setup health lists any that are left.
3. **The agents**, one by one. Running agents keep their models and apps running while they restart.
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
     volume keeps the machine. Each machine's page shows that command, complete
     ([how](#update-an-agent)).
   - **Manual**: `git pull`, `uv sync`, restart the agent.

The **Machines** page shows each agent's version. Upgrade agents soon after the server: a much
older agent may lack what a newer server asks for.

From 1.4.5 on, a server and an agent of different versions keep working together while you upgrade, in either
order: each side ignores a message it does not know (with a warning in its log), and a server leaves out a card
of a kind it does not know yet instead of refusing the agent. The machine's page then lists it under
**Detection notes** as *unknown vendor*; update the server to use that card.

## Update an agent

Every machine's page has an **Update the agent** section. For admins it shows the complete command that brings
that machine's agent to the server's version, made from what the agent reported about how it runs (agents from
1.4.2 on report it each time they connect):

- **In Docker**: the agent reads its own container through the engine it already uses: its name, image,
  network, restart policy, GPUs (`--gpus`), devices and group ids (AMD's `/dev/kfd`, `/dev/dri` and the
  numeric `--group-add`), mounts and the variables it was given with `-e`. The command pulls the agent image
  of the server's version, removes the old container and starts the same one on the new image:

   ```bash
   sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.2 && \
   sudo docker rm -f forgeyard-agent && \
   sudo docker run -d \
     --name forgeyard-agent \
     --restart always \
     --network host \
     --gpus all \
     --device /dev/nvidiactl \
     --device /dev/nvidia-uvm \
     --device /dev/nvidia-uvm-tools \
     --device /dev/nvidia0 \
     --device /dev/nvidia1 \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
     -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     -e HF_TOKEN="$HF_TOKEN" \
     docker.io/tonyhuang1101/forgeyard-agent:1.5.2
   ```

   On an NVIDIA machine the command also passes the GPU device nodes with `--device` next to `--gpus` (from 1.4.5,
   the ones the agent reports; for an older agent the host's, listed by a short shell loop), so the agent keeps its
   GPUs when the host runs `systemctl daemon-reload`.

   A machine that gets its files through the server (**Always through the server**) gets the image carried
   instead: `docker save` on a machine that can reach Docker Hub, piped over `ssh` into `docker load` there
   (replace `USER`), then the same `docker rm -f` and `docker run`. The other machines show that way too, under
   *cannot reach the registry?*.
- **Installed with install.sh**: the one-line install again, without `--join` ([why](#upgrade-the-server-and-the-agents)).
- **From a checkout**: `git checkout` of the server's version and `uv sync`; then restart the agent the way you
  started it.

Copy the command and run it on that machine. Its models and apps keep running while the agent is replaced, and
the volume (or install.sh's state directory) keeps the machine: it comes back online as itself within seconds.

The agent never reports a secret: not the join token (a joined machine does not need it again), not its
credential, and not a variable whose name or value looks like one (`…TOKEN`, `…KEY`, `…PASSWORD`, a Hugging
Face or GitHub token, a URL with a password). Such a variable appears as `-e NAME="$NAME"`: set it in your shell
first (`export HF_TOKEN=…`), and the page says so. Values that cannot be repeated safely are left out and named.

**An agent older than the server** says so on its tile on **Machines** and on its page: *agent 1.4.0, server
1.4.2: update*; the hint opens the command. So does an agent of the server's version that lacks a feature such
an agent offers (built from older code), and **Setup health** lists both. An agent before 1.4.2 does not report
how it runs: update it by hand once ([how](#upgrade-the-server-and-the-agents)); from then on its page shows the
command.

## Check the setup health

Forgeyard checks its own setup for settings and combinations that are broken, risky or useless:
a misspelled setting, a key that may spend on cloud providers with no budget, a shared machine whose
GPU access nobody chose, an agent older than the server, a route that tries the cloud before its
local tier. Admins see the results in three places:

- **Get started → Setup health** lists every finding, with what to do and a link to where it is
  fixed.
- **A count in the top bar** shows errors and warnings. It disappears when there are none.
- **A banner on the page concerned** (Machines, Serving, API keys, Cloud, a cloud model,
  a deployment, Users) shows that page's findings.

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
| `workloads` | a process service, tasks (ok, retries, `max_duration`), a container task when the machine has Docker or Podman; on the GPU machine, that an app sees only its card |
| `models` | deploys small models and waits for them: Qwen3-1.7B GGUF on each CPU machine; a chat, an embedding, a rerank and a speech model on the GPU machine (each skipped if its estimate does not fit); the estimate on the deployment page; **Try it** |
| `gateway` | `/v1`: chat, streaming, embeddings, rerank, transcription; key allowlists, limits and revoking; the key guard on every replica port; balancing between the two CPU machines; the Playground; usage |
| `cloud` | with `--cloud-provider`: spilling, keys set to **Never**, local only, the cloud maximum, cost, and budgets with their alerts |
| `passthrough` | a small HTTP app exposed at `/x/`: as a process on the CPU machine, in a container on the GPU machine. Keys (none, listed), the path as sent, refused paths, credentials and forwarded headers replaced, the forced sandbox, redirects, streaming, the body limit, rpm, concurrency, the read timeout, the replica's port behind the key guard, usage |
| `tts`, `img`, `video` | speech, image and video generation on the GPU machine (and the CPU machine where a CPU engine exists): the SDK's calls, formats, caps, the queue, usage and the key guard |
| `vision` | detection, segmentation and depth models with the vision recipe: the results, refusals, keys and usage |
| `relay` | models through the server: with the CPU machine's **Internet access for downloads** set to **Always through the server**, **Download to…** of a small GGUF (26 MB) arrives through the server; then a second machine (`--relay-second-machine`, default the second CPU machine, else the GPU machine) gets it from the first machine's cache. Both machines' setting goes back, and the model leaves their caches |
| `images` | on a machine with Docker or Podman (`--engine-machine`, default the GPU machine): its engine images; an image file of a few KB that the script builds is uploaded, copied to a second machine that takes images (`--second-engine-machine`), and removed with **Remove engine image** (a recipe names it for a few seconds, since only images a recipe names may be removed) |
| `scale` | sleep when idle, waking (by a request, **Wake now**, the Playground), spill while waking, `model_waking`, `cannot_wake`, **Stop**, automatic replicas with draining, start times and their metrics ([below](#check-sleep-waking-and-automatic-replicas)) |
| `console` | every main page as an admin and as a plain user, at 1440 and 390 px: it loads, names itself in the tab, has no errors and no sideways scroll |

`--only gateway,cloud` or `--skip console` choose groups; `gateway` and `cloud` deploy the models
they need even when `models` is not chosen. The exit code is 0 when nothing failed, 1 when a check
failed, 2 when the run could not start.

What to know before you run it:

- **It cleans up after itself.** Everything it creates is named `accept-<run>-…`: deployments,
  apps, routes, API keys, a throwaway user, and catalog entries it had to add. It removes them
  at the end, also after a failure or **Ctrl-C**, with the relay group's model in the machines'
  caches, their fetch setting, and the images group's image and recipe. Usage rows and audit events
  of the run stay. Only on an agent older than 1.4.2, which cannot remove images, the tiny image
  `accept-<run>-image:1` stays, and the report says so.
- **A card shared with other work** (1.4.5): a deployment or app the server cannot place, because no
  card has the memory free, is skipped after `--place-timeout` seconds (5 minutes) with the server's
  reason (`Cannot be placed: GPU #4 has 1.8 GiB available, needs 3 GiB`), instead of waiting for
  `--timeout` (40 minutes, which stays the limit for downloads and starts). Before each group that
  deploys on the GPU machine (`tts`, `img`, `video`, `passthrough`, `vision`, `scale`), the run stops what its
  earlier groups left on a GPU and no later group needs, and removes it (the `models` group's
  deployments are only stopped: the console group shows one; the speech-to-text model stays while
  `tts` may use it), so each group has the run's share of the card to itself. The report's notes list
  what was freed.
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
- **Replica ports are called the way the gateway reaches them.** On a machine whose agent serves TLS (1.4.5), the
  key guard checks (here and in `scripts/accept_*.py`) connect over TLS to the replica's address, with the
  machine's binding name (`m<id>-<hash>.replica.forgeyard.invalid`, from **Machines**) as the name the certificate
  must carry, trusting only Forgeyard's CA. Each such port also gets three checks: plain HTTP is refused, the
  certificate verifies with that machine's name, and another machine's name (when two machines serve TLS) and a
  name no machine has are refused. A machine with an older agent is called with plain HTTP as before, with an
  information line that its traffic is not encrypted. The CA is the one given with `--ca`; when the server uses its
  own certificate or `TLS=off`, point `FORGEYARD_REPLICA_CA` at the server's `data/tls/ca.crt`. Without any CA the
  calls still run, and the certificate checks are skipped. Only a port that cannot be reached at all is skipped as
  another network's; one that closes the connection or shows a wrong certificate fails.

### Check sleep, waking and automatic replicas

`scripts/accept_scale.py` checks 1.5's [Sleep when idle](concepts.md#sleep-when-idle) and
[Automatic replicas](concepts.md#automatic-replicas) on your own server and machines. It is also the `scale` group
of `accept_all.py`, with the same options and the same report:

```bash
FORGEYARD_PASSWORD='…' python3 scripts/accept_scale.py --server https://192.168.1.20:7860 \
  --ca forgeyard-ca.pem --user admin \
  --cpu-machine cpu-01 --second-cpu-machine cpu-02 --gpu-machine gpu-01 \
  --cloud-provider openai --out accept-scale.json
```

It deploys Qwen3-1.7B GGUF on llama.cpp on the CPU machine, with **Sleep when idle** and an idle time of 1 minute
(`--scale-idle-minutes`), and checks, numbered as in its report:

1. **It goes asleep:** its replicas stop and their port no longer answers, its reservation on the machine is freed
   (`/metrics`, against what the machine's other replicas hold), `/v1/models` still lists it without waking it, and
   the model stays in the machine's cache.
2. **A request wakes it, once:** 20 requests at once (`--scale-parallel`) give one wake (one *Woken by a request*
   event, `forgeyard_deployment_wakes_total{by="request"}` one more) and are all answered.
3. **Wake now** (Start) and a Playground request wake it too, and the events say who did.
4. **Spill while waking**, with `--cloud-provider` only: the provider becomes tier 1 of the model's route; a key
   that may spill is answered by the cloud at once (`X-Forgeyard-Spilled: waking`, the one request that reaches the
   provider), a structured-output request with the same key waits and is answered locally, and once the model is
   ready the key is served locally again.
5. **A short wait:** with *Wait while it wakes* at 10 s (`--scale-wake-wait`), a request gets 503 `model_waking`
   with a `Retry-After` of at least 5 s and at most the last start, and the wake carries on. Only a model that starts
   slower than that can show it: a llama.cpp model on a CPU is ready in seconds, so there the step is skipped, saying
   so. With `--gpu-machine` the run also deploys Qwen3-0.6B on vLLM there and checks steps 1, 2 and 5 on it.
6. **Cannot wake**, only when you name a machine whose agent the run may stop: `--stoppable-agent cpu-01` with
   `--agent-stop` and `--agent-start`, two shell commands run on the computer where the script runs (the start
   command must return at once, leaving the agent running). While the model sleeps, pinned to that machine, the
   agent is stopped; a request gets 503 `cannot_wake` at once, naming the machine; once the agent is back the model
   starts and answers. Without these options the step is skipped: the script never stops a machine it was not told
   it may.
7. **Stop is not woken:** **Stop** on the asleep deployment; a request gets 503 `no_ready_replica`, nothing wakes it,
   and `/v1/models` no longer lists it.
8. **Automatic replicas**, on the two CPU machines: a deployment with replicas *Automatic, by load* from 1 to 2,
   *Concurrent requests* 2 and a scale-down window of 2 minutes (`--scale-down-minutes`). After its 2-minute pause,
   4 clients (`--scale-clients`) add a second replica within about a minute, on the other machine; every request is
   answered, and none twice (each replica's served count equals the requests sent to it). Then a long stream on the
   new replica: the scale-down drains it while the stream runs, short requests meanwhile go to the other replica, the
   stream ends complete, and the replica is removed only after its last byte. Nothing is added back for a minute.
   Without a second CPU machine, or without room for two replicas, the step is skipped.
9. **Automatic replicas with sleep:** the same deployment with *Sleep when idle* and a minimum of 0 goes asleep with
   1 replica and a request wakes it with 1.
10. **Start times and metrics:** each deployment's last start (`last_start`) and, on `/metrics`, the start times,
    the wakes, the requests that waited for a wake, the deployments' replicas and load, the autoscaler's decisions
    and the spills while waking.

What to know before you run it:

- **Time:** the sleepy model, automatic replicas and the vLLM model run side by side, about 15 minutes when the
  machines already have the models; step 6 and step 7 come after them, each waiting for one more sleep.
- **Metrics:** `/metrics` answers only a direct request from the server's own host unless the server has
  `METRICS_TOKEN`. Run the script there, or pass `--metrics-token` (or `FORGEYARD_METRICS_TOKEN`); otherwise the
  metric checks are skipped, saying so.
- **It cleans up after itself,** as `accept_all.py` does: everything is named `accept-<run>-scale-…` (deployments,
  their routes, API keys, a catalog entry it had to add) and removed at the end, also after a failure or **Ctrl-C**;
  an agent it stopped is started again. A deployment the server cannot place is skipped after `--place-timeout`
  seconds with the server's reason.

## Scrape Forgeyard with Prometheus

The server answers `GET /metrics` in the Prometheus text format: the gateway's requests, latency and tokens per
model, cloud spills, the waiting line, replicas by state, machines, GPU memory and cloud spend
([every metric](reference.md#metrics)). Labels are model, machine and provider names, never keys, people or
prompts.

**Who may read it.** Without `METRICS_TOKEN`, only a request from the server's own host that came through no proxy
(no `X-Forwarded-For`, `Forwarded` or `X-Real-IP`); anyone else gets 403. With `METRICS_TOKEN` set, every scrape
needs `Authorization: Bearer <token>`, from anywhere, the server's host included; without it or with a wrong one
it gets 401.

**Prometheus on the server's own host**, no token. Save the server's CA from **Trust this server** as
`/etc/prometheus/forgeyard-ca.pem`; the server's certificate always names `127.0.0.1`:

```yaml
scrape_configs:
  - job_name: forgeyard
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/forgeyard-ca.pem
    static_configs:
      - targets: ["127.0.0.1:7860"]
```

**Prometheus anywhere else, or in Docker:** set a token on the server (a long random value, e.g.
`openssl rand -hex 32`) in its `.env` or with `-e METRICS_TOKEN=…`, restart it, and give Prometheus the same value
in a file:

```yaml
scrape_configs:
  - job_name: forgeyard
    scheme: https
    authorization:
      credentials_file: /etc/prometheus/forgeyard-metrics-token
    tls_config:
      ca_file: /etc/prometheus/forgeyard-ca.pem
    static_configs:
      - targets: ["192.168.1.20:7860"]
```

- **Docker needs the token.** A request into a container through a published port does not arrive from the
  container's loopback, and a Prometheus container is another host: neither is "this host".
- **The name in `targets`** must be one the server answers to and its certificate names: an IP address always
  works. For a name, such as the server's container name on a Docker network (`forgeyard:7860`), add it to
  `ALLOWED_HOSTS` and `TLS_EXTRA_NAMES` (otherwise 421, or a certificate error).
- **Behind your own reverse proxy**, scrape through it with the token: a request it forwards is never treated as
  local, even from `127.0.0.1`.

Some queries to start from:

| Query | What it shows |
|---|---|
| `sum by (model) (rate(forgeyard_gateway_requests_total[5m]))` | requests per second per model |
| `sum by (model) (rate(forgeyard_gateway_requests_total{status=~"5.."}[5m]))` | server errors per second |
| `histogram_quantile(0.95, sum by (le, model) (rate(forgeyard_gateway_time_to_first_byte_seconds_bucket[5m])))` | the 95th percentile of the time to the first byte |
| `sum by (model, reason) (rate(forgeyard_gateway_spills_total[1h]))` | why requests leave the local tier |
| `forgeyard_machine_up == 0` | machines that are offline (an alert) |
| `forgeyard_gpu_memory_free_bytes / forgeyard_gpu_memory_total_bytes` | the free share of each card |

Counters start again from 0 when the server restarts (Prometheus' `rate()` handles that). The state (replicas,
machines, memory, spend) is read from the database at each scrape: scrape every 15 to 60 seconds, not faster.

## Back up and restore

**What to back up on the server**, all in its `data/` folder unless you moved it (in Docker: the
`/data` volume, [backed up like this](#run-the-server-in-docker)):

| What | Why |
|---|---|
| `data/forgeyard.db` (or your PostgreSQL database) | everything Forgeyard knows: users, machines, models, deployments, keys, usage |
| `data/secret.key` | it encrypts stored secrets (Hugging Face tokens, replicas' keys, apps' environment values). Keep it **apart** from the database backup |
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
the key changes.

**Or download the key from the console** (1.4.2). An admin can download the secret key and the
server's CA (`ca.key` and `ca.crt`) as one file, encrypted with a passphrase they choose:

1. Click **Download a backup…** on the reminder under **Get started → Setup health**, or **Download a
   key backup…** under **For admins** on **Trust this server**.
2. Type your password again (wrong passwords count like failed sign-ins: after a few, you wait), and
   a backup passphrase twice: at least 12 characters, not your password. Write the passphrase down:
   nothing can open the file without it.
3. Click **Download the backup**. The browser saves `forgeyard-key-backup-<key id>-<date>.fykey`.

The download ticks off the reminder for that key id and is recorded in the audit log
(`secret_key.backup_downloaded`). The server keeps neither the file nor the passphrase. Keep the
file apart from the database backup and the passphrase apart from the file: together they read
every stored secret and sign certificates your devices trust. The server certificate is not in it:
the server makes a new one from the CA when it starts. A server with your own certificate
(`TLS_CERT_FILE`) or `TLS=off` has no CA of Forgeyard's, so the file holds only the key; back up
your certificate yourself. The file's format is in
[Reference](reference.md#the-key-backup-file).

**On each machine** (optional): `/var/lib/forgeyard-agent` (the credential; without it the machine
joins again with a new token) and `/etc/forgeyard/agent.toml`. The model cache can always be
downloaded again.

**Restore**: stop the server, put the files back in `data/` (and `.env`), start it. With a wrong or
missing `secret.key` the server refuses to start and says so, instead of losing the secrets.

From a key backup downloaded from the console, put the database back first, then, with the server
stopped:

```bash
uv run --no-sync python -m forgeyard secrets restore forgeyard-key-backup-ab12cd34-20261117.fykey
```

It asks for the passphrase and writes `data/secret.key` (mode 600) and `data/tls/ca.key` (600) and
`ca.crt` (folders 700); `--data-dir DIR` writes into another data folder. A wrong passphrase, a
changed or cut-off file writes nothing. It never replaces a different key or CA that is already
there: it stops and says so, and `--force` replaces them, keeping the old files as
`<name>.replaced-<time>`. The same key already there is fine. `--passphrase-file PATH` reads the
passphrase from a file's first line, for scripts. When the server starts it checks that the key
belongs to the database, as always.

If the key is really lost, stop the server and start over with:

```bash
uv run --no-sync python -m forgeyard secrets reset --yes
```

It makes a new key and drops what the old one encrypted: set the server's Hugging Face token again
(and any deployment's own), model replicas restart with new internal keys, and apps with lost
environment values have to be created again (their events say so).

## Look at a model or an engine

Open **Models** and click a model's name in the **Catalog**, or an engine's name on the **Engines** tab
(names link to these pages everywhere: on **Serving**, on a deployment's page, in a machine's model
cache, in **Download to…**). Everyone signed in may look.

A **model's page** (`/models/<name>`) has a line of facts (task, size, licence, what one copy needs with
the first engine's defaults, how many deployments) and links to its sections:

- **Overview**: the source and revision (with a link to Hugging Face), the licence, the task and the path
  under `/v1` it answers at, the architecture.
- **Machines**: each machine, whether its model cache has the model (as its agent last reported, with
  the size there and what uses it), whether a copy **fits** there now (on a GPU, else on the CPU; the
  deploy page's estimate, with the engine's defaults), and the engine variant it would run with whether
  that image or program is there. Pick another engine above the table to see its verdicts.
  **Deploy here** and **Download here** start from that machine.
- **Deployments** of the model, and the **Engines** that run it (each opens its page).
- **Files**, folded to one line (how many files a machine downloads, their size, the model file);
  **Show files** lists them with their sizes, and the repository's other files (other quantisations
  and projectors) that are not downloaded.

**Deploy**, **Download to…** and **Remove** are at the top, as on the catalog. An admin sets the
**Display name** there, for example *Qwen3 4B* for `accept-qwen3-4b`; leave it empty to show the name.
The name itself never changes.

An **engine's page** (`/models/engines/<name>`) has **Overview** (what it serves and at which path, the
formats it reads, how it shares a GPU), **Variants** (per kind of hardware: the image or program, whether
the image is pinned by digest, what the machine needs, and the commands to carry an image to a machine
without registry access), **Machines** (the variant each machine would run, and whether its image is in
the machine's engine, as the machine last listed its images, or its program in the model cache),
**Parameters** (folded: their defaults and ranges), and the **deployments** and **catalog models** that use
it. A connected machine lists its images again when the page opens and its last listing is older than ten
minutes; **List again** asks now. An offline machine shows its last listing. An admin **edits** or
**deletes** a custom engine there, and **copies** a built-in one as a custom engine (for example to try a
newer image).

## See whether an engine has a newer version

Forgeyard pins each built-in engine to one version (vLLM 0.32.0, llama.cpp 1.0.0, …) and moves it only with
its own releases, after testing. To see whether upstream has published something newer meanwhile, an admin
opens **Models → Engines** and clicks **Check for newer versions**. Nothing runs on a timer and nothing
changes: the check only reads.

The server asks, through its own [network rules](reference.md#models) (https and public addresses only;
`MODEL_ALLOW_PRIVATE_HOSTS` for a mirror on your network), each built-in variant's **own** image repository
on Docker Hub or GHCR for its tags, and GitHub for the release notes of what it finds. It takes about half a
minute (at most a minute and a half). The result is kept until the next check, with when it ran and who ran
it, and everyone signed in sees it; the check is in the audit log.

- **The cards** get a badge: *0.34.0 available*, *Newer build*, *Up to date* or *Could not check* (when a
  registry or GitHub could not be reached; the badge's tooltip says why, and the other engines are still
  checked). The badge opens the engine's page.
- **An engine's page** has a **Newer versions** section, one block per newer version, newest first:
  - which variants have an image of that version, with its tag and the digest it has now, and why the others
    do not (for example *No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at
    0.32.0*: vLLM stopped publishing CUDA 12.9 images). A process variant (`cpu-process`) is included only
    when the release publishes the same archive with its SHA-256;
  - a link to the release notes, and the flags and settings this engine passes that the notes mention (for
    vLLM 0.34.0: `--gpu-memory-utilization`, whose default and meaning changed). Read those first: a newer
    version can change defaults and memory use, and Forgeyard's estimates were measured on the pinned one.

What counts as newer: a **stable** release published as an image, in the same form as the built-in's tag
(`v0.34.0-cu129` for a `v0.32.0-cu129` variant). Release candidates, nightly builds and model-specific images
are never offered, and a GitHub release whose images are not out yet is only mentioned. Every newer version
is listed, so you can take the smaller step. An engine published only under moving tags (stable-diffusion.cpp's
`master-cuda`) shows a **newer build** when its tag points at a later image. Forgeyard's own vision image is
not checked: it moves with Forgeyard.

## Try a newer version of an engine

When a check found a newer version, an admin can try it without touching the built-in engine. On the engine's page,
each version in **Newer versions** has **Try this version**. It makes a **custom engine**: a copy of the built-in
with that version's images, named `<engine>-x-y-z` (`vllm-0-34-0`; you can change the name). Nothing else changes:
the built-in stays as it is, and no deployment moves to the copy until you choose it.

The dialog says, before anything is made:

- **which variants the copy has**: those with an image of the new version. A variant without one is left out with
  the reason (vLLM 0.34.0 has no CUDA 12.9 image); machines that need it keep using the built-in. A process
  variant (`cpu-process`) is included when the release publishes the same archive with its SHA-256.
- **signatures**: llama.cpp signs its images and release archives (GitHub artifact attestations). Forgeyard checks
  each signature while it makes the copy, and makes no copy when one does not verify or is missing. When the
  signatures cannot be checked at all (GitHub or Sigstore cannot be reached from the server), the dialog says why
  and offers **Create it without verification**: the copy is then marked *Not verified* wherever its signatures are
  shown, and the audit log says who chose it. vLLM, vLLM-Omni and speaches publish no signatures, and stable-diffusion.cpp only an unsigned build
  record: their copies are pinned by digest only, and the dialog and the page say so.
- **what to read first**: a new version can change flags, defaults and memory use. The copy keeps the built-in's
  arguments as they are, and Forgeyard's estimates were measured on the built-in's version. The flags its release
  notes mention are listed again, with a link to the notes.

**Make the copy** resolves every image's tag again. If a tag points at another image than when you checked, nothing
is made: check again, then try. Each image is pinned by its digest, and the ids Forgeyard needs to recognise it are
recorded (the same ids the built-in images have). So a copy's image **comes through the server** to a machine that
cannot reach the registry, **runs offline** after `docker load`, and is listed on machine pages and removable with
**Remove** like a built-in's. The copy is in the audit log as `recipe.created`, with where it came from.

The copy's page says where it came from, when it was made and by whom, each variant's pinned digest and what was
verified about it (the signing workflow and the build, or *unsigned upstream*), and what was left out. The catalog's
engine arguments for the built-in (Qwen3-Reranker's `--hf-overrides` for `vllm-pooling`) apply to its copies too.

To use the copy, [try it out](#try-out-a-new-engine-version-before-switching) first, then move a deployment to it on the
deployment's **Edit** page ([Move a deployment to another engine version](#move-a-deployment-to-another-engine-version)),
or deploy a model with it (pick it as the engine on the deploy page), and compare it with the built-in before you move
other deployments. **Download to…** on the copy's page fetches its image to the machines first
([Download an engine ahead of time](#download-an-engine-ahead-of-time)). Delete the copy when you are done with it: it cannot be deleted while a
deployment uses it. Remove its image from the machines first (their pages, **Engine images**): once the copy is
deleted, Forgeyard no longer counts that image as one of its own.

## Try out a new engine version before switching

Before you move deployments to a new version of an engine, **try it out**: Forgeyard deploys one model with it on one
machine for a few minutes, checks it and removes it again. It is usually done on a copy made with
[Try this version](#try-a-newer-version-of-an-engine), but works on any engine; a trial of the built-in engine gives
the numbers its copies are compared with. Admins and operators can start and cancel trials; everyone signed in sees
the results.

1. Open the engine's page (**Models**, the **Engines** tab, then the engine) and click **Try it out**.
2. Pick a **model** this engine serves and a **machine** (the machines a variant of the engine runs on; an offline one
   cannot be picked). The **parameters** are those of a deployment of that model on the built-in engine when there is
   one, so the trial runs it as it is served now; else the engine's defaults. Change them in the dialog if you like
   (context, concurrency, **Exclusive** or **Share VRAM** on a GPU). The advanced ones keep their values.
3. The dialog shows the estimate and whether it fits on that machine now, refused with the usual reasons when it does
   not, and says when the engine's image is not on the machine yet: the trial then pulls it first, which can take many
   minutes (a vLLM image is about 9 GiB); **Download to…** on the same page fetches it ahead.
4. **Time to start** bounds the start, downloads included (30 minutes by default, 5 to 180). Click **Start the trial**.

The trial's deployment is **isolated**: it has no route, so it is not in `/v1/models`, no API key and no Playground
can call it, and the gateway counts no usage or cost for it. It never sleeps and is never scaled, Serving names it
only as a trial in progress, and a machine's page shows the card it holds as *Trial of* the engine. While it runs,
neither the engine nor the model can be deleted. At most four trials run on a server at once, and one per engine.

Forgeyard then checks, straight against the replica with its internal key (as **Try it** does):

| Check | What it is | Fails when |
|---|---|---|
| **Starts** | How long it took to become ready, part by part (model, image, engine), against the built-in engine's last start of the same model | It cannot be placed, its start fails (its last output is kept), or it is not ready within the time limit |
| **Memory** | What it uses on its GPU (or of the machine's RAM): the machine's readings after it was ready minus those before the engine started, a few each, against what the replica reserved | It uses more than it reserved (5% allowed for readings on a busy card) |
| **Answers** | One request to warm it up, then five more of its model's kind: chat (a fixed question, temperature 0, at most 128 tokens), embeddings, rerank (the Paris sentence must come first), text classification, speech to text (the test clip's words), text to speech, images (one), vision | An answer is not one of that kind, or is an error |
| **Latency** | p50 and p95 of those five, and the first one | — (it is shown, not judged) |
| **Same answer** | When a deployment of the same model on the built-in engine is ready, the same request once to it, and whether the answers match | — (a new version may answer differently: read both) |

Video is not asked (a video takes minutes): only its start and memory are checked. On a card other work shares, the
memory is approximate, and the check says so. When other work released memory meanwhile, or the machine sent no
readings in time, the memory is **not measured**: the check and the result say why in words, never with a number. The trial **passes** when no check fails; then, or when a check fails,
when you **Cancel the trial**, when it runs out of time, or when the server restarts during it, its deployment is
removed. A trial is in the audit log as `engine_trial.started`, then `engine_trial.finished` or
`engine_trial.cancelled`.

The engine's page keeps every trial under **Trials**, newest first: the model, the machine, who ran it and the
result. **Checks** opens a trial's table (measured, against what, a note) with how it compares with the built-in
engine: a deployment of the same model there (its last start and what it reserves) and the built-in's own latest
passed trial (its start, its memory and its latency). An engine's card in **Engines** shows its last result, and
**Edit** on a deployment says how the chosen version's last trial went (*Last tried out on gpu-01 with qwen3-0.6b:
Passed*) or that it was never tried. That is information only: you can move a deployment to a version that was never
tried.

From a script: `POST /api/recipes/{name}/trials` ([reference](reference.md#trial-runs-through-the-api)).

## Move a deployment to another engine version

A deployment can move between a built-in engine and its copies made with [Try this version](#try-a-newer-version-of-an-engine),
for example from `vllm` (vLLM 0.32.0) to `vllm-0-34-0` and back. Its name, served model name, API keys and routes stay,
so clients see only the restart.

1. On the deployment's page choose **Edit** (admins and operators).
2. Under **Engine**, pick the version in **Engine version**: the built-in one and each copy of it that serves the model
   the same way. An engine without copies shows only itself, with a link to its page, where you can check for newer
   versions.
3. The form shows the new version's parameters. **What saving does** lists the engine change, the parameters the new
   version does not take (they are dropped), how the replicas restart, and on the right the new estimate (a copy can
   count memory differently). A version that does not fit now is refused like any other change.
4. **Save and restart**.

The replicas restart as for any change: with several, one at a time, so the model keeps answering. With one running
replica the model does not answer until the new one is ready, and the form says so before you save:

- about how long: its last start, from its events; else the middle value of the latest starts of the other
  deployments of this engine on this server; else a typical start for the engine (vLLM about two minutes on an A100,
  llama.cpp a few seconds);
- that a new engine version can take longer on its first start (it may compile first), and, when its image is not on
  the machine yet, that it is downloaded first ([download it ahead](#download-an-engine-ahead-of-time));
- how to avoid the gap: raise **Replicas** to 2 in the same change when there is room for two (the new replica
  starts first), or turn on automatic replicas; and, when the deployment's route has a cloud tier, that keys allowed
  to use the cloud are answered there meanwhile.

It only says so: saving does what it always did. Starting the new version beside the old one needs room for both on
the card, which a shared GPU often does not have, so Forgeyard does not do it for you.

To go back, choose the built-in engine again. The change is in the audit log as `deployment.changed`, with `recipe`
(from and to), both versions and the dropped parameters, and the deployment's events say *engine vllm →
vllm-0-34-0*. A deployment's page says when it runs a copy (*Runs a copy: vLLM 0.34.0 (based on vllm)*). When a
later Forgeyard release moves the built-in engine to the copy's version or past it, the copy's page says so: move the
deployments back, then delete the copy. Nothing moves by itself.

From a script: `PATCH /api/deployments/{id}` with `"recipe"` ([reference](reference.md#change-a-deployment-through-the-api)).

## Download an engine ahead of time

**Download to…** on an engine's page (a built-in engine or a copy) fetches only the engine to the machines you
choose: the image of the variant each machine would run, by its pinned digest, or its process program, checked
against its SHA-256. No model, and nothing is started. Admins and operators can do it. Use it to put a 9 GiB vLLM
image on a machine before you move a deployment there.

1. Open the engine's page (**Models**, the **Engines** tab, then the engine) and click **Download to…**.
2. Tick the machines. Each line says the variant it would get and whether it can: **fits**, **already here**,
   **downloading**, **offline**, **update the agent**, or **No variant**, with why, on a machine no variant of this
   engine runs on (vLLM on a machine without a supported card, for example).
3. Click **Download to N machines**.

The progress shows on the engine's page (in its machines table and under **Downloads of this engine**) and on each
machine's page, with **Cancel**. A machine set to fetch through the server gets the image that way
([Give an offline machine models through the server](#give-an-offline-machine-models-through-the-server)): a copy's
image comes through the server like a built-in's, by the ids recorded when it was made. Each request and cancel is in
the audit log (`engine.download_requested`, `engine.download_cancelled`), and deleting the engine ends its downloads
under way. The API is in [the reference](reference.md#download-a-model-ahead-through-the-api).

## Download a model before deploying it

**Download to…** puts a model, and by default its engine, into the model cache of the machines you
choose now, so a deployment there starts without waiting for the download. Nothing is started.
Admins and operators can do it.

1. Open **Models** (the **Catalog** tab), and click **Download to…** on the model's row. Or open a machine's
   page and click **Download a model here…** above **Models in cache**, then pick the model.
2. Tick the machines. Each line says whether that machine can take it:
   - **fits**: how much it downloads, the free disk, and the engine it would fetch;
   - **already here**: the model (and the engine) is in that machine's cache;
   - **downloading**: a download of this model is already under way there;
   - **offline**, **no model cache**, **not enough disk** (the model plus a 1 GiB margin);
   - **update the agent**: an agent older than 1.4 cannot download ahead.
3. **Also the engine, for the recipe** is on by default. For the recipe you choose (the first one
   that serves the model, as on the deploy page), it fetches what that machine would run: the engine
   archive of a process variant, or the image of a container variant, pulled as a deployment pulls
   it. Untick it to download the model only.
4. Click **Download to N machines**.

The progress shows under the model's row in the catalog and on each machine's page, with the same
words and bar as a deployment's download. **Cancel** stops it at once on that machine; what is on
disk is kept, and **Download to…** again goes on from there. A download on a machine that goes
offline waits and goes on when its agent is back. When it is done, the machine's **Models in cache**
lists the model, and placement prefers that machine for a deployment of it.

Each request and each cancel is in the audit log (`model.download_requested`,
`model.download_cancelled`). A single GGUF file from a URL has no published checksum: its SHA-256 is
recorded after its first complete download, and every later download must match it
(`model.checksum_recorded`). The API is in [the reference](reference.md#download-a-model-ahead-through-the-api).

A machine that cannot reach Hugging Face or GitHub itself gets the files through the server: see the
next section, and [Run a machine without internet access](#run-a-machine-without-internet-access).

## Give an offline machine models through the server

A machine with no internet access of its own (behind a firewall, on a DNS filter, or reached only
through an SSH tunnel) can still download models, engine archives and pinned engine images: **the
server fetches them and
passes the bytes on**, storing nothing. The agent asks the server the way it already talks to it,
outbound, over HTTPS with the CA it pins and its machine credential, so it works through whatever
already carries the agent's connection (for example an `ssh -R 7860:…` tunnel). Nothing on the
machine or in the tunnel changes. For the whole path, from joining such a machine to a deployed model,
see [Run a machine without internet access](#run-a-machine-without-internet-access).

1. Open the machine's page (admins). Under **Models in cache**, set **Internet access for downloads**:
   - **Auto** (the default): downloads directly; once the source cannot be reached from that machine
     (the name does not resolve, the connection is refused, reset or times out, the network is
     unreachable), the rest of that download, and the next ones from that host for an hour, come
     through the server. An HTTP error (a missing file, a refused token) or a checksum mismatch never
     switches: the server would get the same answer.
   - **Always through the server**: for a machine that has no internet access at all. It never tries
     directly, so there is no wait for a failure first.
   - **Direct only**: never through the server.
2. Deploy, or use **Download to…**, as usual. The progress says *Downloading the model via the server:
   … of …* on the deployment's page, under the model in the Catalog and on the machine's page, and a
   finished download says *Downloaded … via the server*. The **Download to…** dialog says *via the
   server* for a machine set to always use it.

What stays the same: the agent resumes a download from what is on disk after a break (the server
passes `Range` on to the source), retries, stops at once on **Cancel**, and checks every file as a
direct download does (the SHA-256 of large files, the git blob id of small ones, the pinned SHA-256
of engine archives, the recorded SHA-256 of a URL model). A file changed on the way is refused and
deleted, so the server is trusted with nothing it could not already do.

What is different:

- The server uses its own Hugging Face token (**Models → Sources**), or the deployment's own,
  for gated models. Set to **Always through the server**, the machine is never sent a token at all.
- The server's own rules for model sources apply: https and public addresses only (unless
  `MODEL_ALLOW_PRIVATE_HOSTS`), and URL models only from the allowed hosts.
- The server serves only what that machine is meant to download right now (its deployment's replicas
  and its **Download to…** requests); cancelling a download, or removing the deployment, closes it
  within seconds. At most two downloads per machine and four in all go through the server at a
  time; the others wait their turn.
- Every byte crosses the server's network twice (in from the source, out to the machine), so keep
  machines that can reach the internet on **Auto** or **Direct only**.
- Placement no longer avoids such a machine for a model it could not download itself: the
  deployment's page says *amd-01 could not reach huggingface.co just now; the model comes through
  the server*, or *amd-01 downloads the model and the engine through the server (its internet
  access setting)*.

**From another machine** (1.4.1). Before the server fetches a model file from the internet for such a
machine, it asks the machines that already have the model in their cache. One of them sends the file
to the server, outbound like everything its agent does (no machine opens a port, and nothing in a
tunnel changes), and the server passes it on, storing nothing. On a LAN that is faster than the
internet, and it spares the server's internet connection. There is no setting:

- **Which machines send**: any other machine whose agent is 1.4.1 or later and lists the model in its
  cache, whatever its own internet access (a machine with none can send too). Only files it downloaded
  and checked are sent, and only by the digest the server holds for them. A URL model comes from a
  machine only once its SHA-256 is recorded.
- **Which one**: a machine that has the very GGUF file first, then the one sending fewer files. One
  that does not have the file, already sends two, or does not start within 10 seconds is skipped for
  the next one, and then the internet is used, all before the machine that asked gets a byte. A
  machine that breaks off mid-file ends that answer short; the download resumes from what is on disk,
  and the server picks a source again.
- **Checks**: the machine that asked checks every file as always. The server also checks a whole file
  on its way and holds back its last piece until it matches: a machine whose copy does not match is
  not asked for that file for an hour, the audit log names it (`node.relay_copy_refused`), and the file
  starts over from elsewhere. Remove that model from the sending machine's cache and download it again.
- **What you see**: *Downloading the model via the server, from gpu-host: … of …*, *Downloaded … via the
  server, from gpu-host*, and in **Download to…** *via the server, from gpu-host* when a machine would
  send it.
- Only machines that get their files through the server use it (**Always through the server**, or
  **Auto** after the source could not be reached). A machine that reaches the internet keeps
  downloading directly.
- A gated model can reach a machine this way without any Hugging Face token being used, because another
  machine already has it. As before, the server decides what each machine gets.
- Engine archives keep coming from the internet through the server: agents delete an archive once it is
  unpacked, so no machine has one to send.

**Engine images** (container variants) come through the server too, when the recipe pins them by
digest, as every built-in recipe does:

- When the image is not on the machine, the agent pulls it as usual (**Auto**); a pull that cannot reach
  the registry (`lookup ghcr.io: no such host`, a timeout) switches to the server, and so do the next
  images from that registry for an hour. Set to **Always through the server**, it never tries the
  registry.
- The server reads the image's manifest **for that machine's platform** (amd64, arm64) by the digest
  Forgeyard recorded for it (`forgeyard/recipes/images.lock.json`), with an anonymous pull token from
  the registry, and serves that manifest, its config and each of its layers, by digest. It serves no
  digest that manifest does not name, and checks each whole layer on its way through.
- The agent checks every piece against its digest, keeps them in its model cache under
  `.partial/image-…` (a break resumes the layer it was in; **Remove partial downloads** clears them),
  builds an archive `docker load` and `podman load` both accept, loads it, gives it the recipe's tag,
  and runs it only when the loaded image matches the recorded ids, as for an image loaded by hand. Then
  it deletes the pieces.
- The progress says *Pulling the image via the server: 120 MiB of 300 MiB, 3 of 7 layers*, then *loading
  it*. Every layer crosses the server's network twice, like a model file.
- An image named by a tag only (a custom recipe's, or one you built) cannot come through the server:
  there is no recorded digest to check it against. Upload it, or copy it from another machine (next
  sections). Forgeyard's own vision image is pinned since 1.4.1 and comes through the server.

The machine's agent must be 1.4 or later; an older one downloads directly whatever the setting says,
and the setting tells you to update it (also when it relays models but not yet images). To send files to
other machines, an agent must be 1.4.1 or later; the machine that asks needs only 1.4.

## Upload an engine image to a machine

An admin can put an image on a machine from the console: an image that cannot come through the server
(a custom recipe's image named by a tag only, an image you built yourself), or one the machine would
take a long time to pull.

1. On a machine that has the image (or can pull it), save it to a file:

   ```bash
   sudo docker save ghcr.io/example/engine:1.2 | gzip > engine.tar.gz
   ```

   `podman save` works too. For a pinned image, pull it by its digest and tag it first, as in
   [Offline machines](#offline-machines), so the file holds exactly that image under the recipe's tag.
2. Open the target machine's page → **Engine images** → **Upload an image**, choose the file, and
   **Upload**. The dialog shows how much has been sent, then *Loading it into …'s container engine*.
3. When it is loaded, the dialog lists each image the file held: its tags, its id, and which recipe's
   image it is. *The pinned image of llama-cpp (vulkan), checked by its recorded ids*: deployments
   there use it with no pull. *Named as the image of …*: a recipe that names it by a tag only uses it
   as it is. *Not the image of any recipe*: a custom recipe can name it by its tag.

The file goes from your browser through the server straight into the agent, which streams it into
`docker load` (or `podman load`) as it arrives: the server keeps only a small buffer, and the machine
needs no other route to your computer. The container engine checks each layer as it loads it; a file
that breaks off is never loaded. One upload at a time per machine, two in all; the largest file is
`IMAGE_UPLOAD_MAX_GB` (64 GiB unless set). The machine's agent must be 1.4 or later, with a container
engine, and online. Every upload, and why one failed, is in the audit log (`node.image_uploaded`).

To copy an image another machine already has, no file is needed: see the next section.

## Copy an engine image from another machine

An admin can copy an image one machine's container engine has to another machine, through the server
(1.4.1): a custom recipe's image named by a tag only, an image you built on one machine, or an image a
machine without registry access needs. Nothing is copied automatically.

1. Open the target machine's page → **Engine images** → **Copy from another machine**.
2. Choose the machine to copy **From**. Its agent lists the images in its engine: each with its tags, id,
   size and platform, and which recipe's image it is. The dialog shows the images a recipe names (1.4.2);
   tick **Show all images** for the others, such as other people's images on a shared machine.
3. Pick one and **Copy**. The other machine runs `docker save` (or `podman save`) on it, under its own
   tags, and sends it to the server, which passes it straight into the target's `docker load`, keeping
   only a small buffer. When it is loaded, the dialog lists what was loaded, which recipe's image it is,
   and *The same image as on gpu-host (same content id)*: what was loaded is what the other machine has.

What to know:

- **Trust**: an image named by a tag only has no recorded ids, so the target runs whatever the other
  machine has under that tag. Copy only from a machine you trust. A pinned image is still checked by its
  recorded ids before it runs.
- A `docker save` stream has no size ahead and cannot resume: a copy that breaks off starts again from
  the beginning, and the target loads nothing from a broken one. The dialog says so and what the other
  machine says, for example *The copy broke off after 131072 bytes: gpu-host stopped sending, so nothing was
  loaded on gpu-01. gpu-host says: docker save failed: …*: fix that on that machine, then copy again.
- The limits are the upload's: one copy or upload at a time per target machine, two in all, at most
  `IMAGE_UPLOAD_MAX_GB`; and one copy at a time from each machine.
- The machine copied from needs an agent of 1.4.1 or later with a container engine; the target, 1.4 or
  later with one. Both must be online. Every copy, and why one failed, is in the audit log
  (`node.image_copied`, with both machines and the ids loaded).

## Free disk space on a machine

A machine's page lists **Models in cache**: what its agent has downloaded, with sizes. Each Hugging
Face revision is a row; a revision with several GGUF files has a row for each file under it. Single
GGUF files from a URL and the engine archives of process engines are listed too. Above the list are
how much the cache takes on that disk and how much of the disk is free.

- **Remove** deletes one entry from that machine. Admins and operators can do it. It is refused
  while a deployment there uses the model, and the row says which deployment: stop it first. Files
  that another revision of the same repository shares stay, as `hf cache rm` does. The next
  deployment that needs the model there downloads it again.
- **Remove partial downloads** deletes what unfinished downloads left behind. A download resumes from
  those files, so remove them only when you do not want that download back. It is refused while a
  download runs on that machine.
- An agent older than 1.4 lists its models without sizes and cannot remove them. Update the agent.

Each removal is in the audit log as `model.cache_removed`, with what was removed and how much was
freed. The API is in [the reference](reference.md#a-machines-model-cache-through-the-api).

### Engine images

Engine images take disk space too, and old ones pile up when recipes move to newer images. The machine's
page lists them under **Engine images** (1.4.2): each image a recipe names, as its agent lists it now (a
pinned image recognised by its recorded ids, an image a recipe names by a tag only by that tag), with its
size, the recipes that name it, and the deployments on that machine that use it. Above the list is the free
space where the engine keeps its images, when the agent can see that directory (an agent in a container
usually cannot). The engine's other images are only counted: no recipe names them, so they are not
Forgeyard's.

- **Remove** (admins) deletes one image from that machine. The row asks first, then the agent runs
  `docker rmi` (or `podman rmi`) on it, never with `-f`. When it is done, the console says how much was
  freed: up to the image's size, since layers another image shares stay, and the free space after when the
  agent can see the engine's disk. A deployment that needs the image again pulls it (or gets it through
  the server, or you load it again).
- It is refused, and the row says why, while a deployment on that machine uses the image (stop it first),
  for the image the agent itself runs from, and while any container of the engine uses it, even one
  Forgeyard did not start (the engine keeps such an image; the message names the container).
- It is never offered for an image no recipe names.
- The agent removes only an image of its own listing, by its id: the server never names an image for it to
  delete. An image with several tags is removed by those tags.
- An agent older than 1.4.2 lists the images but cannot remove them. Update the agent.

Each removal the agent was asked for is in the audit log as `node.image_removed`, with the image, its
tags, the recipes that name it, and how much was freed or why it was not removed. The API is in
[the reference](reference.md#a-machines-engine-images-through-the-api).

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

Models and apps still running on a revoked machine are not stopped by the server, which can no longer
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
