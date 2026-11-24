# Troubleshooting

Problems seen on real machines, each with what you see, why, and what to do. Start with the
agent's own log: on an installed machine `journalctl -u forgeyard-agent -n 50`, in Docker
`docker logs --tail 50 forgeyard-agent`.

## The browser warns about the certificate

**You see:** *Your connection is not private* (Chrome) or *Warning: Potential Security Risk Ahead*
(Firefox) when you open Forgeyard.

**Why:** Forgeyard made its own certificate authority (CA) on its first start, and your browser
does not know it yet. The connection is encrypted either way; the browser cannot yet tell this
server from an impostor.

**Do:**

1. Click past the warning once (*Advanced* → *Proceed* / *Accept the risk*).
2. Open **Trust this server** (sign-in page or account menu, or `/trust`).
3. Compare the fingerprint there with the one in the server's startup log
   (`HTTPS with Forgeyard's own CA. CA fingerprint sha256:…`) or in a join command.
4. Download the CA and install it with the steps for your device. Close and reopen the browser.

If the warning comes back on a device that trusted the CA before, the server's CA changed (its
`data/tls/` was lost or replaced). Check the new fingerprint with the administrator before you
trust it.

A browser warns as well when you open the server by a name that is not in its certificate. The
certificate covers `PUBLIC_URL`'s host, `ALLOWED_HOSTS`, `TLS_EXTRA_NAMES`, the host name and the
machine's own addresses; add a missing name to `TLS_EXTRA_NAMES` and restart the server.

## An agent shows offline

**You see:** the machine is *offline* on **Machines**, with *last heartbeat … ago*.

**Do:** look at the agent's log. The last lines say why:

| The log says | Why | Do |
|---|---|---|
| `Connection to https://… lost or refused: [Errno 111] Connection refused` | the server is not running, or not on that address or port | start the server; check `HOST=0.0.0.0` and the firewall |
| `… is not the server this agent trusts: certificate verify failed` | the server's CA changed, or something in between answers instead | if the CA changed on purpose: `sudo forgeyard agent trust --server https://… --ca-fingerprint sha256:…` with the new fingerprint, then restart the agent |
| `The server refused this machine's credential (revoked or removed)` | an admin revoked or removed the machine | join again with a new token: `sudo forgeyard agent join --rejoin --join-token fyj_…` |
| `The server does not answer to the host name in --server` | the agent reaches the server by a name the server does not accept (HTTP 421) | add the name to the server's `ALLOWED_HOSTS`, or use its IP address |
| `If the server now uses HTTPS …, run once: forgeyard agent trust …` | the agent joined over plain HTTP (Forgeyard 1.0) | run the `agent trust` command it shows; the machine keeps its credential |
| `Connected to … as '…'` and then nothing | the agent is fine; the machine came back online | reload the page |

If the service itself does not run (`systemctl status forgeyard-agent` says *failed*), the log above
the failure names the setting that is wrong, for example
`/etc/forgeyard/agent.toml has 1 problem: - [workloads] limits wants one of auto, systemd, rlimit`.
Check the settings with:

```bash
sudo forgeyard agent config
```

It prints every setting, its value and where it came from (flag, environment, file or default).

Two agents with the same credential (a copied state directory) push each other off: the server
keeps only the newest connection, and closes the older one with *another agent connected with this
credential*.

Meanwhile its replicas get no requests, and after 10 minutes those of unpinned deployments move to other machines
that fit ([When a machine stays offline](how-to.md#when-a-machine-stays-offline)).

## The installer refuses the fingerprint

**You see:** `forgeyard-ca.der: FAILED` and `sha256sum: WARNING: 1 computed checksum did NOT match`,
or the installer says `Refused: the CA … sent has fingerprint sha256:…, not sha256:…`.

**Why:** the CA the machine received is not the server's. Either the command is from another
server, the server's CA changed since the command was made, or something between the machine and
the server answers in its place.

**Do:** create a fresh join command on **Machines** and compare the fingerprint with the server's
startup log. If they match and the machine still gets another CA, something on the network
(a TLS-inspecting proxy, for example) is in between: install from a network without it, or pin that
proxy's CA with `--ca-file` if you trust it.

## Image pulls fail: error getting credentials

**You see:** a deployment's replica fails with `docker: error getting credentials - err: exit status
255` (or `pull access denied`), while `docker pull` works when you run it yourself.

**Why:** your Docker configuration names a credential helper (`credsStore` in
`~/.docker/config.json`) that cannot run where the agent runs: a desktop keychain without a session,
a helper missing in the agent's container.

**What Forgeyard does:** the agent pulls an engine image through Docker's API on its socket, which
needs no helper. When it has to use the `docker` CLI instead (no socket, a registry that wants your
credentials) and the pull fails this way, it pulls again anonymously, without the helpers. That is
enough for every built-in engine image, since they are all public.

**Do,** if it still fails (a private image, or no internet on the machine):

- pull the image on the machine yourself, once: `docker pull vllm/vllm-openai:v0.32.0`; the agent
  then finds it locally;
- or fix the helper (`docker-credential-<name> list` must work for the user the agent runs as).

## The Forgeyard image does not pull

**You see:** `docker run` of the agent (or the server) stops with one of:

- `manifest unknown` or `not found`: no image with that tag;
- `no matching manifest for linux/arm/v7` (or another platform);
- `toomanyrequests: You have reached your pull rate limit`;
- `dial tcp: lookup registry-1.docker.io … no such host`, or a timeout.

**Why, and what to do:**

- **No such tag.** The agent's tag is the server's version. A server built from source between two
  releases names a version that was never published. Build the image on the machine instead
  ([Offline machines, or build it yourself](how-to.md#offline-machines-or-build-it-yourself)), or
  point `AGENT_IMAGE` at a published version.
- **Another platform.** The images are built for linux/amd64 and linux/arm64 only; `uname -m` must
  say `x86_64` or `aarch64`.
- **Rate limit.** Docker Hub limits anonymous pulls per address. Sign in once on that machine
  (`sudo docker login`), wait, or pull through a mirror and set `AGENT_IMAGE` to it.
- **No access to Docker Hub** (no internet, a firewall or a DNS filter): carry the image over as a
  file: pull and `docker save` it on a machine that can, then `docker load` it on this one
  ([how](how-to.md#offline-machines-or-build-it-yourself)). The agent's `docker run` then finds it
  locally. The models' engine images need the same on such a machine:
  [Offline machines](how-to.md#offline-machines).

## A replica fails: the image is not on this machine

**You see:** a deployment's replica fails with `the image is not on this machine and could not be
pulled; on an offline machine, docker load the image (Forgeyard checks it by its recorded ids)`,
followed by Docker's error (`lookup ghcr.io … no such host`, a timeout, `pull access denied`).

**Why:** the engine image is not on the machine, neither by its pinned digest nor by its tag, and the
machine cannot reach the registry. When the pull got under way first, the message also says where it
stopped: `at layer 4f4fb700ef54, 3 of 7 layers done, 0.80 of 1.90 GiB`.

**Do:** with a 1.4 agent, let the server fetch it: set the machine's **Internet access for downloads**
to **Auto** or **Always through the server**
([Give an offline machine models through the server](how-to.md#give-an-offline-machine-models-through-the-server)),
or upload the image file from the machine's page
([Upload an engine image to a machine](how-to.md#upload-an-engine-image-to-a-machine)). Or carry the
image over as a file and load it: [Offline machines](how-to.md#offline-machines). The recipe's page
lists the commands for each of its images. The agent tries again by itself; the next start uses the
loaded image. If the machine should reach the registry, check its DNS and proxy
first (`docker pull` of the image there must work).

When the message instead says `… is loaded but Forgeyard has no recorded ids for this image`, the
image comes from a custom recipe: Forgeyard cannot check a loaded copy of it, so name it by its tag
only (without `@sha256:…`) in that recipe.

## A replica fails: a different image is loaded under that tag

**You see:** `a different image is loaded under ghcr.io/…:<tag> (its ids do not match the recorded
ones), so it is not run, and the pinned image could not be pulled`.

**Why:** an image is loaded under the recipe's tag, but it is not the image the recipe pins: another
build (an older engine under a reused tag, a tag moved on the registry), another platform's layers
claimed as this one, or an image changed and tagged locally. Forgeyard never runs it in place of the
pinned image, and asks the registry for the pinned one, which this machine cannot reach.

**Do:** load the image the recipe names, pulled by its digest as in
[Offline machines](how-to.md#offline-machines); `docker load` replaces the tag. `docker image inspect
<tag>` shows what is loaded now.

## An image pull is slow, or fails with "made no progress"

**You see:** a replica shows `pulling the image: 0.80 of 1.90 GiB (42%)` for a long time (or
`3 of 7 layers`, or `unpacking`), or it fails with `the image pull made no progress for 5 minutes (at
layer …)`.

**Why:** before it starts a container, the agent pulls the engine image itself and reports how much
has arrived, layer by layer. Engine images are large (vLLM's is several GB), so at under 1 MB/s a pull
takes an hour or more. A pull fails only when **no new bytes arrive for 5 minutes** (15 while the
layers are unpacked): a slow pull that keeps moving is never stopped, however long it takes. The
total can grow at first: Docker learns a layer's size only when it starts downloading it, and until
every size is known the page shows the layers instead of a percentage.

**Do:**

- wait: a failed or stalled pull is tried again, and Docker keeps the layers it finished;
- on a network that stalls for long stretches, raise the limit on that machine:
  `FORGEYARD_PULL_STALL_MINUTES=15` (or `pull_stall_minutes = 15` under `[workloads]` in
  `agent.toml`), then restart the agent;
- or pull it there yourself once (`sudo docker pull <image>`, the recipe's page lists it), or carry it
  over as a file: [Offline machines](how-to.md#offline-machines). A local image is never pulled again.

The progress needs Docker's (or Podman's) API socket: the agent's container mounts
`/var/run/docker.sock`. Without it, the agent pulls with the CLI, which reports layers but no bytes.
`forgeyard agent runtimes` says which one it uses (`image pulls through the engine API`).

## A replica says "cancelled: the deployment was removed"

**You see:** in the agent's log, or as the last word of a replica, `cancelled: the deployment was
removed` (`… was stopped`, `… was changed`).

**Why:** the deployment was deleted, stopped or edited while that replica was still downloading its
model or pulling its image. The agent ends the download or the pull at once and releases the model,
so another deployment of the same model starts from what is already on disk. Nothing is wrong with the
machine or the image; it is not an offline machine's "could not be pulled".

**Do:** nothing. Deploy again to go on: the model download resumes where it stopped.

## Hugging Face downloads are slow

**You see:** a deployment stays in *downloading* for a long time; its page shows the progress and
the speed.

**Why:** the agent downloads models from Hugging Face directly, at whatever speed the machine's
network reaches it. At 4 MiB/s, a 16 GB model takes over an hour.

**Do:**

- wait: downloads resume where they stopped, and a model is downloaded only once per machine;
- use a mirror: set `HF_ENDPOINT` on the server to it. For a mirror on your LAN, also set
  `MODEL_ALLOW_PRIVATE_HOSTS=true` on the server and `allow_private_hosts = true` under `[models]`
  in each agent's `agent.toml`;
- copy a model cache from another machine that has it (same layout, see
  [Move a model cache](how-to.md#move-a-model-cache));
- or put the files in a directory on the machine and add them as a `local://` model.

## A machine cannot reach Hugging Face

**You see:** a replica on that machine waits in **backoff** with *Qwen3-1.7B-Q4_K_M.gguf: download
failed: cannot resolve huggingface.co: …* (or *github.com* for a llama.cpp engine archive, or an
image that *could not be pulled*), then its deployment's page says *cpu-01 could not reach
huggingface.co; placed on cpu-02 instead*.

**Why:** the machine is set to **Direct only**, or its agent is older than 1.4 (it cannot download
through the server). Set to **Auto** (the default) or **Always through the server**, a 1.4 agent gets
the model and the engine archive through the server instead, and none of this happens (see below).

The agent tries a download five times, a minute in all; when it still could not reach the
source (the name did not resolve, the connection was refused or timed out, the network was
unreachable), the server remembers for an hour that this machine cannot reach that host. A replica
of a deployment that is not pinned to the machine is then placed again on another machine that
fits, and new replicas that would download from that host go elsewhere first. A machine that
already has the model (and the engine) needs no download, so it is still used for it.

The replica stays on the machine, and keeps trying, when:

- the deployment is pinned to it: *the deployment is pinned to cpu-01, so the replica stays there
  and tries again*;
- no other machine can take it: *no other machine can take it, so it tries again on cpu-01*.

An HTTP error (a missing file, 401 for a gated model), a checksum mismatch or a full disk is not
counted: another machine would get the same answer.

**Do:**

- let the server download it for the machine: on the machine's page set **Internet access for
  downloads** to **Auto** or **Always through the server**, and update its agent to 1.4 if the page
  says so ([Give an offline machine models through the server](how-to.md#give-an-offline-machine-models-through-the-server);
  the whole path for such a machine: [Run a machine without internet access](how-to.md#run-a-machine-without-internet-access)).
  The replica's progress then says *via the server*. A pinned engine image comes the same way; an
  image named by a tag only does not (upload it from the machine's page);
- let the machine reach the host (DNS, a firewall rule, a proxy route); Forgeyard tries it again
  for new placements an hour after the last failure, or at once after a server restart;
- or bring the files to it: copy a model cache from a machine that has the model
  ([Move a model cache](how-to.md#move-a-model-cache)), `docker load` an engine image (see
  [A replica fails: the image is not on this machine](#a-replica-fails-the-image-is-not-on-this-machine)),
  or add the model from a local directory as a `local://` model;
- or use a mirror the machine can reach (`HF_ENDPOINT`, see
  [Hugging Face downloads are slow](#hugging-face-downloads-are-slow)).

**Through the server, it still fails:**

- *… could not pull …: This image is not pinned by digest, so it cannot come through the server*, or
  *… is not an image Forgeyard pinned by digest*: a custom recipe's image named by a tag only. Upload it
  ([Upload an engine image to a machine](how-to.md#upload-an-engine-image-to-a-machine)), or copy it
  from a machine that has it
  ([Copy an engine image from another machine](how-to.md#copy-an-engine-image-from-another-machine)).
- *… has no build for amd-01's architecture*: the image has no build for that machine's platform
  (for example arm64); pick a recipe or variant that has one.
- *the image through the server: … SHA-256 mismatch* (a layer), *the manifest the server sent … is
  not one of the image's recorded digests*, or *… does not match the recorded ids: not running it*: what
  arrived is not the pinned image. Nothing was loaded; the server's log says which registry answered.

- *download failed: through the server: the server could not reach huggingface.co: …*: the server
  cannot reach the source either. Fix the server's network, or use a mirror (`HF_ENDPOINT`).
- *through the server: huggingface.co refused access (HTTP 401) …*: a gated model; set the server's
  Hugging Face token under **Models → Sources** (the machine's own token is not used through the
  server), or give the deployment its own.
- *through the server: … does not download through the server*: the machine was set to **Direct
  only** while it downloaded.
- *SHA-256 mismatch*: the bytes changed on the way (a proxy that rewrites them, a broken disk). The
  file was deleted; the next try starts it again.
- The server's log has a line per file it passed on: *Relayed <repo>@<revision>/<file> to amd-01:
  812.3 MiB in 41.0 s (19.8 MiB/s), finished.*, or why it ended. A file from another machine (1.4.1)
  says *… <file> from gpu-host to …*, and a machine that could not send it is named with why:
  *gpu-host could not send <file> for … (not_found: …); trying the next machine*.
- *Relay of <repo>@<revision>/<file> to amd-01 cut short: the source broke off (…); 104857600 of
  2147483648 bytes sent from huggingface.co in 12.3 s.* (the server's log, 1.4.2): the source (a host on
  the internet, a registry, or `machine gpu-host`) stopped in the middle of the file. Nothing is wrong on
  the server: the agent asks for the rest again and the download goes on from what is on disk. When it
  keeps happening with the same source, look at that network, or at that machine's agent log.
- *gpu-host's copy of <file> does not match its checksum* (the server's log, and `node.relay_copy_refused`
  in the audit log; the sending machine's own log says *A cached file failed its checksum*): that
  machine's copy is damaged. The file came from elsewhere; remove the model from that machine's cache
  (its page) and download it again. It is not asked for that file for an hour.

## vLLM does not start on a shared card

**You see:** a vLLM replica fails soon after it starts, and its output ends with something like
`ValueError: Free memory on device (…) is less than desired GPU memory utilization (…)`.

**Why:** vLLM's `--gpu-memory-utilization` is a fraction of the card's **total** memory, and vLLM
refuses to start unless that much is **free**. Forgeyard computes it from the replica's own
reservation, so it fits next to what others use; but if someone else took memory on the card
between placing and starting, there is no longer enough.

**Do:**

- look at the card on **Machines**: its free memory is what counts;
- stop and start the deployment: it is placed again with today's free memory;
- or lower what it needs: a shorter **Context length**, fewer **Concurrent requests**, or a
  smaller model. The deploy page shows the new estimate before you deploy.

## llama.cpp does not start with the FP8 KV cache

**You see:** a `llama-cpp` deployment whose *KV cache type* is `f8_e4m3` fails right after it starts,
and its output ends with `FP8 cache is not supported for architecture '…'`; or deploying it on an AMD
APU answers *vulkan: kv_cache f8_e4m3 is not available on this variant*.

**Why:** llama.cpp 1.0.0 refuses the FP8 cache for some architectures (MLA models such as DeepSeek, and
a few others), and it has no FP8 for Vulkan at all (llama-server would abort), so Forgeyard never
places an FP8 deployment on the `vulkan` variant.

**Do:** set *KV cache type* back to `f16` (**Edit** on the deployment's page, under **Advanced**). On
an AMD card the ROCm image is built for, FP8 runs on `rocm`.

## A deployment does not fit

**You see:** *Deploy* answers *Nothing can take a replica that needs … GiB per GPU*, followed by one
line per machine.

**Why:** Forgeyard places a replica only where the estimate fits into memory that is free **now**,
on cards Forgeyard may use. It refuses rather than start something that would fail. Each machine's
line says why it was not chosen:

| Line | Means |
|---|---|
| `no allowed GPU that vllm supports` | no card on the GPU allowlist, or no recipe variant for that card and driver |
| `needs 14.38 GiB per GPU; available: #0 12.07 GiB, … (2.31 GiB short)` | the best card misses that much free memory |
| `needs 1 GPU it can have; in use: #1 (qwen)` | the cards are taken by replicas that do not share |
| `…; fits if both share VRAM: #1 has 60.00 GiB available, held by qwen` | the memory is there, but the card is held exclusively: turn on **Share VRAM** on both ([GPU sharing](concepts.md#gpu-sharing)) |
| `needs 3.08 GiB of RAM, 1.20 GiB available (1.88 GiB short)` | a CPU model, and not enough free RAM |
| `no container runtime (…)` | the recipe needs Docker or Podman, which the machine does not have |
| `its agent does not serve models` | the agent has no model cache (`[models] cache` in its settings) |
| `no free port: 3 of 3 in use (host ports 33000-33002)` | every port of the machine's [port range](#a-replica-waits-no-free-host-port) is held by its replicas |
| `offline` | the machine's agent is not connected |

**Do:** free memory on a card (stop something), allow more cards ([GPU access](how-to.md#restrict-gpus-on-a-shared-machine)),
lower the context length or concurrency, choose a quantised or smaller model, or deploy it to wait
until room appears (the API's `"wait": true`).

## A replica stays pending

**You see:** a deployment was placed on a machine (*Replica 0 placed on …*), the machine
is online, and the replica stays **pending**: nothing is downloaded or started. After 2 minutes the
deployment page says *Replica #0 was never picked up*, with the machine, its agent's
version and what to do, for example *gpu-01's agent (1.2.0) has not taken this replica for 3 minutes;
update the agent (the server is 1.3.1)*. An agent gets its work list within a second of a placement
(or of connecting) and answers every list with the state of each replica, so two minutes of silence
from a connected agent is not a slow start.

**Why:** the machine's agent is not acting on its work list. Agents up to 1.2.0 kept the number of the
last work list they received across connections, and a server restarted since then numbered its
lists from 1 again: the agent took them all for old ones and dropped them without a word. A server
from 1.3.0 numbers them so that this cannot happen, also for those older agents; an agent from
1.3.0 starts a new count on every connection and logs any list it drops.

It also happens with an agent that lacks something the replica needs and drops it, or one that is
stuck.

**Do:** run the server's own version on the agent ([upgrades](how-to.md#upgrade-the-server-and-the-agents)); until then,
restarting the agent (`sudo systemctl restart forgeyard-agent`, or `docker restart forgeyard-agent`)
makes it take its work at once. When the agent already runs the server's version, the message says
to restart it and look at its log (`journalctl -u forgeyard-agent`, or `docker logs forgeyard-agent`). **Setup health** flags agents older than the server, and replicas
that waited for a machine for more than 15 minutes.

## Automatic replicas do not add a replica

The deployment's page says why under **Replicas**:

- *Wants 3 replicas, room for 2: …*: one more does not fit anywhere now; each machine's reason follows, as for a
  deploy ([A deployment does not fit](#a-deployment-does-not-fit)). Free memory on a machine, raise *Most
  replicas* only where there is room, or unpin the deployment. Forgeyard does not add a replica that would wait.
- *Not scaling now: …*: an edit restarts its replicas, a replica's machine is offline, or it was started, woken
  or changed less than 2 minutes ago. It goes on by itself.
- Nothing said, and the load stays below the target: the load counts requests in flight and waiting in the
  gateway's line against *Concurrent requests* × the target (80 %). With *Concurrent requests* 32 a replica takes
  25.6 requests before one more is wanted; lower *Concurrent requests* or the target under **Advanced**.
- A replica is still starting: only one starts at a time; the next one is considered once it is ready.
- It is at *Most replicas*, or it is a video deployment (never scaled automatically).

A replica that should go stays while it is **draining**: it finishes the requests it runs first (at most 30
minutes). If the load rises again it is kept (*Kept replica 1: …* in the events).

## A replica waits: no free host port

**You see:** a replica is not placed, and the deployment page says *Cannot be placed: gpu-02: no free
port: 3 of 3 in use (host ports 33000-33002)*, or *not enough free ports: 2 of 3 in use, it needs 2*. The
deploy preview gives the same line for that machine.

**Why:** an agent publishes services and model replicas on the host ports of its port range
(`FORGEYARD_PORT_RANGE`, default `30000-32767`). A model replica takes two of them (the agent's key guard
listens on one, the engine on the other). The server counts the ports the machine's replicas hold and skips a machine without enough free
ones, like a machine without memory, instead of sending a replica that could only wait there. A small
range is common on a machine reached through an SSH tunnel, where every port in the range is forwarded.
Agents older than 1.4.2 do not report their range, and their ports are not counted.

A replica the agent could not give a port (it was placed before its server counted ports) reports
*no free host port left in 33000-33002*. When the count agrees, the server takes it off that machine: it
goes to a machine with room, or waits with the reason above. When the count says there is room, a program
outside Forgeyard holds ports of the range: the agent skips them, and tries again in backoff.

**Do:** stop something on that machine, deploy to another machine, or give the agent a larger range
(and forward or open those ports so the server can reach them), then restart the agent:

```bash
sudo forgeyard agent runtimes   # the last line shows the range the agent uses
```

## A machine publishes its replicas on a loopback address

**You see:** *gpu-02 publishes its replicas on 127.0.0.1* under **Get started → Setup health** and on the
machine's page, and models deployed there may answer 502 or something unexpected.

**Why:** an agent publishes replicas on its own address towards the server: the local end of its connection,
unless `FORGEYARD_BIND_ADDRESS` names another. An agent that reaches the server through an SSH tunnel or a proxy
on its own host connects to `127.0.0.1`, so that is the address it reports, and the gateway dials `127.0.0.1` on
the **server's** host: the server's own ports, not the machine's. From 1.4.5 the agent also tells the server its
end of the connection; when that is the peer the server sees, nothing sits between them, the agent runs on the
server's own host and loopback is right. Only the other machines are checked, and only those with a 1.4.5 agent.
The warning does not appear when every port of a range of up to 64 ports is forwarded to the server's host
(something listens on each on the server's loopback), as in the guide's
[SSH tunnel](how-to.md#run-a-machine-without-internet-access).

**Do:** one of these.

- Forward the machine's port range back through the tunnel too: choose a small range
  (`FORGEYARD_PORT_RANGE=33000-33002`) and add one `-L` per port to the `ssh` command that carries the agent's
  connection ([how](how-to.md#run-a-machine-without-internet-access)).
- Or give the agent an address the server can reach (its LAN or VPN address) with `FORGEYARD_BIND_ADDRESS`
  (`--bind-address`), and open the port range there.

Then restart the agent. If the server does reach the machine at that address in a way Forgeyard cannot see (a
proxy on the server's host, say), snooze the warning.

## New processes on a machine cannot open its GPUs

**You see:** an error under **Get started → Setup health** and on the machine's page: *New processes on gpu-01 cannot
open its GPUs (/dev/nvidia0: Operation not permitted)*. A process engine there fails at once with
`cuInit: CUDA_ERROR_NO_DEVICE` or `Failed to initialize NVML: Unknown Error`, while models in containers on the same
machine keep answering and the machine's page still lists every GPU. Deployments that need a process on those GPUs
wait, with the reason *new processes there cannot open its NVIDIA GPUs*.

**Why:** the agent checks every 5 minutes, and when it starts, that a new process running as its workload account
can open the GPU device nodes (`/dev/nvidiactl`, `/dev/nvidia-uvm` and each `/dev/nvidiaN`; on AMD `/dev/kfd` and
the render nodes). With `docker run --gpus all` the NVIDIA runtime hook gives the agent's container its GPU devices
behind Docker's and systemd's back. When the host runs `systemctl daemon-reload` (a package install, an unattended
upgrade, any unit change: often, on a shared host), systemd applies the container's cgroup again from what it knows,
and that device access is gone. What is already open keeps working (the agent's GPU readings, the engines that were
running); every new open inside that container is refused. New containers get the access afresh, so models in
containers are not affected. AMD agents are not affected either: their command passes `/dev/kfd` and `/dev/dri` with
`--device`. While the check fails, Forgeyard places no process workload on those GPUs.

**Do:**

1. Restart the agent container: `sudo docker restart forgeyard-agent`. It gets its devices back and the error goes
   away at its next check (it checks again when it connects). Models in containers keep running.
2. Update it with the machine page's **Update the agent** command (the error's **Update command** opens it). From
   1.4.5 that command passes the NVIDIA device nodes with `--device` next to `--gpus all`, which Docker records, so a
   reload no longer takes them away:

   ```bash
     --gpus all \
     --device /dev/nvidiactl \
     --device /dev/nvidia-uvm \
     --device /dev/nvidia-uvm-tools \
     --device /dev/nvidia0 \
     …
   ```

   An agent before 1.4.5 does not report its device nodes; its command lists the host's with a short shell loop
   instead (`$(for d in /dev/nvidiactl … /dev/nvidia[0-9]*; do … done)`), which skips a node the host does not have.
   If `/dev/nvidia-uvm` is missing on the host, run `sudo nvidia-modprobe -u -c=0` first.

If it still happens with the device flags: some `runc` versions with systemd's cgroup driver drop the rules anyway
when the host has no `/dev/char` links for the NVIDIA devices. Create them with the NVIDIA Container Toolkit
(`sudo nvidia-ctk system create-dev-char-symlinks --create-all`; NVIDIA's discussion #1133 of nvidia-container-toolkit
has a udev rule that keeps them after a reboot), or start the agent with CDI instead of `--gpus all`
(`--device nvidia.com/gpu=all`, after `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`). Changing Docker's
cgroup driver to cgroupfs also avoids it, but restarts every container on the host: not on a shared host.

Outside a container (install.sh or a checkout) the same error means the workload account itself may not open the
devices: check their permissions and the account's groups (`video`, `render`), then restart the agent.

## Traffic to a machine is not encrypted

**You see:** *Traffic to gpu-02 is not encrypted: update the agent* under **Get started → Setup health** and on the
machine's page.

**Why:** from 1.4.5 the gateway reaches every replica over TLS, but gpu-02's agent is older and serves its replicas
over plain HTTP only. So the replicas' internal keys, the prompts and the answers cross the network in clear
between the server and gpu-02. It keeps serving: nothing stops until you update it.

**Do:** update the agent (the warning's **Update command** opens the command for that machine;
[Update an agent](how-to.md#update-an-agent)). Once the new agent connects it gets its certificate within a second,
and the warning goes away. Models deployed there keep running across the update (the agent adopts them).

## A machine has no certificate for its replicas

**You see:** an error under **Get started → Setup health**: *gpu-02 has no certificate for its replicas*, *gpu-02's
replica certificate has expired* or *… is not being renewed*; models deployed only there answer 503, the others
answer from replicas on other machines.

**Why:** a 1.4.5 agent serves its replicas over TLS only, with a certificate the server signs for that machine. It
asks for one when it connects and renews it 10 days before it ends (it is valid 30 days). The gateway never falls
back to plain HTTP for such a machine, so without a valid certificate its replicas cannot be reached. The error
names the reason when the server refused the request (it is also in the server's log). Usual causes:

- the agent has not been connected for a long time (an offline machine cannot renew);
- the server cannot read or make its CA in `data/tls` (a `ca.crt` without its `ca.key`, or a read-only data
  directory): the server's log says *no certificate can be made*;
- the agent's state directory is not writable, or the agent cannot make a key: its log says *cannot be used*.

**Do:** look at the agent's log (`journalctl -u forgeyard-agent`, or `docker logs forgeyard-agent`) and the server's.
Fix what they name, then restart the agent: it asks again at once. If `data/tls` lost its `ca.key`, restore it from a
backup ([Back up and restore](how-to.md#back-up-and-restore)); a new CA also works for the replicas (every
agent gets a new certificate by itself), but browsers and agents that trusted the old one for the server must trust
the new one.

## Errors from /v1: 401, 403, 429, 503

The gateway answers errors in the OpenAI format, `{"error": {"message", "type", "param", "code"}}`.
The `code` says what happened.

### 401

`missing_api_key`, `invalid_api_key`, `expired_api_key`, `revoked_api_key`, `account_disabled`: the
key is missing (send `Authorization: Bearer fyk_…`), wrong, expired or revoked, or its owner's
account is disabled. Check the key on **API keys**; create a new one if needed.

### 402

**You see:** `402` with `budget_exhausted` or `budget_unpriced`, and the header
`X-Forgeyard-Budget-Exhausted: key` (or `person`, or `provider:<name>`).

**Why:** the request could only be served by a cloud provider, and a monthly budget kept it from
every provider that could serve it:

- `budget_exhausted`: the budget of the key, of its owner or of the provider is used up, or this
  request's reserved cost would go past it. The `budget` object in the error gives the budget, what
  was spent and reserved, the most this request could cost, and `resets_at` (the first of next month,
  00:00 UTC).
- `budget_unpriced`: a budget applies, but the provider's model has no price, so its cost cannot be
  reserved.

Local replicas are never limited by a budget: when one could serve the request, it does.

**Do:**

- `budget_exhausted`: wait for `resets_at`, or have the budget raised (a key's on **API keys** →
  **Limits**, a person's on **Usage** → **Cost** → **Budgets this month**, a provider's on its page).
  Starting a local deployment of the model also helps. A request with a lower `max_tokens` reserves
  less, so it may still fit. OpenAI's SDKs do not retry 402, and retrying does not help.
- `budget_unpriced`: an admin sets the model's prices on its provider's page (**Prices**, or
  **Refresh prices from OpenRouter**).

More in [Budgets](cloud-providers.md#budgets).

### 403

`model_not_allowed`: the key is limited to some models, and this is not one of them. The key's
owner (or an admin) can change the list on **API keys**.

### 429

`rate_limit_exceeded`: over the key's requests or tokens per minute. Wait for the number of seconds
in the `Retry-After` header; limits count the last 60 seconds. Limits an admin set on your key are
locked; ask an admin to change them.

### 503

`no_ready_replica`: the model exists, but none of its replicas is ready: still downloading or
starting, stopped, or its machine is offline. The message lists each replica's state. Look at the
deployment's page; see also [A model is listed but answers 503](#a-model-is-listed-but-answers-503).

`model_waking` (1.5.0): the deployment sleeps when idle; the request woke it, but it was not ready within its
`wake_wait`. Retry after `Retry-After`; if it keeps happening, raise `wake_wait` (and the client's timeout), give
the route a cloud tier, or turn *Sleep when idle* off. `cannot_wake`: it cannot start now, for the reason given (no
room on any machine, its pinned machine offline, its engine failing): see the deployment's events
([Sleep when idle](concepts.md#sleep-when-idle)).

Others: `404 model_not_found` (no model by that name for this key: see `/v1/models` and
[A model is missing from /v1/models](#a-model-is-missing-from-v1models)), `502` (a replica could not be
reached or refused its internal key: usually its agent restarted; it recovers within seconds),
`504 upstream_timeout` (no answer within `GATEWAY_READ_TIMEOUT`, 600 s by default) and, for streamed
requests only, `504 first_answer_timeout` (the stream did not start within the deployment's *Wait for
the first answer*, 120 s by default: raise it under **Advanced**). Every code is listed in the
[reference](reference.md#errors).

### Errors from cloud providers

A model that goes to a cloud provider adds these. The answer's `X-Forgeyard-Target` says whether a
provider answered.

- `404 model_not_found` for a model listed on **Cloud models**: it is cloud-only, and the key's cloud
  setting is not **Allowed**.
- `400 cloud_only_model`: the request had `X-Forgeyard-Local-Only: 1` (or the route is
  **Local only**), but the model is served only by a cloud provider.
- `502 provider_auth_failed`: the provider refused Forgeyard's key for it; an admin fixes it on
  **Cloud → Providers**. `502 provider_blocked`: the egress rules refused the provider's address.
  `502 provider_redirect`: the provider answered with a redirect, which is never followed; check its
  base URL.
- `429 provider_rate_limited`: every target of the cloud tier was rate limited; wait for
  `Retry-After`.
- `402 budget_exhausted`, `402 budget_unpriced`: a monthly budget; see [402](#402).
- Any other 4xx keeps the provider's status, code and message, prefixed with the provider's name.

More in [Cloud providers](cloud-providers.md#troubleshooting).

## A program gets 410 apps_removed

**You see:** a script or program gets **410 Gone** with the code `apps_removed`: from `POST /api/workloads`,
`GET /api/workloads/templates`, `PUT …/gateway`, `POST …/stop` or `…/start` or `DELETE` of a workload, from
anything at `/x/`, or from `POST /api/playground/vision/<name>/<task>`. Or creating or changing an API key with
`workloads` set to `listed` or `all` gets **422** `apps_removed`.

**Why:** apps, the templates and `/x/` were removed in 1.5.3 ([Apps (removed)](reference.md#apps-removed)). The
upgrade removed the apps that were left; the audit log has one `workload.removed_in_upgrade` entry for each, with
what it ran.

**Fix:** deploy what the program needs as a model and call it at `/v1`; a model that no built-in engine runs gets
a custom engine (**Models → Engines**). For a model deployment's workload, the 410 names its deployment: stop,
start or delete that instead. A key script: leave `workloads` and `workload_list` out.

## A program does not trust the certificate

**You see:** a program that calls `/v1` fails with `certificate verify failed`,
`self-signed certificate in certificate chain`, `unable to get local issuer certificate`, or
`SELF_SIGNED_CERT_IN_CHAIN`. The same address works in a browser that trusts the CA.

**Why:** the server's certificate comes from Forgeyard's own CA, and each program decides on its
own which authorities it trusts. Installing the CA in the browser or the system does not reach a
program that brings its own list: Python's `requests` and the OpenAI SDK (through `httpx`) use the
`certifi` package's list, not the system's, and Node.js has its own list built in.

**Do:** get `forgeyard-ca.pem` from **Trust this server**, check its fingerprint, then give it to
the program in one of these ways:

| Program | How |
|---|---|
| curl | `curl --cacert forgeyard-ca.pem …`, or install the CA in the system (below) |
| Python, OpenAI SDK, `httpx` | in code: `http_client=DefaultHttpxClient(verify=ssl.create_default_context(cafile="forgeyard-ca.pem"))` for the SDK (`verify=` the same for `httpx`), as in the [quick start](quick-start.md#python-with-the-openai-sdk); or `SSL_CERT_FILE` |
| Python, `requests` | `REQUESTS_CA_BUNDLE` |
| Node.js | `NODE_EXTRA_CA_CERTS=forgeyard-ca.pem`: added to Node's own list, which stays |
| Anything else | its own setting for a CA file, if it has one |

`SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` **replace** the program's list. Pointing them at
`forgeyard-ca.pem` alone is fine for a program that only talks to Forgeyard, but it then fails on
every other HTTPS site (a cloud API, a package index). For such a program, install the CA in the
system and point the variables at the system's list, which then holds both:

```bash
sudo cp forgeyard-ca.pem /usr/local/share/ca-certificates/forgeyard-ca.crt
sudo update-ca-certificates
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

That is Debian and Ubuntu; **Trust this server** shows the steps for other systems. Restart the
program after you set the variables. A program that cannot be given a CA at all needs a certificate
it already trusts: [use your own certificate or a reverse proxy](how-to.md#use-your-own-certificate-or-a-reverse-proxy).

## A model is missing from /v1/models

**You see:** a model you expect is not in `GET /v1/models`, and calling it answers
`404 model_not_found`.

**Why:** `/v1/models` lists only the models **this key** may use right now. A model is left out
when:

- **its deployment is stopped**, and no provider the key may use serves it. A starting deployment
  stays in the list; a stopped one leaves it until it is started again;
- the key is limited to **other models** (then a call answers `403 model_not_allowed`);
- only a **cloud provider** serves it, and the key's cloud setting is not **Allowed**: for that key the
  model does not exist;
- the deployment or the route was **deleted**, or its name is not what you think: the name programs
  use is the deployment's name (or the route's), shown as *Model name* on its page.

**Do:** open **Serving** and check the model's state; **Start** a stopped deployment. On **API keys**,
check the key's models and its *Cloud providers* setting.

## A model is listed but answers 503

**You see:** the model is in `/v1/models`, but calling it answers `503 no_ready_replica`.

**Why:** the deployment is meant to run, but no replica is ready yet: the model is still
downloading, the engine is still starting, a replica failed and is restarting, or its machine is
offline. The message gives each replica's state, for example
`replica 0 on gpu-01: downloading`. A stopped deployment answers the same way
(`The deployment is stopped.`), if you call it anyway.

**Do:** open the deployment's page. Wait while it downloads or starts (the page shows the progress).
If a replica keeps failing, its output on that page says why; a machine that is offline is in
[An agent shows offline](#an-agent-shows-offline). To have such requests go to a cloud provider
meanwhile, put a cloud model in a later tier of the deployment's **Routing and fallback**
([Cloud providers](cloud-providers.md#let-a-deployment-spill-to-a-cloud-model)); another local deployment
works too ([Share requests between two local deployments](how-to.md#share-requests-between-two-local-deployments)).

## A request does not spill to the cloud

**You see:** a deployment has a cloud model in a later tier of its routing and fallback, but requests still fail (503, 429) or wait while the
local replicas are busy or down. The answers carry no `X-Forgeyard-Spilled`.

**Why**, in the order to check:

1. **The key's cloud setting is Never.** It is **Never** for every key unless someone changed it,
   including every key made before 1.2. Such a key never reaches a cloud provider; it gets the local
   error instead.
2. The route has **Local only**, or the request sends `X-Forgeyard-Local-Only: 1` (the Playground's
   *Local only (never the cloud)* sends it).
3. The endpoint does not go to providers: only chat, completions and embeddings do; rerank, score
   and speech to text stay local.
4. The reason is not one that spills. A request moves on only before its answer starts, when the
   local tier is unavailable, busy, or answers 429 or 503; a prompt too long for the local context
   only with **Spill a prompt that is too long**. A 400, a timeout after the request was sent, or a
   stream that had started never spill.
5. A **budget** is used up: the answer carries `X-Forgeyard-Budget-Exhausted`.
6. The provider, or its target in the route, is **disabled**.

**Do:** on **API keys**, open the key's **Limits** and set *Cloud providers* to **Only to spill**
(or **Allowed**, for cloud-only models too), then **Save limits**. People can choose at most what an
admin allows under **Cloud policy**. In the Playground, admins may use the cloud and everyone else
gets that most. Details: [When a request spills](cloud-providers.md#when-a-request-spills).

## An image is refused

**You see:** a chat request with an image (or audio, or video) answers 400 or 413; the `code` says
why. Nothing of it reached the engine.

| `code` | Why | Do |
|---|---|---|
| `media_not_supported` | The model does not read that medium: a text model, a GGUF model added without its projector file, or video while the deployment's *Videos per request* is 0 | Use a vision model; add the GGUF model again with its projector; turn video on under **Advanced** |
| `too_many_media_items` | More images than the deployment's *Images per request* (4) | Send fewer, or raise it |
| `media_url_refused` | The URL is `file://` or another scheme, has credentials, or its host is (or redirects to) a private, loopback, link-local or metadata address | Send a public `https://` URL or a `data:` URI. For an image server on your LAN, set `MEDIA_ALLOW_PRIVATE_HOSTS=true` on the server |
| `media_fetch_failed` | The gateway could not download it: the server answered another status than 200, took longer than 20 s, redirected more than 3 times, or sent something that is not an image | Open the URL yourself; some sites refuse downloads by programs. Send the file as a `data:` URI instead |
| `media_too_large` (413) | The file is larger than `MEDIA_FETCH_MAX_MB` (20) | Send a smaller image, or raise the setting |
| `request_too_large` (413) | The request, with images inlined as base64 (a third larger than the files), is over the deployment's *Largest request* (64 MiB) | Send smaller images, or raise it under **Advanced** |
| `file_input_not_supported` | A `file` part (PDF): no built-in engine takes files, and the gateway does not convert them | Send the pages as images, or the text |
| `media_local_only` | The model is served only by cloud providers; media stay on your own deployments | Use a local vision model |

A vision model on vLLM that **does not start after video was turned on**: vLLM reserves memory for
a long video at startup and is left with too little for the KV cache. Lower the context length, or
turn video off again.

## Speech is refused, or sounds wrong

**You see:** `/v1/audio/speech` answers 400; the `code` says why. Nothing reached the engine.

| `code` | Why | Do |
|---|---|---|
| `voice_cloning_not_supported` | The request carries a reference recording (`ref_audio`, `speaker_ref_b64`, …), a voice given as audio or a link, or `task_type: "Base"`; or a program tried to upload a voice | Use one of the model's voices: `GET /v1/audio/voices?model=…` |
| `input_too_long` | More characters than the deployment's *Longest input* (by default a quarter of the context, 1,024, for vLLM-Omni and llama.cpp; 4,096 for speaches) | Split the text into sentences or paragraphs, or raise it under **Advanced** (and the context with it) |
| `unsupported_response_format` | The engine does not make that format (mp3 or opus from llama.cpp, opus or aac from speaches) | Ask for one it makes; the message lists them |
| `stream_format_not_supported` | `stream_format: sse` to llama.cpp or speaches | Use `audio`, or leave it out |
| `language_not_supported` | Chinese, Japanese or Korean to speaches: its released versions delete those characters | Use a Qwen3-TTS deployment |

**The audio is cut off at the end:** llama.cpp stops at `n_predict` audio frames; the gateway sets
ten a character (at least 512, at most the context) when the request gives none. A model that speaks
slowly may need more: send `n_predict` yourself, and keep it within the deployment's context.

**The Chinese is read in English, or badly:** on llama.cpp, Qwen3-TTS speaks English unless it gets
`lang`. The gateway sets it from Chinese, Japanese or Korean characters; for text mostly in Latin
letters, send `"lang": "zh"` yourself.

**The voice is not the one asked for:** OpenAI's names (`alloy`, `nova`, …) are not the model's
voices; the gateway uses the model's default voice and names it in `X-Forgeyard-Voice`. Ask for one
the model lists.

**Streaming sends everything at the end:** some models make the whole utterance before any audio
comes out (Chatterbox on llama.cpp does). Qwen3-TTS streams.

## An image request is refused, or slow

**You see:** `/v1/images/generations` answers 400; the `code` says why. Nothing reached the engine.

| `code` | Why | Do |
|---|---|---|
| `response_format_not_supported` | `response_format: url`. Forgeyard keeps no copies of images, so it has no URL to give | Leave it out (or `b64_json`) and decode `data[].b64_json` |
| `too_many_images` | `n` over the deployment's *Images per request* | Ask for fewer, or raise it (memory grows with it on vLLM-Omni) |
| `image_too_large` | A side over *Largest side* (by default the model's own size) | Ask for a smaller size, or raise *Largest side* under **Advanced** |
| `invalid_size` | Not `WIDTHxHEIGHT`, or sides that are not multiples of 16 (64 for Stable Diffusion's UNet) | Use the size the message suggests |
| `too_many_steps` | `num_inference_steps` over *Most steps* | Ask for fewer |
| `invalid_request` | No prompt, or `<sd_cpp_extra_args>` in it | Send a prompt; use `num_inference_steps`, `seed`, `negative_prompt` instead |

**429 `queue_full`:** the deployment is busy and its queue is full. Wait `Retry-After` seconds, add a
replica, or raise *Queue length*. **503 `queue_timeout`:** a request waited longer than *Timeout*.

**504 `upstream_timeout`:** making the images took longer than the deployment's *Timeout* (600 s for
vLLM-Omni, 1,800 s for stable-diffusion.cpp). On a CPU, ask for a smaller size, fewer steps or fewer
images, or raise *Timeout*.

**An `engine_error` about memory:** a vLLM-Omni deployment holds what its largest request needed. If
a shared card runs out, lower *Images per request* or *Largest side*, or turn on *Offload to host
memory* (slower) or *VAE tiling* under **Advanced**.

**The image does not look like the model's examples:** stable-diffusion.cpp takes a model's sampling
settings from the catalog entry's engine arguments (Z-Image-Turbo: `--cfg-scale 1 --steps 8`). A model
added from search has none: add them to its entry.

**An `sd-cpp` replica does not start on a CPU machine:** its process engine needs glibc 2.39 or newer
(Ubuntu 24.04, Debian 13) and a 1.3 agent, which unpacks `.zip` engines. An older agent shows "cannot
unpack .zip engines" in the placement: update it.

## An image edit is refused

**You see:** `/v1/images/edits` (or **Edit** in the Playground) answers 400 or 413; the `code` says why. Nothing
reached the engine.

| `code` | Why | Do |
|---|---|---|
| `edits_not_supported` | The model makes images from text and edits none | Use `/v1/images/generations`, or a model whose page says *Edits images* (FLUX.2 [klein]) |
| `generation_not_supported` | The model only edits (Qwen-Image-Edit), and the request went to `/v1/images/generations` | Send it a picture at `/v1/images/edits` |
| `missing_image` | No picture in the request | Send it as `image` (multipart) or `images: [{"image_url": …}]` (JSON) |
| `too_many_input_images` | More pictures than the deployment's *Input images* (1 by default) | Send fewer, or raise *Input images* under **Advanced** (up to what the model takes: 4 for FLUX.2 [klein]) |
| `input_image_too_large` | A picture wider or taller than *Largest input* | Scale it down, or raise *Largest input* (the estimate grows with it) |
| `unsupported_image_format` | Not a PNG, JPEG or WebP (an iPhone's HEIC photo, a GIF) | Convert it first |
| `invalid_image` | Its header does not say its size, or the data: URL is not base64 | Send the file again; check how it was encoded |
| `mask_not_supported` | A `mask` | Leave it out and describe the change in the instruction |
| `file_id_not_supported` | A picture given as a `file_id` | Send the picture itself, or its address |
| `media_url_refused`, `media_fetch_failed` | An address the server may not or could not fetch (a private address, a failed download) | Send the picture itself as a file or a data: URL |
| `unsupported_output_format` | A format the engine does not make for edits (stable-diffusion.cpp: `png` and `jpeg` only) | Ask for one it makes |

**413 `media_too_large`:** a picture over `MEDIA_FETCH_MAX_MB` (the server's setting). **413 on the whole request:**
it is larger than the deployment's *Largest request*. The rest (`too_many_images`, `image_too_large`, `invalid_size`,
429, 504) is as for [image requests](#an-image-request-is-refused-or-slow).

**The result ignores the second picture:** the deployment takes one (*Input images* 1, the default): it answered
`too_many_input_images`, or you sent one. Raise *Input images*.

## A video fails, or is refused

**You see:** `POST /v1/videos` answers 400; the `code` says why. Nothing reached the engine.

| `code` | Why | Do |
|---|---|---|
| `video_too_long` | `seconds` over the deployment's *Longest video* | Ask for fewer seconds, or raise it (memory grows with it) |
| `video_too_large` | A side over *Largest side*, or more pixels a frame than the default size scaled to it | Ask for the model's own size (832x480 for Wan2.1), or raise *Largest side* |
| `invalid_size` | Not `WIDTHxHEIGHT`, or sides that are not multiples of 16 (32 for Wan2.2-5B) | Use the size the message suggests |
| `input_reference_not_supported` | A start image for a model that makes videos from text only, or a reference the engine would fetch itself (`image_reference`, `video_reference`, `audio_reference`) | Leave it out, or use a model that takes a start image (Wan2.2-TI2V-5B) |
| `input_reference_required` | An image-to-video model without a start image | Send it as `input_reference` |
| `input_image_too_large`, `unsupported_image_format`, `invalid_image`, `file_id_not_supported` | The start image is wider or taller than *Largest start image*, not a PNG, JPEG or WebP (convert HEIC photos first), unreadable, or a `file_id` | Send a smaller PNG, JPEG or WebP picture itself (a file, a data: URL or an address) |
| `too_many_steps`, `prompt_too_long` | Over *Most steps* or *Longest prompt* | Ask for fewer, or shorten the prompt |

**429 `too_many_jobs`:** your key (or you, in the Playground) already has *Jobs per key* videos waiting
or being made on that deployment. **429 `queue_full`:** the deployment holds *Jobs at once*. Wait
`Retry-After` seconds, or delete a job you no longer need.

**It stays `queued`:** a replica makes one video at a time; the others wait on it. Each takes minutes.
Add a replica, or ask for shorter or smaller videos.

**It reports `failed`:** `error.code` says why.

| `error.code` | Why | Do |
|---|---|---|
| `replica_restarted`, `video_lost` | The replica's engine restarted, and its jobs (kept in its memory) went with it | Ask again; look at the deployment's events for why it restarted |
| `replica_gone` | The replica was stopped, moved or deleted | Ask again once the deployment is running |
| `timed_out` | Not done within the deployment's *Longest job* | Ask for a shorter or smaller video, or raise *Longest job* |
| `generation_failed` | The engine failed the video; its message is in `error.message` | Read it; an out-of-memory error: see below |

**410 `video_expired`:** the video was older than *Keep results* and was deleted. **410
`video_lost`:** its replica restarted or was stopped after it was made. Ask for it again either way.

**Out of GPU memory on a shared card:** lower *Longest video* or *Largest side*, or turn on *Offload to
host memory* under **Advanced** (the text encoder alone is 11 GB; slower videos).

## Cost shows requests without a price

**You see:** **Usage** → **Cost** counts requests under *Without a price*, and says the cost above
leaves them out.

**Why:** a cloud request is priced from its provider model's prices when it finishes. It has no price
when the provider did not report the cost itself and the model had no input or output price then, or when the provider answered but reported no
tokens (for example a stream the client closed before the usage arrived). Its cost is unknown, not 0.
Local replicas and providers inside your network always cost 0 and are never without a price.

**Do:** an admin opens the provider's page and sets the model's **Prices**, or uses **Refresh prices
from OpenRouter**. Prices apply to requests from then on: requests already counted stay without a
price. Under a budget, a model without a price is refused (`402 budget_unpriced`) until it has one.

## Agent in a container without systemd

**You see:** processes (engines) fail to start, and the agent's log mentions `systemd-run` or
`Failed to connect to bus`.

**Why:** with `limits = "auto"` the agent uses a systemd scope when it runs as root and finds
systemd. Inside a container that shares the host's `/run`, it may find systemd's files but cannot
talk to it.

**Do:** use rlimits. In `/etc/forgeyard/agent.toml`:

```toml
[workloads]
limits = "rlimit"
```

or `--limits rlimit`, or `FORGEYARD_LIMITS=rlimit` (the agent's Docker image sets this already).
Processes then get resource limits, and their CPU is counted, not capped.

## The server refuses a setting, or warns about one

**It does not start** and the log says `Setting COOKIE_SECURE: …` (or another name): that value
cannot be used. The message lists what the setting takes. The same goes for `TLS`, `REGISTRATION`,
`GATEWAY_CLOUD_DEFAULT` and `GATEWAY_CLOUD_MAX`, and for setting only one of `TLS_CERT_FILE` and
`TLS_KEY_FILE` (`HTTPS: Set both TLS_CERT_FILE and TLS_KEY_FILE, or neither`).

**It starts with lines like `Settings: GATEWAY_CLOUD_DEFUALT is not a setting`**: the server ignores
that name. Each line says why and what to do:

- *is not a setting… Did you mean …?*: a typo, or a setting from another version. Fix the name in
  `.env` or the environment.
- *is ignored*: another setting makes it useless, e.g. `TLS_CA_FILE` without your own certificate,
  or `MODEL_URL_HOSTS` after an admin saved **Models → Sources** (the saved value wins: change it in
  the web UI).
- *ADMIN_PASSWORD is still set*: it is only read on the first start. Remove it from `.env`.

Admins see the same lines, with a link to the fix, under **Get started → Setup health**
([Check the setup health](how-to.md#check-the-setup-health)).

## What to include in a bug report

```bash
forgeyard --version
sudo forgeyard agent config
sudo forgeyard agent inventory --raw --json
sudo forgeyard agent runtimes
journalctl -u forgeyard-agent -n 200 --no-pager
```

Remove anything private (host names, addresses) before you share it.
