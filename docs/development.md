# Development

[繁體中文](development.zh-TW.md)

How to build and test Forgeyard, and where things are in the code. To report a bug or send a change,
start with [CONTRIBUTING.md](../CONTRIBUTING.md); to set up and use Forgeyard, see the
[guide](guide/README.md).

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22.12+ for the UI, and Docker only to
build the container images.

## Build and test

```bash
uv sync --extra server
uv run --no-sync python -m pytest -q          # backend tests (SQLite); a test that hangs fails after 5 min
                                               # with every thread's stack (pytest-timeout)
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # same suite on PostgreSQL — WIPES that database
cd web
npm run dev                                   # UI on :5173, API proxied to https://127.0.0.1:7860
npm run lint && npm run typecheck && npm test && npm run build
cd ..
shellcheck -s sh forgeyard/install/install.sh  # the install script stays POSIX sh
docker build -f Dockerfile.agent -t forgeyard-agent:dev . && scripts/agent_image_smoke.sh forgeyard-agent:dev
docker build -f Dockerfile.server -t forgeyard:dev . && scripts/server_image_smoke.sh forgeyard:dev
```

Schema changes go through Alembic (`forgeyard/migrations`); startup runs `upgrade head`, and a
test fails when the models and migrations drift apart. Test IDs (`FY-…`) are unique across the
project.

After `uv lock` (or a Dependabot update of `uv.lock`), run `uv run --no-sync python -m
forgeyard.install.requirements`: it rewrites the lists of the agent's dependencies, with their hashes, that
install.sh installs from (`forgeyard/install/agent-requirements*.txt`), and FY-INST-020 fails while they
differ from the lock.

The **Dependency audit** workflow (`.github/workflows/dependency-audit.yml`) runs on every pull request and push
to dev and main: pip-audit on uv.lock's runtime set (every extra, no dev group) and `npm audit --omit=dev` for the
web UI. It is not a required check: a new advisory against a package nobody touched must not block other work, but
a red result means a dependency to update in its own pull request. The same locally:

```bash
uv export --locked --no-dev --all-extras --format requirements-txt -o /tmp/requirements-audit.txt
uvx --from pip-audit==2.10.1 pip-audit -r /tmp/requirements-audit.txt --disable-pip
cd web && npm audit --omit=dev
```

## Pinned engine images

Every container variant of a built-in recipe names its image as `repo:tag@sha256:<index digest>`, and
`forgeyard/recipes/images.lock.json` records what a correct copy of it can show (per platform: manifest,
config and content ids). That is how an agent checks a `docker load`-ed copy, and how the server relays the
image to a machine without registry access. `scripts/pin_images.py` checks both against the registries
(exit 1 on any difference); `--write` resolves the tags again and rewrites the recipes and the lock.

- **Bumping an engine:** change the tag in its recipe, run `pin_images.py --write`, review the diff, run the
  tests.
- **Forgeyard's own vision image** (`images/vision`, recipes `vision` and, 1.5.4, `transformers`) is pinned the
  same way (1.4.1). A release publishes `forgeyard-vision:X.Y.Z` and `X.Y.Z-cuda` from that release's code, so its
  digest is only known after the tag: **after a release that changed `images/vision`**, a small PR into `dev`
  changes the two tags in `forgeyard/recipes/vision.yaml` **and** `forgeyard/recipes/transformers.yaml` (the same
  image) to the new version and runs `pin_images.py --write` (FY-VIS-001, FY-VIS-019 and FY-TFM-001 name the pinned
  references; FY-TFM-004 expects the old image's refusal until the pin reaches 1.5.4). A release that did not change it keeps the pin:
  a server upgrade alone never changes the image a vision model runs.

## Extensions

Since 1.6.1 (#190) the server and the console have a small extension interface. It serves a separately distributed
package (the enterprise edition's), which adds pages, API routes, tables and checks without a fork. **It is internal,
not a public plugin API**: it is versioned to the exact release, and a hook may change in any release. An extension
names the one Forgeyard version it was built for, and the server refuses to start with any other.

- **Server** (`forgeyard/extensions.py`, which lists every hook and where it is called): at startup each entry point
  in the group `forgeyard.extensions` is loaded; its object has `name`, `version`, `requires` (the exact Forgeyard
  version), optionally `edition`, and `register(reg)`. The hooks add API routers (after the community's), an Alembic
  branch of migrations (startup runs `upgrade heads`), a machine filter and a check before placement, a check of each
  gateway request (from memory, before the rate limits), an observer of the usage the recorder writes, Setup health
  findings, the features and audit prefixes `GET /api/info` lists, and the engine-switch policy. Without an extension
  every hook gives the community default, `GET /api/info` says `"edition": "community"` and nothing else changes. A
  hook that raises is logged and allows: an extension's bug never stops models from serving.
- **Console** (`web/src/ext/index.ts`): empty lists of pages, menu links, Usage tabs and Setup health cards, which
  the community pages render. An extension's build replaces that file; each entry names a feature and shows only
  while `GET /api/info` lists it.
- **Tests**: `tests/fixtures/ext_sample` is a sample extension that uses every hook, loaded from a real entry point
  object by `tests/test_extensions.py` (FY-EXT-*): a route, a migration, a refused placement and a refused gateway
  request, a finding. Keep it working when a hook's call site moves: it is how public CI notices a broken hook.
  `web/src/pages/Extensions.test.tsx` fills the console's slots the same way.
- **What a placement check is asked** (1.6.2): `fn(db, ctx, ask)`. `ctx.team_id` is the deployment's team: the one
  a deploy or an estimate names, and in an edit's preview the new team when the edit moves it. `ask.adding` is how many
  replicas the placement adds to what the deployment holds now: every replica for a deploy or an estimate, the
  replicas beyond the ones it releases for an edit's preview (`ask.release`, their ids), 1 for a replica started
  (deploy, start, wake, scale-up), and 0 for a replica placed again (`ask.replacing`: an edit's rolling restart, an
  offline move, a failed download, no free port, a removed machine). A quota that refuses only when `ask.adding` is
  above 0 never stops a running model. The table is in `forgeyard/extensions.py`; FY-EXT-012 to FY-EXT-018 check it
  at every call site.
- **GPU hours** are read, not observed: an extension that enforces GPU-hour quotas calls
  `forgeyard.workloads.gpu_hours.month_total(db, team_id=…)` from its placement check (this UTC month's `gpu_ms`, from
  the database, at most 5 seconds behind). The usage observer gets request rows only.
- Community code never checks a licence; it only asks the registry.

## Before a release

The release steps are in [CONTRIBUTING.md](../CONTRIBUTING.md#releasing). One of them guards the image
publish: `.github/workflows/dockerhub-login.yml` (*Docker Hub login*) runs on every push to `dev` and `main`,
logs in to Docker Hub with the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`, checks with
`scripts/dockerhub_check.py` that the token may push to `forgeyard`, `forgeyard-agent` and `forgeyard-vision`
(Docker Hub's token service answers with the actions it grants; nothing is pushed), and logs out. Without
the secrets it warns and stays green. **Before tagging, its latest run on `main` must be green without a
warning** (`gh run list --workflow dockerhub-login.yml --branch main --limit 1`); otherwise re-enter both
secrets (the username is the Docker Hub account name, the token a *Read & Write* access token) and run it
again by hand (*Actions → Docker Hub login → Run workflow*). It is not a required check and does not run on
pull requests.

## Writing tests that do not flake

CI runs on small machines (two cores, often busy; the PostgreSQL job the slowest), so a test that
passes here can fail there now and then. Almost every such failure so far was a test that waited a
fixed time for something to happen, or read a page before the data it needed had arrived.

- **Wait for the state, not for time.** `tests/waits.py` has `wait_until(predicate, what, timeout=10)`
  and, in async tests, `await until(...)`: they return as soon as the state is reached and fail naming
  `what` after the deadline, so the deadline can be generous. To have one request in flight before the
  next, start it in a thread and wait until the fake engine or app has received it (FY-IGEN-007,
  FY-X-014, FY-PGV-008), not `time.sleep(0.2)`. Then have the fake keep it until the test lets it go
  (a `threading.Event`), not for a fixed time: on a busy runner the next request can take longer than
  that time to arrive, and finds the first one already done (FY-PGV-008 held it 1 s, #204; FY-RT-002 and
  FY-X-014 too, #212).
- **Do not time two things the code does at once.** Two requests the gateway sends together can reach
  the fake in either order, and "within 0.5 s" fails on a slow runner. Hold them and check the state
  instead: FY-FW-008 proves the second request got a slot while the first was in flight because both
  are held at the fake engine at the same moment (`FakeEngine.held`), with the line's safety-net
  recheck made longer than the test so only a real wake-up passes (#212).
- **Set up the state before the step that could race past it.** A fake job that must still be at work:
  make the fake engines hang before asking for it, not after (FY-VID-005).
- **Fixtures must look current.** The app's liveness sweep runs every 2 s during the tests, so
  `make_node` says its machine was heard from just now; a machine with no heartbeat went offline
  under a slow test and the next deploy got 409.
- **Stopping a process is not instant.** End process groups with `tests/leaks.py`'s `kill_group()`,
  which waits until every member is gone; the per-test leak check otherwise sees one still dying.
- **A sleep is fine when time is what the test is about**: a timeout, a stall, "nothing happens
  within 0.5 s". Even then, hold what must not come in time with a `threading.Event`, not with a sleep a
  little longer than the timeout, so the timeout is the only thing that can end the request: a starved
  gateway that looks late finds a slept-for answer already there and passes it on (FY-RT-006, FY-FA-006,
  FY-FA-010, FY-X-009). Assert a lower bound the code guarantees ("waited at least `wait_ms`"); an upper
  bound only to tell the timeout from "never", generous, with the reason in a comment. FY-RT-003's
  `< 1.5 s` failed at 1.63 s; it now checks that the first request was still held when the spill came back
  (#226).
- **UI tests: everything from the API arrives later, and in any order.** Use `await screen.findBy…` or
  `waitFor` for anything that comes from a fetch (a form shown once its data is there, a select's
  options from another endpoint, a value set when an answer arrives), and `getBy…` only for what is
  already known to be on the page. After a save, wait for its message before the next click.
  `npm run test:jitter` runs the UI tests with every fake fetch answering after a random 0–150 ms
  (`web/src/test/fetchJitter.ts`): run it a few times after writing UI tests.
- **Try it under load before CI does**: pin the run to two cores and keep them busy, for example
  `for i in 1 2 3 4; do taskset -c 2,3 sh -c 'while :; do :; done' & done`, then
  `taskset -c 2,3 uv run --no-sync python -m pytest -q` (or `npx vitest run`), and kill the four
  loops afterwards.

**When a CI run hangs outside a test.** pytest-timeout fails a test that hangs for 5 minutes and names it, but it
does not cover collection, session hooks or interpreter exit, where a non-daemon thread left behind keeps Python
waiting. So CI runs pytest as `uv run --no-sync timeout --signal=ABRT --kill-after=60s 40m python -X faulthandler -m
pytest -q`: after 40 minutes (the step usually takes 18–23) Python prints every thread's stack and the step fails,
before the job's 45-minute limit cancels it and its log is lost (#286). `-X faulthandler` is needed because pytest
turns its own handler off before interpreter exit. Search the job log for `Fatal Python error: Aborted`; the
threads listed under it show where the run stopped. FY-CIHANG-001 checks that every pytest step in the workflows
keeps this wrapper. To try it locally, run the same command with a short limit (`timeout --signal=ABRT 10s …`).

## Layout

| Path | What |
|---|---|
| `forgeyard/app.py` | FastAPI app: guards, routes, background jobs (usage, alerts, certificate renewal), the built UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]`, `secrets reset` and `forgeyard agent …` |
| `forgeyard/config.py` | every server setting (environment and `.env`) |
| `forgeyard/db.py`, `forgeyard/migrations/` | the database models; Alembic migrations, run on start |
| `forgeyard/api/` | the HTTP API: `auth` (setup, sign-in, sessions, links), `users`, `audit_log`, `machines`, `workloads`, `onboarding` (Get started), `tls` (the CA download), `install` (`/install.sh` and the wheel), `guide` |
| `forgeyard/api/models.py`, `deployments.py` | catalog, recipes, sources; deployments, estimate, try it |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` and the playground (its vision panel through `forgeyard/gateway/vision.py`); API keys, their limits, cloud setting and budget, and usage |
| `forgeyard/api/providers.py` | cloud providers and their models: add, test, import, prices by hand, clone |
| `forgeyard/api/routing.py` | routes and their tiers of targets |
| `forgeyard/api/prices.py` | the price refresh from OpenRouter: preview and apply |
| `forgeyard/api/budgets.py` | monthly budgets of people and providers, and this month's spend |
| `forgeyard/gateway/proxy.py` | forwarding a request through a route's tiers (`Job`, `forward`, `record`); the names of its parts below are importable from it too |
| `forgeyard/gateway/route.py`, `body.py`, `fit.py` | a route and its targets, who may call it and the tiers a request may use; the request body read and parsed; the targets that can take it (media, size, speech, images) |
| `forgeyard/gateway/upstream.py`, `replicas.py`, `cloud.py` | the HTTP client to the replicas, what may be retried, client disconnects; a tier's replicas (concurrency, the line, the first answer); its providers (order, budgets, quirks) |
| `forgeyard/gateway/streaming.py`, `answer.py` | streamed answers (SSE, audio); the answer passed on and its usage counted |
| `forgeyard/gateway/keys.py`, `limits.py`, `balancer.py` | API key checks and cloud settings; requests and tokens per minute; replica order and provider cooldowns |
| `forgeyard/gateway/quirks.py` | what differs per provider kind: how the key is sent, parameters dropped or renamed, how usage is read back |
| `forgeyard/gateway/where.py` | whether a new provider is outside your network, from its kind and its address |
| `forgeyard/gateway/egress.py` | the HTTP client for providers: TLS, no redirects, the address checks, `EGRESS_*` |
| `forgeyard/gateway/pricing.py` | a request's cost from its usage and prices; OpenRouter's list and price matching |
| `forgeyard/gateway/budgets.py` | the budget ledger: reserve, settle, release, the 80% and 100% alerts, 402 |
| `forgeyard/gateway/media.py` | images, audio and video in chat: parts rebuilt, media URLs fetched under the egress rules |
| `forgeyard/gateway/speech.py` | text to speech: request checks, voice cloning refused, what each engine is given, the audio's length |
| `forgeyard/gateway/imagegen.py` | image generation: request checks, per-deployment caps, what each engine is given, the queue, the answer and its size |
| `forgeyard/gateway/videogen.py`, `videojobs.py` | video generation: request checks, per-deployment caps, the job object; the jobs on their replica, the sweeper, expiry, content and usage |
| `forgeyard/gateway/usage.py`, `errors.py` | usage counted in memory and written to the database; errors in OpenAI's shape |
| `forgeyard/machines/` | agent protocol, join and stream endpoints, live connections, offline detection |
| `forgeyard/workloads/` | the workload spec, placement and the GPU allowlist, desired state and status |
| `forgeyard/models/` | model sources, the SSRF-guarded fetcher, inspection (safetensors, GGUF, what a model takes besides text, licence), estimates, recipes, the scheduler, routes, the recommended list, the server's Hugging Face token, downloads ahead of a deployment (`prefetch.py`, with `api/downloads.py`) |
| `forgeyard/recipes/` | the built-in engine recipes (YAML), images pinned by digest |
| `forgeyard/security/` | passwords, sessions, rate limits, host/CSRF middleware, HTTPS and the CA, secrets at rest, `secrets reset` |
| `forgeyard/agent/` | the agent: inventory collectors (NVML, AMD SMI, CPU), connection, state, trust (`agent trust`), how it runs (`how_it_runs.py`, for its update command) |
| `forgeyard/agent/config.py`, `setup.py` | the agent's settings (`agent.toml`, `FORGEYARD_*`, flags) and `agent init` |
| `forgeyard/agent/models.py` | the agent's model cache: downloads, verification, engines |
| `forgeyard/agent/workloads/` | the agent's reconciler, process and container runtimes, the replicas' key guard, logs, health checks, downloads ahead of a deployment (`prefetch.py`) |
| `forgeyard/agent/workloads/supervisor.py`, `state.py`, `runtime.py` | the reconciler: its loop, the desired list and the reports; its state in memory; the `Runtime` protocol every runtime implements (and `ContainerEngine`) |
| `forgeyard/agent/workloads/lifecycle.py`, `fetching.py`, `checks.py`, `keyguards.py`, `admin.py` | the reconciler's parts: a replica's start, end, stop and removal; model, engine and image before a start; health checks; the key guards; an admin's cache and image requests |
| `forgeyard/install/` | the agent's wheel built from the running code, `install.sh`, the commands on the Machines page, an agent's update command and whether it is behind (`update.py`), the published images' names (`images.py`), the server image's health check |
| `forgeyard/guide.py`, `docs/guide/` | the user guide (en, zh-TW), served at `/guide` |
| `docs/design.md`, `docs/https-and-trust.md` | how each part works; HTTPS, trust and secrets |
| `Dockerfile.agent`, `scripts/agent_image_smoke.sh` | the agent's container image and its CI smoke test |
| `Dockerfile.server`, `scripts/server_image_smoke.sh` | the server's container image and its CI smoke test |
| `.github/workflows/publish-images.yml`, `scripts/publish_plan.py` | a release tag publishes both images on Docker Hub |
| `.github/workflows/dockerhub-login.yml`, `scripts/dockerhub_check.py` | every push to `dev` and `main` checks that the Docker Hub secrets log in and may push ([before a release](#before-a-release)) |
| `scripts/accept_all.py` | the full regression run: every shipped feature checked on your own server and machines ([how](guide/how-to.md#check-a-server-end-to-end)) |
| `scripts/accept_m3.py`, `accept_m4.py`, `accept_mm.py` | acceptance runs on real machines, through the API and the official OpenAI SDK |
| `scripts/gpu_probe.py` | checks which NVIDIA GPU a process sees, through the CUDA driver only |
| `scripts/accept_tts.py` | the real-hardware acceptance of text to speech (also the `tts` group of `accept_all.py`) |
| `scripts/accept_img.py` | the real-hardware acceptance of image generation (also the `img` group of `accept_all.py`) |
| `images/vision/` | the vision server the `vision` recipe runs (detection, segmentation, depth, image classification) and, 1.5.4, the `transformers` recipe (image embeddings, text classification): its Dockerfile, pinned models and dependencies, smoke test and its tiny test models (`tinymodels.py`) |
| `scripts/accept_scale.py` | the real-hardware acceptance of sleep when idle, waking and automatic replicas (also the `scale` group of `accept_all.py`; [how](guide/how-to.md#check-sleep-waking-and-automatic-replicas)) |
| `scripts/accept_sso.py` | the acceptance of single sign-on against a real OpenID Connect provider (Keycloak, …) in a browser; it runs alone, not as a group of `accept_all.py`, because it changes the server's sign-in setup for a few minutes |
| `scripts/check_recommended.py`, `pin_images.py` | check the recommended models against Hugging Face; pin the recipes' images by digest |
| `web/` | React + TypeScript + Vite UI, built into `forgeyard/static` |
