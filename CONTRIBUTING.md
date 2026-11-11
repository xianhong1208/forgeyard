# Contributing

Thank you for helping. Bug reports with the output that shows the problem are as welcome as code.

## Reporting

- **A bug:** open an issue with the *Bug report* form. For anything about a machine's GPUs, attach
  `forgeyard agent inventory --json --raw` from that machine (it holds no secrets).
- **A security problem:** never in an issue; see [SECURITY.md](SECURITY.md).
- **An idea:** open an issue with the *Feature request* form first, so we can agree on the
  behaviour before you write it. [ROADMAP.md](ROADMAP.md) says what is planned.

## Setting up

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), Node 22.12+ for the UI, and Docker
only to build the container images.

```bash
uv sync
cd web && npm ci && npm run build && cd ..
uv run python main.py          # https://127.0.0.1:7860, prints a one-time setup token
```

A machine without GPUs can still act as one with made-up hardware, for development only:
`FORGEYARD_AGENT_SIMULATION=1 uv run python -m forgeyard agent --simulate-inventory nvidia-a100x8 …`
(fixtures in `forgeyard/agent/fixtures/`; see *Simulated inventory* in [docs/design.md](docs/design.md)).

## Making a change

1. Branch off `dev`; open the pull request into `dev`. `main` only moves at a release.
2. Every behaviour change comes with tests. Test IDs (`FY-<AREA>-NNN`, in each test's name and
   docstring) are unique across the project: take the next free number of the area.
3. Schema changes go through a new Alembic migration in `forgeyard/migrations`; a test fails when
   the models and the migrations drift apart. Existing data must survive the upgrade.
4. User-facing changes update the docs in the same pull request:
   - the guide in **both** languages (`docs/guide/` and `docs/guide/zh-TW/`);
   - `README.md` and `README.zh-TW.md` when the overview changes;
   - `CHANGELOG.md` under *Unreleased*.
5. UI changes: check them at desktop width and at 390 px (no horizontal scroll, no console
   errors).
6. Run the same checks as CI before you push:

   ```bash
   uv run --no-sync ruff check .
   uv run --no-sync python -m pytest -q
   cd web && npm run lint && npm run typecheck && npm test && npm run build
   shellcheck -s sh forgeyard/install/install.sh
   shellcheck scripts/agent_image_smoke.sh scripts/server_image_smoke.sh
   ```

   CI also builds both container images and runs their smoke tests (`agent-image`, `server-image`)
   and builds them for linux/arm64 (`images-arm64`).

   `TEST_DATABASE_URL=postgresql+psycopg://…` runs the backend suite on PostgreSQL too. It
   **wipes that database**: use a throwaway one.

Commit messages say what changed and why, in plain English. Keep pull requests to one topic.

## Releasing

Maintainers only. On a release branch off `dev`: bump the version in `pyproject.toml`,
`forgeyard/__init__.py` and `web/package.json` (then `uv lock` and `npm install --package-lock-only`),
date the `CHANGELOG.md` section, and open the release pull request into `dev`. After it merges,
`main` follows `dev`, and the annotated tag `vX.Y.Z` goes on that commit.

Pushing the tag runs `.github/workflows/publish-images.yml`, which publishes both images on Docker
Hub (`docker.io/<namespace>/forgeyard` and `docker.io/<namespace>/forgeyard-agent`) for linux/amd64
and linux/arm64, tagged `X.Y.Z`, `X.Y` and `latest` (a pre-release only `X.Y.Z`), with provenance
and an SBOM. It then pulls the amd64 images back by digest, smoke-tests them and lists the digests
in the run's summary. It needs:

- the namespace in `forgeyard/install/images.py` (`DOCKERHUB_NAMESPACE`), the one place it is
  written: the Machines page names the agent image from it too;
- the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with
  *Read & Write*).

Without them, or run by hand (*Actions → Publish images → Run workflow*) on a branch, it builds and
smoke-tests both images, pushes nothing and ends green with a notice saying why. Run by hand on a
tag, it publishes that tag again. The tag must be `v` plus the version in `forgeyard/__init__.py`.

## Licence

Forgeyard is licensed under the [Apache License 2.0](LICENSE). By contributing you agree that your
contribution is licensed the same way (section 5 of the licence).
