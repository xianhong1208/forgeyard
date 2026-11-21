# Changelog

All notable changes are recorded here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. Versions follow [Semantic Versioning](https://semver.org/); each milestone is one minor
version until 1.0.

## [Unreleased]

### Changed

- The DCO check no longer runs on the release pull requests (`dev` → `main` and the `main` → `dev`
  back-merge): every commit reached `dev` through its own checked pull request, and checking them again
  failed on commits merged before the DCO was adopted (#160).

### Fixed

- **Mixed server and agent versions keep working** (#170). A card of a vendor the server does not know (from a
  newer agent) is left out of the machine, with a warning in the server's log and an *unknown vendor* line under
  the machine page's **Detection notes**, instead of refusing the agent's hello. A message type either side does
  not know is ignored, with one warning per connection, instead of closing the socket. Before, both closed the
  connection with 4003, which an agent retries, so a newer agent reconnected forever. A known message that does
  not validate is still refused.


## [1.4.4] — 2026-11-21

More kinds of models, part 1. Zero-shot object detectors find the objects a request names: `/v1/vision/detect`
takes `labels` (Grounding DINO, MM-Grounding-DINO, OWLv2; #142). Text classifiers run on vLLM's pooling engine at
`/v1/classify` (sentiment, language, toxicity; #144), and image classifiers, with fixed classes or zero-shot
(CLIP, SigLIP 2), at `/v1/vision/classify` (#141). Each has its own kind, recommended models with permissive
licences, a Playground view and **Try it**. The built-in `vision` recipe now pins `forgeyard-vision` 1.4.4rc1,
the image published by the `v1.4.4rc1` pre-release, which runs both new vision tasks (#175). No setting and no
migration (the labels live in the catalog entry's details), and the agents need no update: the agent did not
change since 1.4.3, and a machine gets the new vision image like any other pinned engine image. Contributions
are now signed off under the DCO.

### Added

- **Contributions are signed off under the Developer Certificate of Origin** (DCO 1.1): every non-merge commit of a pull request needs a `Signed-off-by:` line with its author email (`git commit -s`); a new `DCO` workflow checks it, bots excepted ([CONTRIBUTING](CONTRIBUTING.md#licence)).
- **Text classification** (#144, vLLM part): a model kind of its own, *Text classification* (task `classify`). A
  sequence classifier with two labels or more (sentiment, language, toxicity…) is added to the catalog with its
  labels, deployed on `vllm-pooling` (vLLM's pooling runner, which picks the classify task by itself), listed under
  **Serving → Text classification**, tried in the **Playground** (texts, one per line; every label's probability as
  a bar, most likely first) and on the deployment's page (**Try it**), and called at **`POST /v1/classify`** in vLLM's
  shape (`input`: a text or up to 2,048; `data[{index, label, probs, num_classes}]`, `usage`). The gateway checks the
  body, sends the engine only `model`, `input`, `truncate_prompt_tokens` and `use_activation` at its `/classify`,
  counts prompt tokens like embeddings, and never sends it to a cloud provider. Three recommended models, MIT or
  Apache-2.0: multilingual sentiment (1 to 5 stars), language detection (20 languages) and toxic comments (6 labels,
  scored on their own). Hugging Face search has the task too. The guide says how a chat model classifies into labels
  you choose with structured outputs (`structured_outputs.choice`, or a JSON schema `enum`). No migration: the
  labels live in the catalog entry's details, like a vision model's classes.
- A chat model called at `/v1/classify` answers 400 `wrong_endpoint` with how to classify with structured outputs.
- **Image classification** (#141), a model kind of its own: *Image classification*, task `image-classify` (text
  classification, #144, is another kind), answered at `POST /v1/vision/classify` by the vision server.
  - Classifiers with fixed classes (`…ForImageClassification`: ViT, ConvNeXt V2, DINOv2…) answer their best
    classes; zero-shot ones (CLIP-like: CLIP, SigLIP, SigLIP 2, ALIGN, AltCLIP, Chinese-CLIP, MetaCLIP 2) score the
    `labels` each request sends, put in a `template` ("This is a photo of {}."), with a sigmoid each (SigLIP,
    `multi_label: true`) or a softmax (CLIP). `top_k` (5; zero-shot: all labels). `labels_required` /
    `labels_not_supported` as for detection; a timm checkpoint (`timm_wrapper`) is refused.
  - Keys, model lists, rpm and usage (one image, its kilopixels, busy time) as for the other vision routes;
    Hugging Face search asks for `image-classification` and `zero-shot-image-classification`; OpenCLIP's own
    copy beside LAION's transformers weights (`open_clip_*`) is neither read nor downloaded.
  - Recommended group *Image classification*: SigLIP 2 base, LAION CLIP ViT-B/32 (MIT), ViT base, ConvNeXt V2 tiny,
    DINOv2 small with its ImageNet head.
  - Playground: a classify view (labels for zero-shot models, scores as bars); Try it asks a zero-shot classifier
    to choose among three test classes. The vision image of 1.4.4 or later is needed (422 `vision_image_too_old`
    on an older one, which has no such task).
  - `scripts/accept_vision.py`: `siglip2-base`, `clip-vit-b-32-laion`, `vit-base-patch16-224`,
    `convnextv2-tiny-22k-224`.
- **Detectors that find what you name** (#142). Zero-shot (open-vocabulary) object detectors (Grounding DINO,
  MM-Grounding-DINO and LLMDet, OWL-ViT, OWLv2), refused by 1.4.3, are detection models now: the
  same `/v1/vision/detect` takes `labels`, the names to find (a JSON list, or a comma-separated form field;
  at most 32 of at most 64 characters), and answers boxes for those names in the usual shape. The vision
  server loads them with `AutoModelForZeroShotObjectDetection` and asks each family its own way (Grounding DINO
  one lower-case prompt, each box taking the name whose words score it highest; OWL one query per name, OWLv2's
  boxes scaled to the square it looks at). Without `labels` a zero-shot detector
  answers 400 `labels_required`; a detector with fixed classes given `labels` answers 400
  `labels_not_supported`. Default thresholds follow the family (0.3, OWL 0.1). OmDet-Turbo stays refused: its
  transformers port loads its backbone through timm, which the vision image does not include (the vision server
  now refuses any `use_timm_backbone` model at start).
  - The catalog marks them *Finds what you name* (`vision.zero_shot`, no classes); a row added before 1.4.3 is
    known by the config it kept (no migration). Hugging Face's search for detection also asks for
    `zero-shot-object-detection`.
  - Recommended: Grounding DINO tiny, MM-Grounding-DINO tiny and OWLv2 base (Apache-2.0, pinned). Grounding DINO
    and MM-Grounding-DINO are estimated with 512 MiB more (measured: 2.24 GiB peak on a CPU with 0.64 GiB of
    files; OWLv2 base 1.13 GiB).
  - Playground: a **Find these** box for them (comma-separated names, then **Find**; the score slider starts at
    0.3). **Try it** asks them for the test image's two shapes (or for the words sent as `input`).
  - They need a vision image from 1.4.4 on: on a recipe whose `forgeyard-vision` image is older, deploying one
    answers 422 `vision_image_too_old` (a pre-release tag such as `1.4.4rc1` counts as the release it leads to,
    1.4.4). The built-in recipe runs one (below).
  - `scripts/accept_vision.py` knows `grounding-dino-tiny`, `mm-grounding-dino-tiny` and `owlv2-base`: two cats,
    a remote and no giraffe on the COCO photo, `labels_required`, and `labels_not_supported` for RT-DETR.

### Changed

- **The built-in `vision` recipe pins `forgeyard-vision` 1.4.4rc1** (CPU `sha256:f4e5940b…`, linux/amd64 and arm64;
  `-cuda` `sha256:9e4ef32b…`), published by the `v1.4.4rc1` pre-release, so zero-shot detectors and image classifiers
  deploy on it. It was 1.4.0.
- A text classifier tagged *text classification* is read as one (1.4.3 refused it as a reranker): one output
  (`num_labels` 1) is still a reranker and two labels or more a classifier, and neither becomes the other.
  Refused as text classifiers, with the reason: zero-shot (NLI) models and DeBERTa, DistilBERT or BART classifiers,
  which vLLM 0.32 does not run (the transformers engine and zero-shot come in 1.5), and regression heads.
- A deployment that names no context starts at the model's own maximum when that is shorter than the engine's
  default (a 512-token classifier or cross-encoder on `vllm-pooling`, whose default is 8192), in the console's
  deploy form, the API and the suggestion shown when a model is added. A context that is named is checked as
  before.

### Fixed

- RoBERTa and XLM-RoBERTa models count their positions after the padding index, so 514 position embeddings hold
  512 tokens: the catalog now says 512 (BGE-M3 and its reranker: 8192, not 8194). Choosing the old maximum as the
  context would have overflowed the position table in vLLM. Models already in the catalog keep the number they were
  added with.
- The get-started page told vision examples to `pip install openai`; they use httpx, as rerank does.

## [1.4.3] — 2026-11-18

Every catalog model and every engine gets its own page in the console (#140): a model's page says where it
is, whether a copy fits on each machine, what uses it and which engines can run it; an engine's page lists its
variants, their images and which machines have them. Model and engine names link to these pages from
everywhere, and an admin can set a model's display name. Zero-shot detectors and text classifiers are refused
when added, instead of failing later as detection models or rerankers (#147); their support comes with 1.4.4.
No setting and no migration, and the agents need no update (an agent from 1.4.1 on already lists its images
for the engine pages).

### Added

- **A page per catalog model and per engine** (#140, layout A+). A model's page, `/models/<name>`: its facts (task,
  size, licence, what one copy needs, deployments), jump links, **Overview** (source, revision with the Hugging Face
  link, licence, task and its `/v1` path, architecture), **Machines** right after it (each machine's model cache
  entry with its size and what uses it, a download under way, whether a copy fits there now with each engine's
  defaults on a GPU or the CPU, and whether the engine variant's image or program is there; **Deploy here**,
  **Download here**), **Deployments**, **Engines** and **Files**, folded to one line (the files a machine downloads
  with their sizes, and the repository's other quantisations and projectors, not downloaded). **Deploy**,
  **Download to…** and **Remove** are at the top, and an admin sets the model's **display name** (its `title`; the
  name stays, since deployments record it). An engine's page, `/models/engines/<name>`: **Overview**, **Variants**
  (image or program, pinned by digest or by tag, what the machine needs, the offline commands), **Machines** (the
  variant each one would run and whether its image or program is there), **Parameters** folded to one line, and
  the deployments and catalog models that use it; admins **Edit** and **Delete** a custom engine and **Copy as a
  custom engine** a built-in one. Everyone signed in may look. Model and engine names link to the pages on the
  Catalog and Engines tabs, Serving, a deployment's page, a machine's model cache and **Download to…**.
- `GET /api/models/{name}` (by name, else by id): the model, its files, each machine's cache entry and engine,
  where it fits per engine and device (the deploy estimate, for everyone signed in to read), and its deployments.
  `GET /api/recipes/{name}` adds each machine's variant and whether its image or program is there, the deployments
  and the catalog models that use it; `?refresh=true` has the connected machines list their images first.
- The server keeps each machine's last image listing (`nodes.runtime_info["images"]`: id, tags, content id, size,
  when): every `image_listed` answer replaces it, a removal the agent confirms and an upload it loaded update it, and
  it outlives a reconnect, so an engine's page says which machines have its image while they are offline too. It
  is not in `GET /api/machines`. No migration (an existing JSON column).

### Changed

- A model can no longer be named `deploy`, `deployments`, `routes`, `engines`, `catalog`, `sources`,
  `recommended`, `search` or `inspect` (422 `reserved_name`): those are other pages under `/models` in the console,
  or other paths under `/api/models`.

### Fixed

- **Zero-shot detectors are no longer added as detection models** (#147). Grounding DINO, MM-Grounding-DINO and
  LLMDet, OWL-ViT, OWLv2 and OmDet-Turbo have architectures ending in `ForObjectDetection`, so the catalog took
  them as detection models, and their replicas failed: the vision server loads detectors with
  `AutoModelForObjectDetection`, which does not know them. They are now known by their `model_type` or
  architecture (transformers' zero-shot detection models, as of the 5.23.0 the vision image pins), and adding
  or re-tasking one answers 422 `not_a_vision_model`: "a zero-shot (open-vocabulary) detector … support comes
  with #142". The Add a model preview says so too. One added before this release stays in the catalog (no
  migration) and deploying it answers the same 422.
- **Text classifiers are no longer filed as rerankers** (#147). Every model Hugging Face tags
  `text-classification` was taken as a reranker, because many cross-encoder rerankers are tagged that way. The
  inspector now applies vLLM's rule to a `…ForSequenceClassification` model's `config.json`: one output
  (`num_labels` 1, or one `id2label` entry) is a reranker; two labels or more (sentiment, topic, NLI) is a text
  classifier, and inspecting or adding it as a reranker answers 422 `cannot_inspect` with its labels ("Text
  classification comes with #144"). bge-reranker, the ms-marco cross-encoders and causal-LM rerankers such as
  Qwen3-Reranker are added as before. Rows already in the catalog are not changed.

## [1.4.2] — 2026-11-18

A patch release from running 1.4 on real machines. A machine's page shows the exact command that updates its
agent, made from how the agent runs, and an agent behind the server says so (#121, #122). A machine's engine
images are listed and can be removed when nothing uses them, and **Copy from another machine** lists engine
images first (#123, #124). Placement counts a machine's free host ports (#125). An admin can download the secret
key and the CA as one passphrase-encrypted backup from the console, and `forgeyard secrets restore` puts them
back (#127). A *Docker Hub login* workflow catches a wrong Docker Hub secret before a release, not after the tag
(#128). The guide walks through a machine without internet access in one place (#129), and a relay whose source
breaks off reads clearly in the server's log (#130). `scripts/accept_all.py` checks relaying, image upload and
image copy (#126). No setting and no migration; update the agents to 1.4.2 so that they report how they run
and their port range, and can remove engine images (`image_remove`).

### Added

- **Download a backup of the secret key from the console** (#127). Admins only: **Download a backup…** on the
  setup advisor's "Back up the secret key" reminder, or **Download a key backup…** under **For admins** on
  **Trust this server**. The admin types their password again (checked like a sign-in: wrong ones count toward
  the same back-off and are audited as `secret_key.backup_failed`, answered 401 `wrong_password` so the console
  does not sign out) and a passphrase of their own, twice (12+ characters, not the password). The answer
  (`POST /api/secret-key/backup`, `Cache-Control: no-store`, an attachment named
  `forgeyard-key-backup-<key id>-<date>.fykey`) holds the secret key and, with Forgeyard's own CA, `ca.key` and
  `ca.crt`, sealed with AES-256-GCM under an Argon2id key (64 MiB, 3 passes, 4 lanes, random salt and nonce;
  the versioned header is authenticated too). Not the server certificate: the server makes it again from the CA.
  The server keeps neither the file nor the passphrase; the download is audited
  (`secret_key.backup_downloaded`, key id, never the passphrase) and ticks off the reminder for that key id.
  `GET /api/secret-key` says what a backup holds. The format is in the guide's reference.

- **`python -m forgeyard secrets restore FILE`** puts the key and the CA back (asks for the passphrase;
  `--data-dir`, `--passphrase-file`). It writes nothing on a wrong passphrase or a changed, cut-off or unknown
  file, never replaces a different key or CA without `--force` (which keeps the old files as
  `<name>.replaced-<time>`), and writes the keys with mode 600 in folders with mode 700.

- **Update an agent with the exact command, from its machine's page** (#121). An agent now reports how it runs,
  in `hello.install` (older servers ignore it): in a container, it reads its own container through the engine
  it already uses (`docker inspect` / `podman inspect` of the container it finds in `/proc/self/mountinfo`):
  name, image, network, restart policy, `--gpus` from the GPU requests, devices, `--group-add` ids, mounts
  (including the image's unnamed state volume, by name), the variables given with `-e` (the image's own left
  out), extra arguments and a few other options; installed with install.sh, its systemd unit, program and
  agent.toml; otherwise its checkout. It never reports the join token, its credential, or a variable whose
  name or value looks like a secret (named only), and nothing about a container that is not the agent image.
  The machine's page (admins, `GET /api/machines/{id}/agent-update`) shows **Update the agent**: for Docker,
  `docker pull` of the server's agent image, `docker rm -f` and the same `docker run` on it; for a machine that
  gets its files through the server, `docker save … | ssh … docker load` first (offered to the others too);
  for install.sh, the one-line install without a join token; for a checkout, `git checkout` and `uv sync`.
  Every reported value is checked again and shell-quoted; a secret variable is passed as `-e NAME="$NAME"`
  with a note to export it. An agent before 1.4.2 says that it does not report this, with the guide. Stored in
  the machine's existing `runtime_info` (key `install`), never shown in the machine list. No migration.

- **An agent behind the server says so** (#122). The Machines tiles and the machine's page show *agent 1.4.0,
  server 1.4.2: update*, opening the update command; also for an agent of the server's version that lacks a
  feature such an agent declares (built from older code). The machine list carries `agent_behind` and
  `server_version`; **Setup health**'s older-agent finding (V21) covers the missing features too and opens the
  machine's update command.

- **Remove engine images a machine no longer uses** (#123). A machine's page lists its **Engine images**: each image
  a recipe names, as its agent lists it now (a pinned image recognised by its recorded ids, a tag-only one by its
  tag), with its size, the recipes that name it and the deployments on that machine that use it, and the free space
  where the engine keeps its images when the agent can see it (`GET /api/machines/{id}/engine-images`, everyone
  signed in). The engine's other images are only counted. **Remove** (admins) asks in the row, then the agent runs
  `docker rmi` / `podman rmi` without `-f` (`POST /api/machines/{id}/engine-images/remove`, new agent feature
  `image_remove`: `image_remove` / `image_removed`); the console says how much was freed. Refused, with the reason,
  while a deployment there uses the image (409 `image_in_use`, naming it), for the image the agent runs from
  (`agent_image`, which a 1.4.2 agent now reports in `image_listed`), while any container of the engine uses it (the
  agent checks, naming the container), and never offered for an image no recipe names (`not_an_engine_image`). The
  agent removes only an image of its own listing, by its id (by its own tags when it has several). An older agent
  lists the images and says to update it. Audited as `node.image_removed`.

- **`scripts/accept_all.py` checks relaying, image upload and image copy** (#126): two new groups after `vision`.
  `relay`: on the CPU machine (`--relay-machine`) with its `fetch_via` set to `server`, **Download to…** of a 26 MB
  GGUF (`ggml-org/tiny-llamas`, `stories15M-q8_0.gguf`, model only) must end `done` via `server`, with the file at
  its size in the machine's model cache; then on a second machine (`--relay-second-machine`, default the second CPU
  machine, else the GPU machine) the same download must come from the first machine's cache (via `machine`, the
  source named, and the dialog naming it first). `images`: on a machine with a container engine (`--engine-machine`,
  default the GPU machine), its engine-image listing; an image file the script builds itself (a few KB, one layer,
  no Docker needed where it runs) uploaded and loaded (no recipe match, audited, only counted on the machine page,
  Remove refused as `not_an_engine_image`); copied to a second machine that loads images
  (`--second-engine-machine`; same content, audited); then removed with **Remove engine image**, for which a custom
  recipe names it for a few seconds (Forgeyard removes only images a recipe names). Everything is put back at the
  end, also after a failure: both machines' `fetch_via`, the model in their caches (and the part file of a download
  it broke off, unless the machine already had partial downloads: those are not the run's to delete, and the report
  says so), the image and the recipe (an agent older than 1.4.2 cannot remove it: the report says it stays). Without the machines or agent features they
  need, the checks are skipped with the reason.

### Changed

- **The guide walks through a machine without internet access in one place** (#129): the new how-to section
  *Run a machine without internet access* (en and zh-TW) goes from joining such a machine (only the server
  to reach; the port range through a firewall or an SSH tunnel, counted by placement) through its
  **Internet access for downloads**, models (through the server, from the internet or from another machine's
  cache, and where the progress says which), engine images (pinned ones through the server, the vision image
  too; tag-only ones uploaded or copied; removing old ones) and updating its agent (the carry command), to the
  troubleshooting it may need. The guide's index, Concepts, the related how-to sections and Troubleshooting
  link to it. Also fixed on the way: the relay section no longer says the vision image is tag only (pinned
  since 1.4.1); the offline AMD image-generation note says the image and the model come through the server
  with a 1.4 agent; the loaded-image check names its version (1.3) instead of "this release"; Troubleshooting
  explains the relay's *cut short* log line and offers copying a tag-only image; the guide index's How-to
  summary no longer lists services and tasks (gone from the console in 1.4) and names the 1.4 sections.

- **Copy an image lists engine images first** (#124). The **Copy from another machine** dialog shows the images a
  recipe names, and only those until **Show all images** is ticked (on a shared machine the other images are other
  people's).

- Placement counts a machine's free host ports (#125). The agent reports its port range
  (`FORGEYARD_PORT_RANGE`) in its hello (`runtime_info.ports`; older servers drop it), and the server counts
  the ports its replicas hold there: one per published port, two behind the agent's key guard (every model
  replica, an app exposed at `/x/`). A machine without enough free ports is skipped like one without memory,
  and the plan says why (*no free port: 3 of 3 in use (host ports 33000-33002)*); a replica with nowhere
  else to go waits unplaced with that reason instead of restarting in backoff on the machine. A replica the
  agent reports with *no free host port left* is taken off the machine when the server's count agrees.
  Agents older than 1.4.2 report no range and are not counted, as before. `forgeyard agent runtimes` shows
  the range. No setting and no migration.

- **A wrong Docker Hub secret shows up before a release, not after the tag** (#128). 1.4.0's publish failed
  half-way on *unauthorized: incorrect username or password*. A new workflow, *Docker Hub login*
  (`.github/workflows/dockerhub-login.yml`), runs on every push to `dev` and `main` (and by hand): with the
  secrets set it logs in to Docker Hub with the same pinned action as the publish workflow, asks Docker Hub's
  token service whether the token may push to `forgeyard`, `forgeyard-agent` and `forgeyard-vision`
  (`scripts/dockerhub_check.py`: a read-only token or another account's is granted pull only), pushes
  nothing and logs out; without them it warns *Docker Hub secrets not set: a release would not push images*
  and stays green. It is not a required check (it does not run on pull requests). CONTRIBUTING.md ›
  Releasing and docs/development.md: before tagging, its latest run on `main` must be green, and how to fix
  it when it is not.

### Fixed

- A relay whose source breaks off mid-file reads as such in the server's log (#130). The server writes one
  warning naming the file, the machine, the source (a host on the internet, or `machine <name>`) and the bytes
  sent (*Relay of acme/x@1a2b3c4d5e6f/model.gguf to gpu-02 cut short: the source broke off (…); 104857600 of
  2147483648 bytes sent from huggingface.co in 12.3 s.*), in place of uvicorn's *Exception in ASGI
  application … Response content shorter than Content-Length*, which read like a server bug. The answer still
  ends short of its Content-Length (the connection closes), so the agent asks for the rest again as before;
  only uvicorn's record of such an answer is dropped, every other one of its errors is kept.


## [1.4.1] — 2026-11-17

A patch release for machines without internet. A machine that gets its files through the server now gets a
model file from another machine that already has it in its cache, still through the server, before the
internet (#116); an admin can copy an engine image from one machine to another (#117). The vision image is
pinned by digest like every other engine, so a machine without registry access gets it through the server
(#115), and a fully named Docker Hub image (`docker.io/<org>/<name>`) can be relayed and pinned
(`split_image`). Test IDs are unique across the project (#114). No setting and no migration; rebuild the
agent images to get the new agent features (`relay_source`, `image_save`).

### Added

- **A model file from another machine, through the server** (#104, part 1). A machine that gets its files
  through the server (**Always through the server**, or **Auto** after the source could not be reached) now
  gets a model file from another machine that already has it in its cache before the internet. The source
  agent sends it outbound to its own server (agent feature `relay_source`: the server asks with `relay_send`
  over the WebSocket, the agent POSTs the bytes to `/agent/v1/relay-send/<ticket>` and answers `relay_sent`),
  and the server pipes it into the unchanged relay GET of the machine that asked, storing nothing: no agent
  opens a port, and the machine that asks needs only a 1.4 agent. The file is named to the source by the
  digest the server holds (the SHA-256 or git blob id of a Hugging Face file, the recorded SHA-256 of a URL
  model) and looked up only among what that agent found on its own disk; the source holds the model's lock
  while it sends, so Remove waits. The server picks a machine listing the GGUF file itself, then the one with
  fewer sends, then the latest heartbeat; one that does not have the file, already sends two or does not start
  within 10 seconds is skipped for the next, then the internet is used, before the first byte. The server
  hashes a whole file on its way and holds back the last piece until it matches: a machine whose copy does not
  match is not asked for that file for an hour, the audit log names it (`node.relay_copy_refused`), and the
  file starts over from elsewhere. `Range` resumes, the relay's cancel and limits apply, plus two sends at a
  time per source. Progress says *via the server, from <machine>*, a download's `via` is `machine` with its
  `source`, and **Download to…** names the machine that would send it. No setting and no migration. Engine
  archives keep coming from the internet through the server.
- **Copy an image from another machine** (#104, part 2). On a machine's page, **Engine images → Copy from another
  machine** (admins): pick a machine and one of the images its agent lists (`GET /api/machines/{id}/images`), and
  **Copy** (`POST /api/machines/{id}/images/copy`). The source agent (new feature `image_save`) saves the image
  whose id was asked for, one of its own, under its own tags, and POSTs the `docker save` stream, chunked, to
  `/agent/v1/image-send/<ticket>`; the server pipes it into the target's image upload (`image_load`, unchanged),
  keeping nothing. The answer says what was loaded, which recipe's image it is, and whether it is the image the
  source has (by content id). A save that breaks off never ends the target's stream cleanly, so nothing is loaded.
  The upload's limits apply, plus one copy at a time per source. Audited as `node.image_copied`. Never automatic:
  a tag-only image is trusted as the source machine has it.

### Changed

- **The vision image is pinned by digest, like every other engine** (#113). The built-in `vision` recipe
  names `docker.io/tonyhuang1101/forgeyard-vision:1.4.0@sha256:ab60eb21…` (CPU, linux/amd64 and arm64) and
  `…:1.4.0-cuda@sha256:77344971…` (NVIDIA, linux/amd64) instead of the image of the server's version
  (`forgeyard_image`), and `images.lock.json` records their ids (`scripts/pin_images.py`). So a copy
  loaded with `docker load` is checked by its ids, a machine without registry access gets the image
  through the server (#102), and a server upgrade alone no longer changes the image vision models run:
  after a release that changes `images/vision`, a small PR moves the pin (docs/development.md, *Pinned
  engine images*; CONTRIBUTING.md, *Releasing*). `forgeyard_image` still works in a custom recipe (a copy
  of 1.4.0's recipe), tag only, unchecked and not relayed.
- The ROADMAP's 1.4.1 row says what was decided: still through the server. Direct transfers between machines
  are in Later.
- The server accepts up to 32 features in an agent's hello (agents still send at most 16).

### Fixed

- An image named on Docker Hub in full (`docker.io/<org>/<name>:<tag>`) is read from Docker Hub's
  registry API (`registry-1.docker.io`) by the server's image relay and `scripts/pin_images.py`:
  `https://docker.io/v2/` is the website's redirect, so such a pinned image could not be relayed or pinned.

## [1.4.0] — 2026-11-17

Easier to see, and models on machines without internet. **Machines** are tiles, each with a page that
maps every card's memory to what holds it; **Serving** lists the running models by kind, and **Models**
is the library: Catalog, Engines (custom engines added and edited in the console) and Sources. Cloud
providers and cloud models are on **Cloud**, a deployment's cloud fallback is on its own page, and the
Playground has one model picker. Usage and cost are charted by day, week or month, and Usage's tables
stay short on a phone. Vision models (object detection, segmentation, depth) are a model kind of their
own at `/v1/vision/…`. **Download to…** puts a model and its engine on chosen machines before you deploy
it; a machine's page lists the models in its cache and removes them; a machine that cannot reach
Hugging Face, GitHub or a registry gets model files, engine archives and pinned engine images through
the server, which stores nothing, and admins can upload an image file to it. Placement avoids a machine
that could not fetch what a replica needs. Apps left the console, and their API, the templates and
`/x/` are deprecated (removed in 1.5). The API keys page folds revoked keys away, and revoked keys
leave the budgets. The settings deprecated in 1.3.1 are no longer read. Migration `0015`; rebuild the
agent images to get the new agent features.

### Added

- **Vision models under Models: object detection, segmentation and depth** (#86, part 1: the engine and
  the API). They are model kinds of their own (tasks `detect`, `segment`, `depth`), added from Hugging
  Face and deployed like any other model.
  - The catalog recognises them from `config.json`: an `…ForObjectDetection` architecture (RT-DETR,
    D-FINE, DETR without a timm backbone…), SAM 2 / 2.1, or an `…ForDepthEstimation` one (Depth
    Anything, DPT…); the search's tasks ask Hugging Face for object-detection, mask-generation and
    depth-estimation models, and a result shows its licence. **Permissive licences only** (Apache-2.0,
    MIT, BSD), as the 1.3 templates: another licence, or none, is 422 `not_a_vision_model` when it is
    added, and so is a model of another task or a DETR on a timm backbone.
  - A built-in recipe `vision` runs Forgeyard's own vision server (`images/vision`), now able to load the
    model from the machine's model cache (`--model`), so the cache page, offline machines (and *Download
    to…*, #35) work as for other models: the CUDA build on NVIDIA (7.5+, driver 580+, a shared card), the
    CPU build elsewhere. Its image is named by the server's version (`forgeyard_image`), not pinned by
    digest: it is built with the same release. The server asks for the replica's key
    (`VISION_API_KEY`) on every path but `/health`, behind the agent's key guard.
  - Estimate: the files plus 1 GiB on a GPU, 1.25 GiB of RAM on a CPU (measured: 0.66 to 1.12 GiB peak).
  - The gateway answers `POST /v1/vision/detect`, `/v1/vision/segment` and `/v1/vision/depth` by model
    name: multipart (`model`, an `image` file, the options) or JSON (`image` in base64), only `model`
    changed, the vision server's JSON passed back. API keys, their model lists and rpm apply; no cloud.
    Usage counts one image, its kilopixels and the replica's busy time (the existing `images`,
    `kilopixels` and `busy_ms` units: no migration). A model of another task is 400 `wrong_endpoint`;
    no image 400 `missing_image`.
  - **Try it** on a deployment sends a test image (the box around its red square for SAM 2); the
    Playground's options list the vision models, and the console reaches them at
    `/api/playground/vision/<task>`.
  - The three models of the 1.3 templates are recommended (group *Object detection, segmentation and
    depth*), at the same pinned revisions, on the CPU by default.
  - `scripts/accept_vision.py` and a `vision` group in `scripts/accept_all.py` check them on real
    machines. Apps made from the templates keep running as they are.
- **Vision models in the console** (#86, part 2). **Serving** groups detection,
  segmentation and depth in one **Vision** section, each row with its task and `/v1/vision/…` path;
  the **Catalog** shows a vision model's architecture and classes, a search result its licence, and
  says before adding why a vision model cannot be added (another task, a licence that is not
  permissive, a timm backbone), with **Add** disabled. The Playground's vision panel sends a vision
  deployment the picture as you (picked in the Playground's one model picker, #100). **Try it** on a vision deployment sends the test image. The example calls (Get
  started, API keys, a deployment's page) are curl and httpx with the image as a file field.

- **Download to…: a model, and its engine, on chosen machines before you deploy it** (part of #35).
  From **Models → Catalog** (a model's row) or a machine's page (**Download a model here…**), pick the
  machines: each says whether it fits (what it downloads, the free disk, the engine), is already there,
  is offline, has an agent too old for it, or lacks the disk. **Also the engine** (on by default)
  fetches, for the chosen recipe, what that machine would run: the engine archive of a process variant,
  or the image of a container variant with the 1.3.1 pull progress. Nothing is started.
  - Progress shows under the model's row and on each machine's page, with the words and bar of a
    deployment's download; **Cancel** stops it at once on the agent and keeps what is on disk, so asking
    again resumes. A download on a machine that goes offline goes on when its agent is back. Once done,
    the machine lists the model in its cache and placement prefers it (#71).
  - Admins and operators start and cancel (D9), audited as `model.download_requested` and
    `model.download_cancelled`. API: `GET /api/downloads/targets`, `POST /api/downloads`,
    `GET /api/downloads`, `POST /api/downloads/{id}/cancel`.
  - **URL models are checked (D8):** a single GGUF file from a URL has no published checksum, so the
    SHA-256 the agent computes after its first complete download is recorded in the catalog entry
    (`meta.files[0].sha256`, with the machine and time; audited as `model.checksum_recorded`), and
    every later download must match it.
  - **Internet access per machine** (`nodes.fetch_via`: `auto`, `server`, `direct`; admins only,
    `PUT /api/machines/{id}/fetch-via`, audited as `node.fetch_via_changed`), used by the relay through
    the server (below).
  - Agent protocol feature `prefetch`: `desired.downloads` beside the replicas, `status.downloads` back.
    The agent uses the code a replica's download uses (locks, resume, checksums, cancel) and runs at most
    two at a time. Agents before 1.4 are listed as "update the agent" (rebuild the agent image); an agent
    that comes back without the feature fails what it was asked, saying so.
  - Migration `0015`: table `model_downloads` (what, where, state, progress, who asked, when, why it
    failed), and `nodes.fetch_via`, `auto` for every machine. Finished downloads are kept 30 days, at
    most 20 per machine. The downgrade drops both.
- **Downloads through the server for machines that cannot reach the source** (part of #35, D1/D3). A
  machine with no internet access of its own (one behind an SSH tunnel) gets model files and
  engine archives **through the server**, which fetches them and passes the bytes on, storing nothing.
  The agent asks the way it already talks to the server: outbound HTTPS with its pinned CA and machine
  credential, so the tunnel that carries its connection carries this too.
  - **Internet access for downloads** on the machine's page (admins) now acts: **Auto** (the default)
    downloads directly and switches to the server after a network failure reaching the source (the
    name does not resolve, the connection is refused, reset or times out, the network is unreachable;
    never an HTTP error or a checksum), remembering the host for an hour; **Always through the server**
    never tries directly; **Direct only** never relays.
  - `GET /agent/v1/relay/{replica|download}/{id}/model[/{file}]` and `…/engine`: served only for what
    that machine's current desired state names (a replica placed there and wanted running, or a
    download to it under way), from the source URL the server builds from the catalog entry or recipe,
    with the server's (or the deployment's) Hugging Face token and its own model-source rules. `Range`
    is passed through, the body streamed in 1 MiB chunks with backpressure; a relay that is no longer
    wanted (download cancelled, deployment removed, machine revoked or set to Direct only) is closed
    within 2 s. Two relays per machine and four in all (429 with `Retry-After` beyond). One log line per
    file: what, to which machine, how much, how fast, how it ended.
  - The agent keeps its own resume, retries, cancel, progress and checks: a relayed file that fails its
    SHA-256 (or git blob id, or the engine's pinned SHA-256) is refused and deleted. The Hugging Face
    token is never sent to a machine set to Always through the server; nothing but `Range` goes from the
    agent to the relay.
  - Progress says **via the server** (deployment page, Library, machine page; `download.via` and the
    download's `via`). The Download to… dialog says *via the server* for such a machine.
  - Placement (#71): a machine whose agent relays and whose setting is not Direct only is no longer
    avoided for the model or the engine archive of a host it could not reach; the deployment's page says
    *… could not reach huggingface.co just now; the model comes through the server*. Engine images are
    not relayed yet (the next #35 change) and still count against it.
  - Agent protocol feature `relay`: listed by both sides; model and engine blocks carry `relay: {path,
    mode}`. Older agents are never sent it and download directly as before. Rebuild the agent image.
- **Engine images through the server, and image uploads** (part of #35, D4). A machine that cannot reach
  the registry gets a **pinned** engine image through the server too, layer by layer, the way it gets
  model files; and an admin can upload any image file to a machine from its page.
  - `GET /agent/v1/relay/{replica|download}/{id}/image/manifest` and `…/image/blobs/{digest}`: the
    manifest recorded for the machine's platform in `forgeyard/recipes/images.lock.json` (fetched by that
    digest, refused unless its bytes hash to it and it names the recorded config), and its config and
    layers by digest, never a digest the manifest does not name. From the registry's API with an
    anonymous pull token (Docker Hub, GHCR, quay.io), the server's egress rules, `Range` passed through,
    the token dropped at the CDN redirect; a whole blob is hashed on the way and its last piece held back
    unless it matches. Images named by a tag only are refused (*upload it instead*), and so is a machine
    whose architecture the image has no build for. The same desired-state rule, limits and log lines as
    model files.
  - The agent pulls first (**Auto**) and switches to the server when the pull cannot reach the registry
    (remembering the registry for an hour), or goes straight to the server (**Always through the
    server**). It checks the manifest against the recorded digests, every blob against its digest (a
    break resumes the layer from what is on disk, under `.partial/image-…` in the model cache), the
    config's content id and architecture, then streams an archive both Docker stores and Podman load (an
    OCI image layout plus the classic `manifest.json`, layers as the registry serves them) into `docker
    load` / `podman load`, tags it with the recipe's tag, runs it only when the #10 id check recognises it,
    and deletes the pieces. Progress: *Pulling the image via the server: 120 MiB of 300 MiB, 3 of 7
    layers*, then *loading it*.
  - Placement (#71): a machine whose agent relays images is no longer avoided for a pinned image's
    registry it could not reach (*… could not reach ghcr.io (for the engine image) just now; the engine
    image comes through the server*). A tag-only image still counts against it.
  - **Engine images** on a machine's page, with **Upload an image** (admins): a `docker save` file,
    gzip-compressed or not, sent with progress through the server, which keeps only a small buffer, into
    the agent's `docker load` / `podman load` as it arrives (a file that breaks off is never loaded). The
    dialog lists what was loaded and which recipe's image it is: checked by its recorded ids, named like a
    tag-only recipe image, or neither. `POST /api/machines/{id}/images`; one upload per machine and two in
    all; `IMAGE_UPLOAD_MAX_GB` (64); audited as `node.image_uploaded`.
  - Agent protocol features `relay_images` and `image_load` (listed by both sides; `image_load` /
    `image_loaded` messages). Older agents pull directly and cannot take uploads, and the machine's page
    says to update them. Rebuild the agent image.
  - The registry client that `scripts/pin_images.py` names images with moved into the package
    (`forgeyard/workloads/registry.py`).
- **Usage and cost over time** (#33). Both Usage tabs have a line chart (**Over time**, **Cost over
  time**): per day (30 days), week (13 weeks, from Monday) or month (12 months), UTC, in total or one
  line per key, model, provider or person (admins, with **Everyone**). The Requests tab charts
  requests, tokens (in + out, in, out) or errors, and each unit of the newer model kinds once some
  was counted (media items, characters, images, audio and video length, busy time, workload bytes);
  the Cost tab charts cost in US dollars. The five largest lines are drawn by name and the rest
  added up as *Other*; groups with none are left out. A line keeps its colour when another metric
  re-ranks the lines. Hover or the arrow keys read a period's values aloud and in a tooltip, the
  legend gives each line's total, and **Show the numbers** lists them as a table. The latest period,
  still going on, is drawn dashed.
  - `GET /api/usage/series?step=day|week|month&periods=N&split=none|key|model|provider|user&metric=…`
    sums the same daily usage rows as the tables, so no data changes: at most 366 days, 104 weeks or
    36 months. Whose usage follows `GET /api/usage`: your own, everyone's for admins with
    `everyone=true`.
- **A machine's page lists the models in its cache, with their sizes, and removes them** (part of
  #35). **Models in cache** shows each Hugging Face revision (and each GGUF file of a revision with
  several), single-file models from a URL and engine archives, with their sizes, the catalog models
  they are and the deployments there that use them; how much the cache takes on its disk and what is
  free; and the partial downloads.
  - **Remove** (admins and operators) deletes one like `hf cache rm`: the revision's links, its
    marker and refs, and only the blobs no other revision links to. **Remove partial downloads**
    deletes what unfinished downloads left. Each is audited as `model.cache_removed`, with the bytes
    freed.
  - Refused while a deployment on that machine uses it (409 `model_in_use`, naming it): the server
    checks, and the agent checks again before it deletes anything. Never while a download of the
    same repository (or, for partial downloads, any download) runs there: a download holds a shared
    lock that the removal takes exclusively, without waiting.
  - The agent only looks up the key among the models it found on its own disk; no path is ever made
    from what the server sends.
  - API: `GET /api/machines/{id}/cache`, `POST …/cache/remove` and `POST …/cache/remove-partial`.
    The machine list no longer carries the per-model detail.
  - Agent protocol feature `cache_admin` (`cache_remove`, `cache_prune`, `cache_removed`), and
    `model_cache` gains `models`, `engines_detail`, `used_bytes`, `partial_bytes`, `partial_files`
    (kept within 192 KiB, the largest models first). Agents before 1.4 are listed without sizes and
    without Remove; update them (rebuild the agent image) to remove models. No migration.
- **Placement knows what a machine can fetch** (#56). Among the machines that fit, a machine that
  could not reach a source a replica would download from there in the last hour (the model's host,
  the engine archive's, the image registry) comes last; then replicas spread over machines; then a
  machine that already has the model in its cache, then one that also has the engine archive or
  image; then binpack. The model cache used to count only on a tie.
  - A download that could not reach its source (DNS, connection refused or timed out, network
    unreachable) is remembered per machine and host for an hour, in memory. HTTP errors, a refused
    token, checksums and a full disk are not counted.
  - A replica in backoff because of such a failure is placed again on another machine that fits;
    a pinned deployment's replica stays and says why. On one machine, a later recipe variant that
    needs no such fetch (the process engine when ghcr.io is unreachable) is chosen over the first.
  - The deployment's and workload's pages say why a replica is where it is: *cpu-01 could not reach
    huggingface.co; placed on cpu-02 instead*. The API gives it as the replica's `placement_note`,
    and the placement preview gives `needs` and `unreachable` per machine.
  - Agents list the engine archives they unpacked (`model_cache.engines`); an engine image or
    archive also counts as on a machine once a replica started from it there. Older agents work
    as before.
- **A model workload names the deployment that created it** (part of #36): `GET /api/workloads`
  gives `deployment: {id, name, gpus}`. In the console a model is seen under Serving only (#81, #92).
- **Machines and the models' pages redesigned** (#34, #92). **Machines** is one tile per machine with a mark for its
  kind (NVIDIA GPU, AMD GPU, AMD APU, CPU only), its cards as slots filled from the bottom (what
  Forgeyard reserved, other use, free), free memory, how many models and apps run there
  and their names, heartbeat and agent version, under a count of machines per kind. A machine has
  its own page (`/machines/<id>`): a memory map that splits each card by what holds it, named and
  with what it reserved; what runs there, with **Deploy here**; GPU access
  and runtimes; the agent's details; **Revoke** and **Remove**; and only that machine's setup-health
  findings. **Serving** groups deployments by kind (chat, multimodal chat, embeddings,
  rerank, speech to text, text to speech, images, video, vision), each with its icon, a line on what it does
  and its path at `/v1`; a row says where the deployment runs (machine and cards), its memory and its
  state, and any download, wait or placement note. `GET /api/deployments` gives the model's
  `modalities`, so a chat model that reads images is listed as multimodal chat.

### Changed

- **One thing in one place: Serving runs the models, Models is the library, cloud settings are on
  Cloud** (#34, #36, #81, #92; apps left the console, #88). The console only; the API and the data are unchanged.
  - **Serving** (`/serving`) is a page of its own: the models running now, grouped by kind (the
    Deployments list of Models before), with **Deploy a model** (`/serving/deploy`). A deployment's
    page is `/serving/<id>` and it is changed at `/serving/<id>/edit`.
  - **Models** is the model library only, with three tabs: **Catalog** (the default: the models you
    can deploy, adding from the recommended list, a Hugging Face search or a source, *Download to…*,
    Deploy), **Engines** (the recipes: vLLM, llama.cpp, sd.cpp, speaches, vLLM-Omni, vision, their
    per-hardware variants and custom ones) and **Sources** (admins only: GGUF URL hosts, `local://`
    directories, the Hugging Face token). The console says *engine*; the API and the recipe files keep
    *recipe* (`/api/recipes`). An engine's card is short: its version, what it is, the hardware it runs
    on (NVIDIA, AMD, CPU, *shares a GPU*) and what it serves; the variants, images, requirements and
    offline commands are behind **Details**. Admins **Add a custom engine** (its recipe YAML, optionally
    started from a copy of a built-in engine under a free name, the server's refusal shown at the
    field) and **Edit** or **Delete** a custom one (`PUT` / `DELETE /api/recipes/<name>`; refused while
    a deployment uses it). A catalog row's source is short (the repository and, for a GGUF file, its
    quantisation such as `Q4_K_M`; a URL's host and file; a directory's name), the full source and
    revision in its tooltip, and its actions are stacked so none is clipped. The *Routes and cloud* tab of 1.3 is gone: most of its routes only
    repeated a deployment.
  - **A deployment's page holds its cloud fallback**: the tiers and the spill rules that were
    *Routing and cloud spill*, edited in place by admins and read-only for everyone else, plus the
    other names it also answers as. In the Serving list, a deployment with a cloud tier shows
    *→ <provider>*.
  - **Providers is now Cloud** (`/cloud`), with two tabs: **Providers**, and **Cloud models**, the
    names an admin made for a provider's model (*New cloud model*). A name an admin made for
    deployments only (several behind one name, or one under a second name) is listed there too, under
    *Other names for deployments*. A cloud model's page is `/cloud/models/<id>`, a provider's
    `/cloud/providers/<id>`.
  - **A model is seen under Serving only**: its deployment's page has its replicas, output and events,
    and no longer says *Runs as workload X*; an old link to a model's workload page opens the
    deployment. **Machines** still shows everything that holds a card: its tiles, memory map and
    *Running here* name models (linked to the deployment) and apps (named, not linked: #88).
  - **The Playground has one model picker and no tabs** (#100). The ready models (as before: a ready
    replica, or a cloud model the person may use) are listed grouped by kind with Serving's labels and
    icons (Chat, Multimodal chat, Embeddings, Rerank, Speech to text, Text to speech, Images, Video,
    Vision), each with its task (vision) or *local, then cloud* / *cloud only*; models with no ready
    replica are no longer listed. Picking one opens its panel, and the address keeps it
    (`/playground?model=<name>`, without a step in the history). The **Vision** tab is gone: its address
    `/playground?tab=vision` opens the first ready vision model, or the picker when there is none. With
    nothing ready, the page says so, names how many deployments are not ready (linking Serving), and
    sends admins and operators to **Models → Catalog**. The playground's own API is unchanged.
  - The menu is Machines, Serving, Models, Cloud (admins), Playground, API keys, Usage, Users and Audit
    log (admins), and the pages are numbered 01 to 09 in that order. "Route" and "workload" left the
    console's navigation.
  - 1.3's links open the new places, without a step in the history: `/models/deployments/<id>` (and
    `/edit`, keeping `#cloud-fallback`), `/models/deploy` (keeping `?model=` and `?machine=`),
    `/deployments`, `/models?tab=deployments` and `?tab=routes` (Serving), `?tab=catalog` (the
    Catalog), `/models/routes/<id>`, `/providers` and `/providers/<id>`, and `/workloads…` (a model's
    workload: its deployment; anything else: Machines). Setup-health findings
    link to Serving, a deployment's page or its cloud fallback, **Models → Sources** (the Hugging Face
    token, settings still read from `.env`), a cloud model or `/cloud/providers/<id>`, and show on those
    pages; Get started's steps open the Catalog and **Serving → Deploy a model**.
- **A deployment's workload is stopped, started and deleted only through its deployment** (#36):
  `POST /api/workloads/<id>/stop`, `…/start` and `DELETE /api/workloads/<id>` answer `409
  managed_by_deployment` (naming the deployment) for a model deployment's workload, which the console
  shows only as its deployment. Deleting the workload used to delete the deployment and its route with it;
  stopping it left the deployment's page out of step.
- **Revoked keys leave the budget lists and alerts** (part of #36): Usage → Cost (*Budgets this
  month*), the API keys page's budget meter and warnings, `GET /api/budgets`, and the 80% / 100%
  alerts of a key revoked before its alert was written. What a revoked key spent still counts in its
  person's and its provider's budgets.
- **API keys: revoked keys folded away, shorter rows** (#105). On a server with 172 keys, 163 of them
  revoked, the page was about 19,000 px tall on a desktop and 75,000 px on a phone. UI only; the API
  is unchanged.
  - The list shows the keys that still work. Revoked and expired keys are folded away under one row,
    **Show N revoked keys** (or *expired*, or *revoked or expired*), which opens them below the active
    ones and folds them again (**Hide revoked keys**). The same in *Mine* and *Everyone's*; switching
    between the two folds them again. When every key is revoked, the page says there are no active
    keys and still offers the row. Usage and the audit log still name them.
  - *Limits* reads short: only the limits that are set ("3 requests/min", "10k tokens/min"), or
    *No limits*; the exact values are in its tooltip, and a lock stays next to its limit.
  - Phones: every value of a key stays in its value column (the token limit, the key's prefix and a
    revoked key's "by …" used to wrap under the labels), and a revoked key's card has no empty action
    strip.
  - Machines: the summary above the tiles counts machines, not cards: *1 NVIDIA machine*, *1 AMD
    machine*, *2 CPU-only machines* (*AMD GPU* and *AMD APU* when a server has both).

### Deprecated

- **Apps: their API, the templates and `/x/`** (#88), removed in 1.5 (#89) with a migration that removes
  the apps left. Forgeyard is about running and managing models; an app (a workload that is not a
  model's) drifts from that. Until 1.5 they keep working, and say so:
  - the apps part of `/api/workloads` (create; read, expose, stop, start, delete and the logs of an app;
    the list while it holds an app), `GET /api/workloads/templates`, `/x/<name>/…` and the console's
    `POST /api/playground/vision/<name>/<task>` answer with `Deprecation: @1794787200` (RFC 9745) and
    `Link: </guide/en/reference#apps-deprecated>; rel="deprecation"`, errors included; no `Sunset` yet;
  - the server log warns once an hour at most per kind (apps, templates, `/x/`), naming the request;
  - **Setup health** warns (`apps-deprecated`) while any app exists, naming them.

  A key's `workloads` and `workload_list` settings stay in the API until 1.5. A model deployment's
  workload is not deprecated: it is how models run (fine-tuning in 1.9 will run the same way). Use a
  model deployment instead; a model no built-in engine runs gets a custom engine (**Models → Engines**).

### Removed

- **Apps in the console** (#88). The Apps pages (`/apps`, an app's page, New app and its templates), a
  key's **Apps at /x/** setting on API keys, and the exposed vision apps in **Playground → Vision**
  (vision models replace them, #86) are gone. `/apps…` and 1.3's `/workloads…` open a model's
  deployment, or Machines. **Machines** still names an app that holds a card (not linked), so a card in
  use is never shown free. `GET /api/keys` no longer lists the apps a key could name (`workloads`), and
  `GET /api/playground/vision/options` no longer lists the exposed vision apps (`workloads`).
- **The seven settings deprecated in 1.3.1 are no longer read** (#78): `SECRET_KEY_FILE`,
  `AGENT_HEARTBEAT_SECONDS`, `FORGEYARD_SETUP_TOKEN`, `USAGE_FLUSH_SECONDS`,
  `PROVIDER_COOLDOWN_SECONDS`, `PROVIDER_RETRY_AFTER_CAP` and `OPENROUTER_MODELS_URL`. The behaviour
  is the fixed one they already documented: the heartbeat is a third of `AGENT_OFFLINE_SECONDS` (at
  least 2 s), usage is written every 5 s, a failed provider target is tried last for 30 s (its
  `Retry-After` up to 5 min), a `Retry-After` up to 2 s is waited for once, prices come from
  OpenRouter's public list, the key file is `FORGEYARD_SECRET_KEY_FILE` and the setup token is the
  generated one.
  - One still set, in the environment or `.env`, is not ignored silently: the server logs
    *X is no longer read. X was removed in 1.4, so its value is ignored. Use Y instead …* at start,
    and Setup health lists it (`setting-removed`, which replaces `setting-deprecated`;
    `setup-token-left` is gone with its setting).
  - **Upgrade step:** rename `SECRET_KEY_FILE` to `FORGEYARD_SECRET_KEY_FILE`. Set alone to a key
    file other than `data/secret.key`, it now stops the start with a message that says so, and
    `secrets reset` refuses too, rather than the server looking for (or making) another key and
    calling the real one lost.

### Fixed

- **A pull that failed because the registry could not be reached is read as such, however long the engine's
  message.** Docker 29 puts the cause ("…: no such host") after the image reference twice over; the agent cut
  the message at 300 characters before reading it, so the cause was lost: the console never showed it and
  "Internet access: Auto" never switched to the server. Messages are kept whole until read, and the reason
  shown keeps their start and their end.
- **An error the agent did not expect while starting a replica is now that replica's reason** in the console
  ("the agent failed while starting it (…)"), followed by the usual backoff. It used to read "ended while the
  agent was not watching", with the cause only in the agent's log.
- **`agent init` and `install.sh` no longer write an invalid `agent.toml` when a tool's message has several
  lines.** A note in the file quoted, for example, `podman info`'s multi-line error on a host without a shared
  mount; its second line was not a comment, so the file was not valid TOML and the install stopped at
  "writing … agent.toml failed". Every note and header line is now folded into comment lines.
- **Stopping a replica's key guard no longer waits for a client that stopped reading.** Closing a guard
  (the agent stops, a replica is removed) used to close each stream gracefully, up to 2 s per side plus 5 s
  for the listener; it now aborts every open stream first, so it takes well under a second.
- **Usage's tables no longer go on for pages** (#82). With about 100 models the page was some 48,000
  px tall on a phone: every row wrapped its name over three or four lines and kept every column.
  - Every table on Usage (by model, key, person, day, source, local or cloud; on **Cost** by day,
    key, person, model, provider) lists its top 15 rows by the column it is sorted by, and adds up
    the rest in one last row, *N others* (latencies do not add up, so that row has none); **Show all
    N** lists every row. A column's heading sorts by it, again for the other way round. **Cost** is
    sorted by cost, a table by day starts with the latest day.
  - On a phone (760 px or less) each row is one line: the name, cut short with the whole name in its
    tooltip, and the number it is sorted by. Tapping it (or Enter) opens the other columns under it;
    **Sort by** chooses the order.
  - At desktop widths, a long name stays on one line (cut short, the whole name in its tooltip), and
    a table with more columns than fit scrolls sideways inside its frame instead of cutting off the
    last ones (Busy, Avg, p50 and p95, once images, video or workload traffic were counted).
  - With the data of a server with 88 models (Requests, by model, 7 days): 48,681 → 2,514 px tall at
    390 px, 7,561 → 2,075 px at 1,440 px; Cost by model 23,607 → 2,830 px at 390 px.
- **The catalog's Model column no longer collapses** to a few characters at desktop widths: the model
  library's table keeps its Model, Architecture and Recipes columns readable and fits at 1440 px.
- **A long command no longer fills the Workloads list on a phone**: at narrow widths it shows three lines
  and an ellipsis; the whole command is in its tooltip.
- **Tests that failed now and then on CI** wait for the state they need instead of a fixed time, or
  for the data a page fetches instead of reading it at once (image queue, video jobs, the route editor,
  Speak, a workload's output, and others found the same way). Test machines are heard from when made,
  so the liveness sweep no longer takes them offline in a slow test, and a killed process group is
  waited for until it is gone. `npm run test:jitter` runs the UI tests with every faked API answer
  late and out of order; docs/development.md says how to write tests that do not flake.

## [1.3.3] — 2026-11-15

The READMEs rewritten (what Forgeyard does, how to install it, a quick start), and the Python examples
and console snippets working with the current OpenAI SDK (3.51 and later).

### Changed

- **Both READMEs rewritten** (#32): badges (licence, CI, release, Docker Hub pulls), a one-line pitch,
  a console screenshot, the key features, installing the server and an agent, a quick start with the
  official OpenAI SDK, deployment options, the documentation, and the hardware and models tested
  (unchanged). The long status paragraph became a short "What's new in 1.3". The developer commands
  and the map of the code moved to `docs/development.md` and `docs/development.zh-TW.md`, linked from
  the READMEs and CONTRIBUTING.

### Fixed

- **The Python examples with the OpenAI SDK work with openai 3.51 and later.** That release builds the
  SDK on `httpx2` and no longer installs `httpx`, so `import httpx` in the quick start failed after a
  plain `pip install openai`. The quick start, the troubleshooting page, `docs/https-and-trust.md` and
  the design notes now pass `http_client=DefaultHttpxClient(verify=ca)`, the SDK's own client class,
  which every SDK version since 1.17 has. The console's own snippets (Playground → Code, and the
  *Trust this server* page) do the same; the rerank snippet, which calls `httpx` directly, says to
  `pip install httpx`.

## [1.3.2] — 2026-11-15

A dependency fix for the web build's lint tools: no change to the server, the agent or the console.

### Security

- **`minimatch` ≥ 11.0.1 in the web build's lint tools** (#31): versions 7 to 11.0.0 let an attacker who
  controls a glob pattern blow up memory and CPU. It came in only through eslint and typescript-eslint, which
  run at development time and are not part of the built console. eslint 10.15 uses minimatch 11;
  typescript-eslint 8.72 still asks for 10, so `web/package.json` overrides it to `^11.0.5` until it moves.
  `npm audit` reports 0 vulnerabilities.

## [1.3.1] — 2026-11-15

Change a deployment in place (GPU sharing, context, requests at once, the machine), and see what it
waits for: GPU sharing is pointed out when it would fit, a replica that its machine never picks up
says so, and pulling an engine image shows its progress like a model download, failing only when it
stops making progress. Removing a deployment stops its pull or download at once and says it was
cancelled. Simpler settings: one cloud policy, one server-wide Hugging Face token, agent defaults
that match the installer, and the settings removed in 1.4 deprecated. Also llama.cpp 1.0.0 in every
variant of the `llama-cpp` recipe, with an optional FP8 KV cache.

### Added

- **FP8 KV cache on llama.cpp** (#48): the `llama-cpp` recipe's *KV cache type* (`kv_cache`, under
  **Advanced**, default `f16`) can be `f8_e4m3`, llama.cpp 1.0.0's FP8 cache (`-ctk f8_e4m3 -ctv
  f8_e4m3`, K and V both). The estimate follows it: 1 byte a value instead of 2 (`… x 1 B (f8_e4m3)`),
  so Qwen3-4B at a context of 8,192 needs 576 MiB less a concurrent request (1.125 GiB of KV cache
  becomes 0.5625). The estimate also knows llama.cpp's other cache types for custom recipes (`q8_0` is
  34 bytes for 32 values, …) and refuses a type it does not know rather than guessing. Measured on the
  CPU process variant with Qwen3-1.7B Q4_K_M, a context of 8,192 and 8 threads: peak RSS 2.81 GiB in
  f16 and 2.35 GiB in FP8 (0.46 GiB less; the estimate says 0.44), but generation at 30 tokens/s
  instead of 42. `f16` passes no flag, so every existing deployment runs as before.
- **A recipe variant can refuse parameter values** (`unsupported: {kv_cache: [f8_e4m3]}`): a deployment
  with one of them is never placed on that variant, and the placement says why (`vulkan: kv_cache
  f8_e4m3 is not available on this variant`). The built-in `vulkan` variant refuses FP8: llama.cpp 1.0.0
  has no FP8 code for Vulkan, and llama-server aborted at startup with it (`cannot run the operation
  (SET_ROWS)`, the release's Vulkan build on Mesa's lavapipe). An FP8 deployment on an AMD APU is
  refused (409); a card the ROCm image is built for runs it on `rocm`.
- **Image pull progress** (#28). The agent pulls an engine image itself before `docker run`, through
  the engine's API on its socket (`POST /images/create`, Docker or Podman), and reports it like a model
  download: `pulling the image: 0.80 of 1.90 GiB (42%)`, each layer counted once ("Already exists"
  layers count as done), `3 of 7 layers` while some sizes are not known yet, `unpacking` at the end.
  The deployment and workload pages show a bar. Without the socket (a remote `DOCKER_HOST`, another
  Docker context) it pulls with the CLI, which reports layers only, and keeps the anonymous retry for a
  broken credential helper. A loaded image that matches its recorded ids (#10) is never pulled. A
  failed pull says which layer it was at and how far it got. `forgeyard agent runtimes` says which way
  the agent pulls.
- **`pull_stall_minutes`** (`FORGEYARD_PULL_STALL_MINUTES`, `--pull-stall-minutes`, default 5): an
  image pull fails when no new bytes arrive for that long (three times as long while it unpacks).
- **Change a deployment** (#26): **Edit** on a deployment's page, and `PATCH /api/deployments/{id}`
  for admins and operators. GPU sharing, GPUs per replica, context length, concurrency and the
  recipe's other parameters, the memory override, extra engine arguments, the number of replicas and
  the machine (*Automatic*, or *Pinned to* one machine) can be changed; the model, recipe and names
  stay. The new estimate and placement are checked before saving, with the same refusals as a deploy
  (409 `does_not_fit` unless `wait`), and `dry_run` shows the estimate, where each replica would go,
  what changes and what restarts without saving. Saving restarts the replicas on the new settings:
  several replicas one at a time (the next once the one before it is ready, so the model keeps
  answering; the page shows *updating: 1 of 2 on the new settings*), a single replica stopped and
  started again; changing only the number of replicas, or unpinning, restarts nothing. A stopped
  deployment is only saved. Audited as `deployment.changed`. A replica restarting on its own card
  counts its own memory there as free, so a bigger context that fits once it stops is accepted.
- **GPU sharing is pointed out when it would fit** (#27); chat recipes stay **Exclusive** by default.
  The deploy and edit pages say *This model needs X of a Y card; with Share VRAM the rest can hold
  other models. They will share compute.* when an exclusive model needs half its card or less, with a
  **Use Share VRAM** button; a refusal caused by exclusive cards when the memory is there says *fits if
  both share VRAM* and names the replica holding the card (`share_fits` per machine in the API); and
  Setup health has a note, `deployment-could-share`, for a running exclusive replica that reserves
  less than half of a card of 24 GiB or more.
- **A replica that no agent picks up says so** (#29): placed on a connected machine whose agent has
  not reported it for 2 minutes (after the placement or the agent's last connection), the deployment,
  workload and Models pages say *gpu-01's agent (1.2.0) has not taken this replica for 3 minutes;
  update the agent*, or, for an agent of the server's version, to restart it and look at its log
  (`not_picked_up` per replica in the API).

- **Simpler settings** (#30). One **Cloud policy** on **API keys** instead of two selects: *No cloud*,
  *People may opt in to spill*, *Spill by default*, *Cloud allowed*, or *Custom* (which shows the two
  selects). It stores the same two values as before. A pair that is no preset opens as *Custom*.
- **One Hugging Face token for the server** (#30): admins set it under **Models → Model sources**
  (`PUT`/`DELETE /api/hf-token`, audited as `hf_token.set` / `hf_token.cleared`). It is stored
  encrypted with the secret key like the other secrets and never returned, only whether it is set and
  its last four characters. Inspecting a Hugging Face model uses it, and the agent that downloads a
  Hugging Face model gets it with the download, unless the inspection or the deployment brings its
  own token. A deployment's own token stays an override under **Advanced**, and existing deployments
  keep theirs. It is never sent to an `https://` GGUF host. `secrets reset` drops it when the key is
  lost. Setup health: `gated-model-no-token` (V33).
- **Where a new provider is** (#30): *Outside your network (cloud)* is ticked from the kind and the
  base URL. OpenAI, Anthropic, Gemini, Azure and OpenRouter are outside. An OpenAI-compatible server
  is inside for a private address (RFC 1918, 100.64.0.0/10, loopback, link-local, IPv6 ULA) or a
  local name (a single label, `.local`, `.lan`, `.internal`, `.home.arpa`), outside for a public
  address, and otherwise judged by what its name resolves to on the server. When that cannot be told,
  it stays inside and the form says so. The form shows the reason and admins may change the tick;
  `POST /api/providers/where` gives it, and a provider created through the API without `external` gets
  it. Existing providers are untouched.
- **The deploy form's context length** shows *model max N · largest that fits here M* (#30), each a
  one-click value; the default context stays as it was. `context_hint` in
  `POST /api/deployments/estimate`.
- **Values from `.env` are marked** (#30): until an admin saves **Limits for keys**, **Cloud policy**
  or **Model sources**, those panels say which variables their values come from (`env_seeds` in the
  API).

### Changed

- **llama.cpp b12242 → 1.0.0** (#48) in every variant of the `llama-cpp` recipe: llama.cpp's first
  release with a semantic version (2026-11-13). Its tag is the commit of nightly build b12359, whose
  images and archives are the release's: the CUDA (still `server-cuda` = CUDA 12.8, driver ≥ 570),
  ROCm (7.2.1), Vulkan and CPU images are tagged `server-*-v1.0.0-b12359-b1b770d` and pinned by digest
  (their recorded ids are in `images.lock.json`), and the CPU process variant downloads
  `llama-b12359-bin-ubuntu-x64.tar.gz`, pinned by its SHA-256 (built on Ubuntu 22.04: glibc 2.34 and
  GLIBCXX_3.4.30 at most, as before, so it runs on Ubuntu 24.04 and Debian 13). The Dockerfiles are
  unchanged. Every flag Forgeyard passes is unchanged (checked against the release binary's
  `llama-server --help`, now a test fixture); the removed C API and `--defrag-thold` were never used.
  Text to speech keeps llama-server's own `/v1/audio/speech`, which it already used since b12242 (the
  release notes list it as new since 0.9.0): its request handling is the same at 1.0.0, which only adds
  the server-wide `--tts-lang` and `--tts-voice` defaults (the gateway still sets `lang` per request),
  so the formats (wav, pcm), streaming (`audio` only), the voices from `/props` and the voice-cloning
  refusals at the gateway are unchanged. The MoE expert cache is not used (every layer is offloaded).
  **The 1.0.0 server security fixes** (media URLs to private addresses refused unless
  `--media-allow-private-urls`, CORS and DNS rebinding, MCP tools) **are a further layer here, not the
  protection**: the engine listens on loopback behind the agent's key guard, and the gateway fetches
  media URLs itself and sends only `data:` URIs. `--cors-origins` stays `*`, which keeps the new Host
  check off for the key guard's requests. Measured on the CPU process variant (8 pinned threads),
  b12242 against 1.0.0 with Qwen3-1.7B Q4_K_M: `llama-bench` generates 43.2 and 43.0 against 43.2 and
  43.0 tokens/s (prompt 287 to 305 against 302 to 303); the same chat prompt through llama-server with
  the recipe's flags, 38.6 to 41.5 against 41.5 to 42.6 tokens/s, peak RSS 2.82 against 2.81 GiB.
  Accepted on the CPU process variant (`scripts/accept_all.py` models, gateway and tts groups, with
  balancing over two agents and failover when an engine is killed;
  `accept_mm.py` Qwen3-VL-2B and Qwen2.5-Omni-3B GGUF with image, video and audio; `accept_tts.py`
  Qwen3-TTS 1.7B GGUF and Chatterbox Turbo GGUF); the NVIDIA and AMD Vulkan variants on 1.0.0 are
  still to be accepted on real hardware (they were on b12242).
- **A pull no longer fails after a fixed 30 minutes** (#28). Seen for real: a 1.86 GB image at under
  1 MB/s timed out twice and never started. Only a stall fails it now; `docker run` itself, on the local
  image, has 10 minutes.
- **Protocol:** the server lists the feature `pull_progress`; only then does an agent send an image
  pull as a `download` with phase `image` (`layers`, `layers_done`, `unpacking`) and a reason per
  step. To an older server it says only `pulling the image`, so that server records no event per
  progress report. An older agent works as before, with "starting the container" while it pulls. **The
  agent image must be rebuilt** for real agents to get any of this.
- **A restarted replica goes back to the machine it ran on** (#26) when that machine still takes it,
  instead of the best fit elsewhere: after **Stop** then **Start**, after an edit, and for plain
  workloads too. A stopped, unpinned deployment no longer restarts on a machine that cannot reach its
  model while its old machine has room. The server keeps this in memory; a replica still waiting when
  the server restarts is placed by the usual rules.

- **Agent defaults match the installer** (#30): the model cache is `/srv/forgeyard-models` when the
  agent runs as root (`~/.local/share/forgeyard-models` otherwise), so a machine started without
  `models.cache` now serves models. Process workloads run as `forgeyard-run` when the agent runs as
  root and that account exists; the agent never creates it. `forgeyard agent config` shows both as
  `(default)`. An empty value (`--model-cache ""`, `FORGEYARD_MODEL_CACHE=`, `cache = ""`) still means
  "serve no models". `agent init` writes `~/.local/share/forgeyard-models` for a non-root agent
  (before: `~/.cache/forgeyard-models`).
- **Agents send a heartbeat every third of `AGENT_OFFLINE_SECONDS`** (at least every 2 seconds), 10
  seconds with the default 30, as before (#30).
- **The New user and Register forms ask for no email** (#30): nothing sends mail. The API still takes
  one and the column stays; Account shows it only when there is one.

### Deprecated

These still work in 1.3 and are **removed in 1.4**. The server logs a warning at start naming what
replaces each one, and Setup health lists it (`setting-deprecated`):

- `SECRET_KEY_FILE`: renamed `FORGEYARD_SECRET_KEY_FILE` (with both set, the new name wins).
- `AGENT_HEARTBEAT_SECONDS`: the heartbeat follows `AGENT_OFFLINE_SECONDS`.
- `FORGEYARD_SETUP_TOKEN`: use the printed setup token, or `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
- `USAGE_FLUSH_SECONDS`, `PROVIDER_COOLDOWN_SECONDS`, `PROVIDER_RETRY_AFTER_CAP` and
  `OPENROUTER_MODELS_URL`: fixed at their defaults (5 s, 30 s, 2 s, OpenRouter's public list).

### Fixed

- **A cancelled pull says so** (#28): when a deployment is removed (stopped, changed) while its image
  pulls or its model downloads, the replica's last reason is `cancelled: the deployment was removed`
  (`… stopped`, `… changed`), never the offline advice "the image is not on this machine and could not
  be pulled".
- **Removing a deployment stops its model download at once and releases the model's lock.** Seen for
  real: the download went on writing for some seconds, and a new deployment of the same model waited on
  the lock. The cancel was only set by the next reconcile round, after the agent had looked at every
  running replica (one `docker inspect` each); it is now set as soon as the desired list arrives. It
  also ends a read waiting on a stalled connection (before: up to the 60 s socket timeout; the abort
  also works when the answer closes the connection), a retry's pause (up to 30 s), the checksum of a
  finished file, and a replica's wait for another one's download of the same model. When the agent
  stops, its downloads are cancelled too, instead of running on in their threads.
- **A replica restarted under the same id in a new generation keeps its internal key.** Found with
  #55 on a real run: an edit restarts a replica; the agent stored the new generation's key from the
  desired list, then dropped it while removing the old generation, so the new one waited ("waiting for
  the server …") for good. The agent now forgets a key (and a Hugging Face token) only when the
  replica id is no longer wanted at all. #55 also gives a restarted replica a new id on the server.
- **Model download progress never passes 100%.** Seen for real: `14.54 of 12.93 GiB (112%)` on a
  resume, with 11.66 GiB on disk. The files already complete were counted twice, once in the starting
  total and again as the loop passed them. Progress is now the bytes of that model on disk (finished
  and partial files), each counted once, and the agent, the server and the page also clamp it to the
  total.

## [1.3.0] — 2026-11-14

More kinds of models behind the same gateway: images, audio and video in chat; text to speech at
`/v1/audio/speech`; image generation at `/v1/images/generations`; video generation as jobs at
`/v1/videos`; and any service workload's own HTTP API at `/x/<name>`, with ready-made object
detection, segmentation and depth templates to try in the Playground's Vision tab. Also llama.cpp
b12242 with a built-in Vulkan variant for AMD APUs, built-in recipes on machines without registry
access, setup health checks, the server and agent images on Docker Hub, and `accept_all.py`, a full
regression run on real machines.

### Added

- **Video generation at `/v1/videos`** (#24), OpenAI's Videos API as asynchronous jobs: `POST /v1/videos`
  (JSON, multipart as OpenAI's SDK sends it, or a URL-encoded form: `prompt`, `model`, `seconds`, `size`,
  and `num_inference_steps`, `guidance_scale`, `seed`, `negative_prompt`) answers at once with a job;
  `GET /v1/videos/{id}` polls it (`queued`, `in_progress`, `completed`, `failed`); `GET
  /v1/videos/{id}/content` streams the MP4 (`Range` passed on); `GET /v1/videos` lists the key's own
  jobs; `DELETE /v1/videos/{id}` cancels or deletes. `input_reference` and the other image-to-video
  references are refused (400 `input_reference_not_supported`); thumbnails, `/v1/videos/sync` and remix
  are not offered.
  - **`vllm-omni-video`** (new recipe): diffusers text-to-video pipelines (Wan2.1-T2V-1.3B,
    Wan2.2-TI2V-5B) on NVIDIA with the same `vllm/vllm-omni:v0.32.0` image; the engine keeps its videos
    inside the replica's container, with its own sweep (`FILE_TTL`) behind the gateway's expiry. Frames
    are seconds x the model's frame rate rounded to 4k + 1 (5 s at 16 fps is 81).
  - **Jobs stay on the replica that makes them:** a new table `video_jobs` (migration `0014_video_jobs`;
    no existing row changes) holds the key or person, the deployment, the replica, the engine's job id,
    the status, the size and seconds asked, the timestamps, the expiry and the error; the gateway relays
    status, content and deletion from that replica only, and a server-side sweeper follows the jobs at
    work every few seconds. A job whose replica restarts or goes reports `failed` with the reason
    (`replica_restarted`, `replica_gone`, `video_lost`); a finished video's content then answers 410.
    Jobs survive a server restart. Ids are Forgeyard's own and random; another key's id answers 404.
    The prompt is not stored.
  - **Caps per deployment**, each with a working default: *Default size* (the model's own), *Default
    length* and *Longest video* (5 s), and under Advanced *Largest side*, *Frames per second*, steps,
    *Jobs at once* (8), *Jobs per key* (2; 429 `queue_full` / `too_many_jobs` with `Retry-After`),
    *Keep results* (1,440 minutes; then deleted on the replica, 410 `video_expired`), *Longest job*
    (3,600 s, `timed_out`), *Longest prompt* and *Offload to host memory*.
  - **Usage:** one request per job, recorded when it ends: the video's length in `media_ms` and the GPU
    time in `busy_ms`; a failed job counts as an error only; one deleted while at work as 499. No cloud.
  - **Memory estimate** for video: the weights in 16 bits, the activations per thousand tokens of the
    longest, largest video (latent frames x patches per frame) and one frame's VAE decode; first
    estimates (about 21.6 GiB for Wan2.1-1.3B, 32.8 GiB for Wan2.2-5B). Wan2.1-1.3B used 20.75 GiB on an A100
    (25 of 25 acceptance checks; 832x480 for 5 s in 136 s); Wan2.2-5B is not yet run.
  - **Catalog:** Hugging Face's `text-to-video` is the new `video` task; a diffusers repository naming
    `WanPipeline` is read as a video model with its own size, frame rate and size multiple. A **Video**
    group in the recommended list: Wan2.1-T2V-1.3B (pinned to `0fad780`) and Wan2.2-TI2V-5B, Apache-2.0.
  - **Playground → Video:** a prompt, the sizes and lengths the deployment takes, the job followed until
    it is made, the video played in the page with a download link, and the person's earlier videos;
    through `/api/playground/videos…` with the console's sign-in and CSRF rule, counted as console. A
    deployment's **Try it** asks a replica's video API for its jobs instead of making a video. **Usage**
    shows the video length.
  - `scripts/accept_video.py` (also the `video` group of `accept_all.py`) checks it on a real machine with
    the official SDK: a video made, followed and fetched as a real MP4 (its boxes read: size, frames,
    length), the caps, another key's 404, the per-key cap, deleting, usage, expiry, and a job failing
    when its deployment is stopped under it.
  - Video with stable-diffusion.cpp (AMD, CPU) is not in 1.3: its jobs return WebM, WebP or AVI inside
    the job's JSON, not an MP4 file. No agent change: agents of 1.3 serve it as they are.
- **Generic passthrough at `/x/<name>/…`** (#23): a service workload's own HTTP API behind the gateway,
  for programs with an API key, any method, the prefix stripped and the query kept.
  - **Exposing** a workload (Workloads → New workload → *Gateway*, or later on its page): admins and
    operators; audited. Settings beside the spec, changeable while it runs, the limits collapsed:
    port, read timeout (600 s), largest request (32 MiB), requests at once per replica and per key.
  - **Behind the agent's key guard**, like a model replica: the program on loopback (`${BIND_ADDRESS}`
    in a process's arguments), the guard on the published port, which checks the gateway's internal
    key and **removes** it; health checks never carry it either. Agents announce `workload_guard`;
    an exposed workload is placed only on them. Every port of an exposed workload is guarded.
  - **A key's new *Workloads* setting**: *None* (the default, and every existing key, migration
    `0013_passthrough`), *Listed workloads* or *Every exposed workload*; an admin's value on someone
    else's key is locked, like the cloud setting. 403 `workloads_not_allowed`, 404
    `workload_not_found`.
  - **APIs, not web pages:** `Content-Security-Policy: sandbox`, `nosniff`, `DENY`, `no-store`, CORP and
    `no-referrer` forced on every answer; `Set-Cookie`, `Access-Control-*`, HSTS, `Clear-Site-Data`,
    `Alt-Svc` and `X-Forgeyard-*` dropped; client credentials and forwarded headers replaced by fresh
    `X-Forwarded-*`, `X-Forwarded-Prefix` and `X-Request-Id`; `Location` rewritten under the prefix,
    redirects never followed. Dot segments and encoded separators are refused (400 `invalid_path`), not
    normalised. Answers stream as raw bytes. No WebSockets, gRPC or cloud spill in 1.3.
  - **Limits:** the key's requests per minute, 429 `workload_busy` and `key_busy` with `Retry-After`,
    413 `request_too_large`, 504 `upstream_timeout`; a client that goes away closes the upstream request.
  - **Usage** as the model `x:<name>`: requests, errors, bytes in and out and busy time (new columns on
    **Usage**), latency.
  - `scripts/accept_x.py` (also the `passthrough` group of `accept_all.py`) checks it on real machines.
- **Ready-made workload templates** (#23): **Workloads → Start from a template** fills the new-workload
  form, exposed at `/x/<name>`, on an NVIDIA GPU (shared by memory) or the CPU:
  - *Object detection*: RT-DETRv2 ResNet-18 (`POST /v1/detect`, boxes);
  - *Segmentation*: SAM 2.1 Hiera tiny (`POST /v1/segment` with a point or a box, masks);
  - *Depth estimation*: Depth Anything V2 Small (`POST /v1/depth`, a 16-bit depth PNG).
  All Apache-2.0, shown with each template (no Ultralytics YOLO, AGPL-3.0; no Depth Anything Base or
  Large, CC-BY-NC). They run Forgeyard's own **vision server image** (`images/vision`): the models baked
  in at pinned revisions, dependencies pinned with hashes, an unprivileged user; published with each
  release as `forgeyard-vision:X.Y.Z` (CPU, amd64 and arm64) and `X.Y.Z-cuda` (amd64, NVIDIA driver
  580+), built and smoke-tested in CI. `GET /api/workloads/templates`. `accept_x.py --templates` (and
  `accept_all.py --x-templates`) check them on a real machine: an image in, boxes, masks and depth out.
- **Playground → Vision** (#47): try an exposed vision workload from the console, as yourself, without an
  API key. Pick a workload (its task and model), drop or choose a picture (PNG, JPEG or WebP, up to the
  workload's limit), then: **detection** draws boxes with label and score, with a score slider;
  **segmentation** takes a click (a point) or a dragged box and lays the mask over the picture, with the
  other masks a button away; **depth** shows the depth map beside the photo, in colour or grey. With no
  exposed vision workload, the tab says how to start one from a template. Errors in plain words.
  - Through the console's own routes, `GET /api/playground/vision/options` and
    `POST /api/playground/vision/<name>/<task>`: the console's sign-in and CSRF rule, anyone signed in;
    only exposed workloads started with `--task detect|segment|depth`, and only their `/v1/<task>` (404
    `workload_not_found`, `unknown_task`). The request then follows the `/x/` rules: key guard, header
    rules, body limit, concurrency, timeouts, usage under `x:<name>` as *console*. `/x/` still takes API
    keys only.
  - At `/x/` too: `key_concurrency` is counted per person for the Playground (it has no key), and the
    console's `X-Forgeyard` header is removed like `X-Forgeyard-*`.

- **Image generation** at `POST /v1/images/generations`, OpenAI's request shape, images back as base64
  only, with two engines:
  - **`vllm-omni-image`** (new recipe): diffusers pipelines (Z-Image-Turbo, Stable Diffusion XL,
    FLUX.1-schnell, Qwen-Image) on NVIDIA with the same `vllm/vllm-omni:v0.32.0` image as text to speech,
    the pixel cap repeated in the engine (`--max-generated-image-size`), VAE slicing, optional model
    offload and VAE tiling.
  - **`sd-cpp`** (new recipe): stable-diffusion.cpp's `sd-server` on AMD GPUs and APUs (Vulkan, the
    render node only), NVIDIA GPUs (CUDA) and the CPU as a process without Docker. Its images are
    published only under rolling tags and are pinned by digest (`master-vulkan` at build 32d516c,
    `master-cuda` at build 3bad892, master-1094, with CUDA 12.6, so driver 560 or newer); the CPU
    process is release `master-1043-3be2378` (later Linux releases need glibc 2.43). The first
    `master-cuda` pin (build 465adf3) was replaced (#43): on an A100 it ran Z-Image-Turbo GGUF at about
    45 s a step and crashed (`unspecified launch failure`). With build 3bad892 the A100 makes a
    1024x1024 Z-Image-Turbo GGUF image in about 31 s.
    A single checkpoint runs with `-m`, a model with separate text encoders and VAE with
    `--diffusion-model`, `--llm` / `--t5xxl` / `--clip_l` and `--vae`; a random seed per request; on a
    GPU `--max-vram` from the reservation.
- **`response_format: url` is refused** (400 `response_format_not_supported`): Forgeyard keeps no copy
  of the images it makes. `stream` is refused too (`streaming_not_supported`).
- **Caps per deployment**, each with a working default: *Images per request* (`max_images`, 4), *Default
  size* (the model's own), and under Advanced *Largest side* (`max_side`, the model's own), *Default
  steps*, *Most steps* (`max_steps`, 50), *Longest prompt* (4,000), *Requests at once*, *Queue length*
  (8), *Timeout* (600 s for vLLM-Omni, 1,800 s for stable-diffusion.cpp), *Offload to host memory* and
  *VAE tiling*. Over a cap: 400 `too_many_images`, `image_too_large`, `invalid_size`, `too_many_steps`,
  `prompt_too_long`, `unsupported_output_format`.
- **A queue per deployment in the gateway:** image requests wait for a free replica in order; with the
  queue full, 429 `queue_full` with `Retry-After`; a request that waits longer than the deployment's
  timeout, 503 `queue_timeout`. Image requests get the deployment's own read timeout.
- Only fields Forgeyard knows reach the engine (`negative_prompt`, `num_inference_steps`,
  `guidance_scale`, `seed`, as fields for vLLM-Omni and as `<sd_cpp_extra_args>` for sd-server, which
  clients may not send themselves); vLLM-Omni's `lora` path, `extra_params` and system prompts never do.
  The answer keeps OpenAI's shape without the engines' extras; engine errors come back as
  `engine_error`. `/v1/images/edits` and `/v1/images/variations` answer 404 `endpoint_not_supported`.
- **Usage** counts `images`, their `kilopixels` (read from each image's header) and `busy_ms` (the
  replica's time × the GPUs it holds), shown on **Usage**. No tokens; local only (the new
  `openai-images` routes take no provider); no prompt filtering.
- **Catalog:** the new `image` task (Hugging Face's `text-to-image`) and `diffusers` format; the
  inspector reads a pipeline's components from model_index.json and their safetensors headers, and a
  GGUF / safetensors repository for stable-diffusion.cpp with its text encoder and VAE files found by
  name (or named, `components`). Estimate method `diffusers`: the components (in 16 bits for vLLM-Omni),
  activations for the caps, one VAE decode; model offload.
- **Recommended models:** an *Images* group with Z-Image-Turbo, Stable Diffusion XL base 1.0 and
  FLUX.1-schnell (gated) for vLLM-Omni, Z-Image-Turbo Q8_0 and FLUX.1-schnell Q4_0 GGUF for
  stable-diffusion.cpp, and Stable Diffusion 1.5 Q8_0 GGUF for the CPU; Apache-2.0 and OpenRAIL
  licences only, pinned to commits.
- **Playground: Image**, for image models: a prompt, the sizes the deployment takes, how many, and the
  images with a download link each. A deployment's **Try it** makes one 256x256 image and shows its size.
- The agent unpacks `.zip` engine releases (feature `engine_zip`); a `.zip` engine goes only to agents
  that list it, and an engine without an API key of its own (`sd-cpp`) only to agents with the key guard.
- `scripts/pin_images.py` keeps an image pinned on a rolling tag (it reports the move as information)
  unless `--move-rolling`.
- `scripts/accept_img.py`: the real-hardware acceptance of image generation with the official SDK
  (images that decode at the size asked, checked without Pillow; n; the caps; url refused; the queue;
  usage; the key guard; what the image shows, through a chat model that reads images), and an `img`
  group in `scripts/accept_all.py`.

- **Text to speech** at `POST /v1/audio/speech`, OpenAI's request shape, with three engines:
  - **`vllm-omni-tts`** (new recipe): Qwen3-TTS CustomVoice on NVIDIA with vLLM-Omni 0.32.0
    (`vllm/vllm-omni:v0.32.0`, pinned by digest), nine built-in voices, Chinese (with Mandarin
    dialects), English and eight more languages, style `instructions`, wav, pcm, flac, mp3 and opus,
    raw and SSE streaming. Its two stages (talker and audio decoder) share one card: the estimate adds
    the decoder (`codec_overhead`, 4.5 GiB) and each stage gets its share of the reservation through
    `--stage-overrides`.
  - **`llama-cpp`** now also serves `tts`: a TTS GGUF with its projector (Qwen3-TTS, Chatterbox) on
    llama.cpp b12242's `/v1/audio/speech`, on every variant (AMD Vulkan and ROCm, NVIDIA, the CPU),
    wav and pcm, raw streaming. The gateway sets `lang` from the text's script and `n_predict` from
    its length when a request leaves them out.
  - **`speaches-tts`** (new recipe): Kokoro-82M and Piper voices (a new model format, `onnx`) with the
    same speaches 0.8.3 images as faster-whisper, CPU and CUDA, mp3, flac, wav and pcm. English only:
    its released versions delete Chinese, Japanese and Korean characters, so such input is refused
    (400 `language_not_supported`) instead of answered with silence.
- **Voice cloning is blocked:** reference recordings (`ref_audio`, `speaker_ref_b64`, …), a voice given
  as audio or a link, Qwen3-TTS's `task_type: "Base"` and uploading or deleting voices answer 400
  `voice_cloning_not_supported`; nothing reaches an engine.
- The gateway checks a speech request before sending it (input, format, `speed`, `stream_format`),
  sends each deployment only what it can do (400 `unsupported_response_format`,
  `stream_format_not_supported`), caps the input per deployment (**Longest input**,
  `max_input_chars`, under Advanced: by default a quarter of the context for vLLM-Omni and llama.cpp,
  4,096 for speaches; 400 `input_too_long`), fills in the engine's default format, and replaces a
  missing voice or an OpenAI voice name the model lacks with its default voice, named in the new
  `X-Forgeyard-Voice` header. The audio streams through as the engine makes it.
- **Usage** counts the characters spoken (`characters`) and the audio's length (`media_ms`, read from
  wav and pcm headers or rates, flac, mp3, aac and opus framing, SSE deltas), shown on **Usage**. A
  key's **tpm counts one token per character** of speech, held before sending. Speech is local only:
  routes of the new `openai-speech` API take no provider.
- `GET /v1/audio/voices?model=` lists a speech model's voices (asked of a replica, kept 5 minutes),
  its default voice, formats, stream formats and input cap.
- **Playground: Speak**, for speech models: text with its character count against the cap, the
  model's voices, its formats, streaming, an audio player and a download link. A deployment's **Try
  it** speaks a short sentence and shows the audio's length.
- **Recommended models:** a *Text to speech* group with Qwen3-TTS 0.6B and 1.7B CustomVoice
  (vLLM-Omni), Qwen3-TTS 1.7B GGUF and Chatterbox Turbo (llama.cpp) and Kokoro 82M (speaches), each
  pinned to a commit, with its languages and licence (Apache-2.0 or MIT; a conversion without a
  licence of its own names its original in `license_from`). The catalog reads Qwen3-TTS's voices and
  talker, and Hugging Face's `text-to-speech` as the new `tts` task.
- `scripts/accept_tts.py`: the real-hardware acceptance of text to speech with the official SDK
  (Chinese and English, streaming, every format, voices, refusals, usage, the key guard, and an
  intelligibility check through speech to text), and a `tts` group in `scripts/accept_all.py`.
- **Engine images on machines without registry access.** A built-in recipe's image can be carried
  over as a file (`docker save` on a machine that can pull it, `docker load` on the one that
  cannot), and the recipe works as it is: no custom recipe naming the image by tag only. Recipes pin
  images as `tag@sha256:…`, which Docker finds by the digest only, and a loaded copy often lacks it
  (Docker's classic store drops it; its containerd store can give the copy an id of its own, seen on a
  real offline AMD APU machine: an id found in no registry). So `scripts/pin_images.py` now also
  records, in `forgeyard/recipes/images.lock.json`, every id a correct copy can show (the index, each
  platform's manifest and config digest) and a content id per platform (layers, platform,
  entrypoint, command, environment, user, working directory, stop signal); its check mode verifies
  them against the registry. When the pinned digest is not local but the tag is, the agent runs that
  image by its local id if it matches, never otherwise. A failed pull says the image is not on the
  machine and how to load it, or that a different image is loaded under the tag. Each recipe on
  **Models → Catalog and recipes** lists the commands for its images
  ([Offline machines](docs/guide/how-to.md#offline-machines)). Protocol: agents that list the new
  `image_ids` feature get the ids beside each replica's spec; older agents get exactly what they got
  before.
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
  before, in `GET /api/usage`. Chat fills `media_items` (**Usage** shows a Media column once there are
  any); speech, images, video and the passthrough fill the others.
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
- `scripts/accept_mm.py`: the real-hardware acceptance of media in chat, through the API and the official
  SDK, with idempotent cleanup.
- **Container images on Docker Hub**, for linux/amd64 and linux/arm64: the server
  (`docker.io/tonyhuang1101/forgeyard`, new `Dockerfile.server`) and the agent
  (`docker.io/tonyhuang1101/forgeyard-agent`). A release tag `vX.Y.Z` publishes both with
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

- **Licences with terms are marked on the recommended list.** OpenRAIL (SDXL, SD 1.5), Gemma and Llama
  cards show the licence in a warning colour, say what it asks, and link to it (the original model's
  page for a conversion). Apache-2.0, MIT and BSD models are unchanged.

### Changed
- `speaches-tts` runs on the CPU only: on a real A100 Kokoro used no GPU memory in speaches' CUDA image
  (ONNX Runtime stayed on the CPU) while the deployment reserved the card; on a CPU it makes speech about
  four times faster than it plays.

- **llama.cpp b11977 → b12242** (2026-11-10) in every variant of the `llama-cpp` recipe: the CUDA
  (still CUDA 12.8, driver ≥ 570), ROCm (7.2.1), Vulkan and CPU images, pinned by digest, and the CPU
  release archive, pinned by its SHA-256. b12242 is the first build with published images after
  llama-server gained the OpenAI-compatible `/v1/audio/speech` endpoint (ggml-org/llama.cpp#31763, in
  b12238), which text to speech uses. Every flag Forgeyard passes is unchanged (checked against
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
- **A machine whose agent disconnected while the server was busy could stay "online"** until the
  offline timeout: when the connection's handler was cancelled (a server shutting down) before it
  recorded the machine offline, that step was skipped. It is now shielded from the cancel.
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
