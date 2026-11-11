# Cloud providers

Forgeyard can send requests to model APIs outside it: OpenAI, Anthropic, Google Gemini, Azure
OpenAI, OpenRouter, or any OpenAI-compatible server, such as a vLLM on another network. Your
programs keep one address, one set of API keys, one set of limits and one usage page, and the
local models come first. [繁體中文版](zh-TW/cloud-providers.md)

## How it works

- A **provider** is one account at one of those APIs: its base URL and its API key. The key is
  stored encrypted and is never shown again. Admins add providers on **Providers**.
- A provider's **models** are the names you may send it, such as `gpt-4o-mini` or, for Azure, your
  deployment's name.
- A **route** is a model name at `/v1`. It has **targets** in **tiers**. Every deployment has its
  route, with the deployment as its only target. An admin can add a second tier to it, such as a
  cloud model to use when the local replicas are busy or down. An admin can also make a route that
  has only a cloud model.
- Forgeyard talks to every provider through that provider's **own OpenAI-compatible endpoint**.
  It has no translators to the providers' native APIs, and it does not use LiteLLM. What each
  endpoint cannot do is listed [below](#what-each-providers-endpoint-cannot-do).
- Each API key has a **cloud** setting, and it is **Never** unless someone changes it. See
  [who may use the cloud](#who-may-use-the-cloud).

Only chat, completions and embeddings go to providers. Rerank, score and speech to text stay local.

## Add a provider

On **Providers**, choose **Add a provider**, pick the kind and paste the key. Then:

1. **Test connection** asks the base URL for `/models` with the key. It shows how many models the
   provider listed, or why it refused.
2. Open the provider and add its models: by name, or with **Import from /models** (not for Azure,
   whose list does not show your deployments).
3. Put a model in a route (see [route a model to the cloud](#route-a-model-to-the-cloud)).

**Outside your network** is ticked for every kind except *OpenAI-compatible server*. Keep it ticked
for a third party. Untick it for a server of your own: a provider inside your network does not
count as cloud, so every key may use it.

### OpenAI

Kind *OpenAI*, an API key from platform.openai.com. The base URL is preset
(`https://api.openai.com/v1`). Chat, completions and embeddings work. `max_tokens` is sent as
`max_completion_tokens`, because newer OpenAI models refuse the old name.

### Anthropic

Kind *Anthropic*, an API key from console.anthropic.com. The base URL is preset
(`https://api.anthropic.com/v1`, the compatibility endpoint). If your key belongs to an
organisation and not a workspace, add the extra header `anthropic-workspace-id: <id>`. Anthropic
has no embeddings. Read [what it cannot do](#anthropic-1) before you rely on it.

### Google Gemini

Kind *Google Gemini*, an API key from aistudio.google.com. The base URL is preset
(`https://generativelanguage.googleapis.com/v1beta/openai`). Gemini lists its models as
`models/gemini-…`; the import removes `models/`, which is what requests use. Chat and embeddings
(`gemini-embedding-001`) work.

### Azure OpenAI

Kind *Azure OpenAI*, the key of your Azure OpenAI resource, and your own base URL:
`https://<resource>.openai.azure.com/openai/v1` (the v1 API, without `api-version`). The model names
are your **deployment names** in Azure: add them by hand.

### OpenRouter

Kind *OpenRouter*, an API key from openrouter.ai. The base URL is preset
(`https://openrouter.ai/api/v1`). Model names look like `openai/gpt-4o-mini` or
`anthropic/claude-sonnet-4.5`; import them. OpenRouter routes between its own providers, and
reports the cost of each request.

### A server of your own (vLLM elsewhere)

Kind *OpenAI-compatible server* and its base URL, such as `https://llm.example.org/v1`, plus a key
if it wants one. *Outside your network* is unticked by default: it is your server, so keys need no
cloud permission to use it. Over a VPN with a private address, you may tick **Allow plain HTTP**.
Forgeyard drops nothing from requests to it.

## Route a model to the cloud

To let a deployment spill to the cloud when it is busy or down:

1. Open the deployment and choose **Routing and cloud spill**. You can also go to **Models**, then
   **Routes and cloud**.
2. **Add a tier**, and add the provider's model to it.
3. Choose the rules (see [when a request spills](#when-a-request-spills)) and **Save routing**.

To serve a cloud model under a name of its own, choose **Models**, then **Routes and cloud**, then
**New route**. Give it the name clients will send as `model`, and add the provider's model. Only
keys whose cloud setting is **Allowed** see such a route.

Every answer says who served it, in the header `X-Forgeyard-Target`: `local:<deployment>#<replica>`
or `provider:<provider>/<model>`. When a request moved past the first tier, `X-Forgeyard-Spilled`
says why: `unavailable`, `busy`, `refused`, `too_long`, or `budget` (a [budget](#budgets) kept it
from the cloud tier before). The `model` in the answer's body is what
the upstream wrote. The Playground shows the target under each answer.

## When a request spills

A request moves to the next tier **only before anything was sent back to the client**, and only
when the tier is:

- **unavailable**: no replica is ready, or none could be connected to;
- **busy**: every ready replica already has as many requests in flight through this server as its
  deployment's *Concurrent requests* setting. With **Wait for a free local slot**, it waits up to
  that many milliseconds first (at most 10,000; 0 by default);
- **refusing**: the engine answered 429 or 503;
- **too small**, only when the route has **Spill a prompt that is too long**: the prompt's size
  (about 3 bytes a token, on the high side) plus `max_tokens` is more than the deployment's context.

It **never** moves on when:

- the engine answers another error, such as 400 for a bad request (it would fail anywhere, and
  cost money upstream);
- the request was sent and no answer came in time (504): trying again would double the work;
- the replica refused the gateway's internal key (502);
- the client went away;
- the answer has started streaming.

If the local tier was busy and the cloud cannot take the request either, the request waits for a
local replica after all.

Within a cloud tier, a target that answers 429 or 5xx, or cannot be reached, is skipped and tried
last for 30 seconds (`PROVIDER_COOLDOWN_SECONDS`). Several targets in one tier share the requests by
their weight. When nothing else is left and the provider asked to wait up to 2 seconds
(`Retry-After`, `PROVIDER_RETRY_AFTER_CAP`), the request waits once and tries again.

A request can forbid the cloud for itself with the header `X-Forgeyard-Local-Only: 1`. It never
widens what the key allows:

```bash
curl https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer fyk_…" -H "X-Forgeyard-Local-Only: 1" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3-8b", "messages": [{"role": "user", "content": "Hello"}]}'
```

A route with **Local only** never leaves your network, whatever the keys allow. Use it as a switch
for sensitive models.

## Who may use the cloud

Each API key has a **cloud** setting:

| Setting | Mixed routes (local, then cloud) | Cloud-only routes |
|---|---|---|
| **Never** (`off`) | local only: the request gets the local error instead of spilling | do not exist: 404 and not in `/v1/models` |
| **Only to spill** (`fallback`) | spill as described above | do not exist |
| **Allowed** (`on`) | spill as described above | listed and usable |

- Every key made before 1.2 is **Never**. A new key gets the server's default, which is **Never**
  unless an admin changes it.
- People may give their own keys at most **the most people may give their own keys**, which is
  **Never** unless an admin raises it. Both are under **API keys**, then **Cloud for keys**.
- An admin can set any key's cloud setting. On someone else's key the value is then **locked** for
  its owner, as limits are. **Unlock cloud** gives it back the default.
- A provider inside your network (not ticked *Outside your network*) is not cloud: every key may use
  it.
- In the **Playground**, admins may use the cloud; everyone else gets the most they may give a key.

## What each provider's endpoint cannot do

Forgeyard sends requests in the OpenAI shape to each provider's OpenAI-compatible endpoint, and
passes the answer back unchanged. These are the limits of those endpoints, as their providers
document them (November 2026).

### OpenAI

Nothing on Chat Completions and embeddings. Newer features arrive first, or only, in the Responses
API, which Forgeyard does not serve.

### Anthropic

Anthropic says its OpenAI compatibility is not meant as a long-term or production-ready solution
for most uses. Through it you lose:

- **prompt caching**, so long prompts cost more than through Anthropic's own API;
- **thinking**: it is never returned, and `thinking: {type: adaptive}` is refused;
- **PDF, file and audio input** (400);
- **`n` above 1** (refused), `strict` on tools (ignored), `reasoning_effort` (ignored);
- the **usage breakdown**: `prompt_tokens_details` and `completion_tokens_details` are always
  empty, so Usage shows no cached or reasoning tokens;
- `response_format` other than `json_schema` with `strict: true` (Forgeyard drops a plain
  `{"type": "text"}`, which is the default anyway);
- **embeddings**: Anthropic has none.

On the newest Claude models, a `temperature` below 1 together with `top_p`, or a forced
`tool_choice`, may also be refused. System and developer messages are joined into one. A native
`/v1/messages` passthrough, which keeps caching and thinking, may come later.

### Google Gemini

- The endpoint is in **beta**, and parameters it does not know are ignored silently.
- No file upload or download. Context caching works only through
  `extra_body.google.cached_content`.
- **Gemini 3 thought signatures** (`extra_content.google.thought_signature` on each tool call) must
  be sent back on the next turn. Forgeyard passes them through untouched. A conversation whose
  earlier turns were answered by a local model has none, and Gemini may refuse it with 400.
- Thinking cannot be turned off on some models. Whether thinking tokens count in
  `completion_tokens` has not been checked yet.

### Azure OpenAI

- The model name is your **deployment name**, not the model's.
- Microsoft recommends the Responses API. Only part of Azure's features were in the first release
  of the v1 API, and its content-filter errors are Azure's own.

### OpenRouter and other servers

Only what is specific to one provider behind them. OpenRouter accepts `top_k`, `min_p` and
`repetition_penalty` and passes them on; for the other cloud kinds Forgeyard drops those and other
engine-only parameters (vLLM's `chat_template_kwargs`, `guided_*`, llama.cpp's sampler settings)
when a request leaves for them.

## Cost and prices

**Usage → Cost** shows what requests cost, in US dollars, per day, key, person (admins), model or
provider, with the share served locally and the requests that have no price. It is Forgeyard's
estimate of what the providers charge, not their invoice.

### How a request's cost is worked out

A request is priced once, when it finishes, and its cost is added to the usage numbers. Changing a
price later does not change what was already counted.

- **Local replicas, and providers inside your network** (not marked as cloud), cost 0.
- When the provider **reports the cost itself**, that figure is used. OpenRouter does
  (`usage.cost`); it is exact for OpenRouter traffic.
- Otherwise the cost comes from the provider model's prices, in USD per million tokens:

  ```
  (prompt − cached − cache writes) × input
    + cached × cached input
    + cache writes × cache write
    + completion × output
  ```

  Cached tokens and cache writes are part of the prompt tokens, so they are not counted twice.
  **Reasoning tokens** are part of the completion tokens and are billed at the output price once:
  they are shown on **Requests**, but never added on top. Embeddings have input tokens only.
- Costs are kept in whole **micro-dollars** (millionths of a dollar). Cheap requests are summed
  exactly before rounding, so a thousand requests of $0.0000004 each still add up to $0.0004.

### Requests without a price, and estimates

- A cloud request whose model has **no input or output price** (whichever it used) has an
  **unknown** cost, not 0: it is counted under *Without a price*, and the Cost tab says how many
  there are, with a link to set prices.
- A cloud request that was answered but reported **no tokens at all** (for example a stream the
  client closed before the usage arrived) is also without a price. One the provider refused or failed
  (a 429, a 5xx) costs 0: providers do not bill those.
- **Estimates** are counted too, and said under the table:
  - **Anthropic** leaves `cached_tokens` empty on its OpenAI-compatible endpoint, so cached tokens are
    unknown there; those requests are priced as if nothing was cached. (That endpoint does no prompt
    caching anyway, so the estimate is normally right.)
  - A model with cached tokens or cache writes but no cached or cache-write price is priced at the
    input price for those tokens.
- **Gemini**: whether its compatibility endpoint counts thinking tokens in `completion_tokens` has not
  been verified. If its costs look low for a thinking model, compare them with your Google bill.

### Prices

On a provider's page, each model shows its prices per million tokens, where they came from (*entered
by hand* or *from OpenRouter*) and when. Admins set them:

- **Prices** on a model: input, output, cached input and cache write, in US dollars per million
  tokens, as the provider's pricing page lists them. Leave a field empty when it does not apply.
- **Refresh prices from OpenRouter**, on a provider (its models) or on the Providers page (every
  provider outside your network). Forgeyard reads OpenRouter's public model list,
  `https://openrouter.ai/api/v1/models` (no key is sent; it goes through the egress rules below, so add
  `openrouter.ai` to `EGRESS_ALLOWED_HOSTS` if you use it), and shows a **diff**: each model, how it
  matched, and the old and new prices. Nothing changes until you tick rows and **Apply**.
  - An OpenRouter provider's models match by their own id (*same id*). Other providers' models get a
    **suggested** match from their name (`gpt-4o-mini` → `openai/gpt-4o-mini`,
    `claude-sonnet-4-5-20250929` → `anthropic/claude-sonnet-4.5`); check it and tick it yourself. For a
    model that did not match, type its OpenRouter id and **Compare**. Once applied, the match is
    remembered (*confirmed*).
  - Prices **entered by hand are kept** unless you tick that row to overwrite them.
  - If OpenRouter's price changed between the diff and **Apply**, that row is skipped; read again.
  - There is no automatic sync and no bundled price list: prices change only when an admin applies
    them.

> OpenRouter's prices are usually the provider's list price, but not always (a different region, a
> discount, a model OpenRouter serves through another provider). **Spot-check** a few against the
> provider's own pricing page before relying on them.

## Budgets

A **budget** is what cloud requests may cost in a **calendar month (UTC)**, in US dollars. Budgets
apply only to providers outside your network: local replicas and providers inside your network
cost nothing and are never limited. Without a budget, nothing changes.

| Budget on | Who sets it | When it is used up |
|---|---|---|
| **An API key** | its owner, on **API keys** (**Limits**, *Monthly budget*); an admin on any key, which locks it for the owner | that key's requests no longer spill or go to the cloud |
| **A person** | admins, on **Usage → Cost** (*Budgets this month*) | none of that person's keys, nor their Playground, can |
| **A provider** | admins, on the provider's page | that provider is skipped; other providers in the tier, or local replicas, still serve |

### Reserved before sending

Before a request is sent to a cloud provider, Forgeyard **reserves its estimated cost** against
every budget that applies (the key's, the person's and the provider's):

```
prompt size (about 3 bytes a token, on the high side) × input price
  + (max_tokens, or 4096 without it, × n) × output price
```

If any of those budgets cannot cover it (spent + reserved by requests in flight + this request >
budget), the request is **not sent to that provider**. When it finishes, the reservation is replaced
by the request's real cost; when it fails or is refused, the reservation is released. Several
requests at once cannot overspend a budget together: the reservations are counted in this server, under
a lock. A request can still cost more than its reservation (a provider that ignores `max_tokens`, or a
long answer to a request without it), so set `max_tokens` in your programs, or lower
`BUDGET_DEFAULT_MAX_TOKENS`, if a budget must be held to the cent.

### When a budget is used up

The route becomes **local-only for that scope**; nothing fails if something else can serve:

- the request would have spilled because local was **busy**: it waits for a local replica instead;
- another provider in the cloud tier has room: that provider serves it;
- the answer carries `X-Forgeyard-Budget-Exhausted`, naming the budget that kept the request from a
  provider: `key`, `person` or `provider:<name>`.

Only when **nothing else can serve** the request (the local replicas are down, or the model is
cloud-only) does it fail, with **402**:

```json
{"error": {"message": "The monthly budget of this API key (laptop) is used up: $4.98 spent of $5.00, and this request may cost up to $0.04. It resets on 2026-12-01 00:00 UTC. Models served locally still work.",
           "type": "insufficient_quota", "param": null, "code": "budget_exhausted",
           "budget": {"scope": "key", "name": "laptop", "budget_micros": 5000000, "spent_micros": 4980000,
                      "reserved_micros": 0, "request_micros": 41330, "month": "2026-11",
                      "resets_at": "2026-12-01T00:00:00Z"}}}
```

402 (*Payment Required*) is what OpenRouter answers when credits run out. Unlike 429, OpenAI's SDKs
do not retry it: retrying cannot help before the month ends or an admin raises the budget.

### Models without a price

A model with no input or output price cannot be reserved. **Under any budget**, a request that would
go to it is blocked (402 `budget_unpriced`, "an admin needs to set its prices"). Without a budget it
is used as before, and counted as *without a price*.

### Alerts at 80% and 100%

When a budget's spend this month passes **80%**, and again at **100%**, Forgeyard writes one event to
the **Audit** log (`budget.warning`, `budget.exhausted`), once per budget and month, also across
restarts. It is checked as requests finish and are reserved, so a budget lowered below the month's
spend alerts on its next request. A request refused by a budget whose spend and reservations together are already past 80%
counts as reaching 100% (a single oversized request refused below that does not alert). The pages
show it too:

- **API keys**: a meter per key (spent of budget, and what is reserved by requests in flight), your
  own budget, and a warning past 80%;
- **Usage → Cost**: *Budgets this month*, with every budget that applies to you (admins: everyone's,
  and the providers');
- a provider's page: its budget, and the Providers list flags one past 80%.

There is no e-mail: check the audit log or the pages.

### How spend is counted

The month's spend is the sum of the cost of its requests (see above), read from the usage counts when
the server starts or a new month begins, and added to as requests finish. A cloud request that was
answered but reported no tokens counts at its reservation until the server restarts. `GET
/api/budgets` returns this month's spend per key, person and provider; admins add `?everyone=true`.

## Egress: what leaves your server

- Requests to providers use their own HTTP client: TLS is verified, **redirects are never followed**
  (a key is never sent on to wherever a redirect points), and a proxy from the environment is never
  used.
- `EGRESS_PROXY` sends them through a proxy of yours instead. `EGRESS_ALLOWED_HOSTS` limits them to
  some hosts. `EGRESS_CA_FILE` trusts a company CA as well.
- **https** only. A cloud provider must resolve to public addresses. Private addresses are for
  providers inside your network, and plain http only for those with **Allow plain HTTP**.
- **Link-local and cloud metadata addresses** (169.254.169.254, fd00:ec2::254) are always refused.
  The address is checked before every request, and the connection goes to the address that was
  checked.
- Provider keys are never logged, never in the audit log, never in an answer, and never copied when
  a provider is cloned (the copy starts without a key, disabled).
- Prompts and answers are never logged.

## Troubleshooting

### Test connection says the key was refused

HTTP 401 or 403 from the provider. Check the key on the provider's site. For Anthropic, an
organisation-level key needs the `anthropic-workspace-id` header. For Azure, the key belongs to the
resource in the base URL.

### 502 provider_auth_failed

The provider refused Forgeyard's key for it. Forgeyard never passes this on as 401, which would
make the client blame its own key. An admin fixes the provider's key.

### 502 provider_blocked

The egress rules refused the request before it was sent. The base URL is http, is not in
`EGRESS_ALLOWED_HOSTS`, or resolves to a private, link-local or metadata address. The message says
which.

### A model is not found, but it is in the list on Models

It is a cloud-only route, and the key's cloud setting is not **Allowed**. Such models are "not
found" for such keys, exactly like a name that does not exist.

### A request did not spill

Check, in this order: the key's cloud setting (**Never** never spills); the route's **Local only**;
the request's `X-Forgeyard-Local-Only`; and the reason. A 400, a timeout after sending, or an answer
that had started do not spill. "Busy" counts only requests through this server against the
deployment's *Concurrent requests*.

### 402 budget_exhausted or budget_unpriced

A monthly budget kept the request from every provider that could serve it, and no local replica
could. The message and the `budget` object name the budget (`key`, `person` or `provider`), what was
spent and when it resets. Raise the budget (on **API keys**, **Usage → Cost** or the provider's page),
wait for the month to end, or start a local replica. `budget_unpriced`: the model has no price; an admin
sets it on the provider's page.

### Requests go to the cloud more often than expected

Every replica was at its *Concurrent requests*. Raise it (if the engine and memory allow), add
replicas, or set **Wait for a free local slot** to a few hundred milliseconds. `X-Forgeyard-Spilled`
and Usage (**Local or cloud**) show how often and why.
