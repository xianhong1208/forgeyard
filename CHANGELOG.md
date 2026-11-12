# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. Versions follow [Semantic Versioning](https://semver.org/); each milestone is one minor
version until 1.0.

## [Unreleased]

1.3, "more kinds of models", in progress. First part: images (and, opt-in, video and audio) in chat
through the gateway, and the usage units for all of 1.3.

### Added

- **Setup health** for admins: Forgeyard checks its own setup for settings and combinations that
  are broken, risky or useless, from the settings, the database and the agents' reports, and says
  what to do with a link to the fix. About 40 checks, among them: a key that may spend on cloud
  providers with no budget, a route's cloud model without a price under a budget (402
  `budget_unpriced`), a cloud tier before the local one, a provider marked inside your network with
  a public address, a shared machine whose GPU access nobody chose, an agent older than the server,
  `HOST` on loopback while machines joined from elsewhere, a certificate that expires or does not
  name `PUBLIC_URL`, and keys that never expire and sat unused for 90 days. Shown under
  **Get started → Setup health**, as a count in the top bar and as a banner on the page concerned.
  Warnings and notes can be snoozed for 30 days per admin; errors cannot. `GET /api/advisor`.
- A reminder to **back up `data/secret.key`**, until an admin ticks *I have a backup of key id …*;
  it comes back when the key changes.
- The server warns at start about a name in `.env` or the environment that is not a setting ("did
  you mean …?"), settings that another one makes useless (`TLS_CA_FILE` without your own
  certificate, any `TLS_*` with `TLS=off`, `SECRET_KEY_FILE` next to `FORGEYARD_SECRET_KEY`), an
  offline time under 2.5 heartbeats, the private-address switches, `ADMIN_PASSWORD` or the setup
  token left after setup, and first-value settings (`MODEL_URL_HOSTS`, `GATEWAY_MAX_RPM`,
  `GATEWAY_CLOUD_MAX`…) that the database overrides.
- `.env.example` lists every setting, the common ones first and the advanced ones commented out at
  their defaults; a test keeps it, the code and the reference in step. The reference has a
  *Common settings* table, units, and what 0 and empty mean.
- `CONTRIBUTING.md`: rules for new settings.
- **`scripts/accept_all.py`, the full regression run** on real machines: one command checks every feature
  shipped so far, so each pull request and release can show the old ones still work. Seven groups, run in
  order and chosen with `--only` / `--skip`: `server` (HTTPS with the CA, security headers, 421, CSRF,
  sign-in back-off on a throwaway account, `/guide`, `/trust`, `/install.sh`), `machines` (online, the
  hardware, the GPU allowlist enforced, join commands pinning the CA), `workloads` (a process service,
  tasks with retries and `max_duration`, a container task, GPU isolation with `gpu_probe.py`), `models`
  (small models on the CPU and GPU machines, each skipped when its estimate does not fit; the estimate on
  the deployment page; Try it), `gateway` (`/v1` with the official SDK: every endpoint, streaming with
  and without usage, key allowlists, rpm, revoking, locked limits, the key guard, balancing and failover,
  the Playground, usage, a stopped deployment leaving the list), `cloud` (with `--cloud-provider`:
  spilling, keys that may not, local only, the cloud maximum, cost, key, person and provider budgets and
  their alerts, with exactly one request to the provider) and `console` (every main page as an admin and
  as a plain user at 1440 and 390 px, with Playwright). PASS, FAIL or SKIP with the reason per check, a
  table per group, totals and the time, `--out` JSON. Everything it makes is named `accept-<run>-…` and
  removed at the end, also after a failure or Ctrl-C; a provider budget it changes is put back. Guide:
  *How-to → Check a server end to end*.
- **Images, audio and video in chat** at `/v1/chat/completions` (and the Playground): `image_url`,
  `input_image`, `input_audio`, `audio_url`, `video_url` and `input_video` parts. The gateway rebuilds
  every media part in one shape (`image_url` and `video_url` with a `data:` URI, `input_audio` with
  base64) and keeps only the fields it checked; other part types and `*_embeds` answer 400
  `invalid_content_part`.
- **The gateway fetches media URLs itself**, through the egress rules: public addresses only
  (`MEDIA_ALLOW_PRIVATE_HOSTS` for a LAN image server; link-local and metadata addresses never), every
  redirect checked again (at most 3), `MEDIA_FETCH_MAX_MB` (20) per item, 20 seconds per item, the
  answer's type must match the part. `file://` and other schemes answer 400 `media_url_refused`.
- A request goes only to a deployment that takes what it carries: what the **model** takes (read
  from its `config.json`, or a GGUF model's projector), what the **recipe** takes (its new `media`
  block), and the deployment's **Images per request** (4) and **Videos per request** (0: video is off
  until enabled), in the deployment form's collapsed **Advanced** section. New codes:
  `media_not_supported`, `too_many_media_items`, `file_input_not_supported`, `invalid_media_data`,
  `media_fetch_failed`, `media_too_large` (413), `media_local_only`.
- **Body limit per deployment:** *Largest request* (`max_body_mb`, 64 MiB for vLLM and llama.cpp); the
  gateway reads up to the largest any deployment takes, then applies the model's own (413
  `request_too_large`). Recipes without the parameter keep `GATEWAY_MAX_BODY_MB`.
- **vLLM**: `--limit-mm-per-prompt` from those parameters; `--enable-prompt-tokens-details`.
- **llama.cpp**: GGUF catalog entries can carry a **projector file** (`mmproj-*.gguf`, F16 by
  default, or chosen, or none), downloaded with the model and passed as `--mmproj` (recipes:
  `optional_args` with `if_model: mmproj` and the `{mmproj_file}` placeholder). Images are scaled to
  at most *Largest image* (`image_tokens`, 1,024 tokens, Advanced; `--image-max-tokens`): without it a
  5 MB photo became 4,076 tokens and took 125 s on 8 CPU threads; with it 1,034 tokens and 21 s, the
  same answer.
- Measured on the CPU (Qwen3-VL 2B Q4_K_M with its F16 projector, llama.cpp b11977, 8 threads, context
  4,096 × 2): estimate 3.95 GiB; RSS 3.37 GiB idle, peak 3.86 GiB after a 1,024-token image. A 256 ×
  256 image takes 66 tokens and 1.1 s; llama-server's `prompt_tokens` include the image tokens.
- **The catalog shows each model's licence, whether it is gated, and what it takes besides text**,
  read from Hugging Face when it is added. The recommended list holds only allowed licences
  (permissive, OpenRAIL-style, or terms that allow commercial use: Gemma's terms and Meta's Llama 3.1 /
  3.2 licences stay) and adds a **Vision** group: Qwen3-VL 2B, 4B and 8B (vLLM), Qwen3-VL 2B GGUF
  (CPU) and 8B GGUF (llama.cpp on a GPU or an AMD APU), with their projectors.
- **The estimate counts the vision encoder:** a recipe's `vision_overhead` (vLLM profiles the
  encoder at startup before sizing the KV cache: 1.5 GiB to start with, to be calibrated on an A100)
  and `video_overhead` (1 GiB, when video is on); for GGUF the projector file itself plus llama.cpp's
  buffers.
- **Usage units for the whole of 1.3** (migration 0012): `media_items`, `characters`, `images`,
  `kilopixels`, `media_ms`, `bytes_in`, `bytes_out` and `busy_ms` per usage row, 0 on rows from
  before, in `GET /api/usage`. This release fills `media_items`; **Usage** shows a Media column once
  there are any.
- **llama.cpp on AMD GPUs through Vulkan, built in.** The `llama-cpp` recipe gains a `vulkan` variant
  (the official `server-vulkan` image, 0.3 GB, Mesa's RADV driver). AMD cards now get, in order:
  `rocm` for the Instinct cards (`gfx908`, `gfx90a`, `gfx942`, which have no Vulkan) and the discrete
  Radeon cards the ROCm image is built for (`gfx1030`, `gfx1100`–`gfx1102`, `gfx1200`, `gfx1201`);
  `vulkan` for every other AMD GPU, among them the APUs with unified memory (Strix Halo `gfx1151`,
  Strix Point `gfx1150`, Phoenix `gfx1103`) and cards the ROCm image lacks. The custom
  `llama-cpp-vulkan` recipe used so far on a Strix Halo is no longer needed (guide: *How-to → Add an
  AMD machine*, also for machines that cannot reach `ghcr.io`).
- Recipes: **`amd_kfd: false`** on an AMD container variant gives the container only its card's render
  node (`/dev/dri/renderD*`) and that node's group, not `/dev/kfd` (ROCm's compute interface); the
  `vulkan` variant uses it. The server marks such a card `"kfd": false` in the replica's GPU list; an
  older agent ignores the mark and passes `/dev/kfd` as before.
- Reference: *Built-in recipes*, every variant in order with where it runs and what it pins.
- **Playground:** attach images in the chat tab (sent as `data:` URIs) and see them in the transcript.
- Recipe parameters can be **advanced** (the deploy form's collapsed Advanced section), and a
  deployment made before a recipe gained a parameter renders its default.
- `scripts/accept_mm.py`: the real-hardware acceptance of this part, through the API and the official
  SDK, with idempotent cleanup.
- **Container images on Docker Hub**, for linux/amd64 and linux/arm64: the server
  (`docker.io/<namespace>/forgeyard`, new `Dockerfile.server`) and the agent
  (`docker.io/<namespace>/forgeyard-agent`). A release tag `vX.Y.Z` publishes both with
  `.github/workflows/publish-images.yml`, tagged `X.Y.Z`, `X.Y` and `latest` (a pre-release only
  `X.Y.Z`), with OCI labels, provenance and an SBOM, then pulls the amd64 images back by digest and
  smoke-tests them. Without the Docker Hub secrets or the namespace it builds and tests both and ends
  green with a notice instead of pushing. The namespace is one constant,
  `forgeyard/install/images.py: DOCKERHUB_NAMESPACE`.
- **The server image**: the web UI built in a node stage, exactly `uv.lock` (with PostgreSQL) on
  Python 3.13 slim, run as uid 10001, everything kept in a `/data` volume (database, `secret.key`,
  `tls/`), port 7860, a `HEALTHCHECK` on `/api/health`, the setup token in `docker logs`. Guide:
  *Run the server in Docker* (volume, `PUBLIC_URL`, backups, upgrading by tag, pinning a digest);
  the quick start and the README offer it as a two-command start.
- **`AGENT_IMAGE`** (server setting): the agent image **Add a machine → Docker** puts in its
  command; a mirror, a private registry, a tag or a digest. Empty: the published image at the
  server's own version. Anything that is not a plain image reference stops the server at start.
- CI: `server-image` builds the server image, lints both Dockerfiles with hadolint and runs
  `scripts/server_image_smoke.sh` (first start and setup through the API, HTTPS from the volume's
  CA, the health check, then a new container on the same volume keeps the owner, the CA and
  `secret.key`); `images-arm64` builds both images for linux/arm64.

### Changed

- **llama.cpp b11977 → b12242** (2026-11-10) in every variant of the `llama-cpp` recipe: the CUDA
  (still CUDA 12.8, driver ≥ 570), ROCm (7.2.1), Vulkan and CPU images, pinned by digest, and the CPU
  release archive, pinned by its SHA-256. b12242 is the first build with published images after
  llama-server gained the OpenAI-compatible `/v1/audio/speech` endpoint (ggml-org/llama.cpp#31763, in
  b12238), which text-to-speech will use. Every flag Forgeyard passes is unchanged (checked against
  `llama-server --help` of the release). Measured on the CPU process variant (8 threads), b11977
  against b12242: Qwen3-1.7B Q4_K_M generates at the same speed within the noise of a shared machine
  (44.1 and 42.8 tokens/s in one round, 40.8 and 40.7 in the other); Qwen3-VL 2B reads an image by
  URL in the same 1,034 tokens, in 18 to 20 s on both; Qwen2.5-Omni 3B reads the same audio clip in
  779 tokens on both.
- **Video on llama.cpp with Qwen3-VL** now follows the model's own sampling (2 frames a second, in
  pairs, with timestamps; ggml-org/llama.cpp#31619), and *Video frames per second* applies only to
  models without a rate of their own. The 6-second sample clip on Qwen3-VL 2B takes 5,345 prompt
  tokens instead of 3,565, more than a 4,096-token context: give video deployments 8,192 or more
  (`scripts/accept_mm.py` does for GGUF video runs).
- **The llama.cpp estimate counts video**: 512 MiB more once *Videos per request* is 1 or more (the
  recipe's `video_overhead`, shown as *Video*). Measured on the CPU with Qwen3-VL 2B at a context of
  8,192: peak RSS 3.83 GiB without video and 4.24 GiB with the 6-second clip (4.15 on b11977), against
  an estimate of 3.95 GiB that did not count it.
- `COOKIE_SECURE` takes `auto`, `true` or `false` (also `yes`/`no`/`1`/`0`); anything else stops the
  start, instead of acting as `auto`. A setting the server cannot use is reported by name, without
  a traceback.
- The agent's `limits = auto` asks systemd for a throwaway scope (`systemd-run --scope true`) instead
  of looking for its files, so an agent in a container that sees the host's systemd uses rlimits
  instead of failing every process workload with *Failed to connect to bus*.
- Labels: budget fields say that $0 blocks all cloud spend and empty means no budget; a provider's
  *Answer timeout* shows the server's default; a route's *Wait for a free local slot* says it is in
  milliseconds; the GPU choice on **Machines** is called *GPU access*, like everywhere else.
- **Add a machine → Docker** pulls the published agent image of the server's version; building it
  moved to *Advanced / offline*, next to `docker save` / `docker load` for machines without registry
  access (with the tag to use when `AGENT_IMAGE` pins a digest). While nothing is published the page
  shows the build step as before.
- The agent image builds for linux/arm64 too: its ROCm helpers (libdrm) are installed on amd64 only;
  NVML stays on both. It now carries OCI labels (source, licence, version, revision).

### Fixed
- **Video on llama.cpp** read 4 frames a second (llama-server's default), so a 6-second 720p clip took
  about 11,500 tokens and overran a 4,096-token context (found on a real CPU machine). The `llama-cpp`
  recipe gets *Video frames per second* (`video_fps`, 1 by default, advanced).
- `scripts/accept_mm.py` covers every medium on both engines: `--video-url sample` (a 6.5 MB clip) and
  an audio model for llama.cpp (Qwen2.5-Omni-3B GGUF); a model name with a dot no longer makes an
  invalid deployment name.
- **Large images on vLLM:** a phone photo sent to Qwen3-VL on vLLM became about 16,000 tokens and
  was refused by an 8,192-token context (found on a real A100). vLLM now gets the same *Largest image*
  cap as llama.cpp (`image_tokens`, 1,024 by default) as `--mm-processor-kwargs` `max_pixels`, for
  models whose image tokens grow with the resolution; the inspector records their pixels per token
  (`vision_tile_px`). The same photo then took 1,014 tokens and 2 s (FY-MM-025).
- **After a server restart, connected agents took no new work** until they were restarted: replicas
  placed on them stayed *pending* with no reason. The server numbered each connection's work lists
  from 1, and the agent kept the highest number across connections, silently dropping anything lower.
  The server now numbers them from the clock, so they keep growing (this also fixes agents up to
  1.2.0 without upgrading them), and the agent starts a new count on every connection and logs a list
  it drops (FY-WST-008, FY-SUP-015, FY-AGT-031). Found on real machines after updating the server.

- `PUT /api/keys/cloud` with only one of `default` and `max` reset the other one to `off`; a field
  left out now keeps its saved value. `PUT /api/keys/limits` does the same (`null` still clears one).
- The guide said the Docker form's server address defaults to `PUBLIC_URL`; with `PUBLIC_URL` empty
  it is the address you opened the page with.

### Security

- **Engines never fetch a URL from a request.** vLLM 0.32 and llama-server (b11977, b12242) download any http(s)
  media URL themselves, with no private-address filter (vLLM also follows redirects). Besides the
  gateway doing the fetching, the `vllm` and `vllm-pooling` recipes pass `--allowed-media-domains
  forgeyard.invalid` (a list that matches nothing turns engine fetches off) and
  `VLLM_MEDIA_URL_ALLOW_REDIRECTS=0`. llama-server has no such switch; only the gateway reaches it
  (the agent's key guard), and the gateway sends it only `data:` URIs and base64.
- **Media never go to a provider:** requests with images, audio, video or files are served by local
  deployments only; a route served only by providers answers 400 `media_local_only`, and on a mixed
  route they do not spill.


## [1.2.0] — 2026-11-11

Cloud model APIs behind the same gateway, local first: providers and tiered routes with spill rules,
a per-key cloud setting, cost in US dollars with prices, and monthly budgets. Also the first public
release: Apache-2.0, contributor and security files, and a documentation pass over the whole guide.

### Added
- **Open source under the Apache License 2.0** (`LICENSE`; also in the agent's wheel and the package
  metadata). `CONTRIBUTING.md`, `SECURITY.md` (private vulnerability reports, what Forgeyard assumes,
  known limits) and issue forms for bugs and feature requests.
- **Cloud providers** (admins, page **Providers**): OpenAI, Anthropic, Google Gemini, Azure OpenAI (v1
  API), OpenRouter, or any OpenAI-compatible server, each through its own OpenAI-compatible endpoint,
  with a small quirks table per kind (how the key is sent, engine-only parameters dropped, `max_tokens`
  as `max_completion_tokens` for OpenAI and Azure, Gemini's `models/` prefix, usage read back with
  cached and reasoning tokens). No native translators and no LiteLLM. The provider key is encrypted at
  rest, write-only (the last four characters are shown), never logged, audited or returned, and never
  copied by **Clone**. **Test connection** asks the configured base URL for `/models`; **Import from
  /models** lists what can be added. A provider can be marked as inside your network (a vLLM on your
  VPN): then it does not count as cloud.
- **Tiered routes.** A model name at `/v1` has targets in tiers: local deployments and provider models,
  with weights. Admins add a cloud tier to a deployment's route (**Routing and cloud spill**) or make
  routes of their own, such as a cloud-only model (**Models → Routes and cloud**).
- **Spill rules**, only before the first byte: to the next tier when the local tier is unavailable, busy
  (every replica at its deployment's concurrency; optional `wait_ms`) or answers 429/503; a prompt too
  long for the local context only when the route opts in. Never on other errors, a timeout after
  sending, a refused internal key, a client that went away, or mid-stream; a busy request whose cloud
  tier fails waits for local after all. Within a cloud tier: the next target on 429, 5xx or no
  connection, a 30 s cooldown, one short Retry-After wait (≤ 2 s).
- **A key's cloud setting**: Never (default; every existing key), Only to spill, Allowed. Cloud-only
  models are 404 and unlisted for keys that may not use them. Admins set the default and the most
  people may give their own keys (**API keys → Cloud for keys**); an admin's value on someone else's
  key is locked, like limits. The Playground lists only what the person may use (admins: everything)
  and shows who answered; its **Local only** box sends `X-Forgeyard-Local-Only: 1`.
- Headers: request `X-Forgeyard-Local-Only: 1`; answers `X-Forgeyard-Target`
  (`local:<deployment>#<replica>` or `provider:<provider>/<model>`) and `X-Forgeyard-Spilled`.
- **Egress rules** for providers: their own HTTP client, TLS verified, redirects never followed, no
  proxy from the environment (`EGRESS_PROXY`, `EGRESS_ALLOWED_HOSTS`, `EGRESS_CA_FILE`), https only,
  public addresses only for cloud providers, link-local and metadata addresses always refused, the
  checked address is the one connected to.
- Usage per target (**Usage → Local or cloud**), with cached and reasoning tokens and spilled requests.
- **Cost tracking** (USD; part B1). Every request is priced when it finishes and its cost is stored on
  its usage row in integer micro-dollars, so later price changes never rewrite history: 0 for local
  replicas and providers inside your network; the upstream's own figure when it reports one
  (OpenRouter's `usage.cost`); otherwise `(prompt − cached − cache writes) × input + cached × cached
  input + cache writes × cache write + completion × output` from the provider model's prices (reasoning
  tokens are inside the completion, billed once). A missing price makes the cost unknown, never 0:
  such requests are counted as *without a price*. Anthropic's endpoint (no cached-token details) and a
  missing cache price give estimates, counted as such. Cache writes are now read from usage
  (OpenRouter's `cache_write_tokens`, Anthropic's native `cache_creation_input_tokens`).
- **Usage → Cost**: cost per day, key, person, model or provider, the local and cloud parts, the share
  served locally, and the requests without a price with a link to set prices. `GET /api/usage` returns
  `cost_micros`, `unpriced`, `cost_reported` and `cost_estimated` per row and a local/cloud `split` in
  the totals; `group=provider` is new.
- **Prices per provider model** (admins): input, output, cached input and cache write in USD per
  million tokens, shown with their source and date. **Refresh prices from OpenRouter** (a provider, or
  every provider) reads OpenRouter's public model list without a key through the egress rules, matches
  models (an OpenRouter provider's by id; others by a suggested name that the admin confirms, or an id
  they type), shows old → new, and applies only the rows the admin ticks, only if OpenRouter still
  lists those prices; prices entered by hand are kept unless ticked. No background sync, no bundled
  price list. `POST /api/prices/openrouter/preview` and `/apply`; setting `OPENROUTER_MODELS_URL`.
- **Monthly budgets** (USD, UTC calendar month; part B2) per API key, person and provider, for requests
  to providers outside your network. Before a request is sent to one, its estimated cost (prompt size at
  about 3 bytes a token plus `max_tokens`, or `BUDGET_DEFAULT_MAX_TOKENS` without it, at the model's
  prices) is reserved against every budget that applies, in an in-memory ledger under a lock (seeded
  once a month from the usage rows), so concurrent requests cannot overspend; it is settled with the
  real cost when the request finishes and released when it fails. A budget that cannot cover a request
  makes the route local-only for that scope: a busy local tier is waited for, another provider in the
  tier serves, and `X-Forgeyard-Budget-Exhausted` (`key`, `person`, `provider:<name>`) says so; when
  nothing else can serve, **402** `budget_exhausted` (type `insufficient_quota`) names the budget, its
  spend and when it resets. Under a budget, a model without a price is blocked (402 `budget_unpriced`).
  Alerts at 80% and 100%: one audit event (`budget.warning`, `budget.exhausted`) per budget, month and
  threshold. Key owners set their key's budget unless an admin locked it; admins set people's (**Usage
  → Cost**) and providers' (the provider's page). Meters and warnings on **API keys**, **Usage → Cost**
  and the provider pages. `GET /api/budgets`, `PUT /api/budgets/people/{id}`,
  `PUT /api/budgets/providers/{id}`; `budget_micros` on `/api/keys`.
- Guide page **Cloud providers** (en, zh-TW): each provider, what each endpoint cannot do, the spill
  rules, the key setting, egress, troubleshooting. Settings `GATEWAY_CLOUD_DEFAULT`,
  `GATEWAY_CLOUD_MAX`, `EGRESS_*` and `PROVIDER_*`.
- **Machines → Add a machine → Docker is a form** that writes the complete `docker run`: the model
  directory on that machine (both sides of the `-v` and `FORGEYARD_MODEL_CACHE`), an optional machine
  name (`FORGEYARD_NAME`), the server address as that machine reaches it (a VPN, a forwarded port, another
  LAN address; a warning when its host is not in the server's certificate, with `TLS_EXTRA_NAMES`), and
  under *Advanced* a port range (`FORGEYARD_PORT_RANGE`). The command follows the form as you type, a
  value the agent would reject shows its error instead of a broken command, and the values are
  remembered in the browser. No more hand-editing the command.

### Changed
- Lowering *The most people may give their own keys* applies at once to people's own keys set higher:
  they work at the new maximum on every request (the API keys table shows it), and get their own
  setting back when it is raised. An admin's value and admins' own keys are not capped.
- Migration `0009_providers`: every route becomes one tier-0 target (its deployment, the same upstream
  model name), so existing routes behave exactly as before; `api_keys.cloud` is `off` for every
  existing key; usage rows get a target. The downgrade keeps deployments' routes and drops providers.
- `GET /api/routes` returns each route's targets, tiers and flags, and whether the signed-in person may
  use it; route management moved to `/api/routes/*` and `/api/route-options`.
- `python -m forgeyard secrets reset` also drops provider keys it cannot read.
- Migration `0011_budgets`: `api_keys.budget_micros` and `budget_locked_by`, `users.budget_micros`,
  `providers.budget_micros` (NULL, no budget, on every existing row) and the table `budget_alerts`.
  Gateway errors may carry extra headers and fields (402 is typed `insufficient_quota`).
- Migration `0010_cost`: `usage_daily` gets `cost_micros`, `unpriced`, `cost_reported` and
  `cost_estimated` (0 on existing rows), `provider_models` gets `price_ref` (the confirmed OpenRouter
  id). Clearing every price of a model clears its price source.
- Anthropic's native usage names (`input_tokens`, which leave the cache out) now count cache reads and
  writes into the prompt, like the OpenAI shape.
- The Docker commands on **Add a machine** and in the guide start with `sudo` (said once: leave it out
  as root or in the `docker` group), and the AMD command puts each `--group-add` on its own line.

### Fixed
- `/v1/models` lists a model only when a call from that key could be served: a `fallback` key reaches
  the cloud only after a local tier, so a route whose cloud tier comes first and whose deployment is
  stopped is no longer listed for it.
- An engine's or provider's own `Retry-After` on a 429 or 503 reaches the client with the answer.
- A cloud request a budget refused before anything was sent is counted under that provider (at no
  cost), not as local.
- A budget can no longer be set on a provider inside your network: requests to it cost nothing and
  it was never enforced (422 `not_cloud`).
- Stale text: the Users and Audit pages, the session limits on *My account* (now the server's own
  settings) and the sign-out messages; the audit log can be filtered by API keys, machines,
  workloads, deployments, models, recipes, routes, providers and budget alerts.
- Every page names itself in the browser tab ("API keys · Forgeyard"). After the guide, the tab used to
  keep the guide page's title on every page that followed.
- Gemini's thinking tokens are counted and priced. Its OpenAI-compatible endpoint shows them only in
  `total_tokens`, so the gateway counted a `gemini-2.5-flash` answer at less than half of what Google
  bills (found by a real request); the difference now counts as reasoning, priced as output.
- `/v1/models` no longer lists a model whose deployment was stopped, so clients stop offering a model
  that can only answer 503. It stays listed while its replicas are starting or restarting, and for keys
  that may use the cloud when its route has a cloud tier to spill to (or a provider inside your
  network). `GET /v1/models/{name}` answers 404 "is stopped" for it; calling it still answers 503 with
  the reason.


## [1.1.1] — 2026-11-11

AMD on real hardware. Fixed against a real Strix Halo machine (Radeon 8060S, gfx1151 APU, ROCm 7.0.2, amd-smi 26.0.2), where the agent image now reads the card fully through the amdsmi library.

First run on a real AMD machine (Ryzen AI MAX+ 395 / Radeon 8060S, gfx1151, ROCm 7.0.2, amd-smi
26.0.2, agent image 1.1.0); its tool output is now a test fixture (`tests/fixtures/strix-halo-gfx1151-amdsmi26`).

### Fixed
- **amd-smi 26 crashed the AMD collector** (`'str' object has no attribute 'get'`), so the agent fell
  back to `rocm-smi`. amd-smi prints `"N/A"` where a value or a whole section (`"usage": "N/A"`) is
  not supported; every section is now read defensively, in both the `{"gpu_data": [...]}` and the
  bare-list shapes. What could not be read is listed in the card's `detail.missing` instead.
- **The card's name was `N/A`.** A specific market or board name is used first; an APU whose
  names are generic ("AMD Radeon Graphics") takes the GPU from the CPU's brand string
  ("AMD RYZEN AI MAX+ 395 w/ Radeon 8060S" → "AMD Radeon 8060S"). A discrete card never does.
- **The key was `amd:0x0`.** amdsmi's UUID is the device id plus the card's serial number, and an
  APU has no serial, so `00ff1586-0000-1000-8000-000000000000` and rocm-smi's `0x0` are the same on
  every Strix Halo. A card is now keyed by its UUID only when it has a serial, else by its PCI address,
  and the key is the same whichever tool read it (a rocm-smi reading rebuilds amdsmi's UUID). The keys
  an older agent used go along in `detail.aliases`; the server then moves the card's row, its GPU
  allowlist entry and the claims of replicas running on it to the new key (audited as
  `machine.gpu_keys_changed`). It does so only for a key that disappeared, of the same vendor,
  claimed by exactly one card; anything ambiguous fails closed as before.
- **The driver showed the kernel version** (`7.0.0-38-generic`; amd-smi's is a whole `uname -v`).
  AMD cards report the ROCm version (from `<rocm>/.info/version`, else amdsmi, else
  `amd-smi version`); the Machines page and `forgeyard agent inventory` label it "ROCm" /
  `rocm=`. The kernel driver's string is kept in `detail.amdgpu_driver`.
- **The `amdsmi` library was never used in the agent image**, although the mounted ROCm ships it in
  `/opt/rocm/share/amd_smi`. When `import amdsmi` fails, the agent loads that copy (as `amd-smi`
  itself does) with `ROCM_PATH` pointing at that ROCm; any failure (missing or mismatched
  `libamd_smi.so`, a Python it does not support) is a reason and the CLI is used. What the library
  prints, from Python or from its C++ code on file descriptor 1, goes to stderr (`inventory --json`
  stays valid JSON).
- **The agent image lacked libdrm**, which `libamd_smi.so` loads at run time (`libdrm.so.2`, and the
  unversioned `libdrm_amdgpu.so` for the marketing name): amdsmi from the mounted ROCm then read the
  memory but no ASIC information ("Fail to open libdrm.so.2"). The image installs `libdrm2` and
  `libdrm-amdgpu1` with that link; the smoke test loads both.
- **A partial answer no longer wins.** That half-working amdsmi was taken as the answer: no name, no
  `gfx` target, no unified-memory flag. Now, while a card lacks its PCI address, gfx target, memory
  size or a name, the next sources fill the gaps field by field (amd-smi, rocm-smi, then the KFD
  topology, which alone gives the address and gfx target of a single GPU); the collector's source
  names every one that contributed (`via amdsmi+kfd`), what stays unknown is said, and the live
  metrics keep the merged keys.
- **The AMD `docker run` failed with `Unable to find group render`**: Docker looks group names up in
  the image. The Machines page, the guide and the image smoke test use the host's numeric ids,
  `--group-add "$(getent group video | cut -d: -f3)" --group-add "$(getent group render | cut -d: -f3)"`.
- **`docker run … forgeyard-agent:<version> agent inventory` failed**: the image's command already is
  `forgeyard agent`. The guide shows `… forgeyard-agent:<version> inventory`, and a redundant leading
  `agent` is now accepted.

## [1.1.0] — 2026-11-11

Easier to start and safe on the network: a first-run checklist and recommended models, an agent key guard in front of every model replica, HTTPS from Forgeyard's own CA, secrets encrypted at rest, a one-line agent install, an agent container image and a user guide in English and Traditional Chinese. Accepted on the real 8×A100 host (one shared card) plus a CPU machine.

### Added
- **Key guard in front of every model replica.** vLLM 0.32's `VLLM_API_KEY` only covers `/v1`, `/v2`,
  `/inference` and `/cohere`; `/invocations`, `/rerank`, `/pooling`, `/score`, `/classify` and
  `/tokenize` answered anyone who could reach a replica's port. Now the engine listens on loopback
  only (a process binds `127.0.0.1`, a container is published on `127.0.0.1:<internal port>`), and the
  agent listens on the replica's published port with a small streaming reverse proxy that requires
  `Authorization: Bearer <internal key>` on every path and method (401 `replica_key_required`
  otherwise; constant-time comparison). SSE, chunked and multipart bodies stream both ways; a client
  disconnect closes the engine's connection; hop-by-hop headers are dropped; an engine that is down
  gets the connection closed unanswered, so the gateway fails over as before. Health checks go to the
  engine's loopback port; nothing is open on the guard. The same mechanism for every recipe (the
  engines' own key variables stay as defence in depth; a recipe without `api_key_env` is guarded too).
  An adopted replica's guard opens again once the server has sent its key; while the agent is down the
  replica is unreachable from outside. Replicas an older agent started are restarted behind a guard;
  a process engine found listening beyond loopback is stopped and failed. Agent feature
  `replica_guard`; with it the server gives every model replica a key (`env` null when the engine
  reads none). The deployment page shows "key guard" per replica. `h11` is listed as a dependency (it
  was installed already).
- **Limits an admin sets on someone else's key are locked**: the owner can no longer change that
  value (403 `limit_locked`), can still revoke the key and set the other limit; the API keys page shows
  a lock with the admin's name, disables the field for the owner and offers admins *Unlock* (clearing
  the value, back to the default). Locking and unlocking are audited (`api_key.changed` with `locked` /
  `unlocked`). Migration `0006_key_locks`.
- `scripts/accept_m4.py`: the vLLM paths (`/invocations`, `/rerank`, `/pooling`) on every replica
  port are PASS checks expecting the guard's 401 (they were INFO lines), and a client's API key on a
  replica port must get 401 too; model allowlists are checked for real (a key limited to one deployment
  lists and reaches only that one, 403 `model_not_allowed` for the others; a key with no list made
  before the deployments sees and reaches all of them).
- **Get started** checklist, the home page after sign-in until it is done or hidden, and always
  under the account menu: six steps for admins and operators (add a machine, choose its GPUs, add a
  model, deploy it, create an API key, call `/v1`), three for users (create a key, call `/v1`, see
  usage). Each step's state comes from real state: a machine online; every machine with GPUs has an
  allowlist or a saved "all of them" (read from the audit log); a catalog entry; a deployment with a
  ready replica; an active key of the person's own; a request with one of their keys through the
  gateway; for users, having opened Usage. Steps only admins can do say so to operators. A Python
  and curl example for this server's `/v1` uses the ready deployment (chat first) and the key from
  `FORGEYARD_API_KEY`. `GET/PUT /api/onboarding`, `POST /api/onboarding/seen`; hiding is stored per
  person in the new `users.preferences` (migration `0007_preferences`).
- **Recommended models** on Add a model, from `forgeyard/models/recommended.yaml` (27 entries in four
  groups: Qwen3, other open chat models, speech to text, embeddings and rerank; Gemma 4 E2B, E4B,
  12B, 26B-A4B and 31B need no token, and vLLM 0.32.0 has both of their architectures), checked at load
  (catalog names, `hf://` sources, groups, a built-in recipe that serves the task and format, a GGUF
  file for GGUF entries only, a CPU variant for CPU entries, engine arguments) and by
  `scripts/check_recommended.py` against Hugging Face. Gated entries (Gemma 3, Llama) are marked as
  needing a token and the licence. `GET /api/models/recommended` (admins).
- **Hugging Face search** through the server: `GET /api/models/search?q=&task=&format=&limit=`
  (admins), one request per Hugging Face pipeline tag of the task (embeddings: feature-extraction
  and sentence-similarity; rerank: text-ranking and text-classification), over the guarded fetcher to
  the configured `HF_ENDPOINT` only, 10 s timeout, results trimmed (no chat templates), merged and
  sorted by downloads (for rerank, names containing "rerank" first), cached 5 minutes, 30 searches per minute per person; 503 `hub_unreachable`
  when Hugging Face cannot be reached. A result on the recommended list is added the way the list
  says.
- **Inspecting a model suggests how to run it**: `POST /api/models/inspect` also returns the recipe
  (the one asked for if it fits, else the first that serves the model), the device (GPU when an
  online machine has an allowed GPU, else the CPU when the recipe has a CPU variant), the estimate
  with the recipe's defaults, and the scheduler's verdict per machine, so the page shows "fits on …"
  before the model is added. The deploy page takes `?recipe=` and `?device=` from "Deploy it".

- **HTTPS out of the box, from Forgeyard's own certificate authority.** On the first start the server
  makes a CA (EC P-256, 10 years, key 0600 in `data/tls`) and a server certificate signed by it
  (397 days), naming `PUBLIC_URL`'s host, `ALLOWED_HOSTS`, `TLS_EXTRA_NAMES`, the host name, the
  machine's own addresses and loopback. It is renewed 60 days before expiry, or when a name is
  missing; a running server checks twice a day and switches certificates without a restart. Your own
  certificate: `TLS_CERT_FILE` / `TLS_KEY_FILE` (reloaded when the files change), with `TLS_CA_FILE`
  for the CA agents pin. A missing CA key next to existing certificates stops the start instead of
  making a new CA. See [docs/https-and-trust.md](docs/https-and-trust.md).
- **Agents pin the CA.** The join command carries `--ca-fingerprint sha256:…`. The agent fetches the
  CA, refuses it unless the fingerprint matches, joins and connects over `https://` / `wss://`
  verified against that CA only (not the system store), and keeps it as `server-ca.pem` in its state
  directory. `--ca-file` pins a CA file instead. **`forgeyard agent trust --server https://…
  --ca-fingerprint …`** switches an agent that joined over plain HTTP, keeping its credential. An old
  agent that cannot connect logs that command.
- **Trust this server** (`/trust`, public): why the browser warns, in plain words; the CA's
  fingerprint and expiry; downloads as PEM and DER; steps for Windows, macOS, Linux (Chrome and the
  system store), Firefox, iOS and Android, with the device's own steps opened first; curl and Python.
  Admins also see the server certificate's names and expiry, and the trust command. Linked from the
  sign-in page, the account menu, and the Machines page when machines are offline.
  `GET /api/tls`, `/api/tls/ca.pem` and `/api/tls/ca.crt`.
- `/v1` examples on the API keys page and in *Get started* trust the CA when the server uses its
  own: `curl --cacert forgeyard-ca.pem`, and the OpenAI SDK with
  `http_client=httpx.Client(verify=ssl.create_default_context(cafile="forgeyard-ca.pem"))`.
- **Secrets encrypted at rest**: Hugging Face tokens, model replicas' internal keys and workload
  environment values are stored with AES-256-GCM, each bound to its column, under a key that is not
  in the database: `data/secret.key` (0600, made on the first start), `SECRET_KEY_FILE`, or
  `FORGEYARD_SECRET_KEY`. Migration `0008_encrypt_secrets` encrypts existing rows (SQLite and
  PostgreSQL); a plain-text value afterwards is an error. The database records the key's id, so a
  missing or wrong key stops the start with what to do. `python -m forgeyard secrets reset --yes`
  starts over after a lost key: Hugging Face tokens dropped, model replicas placed again with new
  keys, and lost environment values emptied, with an event on each workload.
- **Engine images pinned by digest**: every built-in recipe image is `repo:tag@sha256:…` (the
  multi-architecture index digest of the tag, checked for linux/amd64 and, where a variant claims
  aarch64, linux/arm64). The agent pulls and runs the digest; the UI shows the tag.
  `scripts/pin_images.py` checks the digests against the registries, and `--write` re-resolves
  them when an engine is bumped.

- **Agent config file**: `/etc/forgeyard/agent.toml` (or `--config PATH`, `FORGEYARD_AGENT_CONFIG`) holds
  every agent setting: server, CA pin, name, labels, state directory, and under `[workloads]` the
  workload account, work directory, port range, bind address, container engine, limits and log size,
  under `[models]` the cache, local directories and private hosts. Each setting also has a
  `FORGEYARD_*` variable; a flag wins over the environment, the environment over the file (labels
  merge key by key). A wrong file is refused with every problem named (unknown settings with the
  closest name, wrong types, bad values, relative paths, two pins). `forgeyard agent init` writes a
  commented file from what it detects (GPUs, Docker or Podman and NVIDIA support, systemd, running in
  a container, a workload account) and prints it; `agent config [--get KEY]` shows the settings in
  effect and where each came from; `agent join` joins with the file's settings and exits.
- **One-line install served by the server**: `GET /install.sh` (HTTPS only, public like the CA). The
  command on the Machines page first downloads the CA, checks its SHA-256 against the fingerprint
  with `sed`, `base64` and `sha256sum -c` (no openssl), and only then fetches the script over a
  connection verified against that CA. The script (POSIX sh, shellcheck-clean) checks the system,
  downloads the agent **from the server** (`GET /install/forgeyard-<version>-py3-none-any.whl`, built
  from the running code, so the agent always matches the server; its SHA-256 is in the script),
  installs it in `/opt/forgeyard/venv` with uv, else a Python 3.13 already there, else after
  installing uv, links `/usr/local/bin/forgeyard`, creates the `forgeyard-run` account, writes
  `agent.toml` with `agent init`, joins, and installs the `forgeyard-agent` systemd service
  (`KillMode=process` so workloads survive agent restarts, `Restart=always`, no restart after a refused
  credential). Without systemd it says how to run the agent. Run again it upgrades and keeps the
  settings and the credential; `--uninstall [--purge]` removes it.
- **Agent container image**: `Dockerfile.agent` (multi-stage, base images pinned by digest, the same
  wheel, nvidia-ml-py, the docker CLI, tini, the `forgeyard-run` account, rlimits). Configured by a
  mounted `agent.toml` or `FORGEYARD_*` variables; joins on the first start with
  `FORGEYARD_JOIN_TOKEN` and keeps the credential in a volume. CI builds it and runs
  `scripts/agent_image_smoke.sh`: its contents, inventory and init in simulation, a real join to a
  server on the runner, and a re-created container that stays the same machine.
- **Add a machine** offers three ways, each with the token and the pinned fingerprint: one-line
  install (recommended), Docker (NVIDIA, AMD or CPU only, with the build command) and manual (a config
  file and the commands).
- **User guide** in English and Traditional Chinese (`docs/guide`, `docs/guide/zh-TW`, the same
  structure): quick start, concepts, how-to, troubleshooting from real incidents, and a reference of
  every server and agent setting. Served in the app at `/guide/<lang>/<page>` (public, rendered on the
  server without raw HTML or images, works offline) with a language switch and copy buttons; "How do
  I…?" links on Machines, Models, API keys, Get started and Trust this server, and a Guide entry in
  the account menu and on the sign-in page. `GET /api/guide`, `/api/guide/{lang}/{page}`. New
  dependency: `markdown-it-py`.

### Changed
- **A joined agent started again with its join settings just starts**: a join token is ignored (and
  the log says so) when the machine already joined the same server, so a container's
  `--restart always` or a re-run install never fails; server and CA values from the file or the
  environment that differ from what the machine joined are reported and ignored (`agent trust`
  changes them); a different pin given as a flag is still refused.
- The README is short and points to the guide; the details it held moved to `docs/design.md`
  (and `docs/design.zh-TW.md`).
- **HTTPS is the default.** Plain HTTP only with `TLS=off`, meant for running behind a proxy that
  terminates TLS (nginx and Caddy examples in the docs); while pages still arrive over plain HTTP,
  every admin sees a warning at the top of each page, and the startup log warns too. Session cookies
  are `Secure` under HTTPS (also behind a trusted proxy that says `X-Forwarded-Proto: https`).
  `Strict-Transport-Security` is sent only with your own certificate: with Forgeyard's CA the
  certificate warning must stay clickable until a device trusts the CA.
- An `http://` `PUBLIC_URL` kept from 1.0 is read as `https://` while TLS is on, with a warning, so
  join commands, set-password links and the gateway address keep working.
- New dependency: `cryptography` (CA, certificates, AES-GCM).
- `scripts/accept_m3.py` and `accept_m4.py` default to `https://127.0.0.1:7860` and take `--cacert`;
  the SDK gets the CA through its `http_client`.
- `npm run dev` proxies the API to `https://127.0.0.1:7860`.
- Spreading a deployment's replicas over machines is documented as the default (unchanged).
- Every empty page says what it is for and offers the next action: Machines (Add a machine, with
  how joining works), Workloads (New workload, or serve a model instead), Models (add a model first
  when the catalog is empty, else deploy one), the catalog, the deploy page with an empty catalog,
  API keys (Create a key), Usage (create a key, open the Playground) and the Playground (deploy a
  model, or who to ask). The Models page no longer says replicas answer only on their own ports.
- The Machines page's button reads "Add a machine", like the checklist.

### Fixed
- Tests no longer leave process workloads running: FY-RG-008/009 and the other tests that start
  real processes as the workload user only removed their directory when a scenario failed half-way,
  so on a busy host two stand-in engines (one on 0.0.0.0) kept running as `nobody`. They now kill
  what still runs from their directory, and two fixtures catch any test that leaves a process
  behind (killed, and reported as that test's error) plus leftovers of earlier runs.

## [1.0.0] — 2026-11-10

The MVP: milestones M0 to M4, each accepted on a real 8×A100 host (one shared card) plus a CPU machine.

### Added — M4: unified API (the MVP)
- **Gateway** at `/v1` on the server's own port, OpenAI-compatible: `GET /v1/models` (and
  `/v1/models/{model}`), `POST /v1/chat/completions`, `/v1/completions` (SSE streaming passed through
  as it arrives), `/v1/embeddings`, `/v1/rerank`, `/v1/score`, `/v1/audio/transcriptions` and
  `/translations` (multipart, the model field rewritten, the audio untouched). Only API keys open it,
  never the console cookie; no CSRF rule there, the host check applies; no CORS. Errors in OpenAI's
  shape: 401, 403 (model not allowed), 404 (unknown model), 400 (wrong endpoint), 429 with
  `Retry-After`, 503 (no ready replica, with each replica's state), 502, 504.
- **Routing**: least requests in flight per ready replica, ties in turn; a replica that cannot be
  connected to is skipped before anything reaches the client and tried last until its agent reports
  it ready again (at most 15 s); a client that disconnects closes the upstream request.
  `GATEWAY_CONNECT_TIMEOUT` (5 s), `GATEWAY_READ_TIMEOUT` (600 s), `GATEWAY_MAX_BODY_MB` (32).
- **API keys**: `fyk_` + 256 random bits, shown once (with curl and Python `openai` examples), stored
  as SHA-256 with a 12-character prefix; optional expiry and model allowlist; rpm and tpm per key, with
  admin-set defaults and caps (`GATEWAY_DEFAULT_RPM`/`_TPM`, `GATEWAY_MAX_RPM`/`_TPM` as initial
  values); revoke, expiry and a disabled owner take effect on the next request. Everyone manages
  their own keys; admins list and revoke all keys and change any key's limits. Audit events
  `api_key.created`, `api_key.changed`, `api_key.revoked`, `api_key_limits.changed`.
- **Limits**: sliding 60-second windows per key in the server's memory; tokens from the answers'
  `usage` (for streamed chats the gateway sets `stream_options.include_usage` and strips that chunk
  unless the client asked for it); `max_tokens` held while a request runs.
- **Usage statistics** per UTC day, source (API key or console), key, user and model: requests,
  errors, limit refusals, prompt and completion tokens, latency histogram (p50/p95); written every
  5 s (`USAGE_FLUSH_SECONDS`). `GET /api/usage` (own; admins everyone's), per deployment on its page.
- **Internal replica keys**: every model replica gets a random key per placement; the agent passes it
  to the engine through the environment only (`api_key_env` in recipes: `VLLM_API_KEY` for vLLM,
  `LLAMA_API_KEY` for llama.cpp, `API_KEY` for speaches), keeps it in memory only, and sends it with
  health checks (speaches guards `/health`); the gateway and *Try it* send it upstream. Never in the
  API, specs, events or logs. Agent feature `replica_keys` (protocol v1, compatible).
- **Playground** in the console (`/api/playground/…`, session + CSRF): chat with streaming,
  embeddings, rerank, speech to text; no API key is created; counted as "console".
- Pages **API keys**, **Usage**, **Playground**; a deployment's page shows its `/v1` model name and
  endpoint, its usage and each replica's requests and whether its engine requires the internal key.
- `scripts/accept_m4.py` (makes its own venv with the official `openai` SDK when needed): the
  real-hardware run.
- Migration `0005_gateway`. `httpx` is a runtime dependency; `openai` a dev dependency (tests).

### Changed — M4
- The request guard is a pure ASGI middleware (streamed answers pass straight through).
- Replicas of one deployment are spread over machines before binpacking.
- *Try it* sends the replica's internal key.

### Fixed — M4
- A process replica whose wrapper died left its engine running and holding the port, so every restart
  failed. Leftovers of the process group were only recognised by a marker in `/proc/<pid>/environ`,
  which a root agent in a container without `CAP_SYS_PTRACE` cannot read for another user's
  processes; the group itself is used then (unless its id was reused).

### Added — M3: model deployment
- **Catalog** of models (admins): `hf://<org>/<name>` pinned to the commit a revision resolves to, a
  GGUF file from an admin-allowed host, or `local://` under admin-allowed directories. Models are
  inspected without downloading the weights: file list and sizes, `config.json`, parameter counts
  from the safetensors headers, the GGUF metadata and exact tensor sizes by HTTP range reads. Tokens
  for gated repositories are used for the inspection only. Per-recipe engine arguments per model.
- **Recipes** as YAML (built-in and admin-added): tasks, formats, protocol per task, parameters,
  estimate method, health check with a start period, and variants per vendor / compute capability /
  gfx target / driver, each a pinned image or a pinned, SHA-256-checked release for a process.
  Built in: `vllm` (vLLM 0.32.0), `vllm-pooling` (embeddings and rerank on the same image),
  `llama-cpp` (b11977: CUDA, ROCm and CPU images, and a CPU process variant) and `faster-whisper`
  (speaches 0.8.3).
- **Estimates** before deploying: weights from parameters x bytes (FP8 aware) or GGUF tensor sizes,
  KV cache from layers x KV heads x head size x tokens, logits buffer, recipe overhead, CUDA context.
  A declared `vram` overrides it.
- **Scheduler** for model deployments: filter (variant for the hardware and driver, runtime,
  allowlist, memory), score (binpack, then a machine with the model already cached). Memory is the
  card's free memory as reported, minus reservations not yet visible there, minus a headroom
  (`VRAM_HEADROOM_MIB`, 512). Refused with every machine's reason and shortfall when nothing fits.
- **GPU sharing** (`share: vram`) enforced by reservation; vLLM's `--gpu-memory-utilization`
  computed from the reservation and the card's total. Replicas on one card start one at a time.
  Workloads can declare `gpus.vram` and `gpus.share` too; M2 workloads keep their behaviour.
- **Deployments**: a service workload (runtime `model`, rendered per replica for its machine and
  variant) plus a **gateway route** (served name, protocol, upstream model name) for M4;
  `GET /api/routes`. Each replica is reachable on its own port meanwhile; **Try it** sends one small
  request in the route's protocol (a bundled public-domain clip for speech).
- **Agent model cache** (`--model-cache DIR`): Hugging Face cache layout keyed by source and
  revision, shared by replicas, resumable downloads verified by SHA-256 or git blob id, progress
  reported, cancelled when unwanted, read-only files, the model's directory mounted read-only into
  containers (the only volume ever mounted); engines for process variants unpacked safely. Tokens
  stay in the agent's memory. `--model-local-dir`, `--allow-private-model-hosts`.
- **SSRF guard** for every model request on server and agent: https, every resolved address public
  (no private, loopback, link-local/metadata, CGNAT, reserved), connections pinned to the checked
  address, every redirect re-checked, credentials dropped when a redirect leaves the host.
- Models pages: deployments (download progress, endpoints), catalog and recipes (add a model with an
  inspection preview, custom recipes, model sources), deploy form with a live estimate and placement
  preview, deployment page (estimated vs live memory, endpoint, try it, output, events).
- API `/api/models` (+ `/inspect`), `/api/recipes`, `/api/model-sources`, `/api/deployments`
  (+ `/estimate`, `/stop`, `/start`, `/try`), `/api/routes`. Audit events `model.added`,
  `model.changed`, `model.removed`, `recipe.created`, `recipe.updated`, `recipe.deleted`,
  `model_sources.changed`, `deployment.created`, `deployment.stopped`, `deployment.started`,
  `deployment.deleted`.
- Agent protocol v1 extended compatibly: feature `models`; desired replicas carry a rendered spec,
  the model (with a token when needed) and an engine; status carries download progress and the model
  cache. M2 agents get specs without the new fields.
- `scripts/accept_m3.py`: the real-hardware run.
- Migration `0004_models`.

### Changed — M3
- The `/deployments` path now opens Models.
- Health checks have `start_period`: failures before the first success do not count during it.
- The agent's reconciler only holds new starts back while replicas are being stopped (a slow start,
  such as an image pull or a download, no longer delays other replicas).
- A container whose image is not on the machine yet still starts when the docker CLI's credential
  helper (`credsStore` / `credHelpers`) fails under the agent. That failure stopped even public
  images, such as the llama.cpp one on the first real run. The agent now pulls once more with a
  copy of the CLI config that leaves out the helpers and keeps everything else, then starts the
  container. Other start failures are reported as before.

### Added — M2: generic workloads
- Workloads: **services** (run until stopped, health-checked, restarted with backoff) and **tasks**
  (run to the end; `retries` with doubling backoff; `max_duration` stops and fails a run), 1-16
  fixed replicas, as a **container** (Docker or Podman CLI) or a **process**. A small explicit spec:
  name, kind, runtime, env, resources (cpu, memory, gpus by count or by index, vendor), ports, health,
  restart, max_duration, retries, replicas, machine.
- Placement: the chosen machine, or the first online machine with the runtime, free allowed GPUs and
  the declared CPU and memory. GPUs are exclusive. What does not fit waits as *pending* with the
  reason and is placed when room appears (also by a background sweep).
- Per-machine **GPU allowlist** (`PUT /api/machines/{id}/gpu-allowlist`, admins; Machines page →
  Change GPU access). Cards outside it are never assigned and are marked *not for Forgeyard*; a card
  in use cannot be taken off; audit event `machine.gpu_allowlist_changed` with before and after.
- Isolation: workloads start from an empty environment (never the agent's); GPU visibility by UUID
  (`CUDA_VISIBLE_DEVICES`/`NVIDIA_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` + `HIP_VISIBLE_DEVICES`)
  for processes, `--gpus "device=<uuid>"` / CDI / `--device /dev/kfd` + the assigned render node for
  containers; empty lists and `NVIDIA_VISIBLE_DEVICES=void` without GPUs. Containers run with
  `--security-opt no-new-privileges`, never `--privileged`, ports published on one address.
- A root agent runs process workloads only as `--workload-user` (refused without one). Processes get
  their own process group, a systemd scope with CPU/memory limits on systemd hosts, rlimits otherwise.
- Desired-state reconciliation: the server sends each agent the full list of its replicas (spec and
  generation) on connect and on change; the agent converges, adopts what it started after a restart
  (pid + start time + boot id; container labels), never starts anything twice, stops before it
  starts, and reports per-replica status (pending, starting, running, healthy, unhealthy, backoff,
  exited, failed, stopped).
- Output: kept per replica on the agent (`--log-max-mb`, 10 MB), live tail on the workload page
  (server asks the agent over the WebSocket), last 100 lines kept on the server when a replica ends.
- Agent protocol v1 extended compatibly: `features` in hello/welcome, and `desired`, `status`,
  `logs_request`, `logs`. M1 agents keep working without work; M2 agents on an M1 server only report
  hardware.
- Agent options `--workload-user`, `--work-dir`, `--port-range`, `--bind-address`,
  `--container-engine`, `--limits`, `--log-max-mb`, `--no-workloads`; `forgeyard agent runtimes`.
- Workloads page (list with replicas, machine, GPUs, addresses), New workload form (with the spec it
  sends), and a workload page with replicas, events, live output, stop / start / delete.
- API `/api/workloads` (list, create, get, stop, start, delete, replica logs). Admins and operators
  run workloads; users see them without environment values or output. Audit events
  `workload.created`, `workload.stopped`, `workload.started`, `workload.deleted`.
- `scripts/gpu_probe.py`: which NVIDIA GPU a workload sees, through the CUDA driver only.
- Migration `0003_workloads`.

### Changed — M2
- The *Deployments* placeholder became **Workloads** (`/deployments` redirects); model deployments
  come with M3.
- The AMD collector records the KFD `unique_id` (as `rocr_uuid`) and `drm_render_minor` per card.
- Revoking or removing a machine sends its replicas back to pending (placed elsewhere, or waiting).

### Added — M1: machines join
- Agent in the same package: `python -m forgeyard agent` (also `python main.py agent`). It joins
  with a one-time token, keeps a WebSocket to the server and reconnects by itself (exponential
  backoff with jitter, at most 15 s).
- Join tokens: created by admins on the Machines page, single use, valid 15 minutes, shown once
  and stored as a SHA-256 hash, with the exact command to run. Unused tokens can be cancelled.
- Per-machine credential issued at join, stored hashed on the server and in a 0600 file on the
  machine. Revoking or removing a machine kills the credential and closes its connection at once.
- Agent protocol v1 over `/agent/v1/stream` (JSON `hello`, `heartbeat`, `inventory`, `welcome`,
  `error`, with application close codes), and `POST /agent/v1/join`. Agents authenticate with a
  Bearer credential only. A browser cookie never counts, a request with an `Origin` header is
  refused, and the host check (421) now covers WebSocket handshakes too.
- Heartbeat every 10 s. A server-side sweep marks a machine offline after 30 s of silence and
  closes the dead connection; machines start offline after a server restart.
- Inventory collectors: NVIDIA via NVML (`--extra nvidia`), AMD via the `amdsmi` library, the
  `amd-smi` CLI or `rocm-smi` (gfx target as `arch`; unified-memory APUs such as gfx1151 detected
  from the gfx target or the KFD topology), and CPU and RAM from `/proc`. All read-only, all fail
  soft.
- `python -m forgeyard agent inventory [--json] [--raw]` shows what the agent detects without
  joining. `--simulate-inventory` (with `FORGEYARD_AGENT_SIMULATION=1`) replays a fixture. Fixtures
  ship for 8 × A100 80GB, 2 × RTX 4090 and a Strix Halo APU, and the server marks such machines
  as simulated.
- Machines page: fleet totals, and for each machine its status, last heartbeat, agent version,
  labels, and every card's vendor, model, arch, free / total memory, unified-memory badge,
  driver, utilisation and temperature. It refreshes every 5 s. Admins add, revoke and remove
  machines; everyone signed in can look.
- Audit events `machine.join_token_created`, `machine.join_token_cancelled`, `machine.joined`,
  `machine.join_refused`, `machine.revoked`, `machine.removed`.
- Settings `AGENT_HEARTBEAT_SECONDS` (10) and `AGENT_OFFLINE_SECONDS` (30).

### Added — M0: skeleton and sign-in
- FastAPI server with SQLAlchemy 2 and Alembic (migrations run at startup); SQLite by default,
  PostgreSQL through `DATABASE_URL`.
- React + TypeScript + Vite web UI served by the server: setup, sign-in, request an account,
  set password, Machines and Deployments (empty until M1/M2), Users, My account and sessions,
  Audit log.
- First run: a one-time setup token printed in the log and kept in a 0600 secrets file, or an
  owner from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- Accounts with roles admin, operator and user; a protected owner; argon2id passwords with a
  minimum length and a common-password check; admin-created accounts set their own password
  through a one-time link; optional self-registration with admin approval.
- Server-side sessions: sign-out, password change, disabling an account and admin sign-out end
  them immediately; 2 h idle and 7-day absolute limits; per-session sign-out.
- Host check (421), CSRF protection on API writes, per-IP request budget and sign-in backoff that
  never locks an account for good and never signs anyone out.
- Audit log of sign-ins and account changes.
- CI: ruff and pytest on SQLite and PostgreSQL 17, frontend lint, typecheck, tests and build.
