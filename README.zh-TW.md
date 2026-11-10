# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器，之後也包括租用的雲端機器。
每台機器上的 agent 回報硬體；你在上面跑 service、task 與開發環境，並用配方部署模型，統一走一個 OpenAI 相容的 gateway。

> **狀態：里程碑 M0 到 M4，MVP 完成（1.0.0）。** server、資料庫、Web UI 與登入已完成；機器透過 agent
> 加入並回報硬體；可以在機器上跑 service 與 task，以容器或程序執行，而且只拿得到分配給它的 GPU；
> 能從目錄挑模型、用引擎配方部署，依記憶體估算放到合適的機器上；所有部署都在同一個 OpenAI 相容的 `/v1` gateway 後面，
> 有 API key、限流、用量統計與 playground。

[English](README.md)

## 執行

需要 Python 3.13 與 [uv](https://docs.astral.sh/uv/)；建置 UI 需要 Node 22.12+。

```bash
cd web && npm ci && npm run build && cd ..   # UI 建置到 forgeyard/static
uv sync
uv run python main.py                        # http://127.0.0.1:7860，啟動時自動跑 migration
```

第一次啟動時沒有任何帳號，log 會印出**一次性設定碼**（setup token）。打開 `/setup`，貼上設定碼並建立
**owner**。設定碼在完成設定前也存在 `data/secrets.json`（權限 0600），完成後即刪除；沒有它就無法搶先佔用
一台剛裝好的伺服器。若想跳過這頁，第一次啟動前設定 `ADMIN_USERNAME` 與 `ADMIN_PASSWORD`。

登入後 Forgeyard 先開 **Get started**：從剛裝好到第一次呼叫 API 的步驟，依序列出，每一步說明為什麼要做，並有按鈕直接到該做的頁面。
admin 與 operator 有六步：加入機器 → 選 Forgeyard 可以用哪些 GPU → 加入模型 → 部署 → 建立 API key → 呼叫 `/v1`
（附上這台 server 可直接複製的 Python 與 curl 範例）。`user` 角色有三步：建立 key → 呼叫 `/v1` → 看自己的用量。
每一步都依實際狀態自動打勾（有機器在線、GPU 選擇已存、目錄裡有模型、有部署 ready、你有可用的 key、你用它經 gateway 送過請求），
所以這份清單也告訴你還缺什麼。按 **Hide this list** 可隱藏（存在你的帳號上）；之後仍可從帳號選單的 **Get started** 打開；
隱藏或全部完成後，Forgeyard 改為開在 Machines。每個空白頁面都會說明它的用途與下一步該做什麼。

設定來自環境變數或 `.env`，見 [.env.example](.env.example)。`HOST=0.0.0.0` 對 LAN 開放。改用 PostgreSQL：
`uv sync --extra postgres` 並設定 `DATABASE_URL=postgresql+psycopg://…`。

## 機器

每台機器跑 **Forgeyard agent**（同一個 repo，`python -m forgeyard agent`）。由 agent 主動連到 server，
server 不需要能連到機器。

1. admin 在 **Machines → Add machine** 取得一行指令，內含**一次性加入 token**（只能用一次、15 分鐘內有效、
   只顯示一次、伺服器只存雜湊）。
2. 在那台機器的 Forgeyard 目錄裡（先 `uv sync`，NVIDIA 機器加 `--extra nvidia`）執行：

   ```bash
   uv run --no-sync python -m forgeyard agent --server http://<server>:7860 --join-token fyj_…
   ```

   agent 用 token 換到**這台機器專屬的憑證**，存在 state 目錄（root 時是 `/var/lib/forgeyard-agent`，
   否則 `~/.local/state/forgeyard-agent`；可用 `--state-dir` 指定），權限 0600；server 只存它的 SHA-256。
   之後啟動不需要 token：`python -m forgeyard agent`。
3. agent 以 `Authorization: Bearer <憑證>` 開 WebSocket 到 `/agent/v1/stream`，先送完整 inventory，之後每 10 秒
   送一次 heartbeat，只帶會變的值（可用／已用記憶體、使用率、溫度）。硬體有變動時再送一次完整 inventory。

- **Inventory：** NVIDIA 用 NVML（`nvidia-ml-py`）：型號、UUID、compute capability 當作 `arch`（`8.0`）、記憶體、
  driver。AMD 用 ROCm 附的 `amdsmi` 函式庫，沒有時退回 `amd-smi` CLI，再退回 `rocm-smi`；`arch` 是 gfx target
  （`gfx1100`、`gfx1151`）。Strix Halo 這類 APU 會標示 **unified memory**，大小取 VRAM carve-out 與 GTT 中較大者。
  CPU 與系統記憶體一定會列成一筆。某個 collector 找不到東西時會說明原因，不會讓 agent 停掉。這些都是唯讀查詢：
  不配置 GPU 記憶體，也不碰執行中的程序。
- **`python -m forgeyard agent inventory [--json] [--raw]`** 不加入 server，只印出 agent 偵測到的硬體。
- **上線／離線：** 30 秒沒收到任何訊息，server 就把機器標成離線。這是 server 每 2 秒自己檢查一次，所以拔網路線
  或 agent 卡住、TCP 連線看起來還在時也抓得到。agent 會自己重連，指數退避加隨機抖動，最長 15 秒。
- **撤銷與移除**（admin）：憑證立即失效，開著的連線也立刻被關掉（agent 會結束）。登入的人都能看 Machines 頁，
  只有 admin 能新增、撤銷、移除機器。建立 token、加入、被拒絕的加入、撤銷與移除都記在稽核紀錄。
- **傳輸：** Forgeyard 假設在 LAN 或 VPN 上，所以用一般的 `ws://` 即可；要 `wss://` 就把 server 放在 TLS
  reverse proxy 或 Tailscale 後面，並用 `https://` 網址。Host 檢查對 agent 也適用：agent 若用名稱而不是 IP
  連 server，該名稱要列在 `ALLOWED_HOSTS`（或設為 `PUBLIC_URL`）。agent 端點永遠不接受瀏覽器的 session cookie，
  且拒絕任何帶 `Origin` 標頭的請求。
- **模擬 inventory**（只供開發與測試）：`--simulate-inventory nvidia-a100x8 | nvidia-rtx4090x2 | amd-gfx1151-apu |
  <file.json>` 用 fixture 取代真實硬體。必須同時設 `FORGEYARD_AGENT_SIMULATION=1` 才能用，server 會把這種機器標成
  *simulated inventory*。

這個里程碑的真機驗收步驟見 [docs/acceptance-m1.md](docs/acceptance-m1.md)（英文）。

## Workload

**workload** 是要在機器上跑的東西：**service**（一直跑到被停止，有健康檢查、會重啟）或 **task**（跑完就結束，
可重試、有時間上限）。每個 workload 有 1 到 16 個 **replica**，每個 replica 跑在一台機器上。在
**Workloads → New workload** 建立，或用 `POST /api/workloads` 送出 spec（欄位見英文 README 的表格）：

```json
{"name": "web", "kind": "service",
 "runtime": {"type": "container", "image": "python:3.13-slim", "args": ["python", "-m", "http.server", "8000"]},
 "ports": [{"name": "http", "port": 8000}], "health": {"type": "http", "path": "/"},
 "resources": {"cpu": 1, "memory": "512Mi", "gpus": {"count": 0}}, "machine_id": null}
```

- **runtime：** `container`（Docker 或 Podman；`image`，可選 `command` 取代 entrypoint、`args`）或 `process`
  （`command`、`args`、可選 `workdir`）。
- **resources：** `cpu`（核心數）、`memory`（`4Gi`、`512Mi` 或位元組數）、`gpus`：`{"count": n}` 或
  `{"indices": [4]}`（要先選機器），可加 `"vendor": "nvidia"/"amd"`。
- **ports（service）：** agent 從自己的範圍（預設 30000-32767）挑 **host port**，頁面上會顯示位址。程序要聽在
  這個 port：`$PORT`，或在參數裡寫 `${PORT}`／`${PORT_<NAME>}`。
- **health（service）：** HTTP 或 TCP；只顯示 healthy／unhealthy，不會因此重啟。**restart：** `always`（預設）、
  `on-failure`、`never`，重啟間隔 1、2、4… 秒，最多 60 秒。
- **task：** 跑超過 `max_duration` 秒會被停止並判定失敗（不重試）；非 0 結束會重試 `retries` 次，間隔
  `retry_backoff` 秒、每次加倍、最多 5 分鐘。

**放置（M2）：** 你選的機器，或依名稱排序第一台有空間的線上機器：有對應的 runtime、allowlist 裡有足夠的空閒 GPU、
spec 宣告的 CPU 與記憶體放得下（扣掉已在上面的 replica）。GPU 是**獨佔**的：一張卡只給一個 replica。放不下的
replica 會停在 *pending* 並寫明原因，一有空間就自動放上去。workload 也可以宣告每張卡需要的記憶體（`gpus.vram`），
並和同樣宣告的 workload 共用卡（`gpus.share: vram`）；模型部署用 M3 的排程器（見「模型」）。

**權限：** admin 與 operator 可以建立、停止、啟動、刪除 workload，也能看輸出。登入的人都看得到清單、狀態與事件，
但看不到環境變數的值與輸出（可能含機密）。建立、停止、啟動、刪除都記在稽核紀錄。

**每台機器的 GPU allowlist：** admin 在 Machines 頁（**Change GPU access**）選這台機器上 Forgeyard 可以分配
哪些卡，預設全部。不在清單上的卡，不管 spec 怎麼寫都不會被分配，頁面會標成 *not for Forgeyard*。使用中的卡不能
從清單拿掉（先停掉那個 workload），每次修改都記在稽核紀錄，含修改前後。

### 隔離

- **空的環境變數。** 程序只拿到 `PATH`、`HOME`（自己的空目錄）、`LANG`、`USER`／`LOGNAME`、spec 宣告的
  `env`、GPU 與 port 變數，以及 `FORGEYARD_REPLICA`／`FORGEYARD_WORKLOAD`／`FORGEYARD_REPLICA_INDEX`。容器只拿到
  宣告的 `env`、GPU 變數與身分變數，其他由 image 提供。agent 自己的環境變數與主機的憑證絕不會傳給 workload。
  GPU 變數、`FORGEYARD_*`、`PORT*` 不能在 spec 裡宣告。
- **GPU（程序）：** `CUDA_VISIBLE_DEVICES` 與 `NVIDIA_VISIBLE_DEVICES` 放分配到的卡的 **UUID**（CUDA 預設依速度
  排序、nvidia-smi 依 PCI bus 排序，用編號可能指到另一張卡，UUID 不會），並設 `CUDA_DEVICE_ORDER=PCI_BUS_ID`。
  AMD：`ROCR_VISIBLE_DEVICES` 放 ROCr UUID（`GPU-<KFD unique_id>`，沒有時用編號），`HIP_VISIBLE_DEVICES` 放重新
  編號的 `0..n-1`。沒有 GPU 時每個清單都是空的（`NVIDIA_VISIBLE_DEVICES=void`），AMD 上程序也不會拿到裝置的
  群組，由核心擋住 `/dev/kfd`。NVIDIA 程序的隔離是「配合式」的（`/dev/nvidia*` 通常人人可讀）；容器才是強制的。
- **GPU（容器）：** `--gpus "device=<uuid>"`（NVIDIA Container Toolkit）或 CDI `--device nvidia.com/gpu=<uuid>`；
  AMD：`--device /dev/kfd` 加上分配到的那幾個 `/dev/dri/renderD*`。一定會設 `NVIDIA_VISIBLE_DEVICES`（沒 GPU 時
  為 `void`），因為 CUDA image 預設寫 `all`，若主機 Docker 的預設 runtime 是 `nvidia`，否則會看到全部的卡。
- **預設不用 root 跑。** agent 以 root 執行時，程序 workload 以 `--workload-user`（無特權帳號，例如 `nobody` 或
  專用帳號）執行；沒設就拒絕執行程序 workload，Machines 頁會寫原因。agent 不是 root 時，程序以 agent 自己的身分
  執行。容器用引擎預設值，加上 `--security-opt no-new-privileges`，絕不用 `--privileged`。
- **資源限制。** 程序在自己的 process group。systemd 主機上、agent 為 root 時，放進有 `MemoryMax` 與 `CPUQuota`
  的 transient scope；否則只有 rlimit：不產生 core dump，沒有 GPU 的 workload 用位址空間上限限制記憶體（GPU
  runtime 會保留很大的位址空間）。這時 CPU 不會被限制，只在放置時計算。容器用 `--cpus` 與 `--memory`。
- **Port。** 容器的 port 只綁在一個位址：agent 連到 server 時用的本機位址（可用 `--bind-address` 改）。
- 能建立 workload 的人就能在機器上執行程式，所以只開放給 admin 與 operator。

### 期望狀態與 agent

server 為每台機器保存「應該在跑的 replica 完整清單」（spec 與 generation），在 agent 連上時與每次變動時送出
（`desired`）。agent 比對實際在跑的東西：停掉不要的、啟動缺少的、套用重啟／重試／時間上限規則，並回報每個
replica 的狀態（`status`）。它不會收到「啟動 X」這種指令，所以掉訊息、重連或 agent 重啟後都只是再收斂一次。

- **接管（adopt）：** agent 把每個 replica 記在 `<state dir>/replicas/`（pid、核心記錄的啟動時間與 boot id；
  容器名稱）。重啟後的 agent 會找到自己的程序（同一個 pid；pid 被別人重用也不會認錯）與容器（label
  `forgeyard.agent`、`forgeyard.replica`、`forgeyard.generation`），直接接管，不會重複啟動。收到 server 的第一份
  清單之前只觀察、不動作。**停掉 agent 不會停掉 workload**；要停請從 UI 停。
- **輸出：** 程序直接寫到 `<state dir>/logs/<replica>/output.log`，只保留最後 `--log-max-mb` MB（預設 10）；
  容器用引擎的 log driver，額度相同。詳細頁會即時顯示：瀏覽器每 2 秒問 server，server 透過 WebSocket 問 agent。
  replica 結束時，最後 100 行會存在 server 給頁面用。
- **協定：** 仍是第 1 版，以相容方式擴充。agent 在 `hello` 列出 `features: ["workloads"]`，server 在 `welcome`
  回覆自己的；雙方都有時才會傳 `desired`、`status`、`logs_request`／`logs`。M1 的 agent 照常運作（只是不會被
  派工作）；M2 的 agent 連到 M1 的 server 只回報硬體。
- **agent 選項：** `--workload-user`、`--work-dir`（每個程序 replica 一個目錄；預設在 state 目錄旁邊，不在裡面）、
  `--port-range`（30000-32767）、`--bind-address`、`--container-engine auto|docker|podman|none|<路徑>`、
  `--limits auto|systemd|rlimit`、`--log-max-mb`、`--no-workloads`；模型用 `--model-cache DIR`、
  `--model-local-dir DIR`（可重複）與 `--allow-private-model-hosts`。`python -m forgeyard agent runtimes
  [--workload-user …]` 不加入 server，只印出這台機器能用哪些 runtime。

workload 的真機驗收步驟見 [docs/acceptance-m2.md](docs/acceptance-m2.md)（英文）。

## 模型

**模型 → Deploy a model**：從目錄挑模型與配方、設定參數，頁面在建立或下載任何東西之前，就顯示一個 replica 需要多少記憶體、
會放到哪裡。一個部署 = 一個 service workload（也出現在 Workloads）加一條 gateway 路由：程式用它的**模型名稱**從 `/v1` 呼叫
（見「Gateway 與 API key」）。部署頁顯示這個名稱、用量、每個 replica，並有一個直接檢查單一 replica 的小小 **Try it**。

- **加入模型**（admin），在 **Models → Catalog and recipes → Add a model** 有三種方式：
  - **Recommended（推薦清單）**：一份精選清單（[forgeyard/models/recommended.yaml](forgeyard/models/recommended.yaml)，容易修改），
    都是能用內建配方跑的模型，分組：Qwen3（chat 0.6B 到 32B、兩個給 CPU 的 GGUF、Embedding 與 Reranker 0.6B / 4B）、
    其他開放 chat 模型（gpt-oss-20b、Gemma 3 4B / 12B / 27B、Llama 3.2 3B 與 3.1 8B）、語音轉文字（faster-whisper 用的 Whisper small 與
    large-v3 turbo），以及 BGE-M3 與它的 reranker。每一項標示 task、建議的配方、大約下載量與一份需要多少記憶體。Gemma 與 Llama 是
    **gated**：需要一個已在 Hugging Face 接受授權條款的帳號的 token。BGE-M3 用的是 `refs/pr/130`，即 Hugging Face 官方轉出的
    safetensors 版本（main 分支只有 PyTorch 權重）。`python scripts/check_recommended.py` 會用 Forgeyard 自己的檢視與估算逐項對 Hugging Face 檢查。
  - **Search Hugging Face（搜尋）**：由 server 呼叫 `<HF_ENDPOINT>/api/models`（瀏覽器不會直接連 Hugging Face），可依 task 與格式
    （safetensors、GGUF、CTranslate2）篩選，依下載數排序，顯示下載數、按讚數、最後更新、是否 gated，以及 Hugging Face 有提供時的大小。
    結果快取 5 分鐘；每位 admin 每分鐘 30 次。沒有網路時頁面會說明，推薦清單（與直接輸入來源）照常可用。
  - **Enter a source（輸入來源）**，見下一點。

  選定模型後會先檢視它（見下一點），並顯示會用哪個配方、照配方預設值一份要保留多少記憶體（GPU 或 CPU；配方兩者都支援時可切換），
  以及**現在放得下**的機器，或為什麼哪裡都放不下；這些都在加入之前。
- **目錄**（admin）：加入 `hf://<org>/<name>`（可指定 revision；GGUF repo 可指定檔案）、允許主機上的 GGUF 網址，或允許目錄下的
  `local://<dir>`。模型**不下載就先檢視**：固定到哪個 commit、檔案與大小、`config.json`、safetensors header（參數量）或 GGUF
  header（用 HTTP range 讀）。gated repo 的 token 只用在這次檢視，不保存。admin 可以替模型加上各配方的引擎參數
  （Qwen3-Reranker 需要 vLLM 的 `--hf-overrides`）。
- **配方**是 YAML（[forgeyard/recipes](forgeyard/recipes)）：task、格式、每個 task 的協定、參數、估算方法、health check，以及依硬體
  （廠商、NVIDIA compute capability 或 AMD gfx target、driver 版本）分的 **variant**；每個 variant 是固定版本的容器映像，或給沒有
  Docker 的 CPU 機器、由 agent 下載並以 SHA-256 檢查的固定版本執行檔。admin 可在模型頁加自訂配方。內建：

  | 配方 | 服務 | 執行在 | 固定版本 |
  |---|---|---|---|
  | `vllm` | chat（safetensors） | NVIDIA、AMD | `vllm/vllm-openai:v0.32.0`（CUDA 13.0，driver ≥ 580）、`-cu129`（≥ 575）、`vllm/vllm-openai-rocm:v0.32.0` |
  | `vllm-pooling` | embeddings、rerank | NVIDIA、AMD，預設共用 GPU | 同樣的映像，`--runner pooling` |
  | `llama-cpp` | chat（GGUF） | NVIDIA、AMD、CPU（容器或程序） | `ghcr.io/ggml-org/llama.cpp:server-*-b11977`，程序用 release `b11977` |
  | `faster-whisper` | 語音轉文字 | NVIDIA、CPU，預設共用 GPU | `ghcr.io/speaches-ai/speaches:0.8.3-cuda` / `-cuda-12.4.1` / `-cpu` |

- **估算**（每個 replica、每張 GPU）：safetensors = 參數量 × 每參數 bytes（FP8 時 embedding 仍是 16 bit）+ KV cache
  （2 × 層數 × KV heads × head 大小 × context × 同時序列數 × bytes）+ 配方的引擎 overhead + CUDA context（0.4 GiB，在 A100 上量的）；
  GGUF = 卸載到 GPU 的層的實際 tensor 大小 + 這些層的 KV cache + logits buffer + overhead；whisper = 模型檔 + overhead。
  宣告 `vram` 就以它為準（`local://` 模型必須宣告）。
- **排程**：先過濾（有對應硬體與 driver 的 variant、runtime、GPU allowlist、記憶體夠），再評分：**binpack**（剛好塞得下的那張卡優先，
  整張大卡留給大模型），同分時**已經快取該模型**的機器優先。記憶體是 agent 回報的卡上**空閒**量（卡可能被 Forgeyard 以外的工作佔用），
  減掉 Forgeyard 已保留但還沒反映在空閒量裡的部分，再減 `VRAM_HEADROOM_MIB`（512）；絕不用卡的總量。放不下時拒絕部署，
  並列出每台機器的原因與差多少（`wait: true` 則改為排隊等候）。同一個部署的多個 replica 會盡量放在**不同機器**上（優先於 binpack），
  讓 gateway 能在它們之間分流與容錯。
- **共用 GPU**（`share: vram`）：同一張卡上多個 replica，各自被限制在自己的保留量內；共用的卡不會再給獨佔的 replica。
  vLLM 的 `--gpu-memory-utilization` 是卡**總**記憶體的比例，而且**空閒**量不夠就拒絕啟動，所以在共用卡上固定 0.9 永遠起不來：
  Forgeyard 把它設成（保留量 − CUDA context）÷ 總量，無條件捨去。同一張卡上的 replica 一個一個啟動（前一個 healthy 才啟動下一個），
  避免兩個引擎同時依同一份空閒量決定自己的大小。
- **模型快取**在每台機器上（`agent --model-cache DIR`）：以來源 + revision 為 key，目錄結構同 Hugging Face cache，該機器上所有 replica
  共用；由 agent 下載（可續傳、每個檔案以 SHA-256 或 git blob id 驗證、頁面顯示進度），以**唯讀**方式掛進容器（只掛該模型的目錄）；
  對程序也是唯讀檔案。下載和 server 走同一套防護：只接受 https 與公開位址、每次轉址都重新檢查（LAN mirror 用 `--allow-private-model-hosts`）。
- **權限**：admin 管目錄、配方與模型來源；admin 與 operator 部署；所有登入的人都能看部署並試用。

真機檢查：[docs/acceptance-m3.md](docs/acceptance-m3.md) 與 `scripts/accept_m3.py`。

## Gateway 與 API key

每個部署都在 **`http://<server>:7860/v1`** 對外，OpenAI 相容，和 console 同一個 port：`GET /v1/models`、
`POST /v1/chat/completions` 與 `/v1/completions`（串流原樣轉送）、`/v1/embeddings`、`/v1/rerank` 與 `/v1/score`（vLLM 的格式）、
`/v1/audio/transcriptions` 與 `/translations`（multipart）。`model` 填部署的 served name。任何 OpenAI client 都能用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://<server>:7860/v1", api_key="fyk_…")
client.chat.completions.create(model="qwen3-4b", messages=[{"role": "user", "content": "Hi"}])
```

- **API key**（**API keys** 頁）：每個人自己建立；key 是 `fyk_` 加 43 個隨機字元，**只顯示一次**，同時給一段 curl 與 Python 範例；
  server 只存 SHA-256 與 12 字元的前綴。可選：到期時間、可用的模型（不選就是所有部署，包括之後新增的）、每分鐘請求數與 token 數。
  撤銷、到期或帳號停用後，下一個請求就失效。admin 可以看與撤銷所有人的 key、改任何 key 的限制，並設定**預設值**（key 沒設時套用）
  與**上限**（一般人替自己的 key 設的最大值）。建立、修改、撤銷 key 都記在稽核紀錄。
- **`/v1` 只認 API key**：完全不讀 console 的 cookie（網頁沒辦法借用已登入的瀏覽器打 gateway），所以 console 的 CSRF 規則不套用；
  host 檢查（421）照樣套用。不送 CORS header。
- **路由：** 該模型 ready 的 replica 中，經這台 server 進行中請求最少的優先，同分輪流。連線失敗（還沒回任何東西給 client 之前）
  就換下一個，失敗的那個排到最後，直到它的 agent 回報它又 ready（最多 15 秒）。client 中途斷線會關掉往上游的連線，引擎就停止生成。
  逾時：連線 5 秒；等回應 header 或串流的下一段 600 秒（`GATEWAY_CONNECT_TIMEOUT`、`GATEWAY_READ_TIMEOUT`）。
- **錯誤**用 OpenAI 的格式 `{"error": {"message", "type", "param", "code"}}`：401 沒有／錯誤／過期／已撤銷的 key；403 這把 key
  不能用這個模型；404 沒有這個模型；400 這個模型不走這個端點；429 超過限制，附 `Retry-After`；503 沒有 ready 的 replica，附每個
  replica 的狀態；502 連不上 replica 或它拒絕內部 key；504 逾時。引擎自己的錯誤原樣轉回。
- **限制**以 key 計，滑動視窗（最近 60 秒），計數在這台 server 的記憶體裡（多台 server 共用限制之後再做）：每分鐘請求數（被拒絕的
  不算）與每分鐘 token 數（取自回應的 `usage`；串流 chat 由 gateway 向引擎要最後的 usage chunk，client 沒要的話再拿掉）；
  請求進行中先保留它的 `max_tokens`，避免同時送出的請求一起超過限制。
- **用量**（**Usage** 頁）：請求數、錯誤、被限流次數、prompt／completion token、延遲 p50／p95，可依日期、模型、key、人或來源
  （API key 或 playground）分組。每個人看自己的，admin 看全部。部署頁顯示最近 7 天。只統計，不計費。
- **Playground**：在 console 裡用串流 chat，也能試 embeddings、rerank 與語音轉文字；走同一套路由，以登入的使用者身分
  （cookie 與 CSRF，和 console 其他地方一樣）。不建立任何 API key，用量算在「console」。
- **不能繞過 gateway 直接用 replica。** 每個模型 replica 都有一把**內部 key**，每次放置都換新。agent 透過**環境變數**交給引擎
  （vLLM `VLLM_API_KEY`、llama.cpp `LLAMA_API_KEY`、speaches `API_KEY`；配方用 `api_key_env` 指定變數名），絕不放在命令列；
  只存在記憶體，health check 也會帶上；UI、API、spec 與 log 裡都看不到。只有 gateway 與 *Try it* 會送出它。不帶它直接打 replica 的
  port 會得到 401（speaches 是 403）。**vLLM 0.32 只在 `/v1`、`/v2`、`/inference`、`/cohere` 檢查 key**：它的 `/invocations`、
  `/rerank`、`/score`、`/pooling`、`/tokenize` 在 replica 自己的 port 上仍然開著，所以在共用網路上請用防火牆讓 replica 的 port 範圍
  （30000-32767）只對 server 開放。M4 之前的 agent 啟動的 replica 沒有 key，要重新放置才有（更新 agent 後把部署停止再啟動）。

真機檢查：[docs/acceptance-m4.md](docs/acceptance-m4.md) 與 `scripts/accept_m4.py`。

## 帳號與登入

- **角色：** `admin` 管理使用者、機器與其 GPU allowlist、模型目錄、配方與模型來源，並看稽核紀錄；`operator` 執行 workload 與部署模型；`user` 只能看，並透過 playground 與自己的 API key 使用已部署的模型、看自己的用量。
  **owner**（第一個帳號）永遠是 admin，不能被降級、停用或刪除，所以不會有人把管理權整個鎖死。
- **密碼：** argon2id；至少 12 字元；常見密碼（含尾巴加數字或符號的變形）與包含使用者名稱的密碼會被拒絕。
- **新使用者**由 admin 建立，admin 拿到一條**一次性連結**（72 小時）轉交對方，由本人自己設定密碼；
  重設密碼也是同樣的連結（24 小時）。token 放在網址的 `#` 後面，不會進到伺服器 log 或 `Referer`。
- **自行註冊**預設關閉。`REGISTRATION=approval` 時可以申請帳號，會先處於 *pending*，等 admin 核准。
- **Session 存在伺服器端。** Cookie（`HttpOnly`、`SameSite=Strict`，https 時加 `Secure`）只放隨機 token，
  資料庫只存它的 SHA-256。登出、改密碼、停用帳號或 admin 強制登出後，下一個請求就失效——複製走的
  cookie 也不能用。閒置 2 小時、最長 7 天就結束，都在伺服器端檢查。*My account* 可以看到並登出每個 session。
- **限流：** 同一帳號失敗 5 次後，每次重試都要等待（2 秒起倍增，最多 5 分鐘）；同一 IP 15 分鐘內失敗 30 次也要等。
  不會永久鎖定，限流也絕不會把人登出。`X-Forwarded-For` 只採信 `TRUSTED_PROXIES`。
- **請求防護：** Host 不是自己的名稱回 **421**（防 DNS rebinding）；對 `/api` 的寫入必須帶 `X-Forgeyard: 1`，
  瀏覽器有送 `Origin`／`Referer` 時必須同源。
- **稽核紀錄：** 登入、登入失敗與所有帳號變更，含操作者、對象與來源 IP。
- UI 只在 **401** 時跳到登入頁（403、429、5xx 都不會），登入後回到原本那一頁。

## 開發

```bash
uv sync
uv run --no-sync python -m pytest -q          # 後端測試（SQLite）
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # 同一套測試跑在 PostgreSQL 上——會清空該資料庫
cd web
npm run dev                                   # UI 在 :5173，API 轉到 :7860
npm run lint && npm run typecheck && npm test && npm run build
```

資料表變更一律走 Alembic（`forgeyard/migrations`）；啟動時執行 `upgrade head`，model 與 migration 不一致時
會有測試失敗。測試編號（`FY-…`）在整個專案中唯一。
