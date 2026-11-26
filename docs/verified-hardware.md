# Verified hardware

[繁體中文](verified-hardware.zh-TW.md)

Every release is accepted on real machines before it is tagged, not only in CI. This page lists those
machines by hardware model and each release's pass counts, starting with 1.4.3. What ran on each kind of
machine is in the README's [Tested on](../README.md#tested-on).

## How a release is accepted

- **[`scripts/accept_all.py`](../scripts/accept_all.py)**, the full regression run: every shipped feature,
  checked through the server's API, `/v1` with the official OpenAI SDK, and the web console, on a running
  server with real agents and real engines. Each check reports PASS, FAIL or SKIP with its reason; a check
  that cannot run there (for example, too little free memory on a shared card) is skipped, never counted as
  passed. The run removes what it created.
- **[`scripts/accept_vision.py`](../scripts/accept_vision.py)**, the vision models: each one deployed with
  the built-in `vision` recipe and called at `/v1/vision/*` on a known photo, then the refusals and the
  usage it records.
- **[`scripts/accept_scale.py`](../scripts/accept_scale.py)** (1.5.0), sleep when idle, waking and automatic
  replicas: a model goes asleep and frees its memory, requests wake it exactly once, a key that may spill is served
  by the cloud meanwhile, a load adds a replica and a stream on it finishes while it is drained. It is also the
  `scale` group of `accept_all.py`.
- **[`scripts/accept_sso.py`](../scripts/accept_sso.py)** (1.6.0), single sign-on against a real OpenID Connect
  provider in a real browser. It changes server-wide sign-in settings for a few minutes, so it runs on its own, not as
  a group of `accept_all.py`, and puts back everything it changed.
- The other `scripts/accept_*.py` scripts cover single features in more depth (chat with images, audio and
  video; text to speech; image and video generation); [docs/development.md](development.md) lists them.

You can run the same scripts against your own server and machines
([how](guide/how-to.md#check-a-server-end-to-end)). The raw logs of these runs are not published: they
name machines and networks. Only the hardware models and the counts are.

## The machines

| Machine | Used for |
|---|---|
| NVIDIA A100 80GB PCIe, an 8-GPU host shared with other work (Forgeyard limited to its cards by a GPU allowlist) | GPU engines: vLLM, vLLM-Omni, llama.cpp (CUDA), stable-diffusion.cpp (CUDA), speaches, the vision image |
| AMD Ryzen AI MAX+ 395 with Radeon 8060S (Strix Halo APU) | Vulkan builds of llama.cpp and stable-diffusion.cpp, and the CPU; files and images through the server, for a machine without internet |
| x86-64 machine without a GPU | CPU engines as processes and containers |
| Google Gemini (`gemini-2.5-flash`) | the cloud tier: spill, cost and budgets |

## Results per release

| Release | Run | Machines | Passed | Failed |
|---|---|---|---|---|
| 1.6.0 | full regression (`accept_all.py`, all groups). The Ryzen AI MAX+ 395 was offline, so models through the server ran with the x86-64 CPU machine and copying an image to another machine was skipped. 21 checks skipped: 5 deploys of the vision and transformers images on the x86-64 CPU machine, which has no container engine (those engines did not change since the 1.5.4 runs below); FLUX.2 [klein] 4B editing and Wan2.2-TI2V-5B, which did not fit on the shared card at that moment (accepted in the 1.5.5 rows below; nothing in those engines changed); 4 that need a second CPU machine in this script, covered by `accept_scale.py`; an expired key, since the API's shortest expiry is a day, covered by a unit test; a provider budget, since the admin has a budget of their own; Gemini, which has no embeddings model; 2 picture checks that need a chat model that reads images; copying an image to another machine; taking a machine offline, which this script does only when told; and 3 information lines. Nothing in 1.6.0 changes an engine, so no engine was accepted separately | A100 host, x86-64 CPU machine, Gemini | 441 | 0 |
| 1.6.0 | sleep when idle, waking and automatic replicas (`accept_scale.py`) | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.6.0 | single sign-on (`accept_sso.py`) against a real Keycloak: a realm with a confidential client that uses PKCE, a groups mapper and six test accounts. Pending then approved accounts, the role mapping, automatic accounts and a refused outside domain, a disabled account, linking the owner's account, the two-factor exemption, the key backup's fresh sign-in, an API key before and after disabling, refused replayed and foreign callbacks | Keycloak on the A100 host, the server, Chrome | 36 | 0 |
| 1.5.6 | full regression (`accept_all.py`, all groups). The Ryzen AI MAX+ 395 was offline, so models through the server ran with the x86-64 CPU machine. 1 failed: registering a transformers model timed out in the TLS handshake with Hugging Face's download host; the row below re-ran its group. 16 checks skipped: 3 deploys of the vision and transformers images on the x86-64 CPU machine, which has no container engine (those engines did not change since the 1.5.4 runs below); Wan2.2-TI2V-5B, which did not fit on the shared card at that moment (the engine did not change since the 1.5.5 `accept_video.py --image` run below); 4 that need a second CPU machine in this script, covered by `accept_scale.py`; an expired key, since the API's shortest expiry is a day, covered by a unit test; a provider budget, since the admin has a budget of their own; 2 picture checks that need a chat model that reads images; copying an image to another machine, since no other machine with a container engine was online; taking a machine offline, which this script does only when told; and 2 information lines. Nothing in 1.5.6 changes an engine, so no engine was accepted separately | A100 host, x86-64 CPU machine, Gemini | 417 | 1 |
| 1.5.6 | the transformers group of the full regression, re-run (`accept_all.py --only transformers`). 6 skipped: 4 deploys on the x86-64 CPU machine, which has no container engine; Gemini, which has no embeddings model; 1 information line | A100 host, x86-64 CPU machine, Gemini | 37 | 0 |
| 1.5.6 | sleep when idle, waking and automatic replicas (`accept_scale.py`) | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.5 | full regression (`accept_all.py`, all groups). 1 failed: the CPU machine did not finish downloading the CPU image-edit model (FLUX.2 [klein] 4B for stable-diffusion.cpp, 15 GiB) within the 40-minute limit; the row below re-ran its group. 20 checks skipped: 5 deploys of the vision and transformers images on the x86-64 CPU machine, which has no container engine (those engines did not change since the 1.5.4 runs below); Wan2.2-TI2V-5B, which did not fit on the shared card at that moment, covered by `accept_video.py --image` below; 4 that need a second CPU machine in this script, covered by `accept_scale.py`; an expired key, since the API's shortest expiry is a day, covered by a unit test; a provider budget, since the admin has a budget of their own; Gemini, which has no embeddings model; 2 picture checks that need a chat model that reads images; copying an image to another machine, since no other machine with a container engine was online; taking a machine offline, which this script does only when told; and 3 information lines. The Ryzen AI MAX+ 395 was offline, so models through the server ran with the x86-64 CPU machine | A100 host, x86-64 CPU machine, Gemini | 441 | 1 |
| 1.5.5 | the image group of the full regression again (`accept_all.py --only img`), once the CPU image-edit model was in the CPU machine's cache; 2 checks skipped: the picture checks need a chat model that reads images | A100 host, x86-64 CPU machine | 66 | 0 |
| 1.5.5 | sleep when idle, waking and automatic replicas (`accept_scale.py`); 1 check skipped: a CPU model wakes in 3.1 s, sooner than the shortest wait | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.5 | image edits with FLUX.2 [klein] 4B (`accept_img.py --edit`) on vLLM-Omni; 1 check skipped (a picture check, as above) | A100 | 32 | 0 |
| 1.5.5 | image edits with FLUX.2 [klein] 4B (`accept_img.py --edit`) on stable-diffusion.cpp Vulkan, on a machine that reaches no registry: the engine image and the model came through the server; 1 check skipped (a picture check) | Ryzen AI MAX+ 395 | 32 | 0 |
| 1.5.5 | image edits with FLUX.2 [klein] 4B (`accept_img.py --edit`) on stable-diffusion.cpp as a process on the CPU, with the 1.5.5rc1 agent image (with the 1.5.4 agent image the engine exited with code 127); 1 check skipped (a picture check) | A100 host's CPU | 32 | 0 |
| 1.5.5 | a video from a start image with Wan2.2-TI2V-5B (`accept_video.py --image`) on vLLM-Omni: 31.42 GiB on the card after the videos, estimate 32.86 GiB. An earlier run hit CUDA out of memory when other work filled the shared card | A100 | 40 | 0 |
| 1.5.4 | full regression (`accept_all.py`, all groups; 16 checks skipped: 5 deploys on the x86-64 CPU machine, which has no container engine, of the vision and transformers images, covered by the runs below; 3 that need a second CPU machine in this script, covered by `accept_scale.py`; an expired key, since the API's shortest expiry is a day, covered by a unit test; a provider budget, since the admin has a budget of their own; Gemini, which has no embeddings model; and information lines). For the first time since 1.4.4 with the Ryzen AI MAX+ 395: models through the server, and an image copied to it from the A100 host | A100 host, x86-64 CPU machine, Ryzen AI MAX+ 395, Gemini | 433 | 0 |
| 1.5.4 | sleep when idle, waking and automatic replicas (`accept_scale.py`); 1 check skipped: a CPU model wakes in 3.4 s, sooner than the shortest wait | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.4 | the transformers engine (`accept_transformers.py`) on the CPU and on CUDA: image and text embeddings, a fixed-label classifier and zero-shot classification; bge-m3-zeroshot-v2.0-c put 12 of 12 Traditional Chinese lines in the right topic with a Chinese template | A100 host (CPU and CUDA) | 105 | 0 |
| 1.5.4 | the transformers engine (`accept_transformers.py`) on a machine that reaches no registry: the vision image (8 layers, 0.64 GiB) and the models came through the server | Ryzen AI MAX+ 395, on the CPU | 37 | 0 |
| 1.5.4 | vision models with the 1.5.4rc1 image (`accept_vision.py`) | A100 | 40 | 0 |
| 1.5.4 | vision models with the 1.5.4rc1 image (`accept_vision.py`) | Ryzen AI MAX+ 395, on the CPU | 37 | 0 |
| 1.5.3 | full regression (`accept_all.py`; 10 checks skipped) | A100 host, x86-64 CPU machine, Gemini | 394 | 0 |
| 1.5.3 | sleep when idle, waking and automatic replicas (`accept_scale.py`), the automatic replicas on two CPU machines; 1 check skipped | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.3 | during development: the upgrade from 1.5.2 (#260) on the development server's database, with no apps left and 93 API keys with `/x/` access: `check-apps` listed the 93 keys before the upgrade and, from the audit log, after it; the upgrade wrote 93 audit entries; `/x/` answered 410; the model deployments were not affected | A100 host, x86-64 CPU machine | 10 | 0 |
| 1.5.2 | full regression (`accept_all.py`; 15 checks skipped) | A100 host, x86-64 CPU machine, Gemini | 488 | 0 |
| 1.5.2 | sleep when idle, waking and automatic replicas (`accept_scale.py`), the automatic replicas on two CPU machines; 1 check skipped | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.2 | during development: Try this version (#253), vLLM 0.34.0 as a copy pinned by digest, Qwen3-0.6B at a 4096 context, concurrency 4, on a card shared with other work: reserved 5.54 GiB, measured +5.17 GiB, ready in 95 s, chat answered; the built-in vLLM 0.32.0 with the same parameters: reserved 5.54 GiB, measured +5.19 GiB, ready in 112 s. A newer stable-diffusion.cpp build as a copy, its image through the server: Stable Diffusion 1.5 GGUF made a 512×512 image | A100 | 3 | 0 |
| 1.5.2 | during development: a deployment switched to another version of its engine (#255), Qwen3-0.6B, a client calling `/v1` every 0.5 s. With one replica, to the vLLM 0.34.0 copy: the notice said about 109 s without answers, 101.6 s measured; back to the built-in: 97 s said, 104.6 s measured | A100 | 2 | 0 |
| 1.5.2 | during development: the same switch with 2 replicas in the same change, restarted one at a time: calls answered during it (the second replica measured +5.15 GiB of 5.54 GiB reserved) | A100 | 345 | 0 |
| 1.5.2 | during development: trial runs (Try it out, #256), Qwen3-0.6B: on the built-in vLLM, ready in 105 s, 6 of 6 answers, p50 59 ms, +5.19 GiB measured of 5.54 GiB reserved; on the vLLM 0.34.0 copy, ready in 92 s, 6 of 6 answers, p50 76 ms (memory not measured: other work on the shared card released memory meanwhile, said so in words); a trial cancelled while starting ended in 5 s and freed the card | A100 | 3 | 0 |
| 1.5.1 | full regression (`accept_all.py`; 15 checks skipped) | A100 host, x86-64 CPU machine, Gemini | 488 | 0 |
| 1.5.1 | sleep when idle, waking and automatic replicas (`accept_scale.py`), the automatic replicas on two CPU machines; 1 check skipped | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.0 | full regression of 1.5.0rc1 (`accept_all.py`, with its new `scale` group; 15 checks skipped) | A100 host, x86-64 CPU machine, Gemini | 488 | 0 |
| 1.5.0 | sleep when idle, waking and automatic replicas on 1.5.0rc1 (`accept_scale.py`), the automatic replicas on two CPU machines; 1 check skipped | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.0 | during development: the same (`accept_scale.py`; 1 check skipped: a wait for a wake that runs out, on the CPU model, which starts in 3 s, sooner than the shortest wait; the vLLM model covers it) | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | 73 | 0 |
| 1.5.0 | during development: vLLM 0.32's compile cache kept between starts (#232), the engine's part of a restart, mean of 3 or 4 each: Qwen3-0.6B on `vllm` 114 s without it, 57 s with it; a text classifier (xlm-roberta-language-detection) on `vllm-pooling` 78 s and 53 s | A100 | 2 | 0 |
| 1.4.5 | full regression of 1.4.5rc1 (`accept_all.py`, 13 checks skipped), with both machines on the rc agent, so the gateway reached every replica over TLS | A100 host, x86-64 CPU machine, Gemini | 432 | 0 |
| 1.4.5 | during development: full regression after the gateway and agent supervisor were split into smaller parts (#173; 29 checks skipped) | A100 host, x86-64 CPU machine, Gemini | 380 | 0 |
| 1.4.5 | during development: Gemma 4 12B at a 16k context on vLLM 0.32, the KV cache estimate against the engine's own figure (#201): 2.091 GiB estimated, 2.09 GiB in vLLM | A100 | 1 | 0 |
| 1.4.4 | full regression (`accept_all.py`, in two runs: 233 + 163) | A100 host, x86-64 CPU machine, Ryzen AI MAX+ 395, Gemini | 396 | 0 |
| 1.4.4 | zero-shot detection and image classification (`accept_vision.py`) | A100 | 77 | 0 |
| 1.4.4 | zero-shot detection and image classification (`accept_vision.py`) | Ryzen AI MAX+ 395, on the CPU | 71 | 0 |
| 1.4.4 | text classification on vLLM 0.32: three recommended models, the key guard, the Playground | A100 | all | 0 |
| 1.4.3 | full regression (`accept_all.py`) | A100 host, x86-64 CPU machine, Ryzen AI MAX+ 395, Gemini | 303 | 0 |
| 1.4.3 | the web console (`accept_all.py` console group) | the same | 31 | 0 |

The Ryzen AI MAX+ 395 machine was not part of the 1.4.5, 1.5.0, 1.5.1, 1.5.2 and 1.5.3 runs; it took part in 1.5.4.
In 1.5.5 it took part in the image-edit acceptance, not in the full regression. It was offline during the 1.5.6 and 1.6.0 runs.

Reports from other machines are welcome: see [CONTRIBUTING.md](../CONTRIBUTING.md).
