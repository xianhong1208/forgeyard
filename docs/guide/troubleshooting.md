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
  locally. The models' engine images need the same on such a machine: pull or load them once, and
  the agent uses them.

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
| `needs 14.38 GiB per GPU; available: #0 12.07 GiB, … (2.31 GiB short)` | the best card misses that much free memory |
| `needs 1 GPU it can have; in use: #1 (qwen)` | the cards are taken by replicas that do not share |
| `needs 3.08 GiB of RAM, 1.20 GiB available (1.88 GiB short)` | a CPU model, and not enough free RAM |
| `no container runtime (…)` | the recipe needs Docker or Podman, which the machine does not have |
| `its agent does not serve models` | the agent has no model cache (`[models] cache` in its settings) |
| `offline` | the machine's agent is not connected |

**Do:** free memory on a card (stop something), allow more cards ([GPU access](how-to.md#restrict-gpus-on-a-shared-machine)),
lower the context length or concurrency, choose a quantised or smaller model, or deploy it to wait
until room appears (the API's `"wait": true`).

## A replica stays pending

**You see:** a deployment or workload was placed on a machine (*Replica 0 placed on …*), the machine
is online, and the replica stays **pending** with no reason: nothing is downloaded or started.

**Why:** the machine's agent is not acting on its work list. Agents up to 1.2.0 kept the number of the
last work list they received across connections, and a server restarted since then numbered its
lists from 1 again: the agent took them all for old ones and dropped them without a word. A server
from 1.3.0 numbers them so that this cannot happen, also for those older agents; an agent from
1.3.0 starts a new count on every connection and logs any list it drops.

**Do:** run the server's own version on the agent ([upgrades](how-to.md#upgrade-the-server-and-the-agents)); until then,
restarting the agent (`sudo systemctl restart forgeyard-agent`, or `docker restart forgeyard-agent`)
makes it take its work at once. **Setup health** flags agents older than the server, and replicas
that waited for a machine for more than 15 minutes.

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

Others: `404 model_not_found` (no model by that name for this key: see `/v1/models` and
[A model is missing from /v1/models](#a-model-is-missing-from-v1models)), `502` (a replica could not be
reached or refused its internal key: usually its agent restarted; it recovers within seconds),
`504` (no answer within `GATEWAY_READ_TIMEOUT`, 600 s by default). Every code is listed in the
[reference](reference.md#errors).

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
- `402 budget_exhausted`, `402 budget_unpriced`: a monthly budget; see [402](#402).
- Any other 4xx keeps the provider's status, code and message, prefixed with the provider's name.

More in [Cloud providers](cloud-providers.md#troubleshooting).

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
| Python, OpenAI SDK, `httpx` | in code: `httpx.Client(verify=ssl.create_default_context(cafile="forgeyard-ca.pem"))`, as in the [quick start](quick-start.md#python-with-the-openai-sdk); or `SSL_CERT_FILE` |
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

**Do:** open **Models** and check the model's state; **Start** a stopped deployment. On **API keys**,
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
meanwhile, add a cloud tier to the model's route ([Cloud providers](cloud-providers.md#route-a-model-to-the-cloud)).

## A request does not spill to the cloud

**You see:** a model's route has a cloud tier, but requests still fail (503, 429) or wait while the
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
admin allows under **Cloud for keys**. In the Playground, admins may use the cloud and everyone else
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
  or `MODEL_URL_HOSTS` after an admin saved **Model sources** (the saved value wins: change it in
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
