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

**What Forgeyard does:** when a pull fails this way, the agent pulls again anonymously, without the
helpers. That is enough for every built-in engine image, since they are all public.

**Do,** if it still fails (a private image, or no internet on the machine):

- pull the image on the machine yourself, once: `docker pull vllm/vllm-openai:v0.32.0`; the agent
  then finds it locally;
- or fix the helper (`docker-credential-<name> list` must work for the user the agent runs as).

## Hugging Face downloads are slow

**You see:** a deployment stays in *downloading* for a long time; its page shows the progress and
the speed.

**Why:** the agent downloads models from Hugging Face directly, at whatever speed the machine's
network reaches it. On one test machine that was 3–4 MiB/s, so a 16 GB model took over an hour.

**Do:**

- wait: downloads resume where they stopped, and a model is downloaded only once per machine;
- use a mirror: set `HF_ENDPOINT` on the server to it. For a mirror on your LAN, also set
  `MODEL_ALLOW_PRIVATE_HOSTS=true` on the server and `allow_private_hosts = true` under `[models]`
  in each agent's `agent.toml`;
- copy a model cache from another machine that has it (same layout, see
  [Move a model cache](how-to.md#move-a-model-cache));
- or put the files in a directory on the machine and add them as a `local://` model.

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

## A deployment does not fit

**You see:** *Deploy* answers *Nothing can take a replica that needs … GiB per GPU*, followed by one
line per machine.

**Why:** Forgeyard places a replica only where the estimate fits into memory that is free **now**,
on cards Forgeyard may use. It refuses rather than start something that would fail. Each machine's
line says why it was not chosen:

| Line | Means |
|---|---|
| `no allowed GPU that vllm supports` | no card on the GPU allowlist, or no recipe variant for that card and driver |
| `needs 14.38 GiB per GPU; available: #4 12.07 GiB, … (2.31 GiB short)` | the best card misses that much free memory |
| `needs 1 GPU it can have; in use: #4 (qwen)` | the cards are taken by replicas that do not share |
| `needs 3.08 GiB of RAM, 1.20 GiB available (1.88 GiB short)` | a CPU model, and not enough free RAM |
| `no container runtime (…)` | the recipe needs Docker or Podman, which the machine does not have |
| `its agent does not serve models` | the agent has no model cache (`[models] cache` in its settings) |
| `offline` | the machine's agent is not connected |

**Do:** free memory on a card (stop something), allow more cards ([GPU access](how-to.md#restrict-gpus-on-a-shared-machine)),
lower the context length or concurrency, choose a quantised or smaller model, or deploy it to wait
until room appears (the API's `"wait": true`).

## Errors from /v1: 401, 403, 429, 503

The gateway answers errors in the OpenAI format, `{"error": {"message", "type", "param", "code"}}`.
The `code` says what happened.

### 401

`missing_api_key`, `invalid_api_key`, `expired_api_key`, `revoked_api_key`, `account_disabled`: the
key is missing (send `Authorization: Bearer fyk_…`), wrong, expired or revoked, or its owner's
account is disabled. Check the key on **API keys**; create a new one if needed.

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
deployment's page.

Others: `404 model_not_found` (no deployment with that model name: see `/v1/models`), `502` (a
replica could not be reached or refused its internal key: usually its agent restarted; it recovers
within seconds), `504` (no answer within `GATEWAY_READ_TIMEOUT`, 600 s by default).

### Errors from cloud providers

A model that goes to a cloud provider adds these. The answer's `X-Forgeyard-Target` says whether a
provider answered.

- `404 model_not_found` for a model listed on **Models**: it is cloud-only, and the key's cloud
  setting is not **Allowed**.
- `400 cloud_only_model`: the request had `X-Forgeyard-Local-Only: 1` (or the route is
  **Local only**), but the model is served only by a cloud provider.
- `502 provider_auth_failed`: the provider refused Forgeyard's key for it; an admin fixes it on
  **Providers**. `502 provider_blocked`: the egress rules refused the provider's address.
  `502 provider_redirect`: the provider answered with a redirect, which is never followed; check its
  base URL.
- `429 provider_rate_limited`: every target of the cloud tier was rate limited; wait for
  `Retry-After`.
- Any other 4xx keeps the provider's status, code and message, prefixed with the provider's name.

More in [Cloud providers](cloud-providers.md#troubleshooting).

## Agent in a container without systemd

**You see:** process workloads fail to start, and the agent's log mentions `systemd-run` or
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
Process workloads then get resource limits per process, and their CPU is counted, not capped.

## What to include in a bug report

```bash
forgeyard --version
sudo forgeyard agent config
sudo forgeyard agent inventory --raw --json
sudo forgeyard agent runtimes
journalctl -u forgeyard-agent -n 200 --no-pager
```

Remove anything private (host names, addresses) before you share it.
