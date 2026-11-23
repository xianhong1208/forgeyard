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
| 1.5.2 | full regression (`accept_all.py`; <<ALL_SKIPS>> checks skipped) | A100 host, x86-64 CPU machine, Gemini | <<ALL_COUNTS>> |
| 1.5.2 | sleep when idle, waking and automatic replicas (`accept_scale.py`), the automatic replicas on two CPU machines; <<SCALE_SKIPS>> checks skipped | A100 host, x86-64 CPU machine, the A100 host's CPU as the second CPU machine, Gemini | <<SCALE_COUNTS>> |
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

The Ryzen AI MAX+ 395 machine was not part of the 1.4.5, 1.5.0, 1.5.1 and 1.5.2 runs.

Reports from other machines are welcome: see [CONTRIBUTING.md](../CONTRIBUTING.md).
