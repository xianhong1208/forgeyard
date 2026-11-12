# Forgeyard 運作細節

每個部分的設計與行為，給認真營運或開發 Forgeyard 的人。要安裝和使用，請先看[使用指南](guide/zh-TW/README.md)。[English](design.md)

## 機器

每台機器跑 **Forgeyard agent**（同一個 repo，`python -m forgeyard agent`）。由 agent 主動連到 server，
server 不需要能連到機器。

1. admin 在 **Machines → Add a machine** 取得**一次性加入 token**（只能用一次、15 分鐘內有效、只顯示一次、
   伺服器只存雜湊），有三種用法：單行安裝（server 提供的 `install.sh`：裝上與 server 同版本的 agent、寫好
   `/etc/forgeyard/agent.toml`、建立 `KillMode=process` 的 systemd 服務）、agent 的 Docker 映像檔
   （`Dockerfile.agent`，每個版本都會發布，見[容器映像檔](#容器映像檔)），或在 Forgeyard 目錄裡搭配設定檔執行。見[使用指南](guide/zh-TW/quick-start.md#3-add-a-machine)。
2. 不論哪種方式，agent 加入的流程都一樣，例如在 Forgeyard 目錄裡：

   ```bash
   uv run --no-sync python -m forgeyard agent --server https://<server>:7860 --join-token fyj_… \
       --ca-fingerprint sha256:…
   ```

   agent 先下載 server 的 CA，指紋不符就拒絕；之後 `https://` 與 `wss://` 都**只信任這個 CA**（不用系統的清單），
   所以中間人會被拒絕。接著 agent 用 token 換到**這台機器專屬的憑據**，存在 state 目錄（root 時是 `/var/lib/forgeyard-agent`，
   否則 `~/.local/state/forgeyard-agent`；可用 `--state-dir` 指定），權限 0600；server 只存它的 SHA-256。
   之後啟動不需要 token：`python -m forgeyard agent`。
3. agent 以 `Authorization: Bearer <credential>` 開 WebSocket 到 `/agent/v1/stream`，先送完整 inventory，之後每 10 秒
   送一次 heartbeat，只帶會變的值（可用／已用記憶體、使用率、溫度）。硬體有變動時再送一次完整 inventory。

- **Inventory：** NVIDIA 用 NVML（`nvidia-ml-py`）：型號、UUID、compute capability 當作 `arch`（`8.0`）、記憶體、
  driver。AMD 用 ROCm 附的 `amdsmi` 函式庫，沒有時退回 `amd-smi` CLI，再退回 `rocm-smi`；`arch` 是 gfx target
  （`gfx1100`、`gfx1151`）。Strix Halo 這類 APU 會標示 **unified memory**，大小取 VRAM carve-out 與 GTT 中較大者。
  CPU 與系統記憶體一定會列成一筆。某個 collector 找不到東西時會說明原因，不會讓 agent 停掉。這些都是唯讀查詢：
  不配置 GPU 記憶體，也不碰執行中的行程。
- **`python -m forgeyard agent inventory [--json] [--raw]`** 不加入 server，只印出 agent 偵測到的硬體。
- **上線／離線：** 30 秒沒收到任何訊息，server 就把機器標成離線。這是 server 每 2 秒自己檢查一次，所以拔網路線
  或 agent 卡住、TCP 連線看起來還在時也抓得到。agent 會自己重連，指數退避加隨機抖動，最長 15 秒。
- **撤銷與移除**（admin）：憑據立即失效，開著的連線也立刻被關掉（agent 會結束）。登入的人都能看 Machines 頁，
  只有 admin 能新增、撤銷、移除機器。建立 token、加入、被拒絕的加入、撤銷與移除都記在稽核紀錄。
- **傳輸：** `wss://`，用釘選的 CA（存在 state 目錄的 `server-ca.pem`）驗證。用一般 HTTP 加入的 agent（1.0）
  執行一次 `python -m forgeyard agent trust --server https://<server>:7860 --ca-fingerprint sha256:…` 即可切換，
  機器憑據不變；在那之前它會一直離線，log 會說明該怎麼做。`--ca-file` 改為釘選你自己的 CA 檔（公司 CA、TLS proxy）。
  Host 檢查對 agent 也適用：agent 若用名稱而不是 IP
  連 server，該名稱要列在 `ALLOWED_HOSTS`（或設為 `PUBLIC_URL`）。agent 端點永遠不接受瀏覽器的 session cookie，
  且拒絕任何帶 `Origin` 標頭的請求。
- **模擬 inventory**（只供開發與測試）：`--simulate-inventory nvidia-a100x8 | nvidia-rtx4090x2 | amd-gfx1151-apu |
  <file.json>` 用 fixture 取代真實硬體。必須同時設 `FORGEYARD_AGENT_SIMULATION=1` 才能用，server 會把這種機器標成
  *simulated inventory*。

## Workload

**workload** 是要在機器上跑的東西：**service**（一直跑到被停止，有健康檢查、會重啟）或 **task**（跑完就結束，
可重試、有時間上限）。每個 workload 有 1 到 16 個 **replica**，每個 replica 跑在一台機器上。在
**Workloads → New workload** 建立，或用 `POST /api/workloads` 送出 spec：

```json
{"name": "web", "kind": "service",
 "runtime": {"type": "container", "image": "python:3.13-slim", "args": ["python", "-m", "http.server", "8000"]},
 "ports": [{"name": "http", "port": 8000}], "health": {"type": "http", "path": "/"},
 "resources": {"cpu": 1, "memory": "512Mi", "gpus": {"count": 0}}, "machine_id": null}
```

| 欄位 | 內容 |
|---|---|
| `runtime` | `{"type": "container", "image", "command"?, "args"?}`（Docker 或 Podman；`command` 取代 entrypoint）或 `{"type": "process", "command", "args"?, "workdir"?}` |
| `env` | 只有明確宣告的變數（見〈隔離〉） |
| `resources` | `cpu`（核心數）、`memory`（`4Gi`、`512Mi` 或位元組數）、`gpus`：`{"count": n}` 或 `{"indices": [2]}`（要先選機器），可加 `"vendor": "nvidia"/"amd"` |
| `ports` | service：`[{"name", "port"}]`。agent 從自己的範圍（預設 30000-32767）挑 **host port**，頁面上會顯示位址。行程要聽在這個 port：`$PORT`，或在參數裡寫 `${PORT}`／`${PORT_<NAME>}` |
| `health` | service：`{"type": "http" 或 "tcp", "port", "path", "interval", "timeout", "failure_threshold", "initial_delay", "start_period"}`。只顯示 healthy／unhealthy，不會因此重啟任何東西。`start_period` 內（第一次成功之前）的失敗不算 |
| `restart` | service：`always`（預設）、`on-failure`、`never`。重啟間隔 1、2、4… 秒，最多 60 秒 |
| `max_duration`、`retries`、`retry_backoff` | task：跑超過 `max_duration` 秒會被停止並判定失敗（不重試）；非 0 結束會重試 `retries` 次，間隔 `retry_backoff` 秒、每次加倍、最多 5 分鐘 |
| `replicas`、`machine_id` | 要幾份；指定一台機器，或 `null` 表示第一台有空間的線上機器 |

**放置（M2）：** 你選的機器，或依名稱排序第一台有空間的線上機器：有對應的 runtime、allowlist 裡有足夠的空閒 GPU、
spec 宣告的 CPU 與記憶體放得下（扣掉已在上面的 replica）。GPU 是**獨占**的：一張卡只給一個 replica。放不下的
replica 會停在 *pending* 並寫明原因，一有空間就自動放上去。workload 也可以宣告每張卡需要的記憶體（`gpus.vram`），
並和同樣宣告的 workload 共用卡（`gpus.share: vram`）；模型部署用 M3 的排程器（見「模型」）。

**權限：** admin 與 operator 可以建立、停止、啟動、刪除 workload，也能看輸出。登入的人都看得到清單、狀態與事件，
但看不到環境變數的值與輸出（可能含機密）。建立、停止、啟動、刪除都記在稽核紀錄。

**每台機器的 GPU allowlist：** admin 在 Machines 頁（**Change GPU access**）選這台機器上 Forgeyard 可以分配
哪些卡，預設全部。不在清單上的卡，不管 spec 怎麼寫都不會被分配，頁面會標成 *not for Forgeyard*。使用中的卡不能
從清單拿掉（先停掉那個 workload），每次修改都記在稽核紀錄，含修改前後。

### 隔離

- **空的環境變數。** 行程只拿到 `PATH`、`HOME`（自己的空目錄）、`LANG`、`USER`／`LOGNAME`、spec 宣告的
  `env`、GPU 與 port 變數，以及 `FORGEYARD_REPLICA`／`FORGEYARD_WORKLOAD`／`FORGEYARD_REPLICA_INDEX`。容器只拿到
  宣告的 `env`、GPU 變數與身分變數，其他由 image 提供。agent 自己的環境變數與主機的憑證絕不會傳給 workload。
  GPU 變數、`FORGEYARD_*`、`PORT*` 不能在 spec 裡宣告。
- **GPU（行程）：** `CUDA_VISIBLE_DEVICES` 與 `NVIDIA_VISIBLE_DEVICES` 放分配到的卡的 **UUID**（CUDA 預設依速度
  排序、nvidia-smi 依 PCI bus 排序，用編號可能指到另一張卡，UUID 不會），並設 `CUDA_DEVICE_ORDER=PCI_BUS_ID`。
  AMD：`ROCR_VISIBLE_DEVICES` 放 ROCr UUID（`GPU-<KFD unique_id>`，沒有時用編號），`HIP_VISIBLE_DEVICES` 放重新
  編號的 `0..n-1`。沒有 GPU 時每個清單都是空的（`NVIDIA_VISIBLE_DEVICES=void`），AMD 上行程也不會拿到裝置的
  群組，由核心擋住 `/dev/kfd`。NVIDIA 行程的隔離是「配合式」的（`/dev/nvidia*` 通常人人可讀）；容器才是強制的。
- **GPU（容器）：** `--gpus "device=<uuid>"`（NVIDIA Container Toolkit）或 CDI `--device nvidia.com/gpu=<uuid>`；
  AMD：`--device /dev/kfd` 加上分配到的那幾個 `/dev/dri/renderD*`。一定會設 `NVIDIA_VISIBLE_DEVICES`（沒 GPU 時
  為 `void`），因為 CUDA image 預設寫 `all`，而主機 Docker 的預設 runtime 若是 `nvidia`，不覆寫就會看到全部的卡。
- **預設不用 root 跑。** agent 以 root 執行時，行程 workload 以 `--workload-user`（無特權帳號，例如 `nobody` 或
  專用帳號）執行；沒設就拒絕執行行程 workload，Machines 頁會寫原因。agent 不是 root 時，行程以 agent 自己的身分
  執行。容器用引擎預設值，加上 `--security-opt no-new-privileges`，絕不用 `--privileged`。
- **資源限制。** 行程在自己的 process group。systemd 主機上、agent 為 root 時，放進有 `MemoryMax` 與 `CPUQuota`
  的 transient scope；否則只有 rlimit：不產生 core dump，沒有 GPU 的 workload 用位址空間上限限制記憶體（GPU
  runtime 會保留很大的位址空間）。這時 CPU 不會被限制，只在放置時計算。容器用 `--cpus` 與 `--memory`。
- **Port。** 容器的 port 只綁在一個位址：agent 連到 server 時用的本機位址（可用 `--bind-address` 改）。
- 能建立 workload 的人就能在機器上執行程式，所以只開放給 admin 與 operator。

### 期望狀態與 agent

server 為每台機器保存「應該在跑的 replica 完整清單」（spec 與 generation），在 agent 連上時與每次變動時送出
（`desired`）。agent 比對實際在跑的東西：停掉不要的、啟動缺少的、套用重啟／重試／時間上限規則，並回報每個
replica 的狀態（`status`）。它不會收到「啟動 X」這種指令，所以掉訊息、重連或 agent 重啟後都只是再收斂一次。

- **接管（adopt）：** agent 把每個 replica 記在 `<state dir>/replicas/`（pid、核心記錄的啟動時間與 boot id；
  容器名稱）。重啟後的 agent 會找到自己的行程（同一個 pid；pid 被別人重用也不會認錯）與容器（label
  `forgeyard.agent`、`forgeyard.replica`、`forgeyard.generation`），直接接管，不會重複啟動。收到 server 的第一份
  清單之前只觀察、不動作。**停掉 agent 不會停掉 workload**；要停請從 UI 停。
- **輸出：** 行程直接寫到 `<state dir>/logs/<replica>/output.log`，只保留最後 `--log-max-mb` MB（預設 10）；
  容器用引擎的 log driver，額度相同。詳細頁會即時顯示：瀏覽器每 2 秒問 server，server 透過 WebSocket 問 agent。
  replica 結束時，最後 100 行會存在 server 給頁面用。
- **協定：** 仍是第 1 版，以相容方式擴充。agent 在 `hello` 列出 `features: ["workloads"]`，server 在 `welcome`
  回覆自己的；雙方都有時才會傳 `desired`、`status`、`logs_request`／`logs`。M1 的 agent 照常運作（只是不會被
  派工作）；M2 的 agent 連到 M1 的 server 只回報硬體。
- **agent 選項：** `--workload-user`、`--work-dir`（每個行程 replica 一個目錄；預設在 state 目錄旁邊，不在裡面）、
  `--port-range`（30000-32767）、`--bind-address`、`--container-engine auto|docker|podman|none|<path>`、
  `--limits auto|systemd|rlimit`、`--log-max-mb`、`--no-workloads`；模型用 `--model-cache DIR`、
  `--model-local-dir DIR`（可重複）與 `--allow-private-model-hosts`。`python -m forgeyard agent runtimes
  [--workload-user …]` 不加入 server，只印出這台機器能用哪些 runtime。

## 模型

**Models → Deploy a model**：從目錄挑模型與配方、設定參數，頁面在建立或下載任何東西之前，就顯示一個 replica 需要多少記憶體、
會放到哪裡。一個部署 = 一個 service workload（也出現在 Workloads）加一條 gateway 路由：程式用它的**模型名稱**從 `/v1` 呼叫
（見「Gateway 與 API key」）。部署頁顯示這個名稱、用量、每個 replica，並有一個直接檢查單一 replica 的小小 **Try it**。

- **加入模型**（admin），在 **Models → Catalog and recipes → Add a model** 有三種方式：
  - **Recommended（推薦清單）**：一份精選清單（[forgeyard/models/recommended.yaml](../forgeyard/models/recommended.yaml)，容易修改），
    都是能用內建配方跑的模型，分組：Qwen3（chat 0.6B 到 32B、兩個給 CPU 的 GGUF、Embedding 與 Reranker 0.6B / 4B）、
    其他開放 chat 模型（gpt-oss-20b、Gemma 4 E2B / E4B / 12B / 26B-A4B / 31B、Gemma 3 4B / 12B / 27B、Llama 3.2 3B 與 3.1 8B）、語音轉文字（faster-whisper 用的 Whisper small 與
    large-v3 turbo），以及 BGE-M3 與它的 reranker。每一項標示 task、建議的配方、大約下載量與一份需要多少記憶體。Gemma 3 與 Llama 是
    **gated**：需要一個已在 Hugging Face 接受授權條款的帳號的 token（Gemma 4 不需要）。**Vision** 群組（1.3）有給 vLLM 的 Qwen3-VL 2B / 4B / 8B，以及給 llama.cpp、附 projector 檔的 Qwen3-VL 2B / 8B GGUF。**Text to speech** 群組（1.3）有給 vLLM-Omni 的 Qwen3-TTS 0.6B / 1.7B CustomVoice、給 llama.cpp 的 Qwen3-TTS 1.7B GGUF 與 Chatterbox Turbo，以及給 speaches 的 Kokoro-82M；語音項目都固定到某個 commit、列出會說的語言，而本身沒寫授權的轉檔版本會標出它的來源（`license_from`），由 `scripts/check_recommended.py` 讀取來源的授權。**Images** 群組（1.3）有給 vLLM-Omni 的 Z-Image-Turbo、Stable Diffusion XL 與 FLUX.1-schnell，給 stable-diffusion.cpp、附文字編碼器與 VAE 的 GGUF 版 Z-Image-Turbo 與 FLUX.1-schnell，以及給 CPU 的 Stable Diffusion 1.5 GGUF；圖片項目都固定到某個 commit，GGUF 項目會寫出它的 `components`。FLUX.1-dev、SD-Turbo、SDXL-Turbo 與 SD3.5 因授權而不收。每一項都寫明**授權**，清單只收允許商業使用的授權（forgeyard/models/recommended.py 的 `ALLOWED_LICENSES`）：寬鬆授權（Apache-2.0、MIT、BSD）、OpenRAIL 類，以及條款允許商業使用的模型授權。Gemma 的條款（允許商業使用、有會延伸到使用者的禁止用途政策，Google 也保留限制違規使用的權利）與 Meta 的 Llama 3.1 / 3.2 社群授權（月活躍使用者 7 億以下可商用，須標示）留在清單上，標為 gated 並顯示授權；禁止商業使用的授權（CC-BY-NC、僅限研究）不能加入。BGE-M3 用的是 `refs/pr/130`，即 Hugging Face 官方轉出的
    safetensors 版本（main 分支只有 PyTorch 權重）。`python scripts/check_recommended.py` 會用 Forgeyard 自己的檢視與估算逐項對 Hugging Face 檢查。
  - **Search Hugging Face（搜尋）**：由 server 呼叫 `<HF_ENDPOINT>/api/models`（瀏覽器不會直接連 Hugging Face），可依 task 與格式
    （safetensors、GGUF、CTranslate2）篩選，依下載數排序（搜尋 rerank 時也會查 text classification，因為很多 reranker 被歸在那一類，並把名稱含 "rerank" 的排在前面），顯示下載數、按讚數、最後更新、是否 gated，以及 Hugging Face 有提供時的大小。
    結果快取 5 分鐘；每位 admin 每分鐘 30 次。沒有網路時頁面會說明，推薦清單（與直接輸入來源）照常可用。
  - **Enter a source（輸入來源）**，見下一點。

  選定模型後會先檢視它（見下一點），並顯示會用哪個配方、照配方預設值一份要保留多少記憶體（GPU 或 CPU；配方兩者都支援時可切換），
  以及**現在放得下**的機器，或為什麼哪裡都放不下；這些都在加入之前。
- **目錄**（admin）：加入 `hf://<org>/<name>`（可指定 revision；GGUF repo 可指定檔案）、允許主機上的 GGUF 網址，或允許目錄下的
  `local://<dir>`。模型**不下載就先檢視**：固定到哪個 commit、檔案與大小、`config.json`、safetensors header（參數量）或 GGUF
  header（用 HTTP range 讀）。gated repo 的 token 只用在這次檢視，不保存。admin 可以替模型加上各配方的引擎參數
  （Qwen3-Reranker 需要 vLLM 的 `--hf-overrides`）。
  檢視（1.3）也會讀出**授權**（card 的 `license`，"other" 時用 `license_name`，否則看 `license:` tag）、是否 **gated**，以及**文字以外能接受什麼**：有 `vision_config` 或圖片 token 是圖片，有 `audio_config` 是音訊，有影片 token 或 `video_preprocessor_config.json` 是影片（Omni 模型看 `thinker_config`）。GGUF repo 的 **projector 檔**（`mmproj-*.gguf`）不算模型選項：加入模型時一併選一個（預設 F16，其次 BF16、Q8_0、第一個；或不選），讀它的 `clip.has_vision_encoder` / `clip.has_audio_encoder`（在 llama.cpp 裡視覺代表圖片與影片），並和模型一起下載。1.3 之前加入的目錄項目沒有這些資訊：safetensors 視為未知（媒體照樣轉送，由引擎回應），GGUF 視為只有文字。
  **語音模型（1.3）：** Hugging Face 的 `text-to-speech` 對應 `tts` task。Qwen3-TTS 的層數來自它的 `talker_config`（寫出音訊編碼的語言模型；Omni 對話模型仍讀原本的設定），內建聲音來自 `talker_config.spk_id`；由 `model.onnx` 組成的 repo 是 `onnx` 格式（speaches 的 Kokoro 與 Piper；整個 repo 都下載，包括 README，因為 speaches 靠 card 決定怎麼執行），Piper 聲音的取樣率讀自它的 `config.json`。TTS 模型的 projector 是它的音訊解碼器，所以存成不接受任何媒體。
  **圖片模型（1.3）：** Hugging Face 的 `text-to-image` 對應 `image` task。有 `model_index.json` 的 repo 是新的 `diffusers` 格式：它的元件（值為 `[library, class]` 的項目）從 safetensors 檔頭讀出（參數量、bytes、dtype），下載 model_index.json 與各元件的目錄，每個元件只取一組權重（旁邊有 fp16 variant 時取一般檔案；頂層的檔案一律不下載，那裡的單檔 checkpoint 會讓下載量加倍），UNet 的 `sample_size` 決定原生尺寸（否則 1024）。以 `image` task 讀取的 GGUF 或 safetensors repo 是給 stable-diffusion.cpp 的模型：所選的檔案，加上旁邊依名稱找到或由呼叫者指定的檔案（`components`：`vae`、`llm`、`t5xxl`、`clip_l`、`clip_g`）；其中有文字編碼器時模型是分開的（`--diffusion-model`），否則是單一 checkpoint（`-m`）；原生尺寸由名稱判斷（Stable Diffusion 1.x 為 512），否則為 1024。
- **配方**是 YAML（[forgeyard/recipes](../forgeyard/recipes)）：task、格式、每個 task 的協定、參數、估算方法、health check，以及依硬體
  （廠商、NVIDIA compute capability 或 AMD gfx target、driver 版本）分的 **variant**；每個 variant 是固定版本的容器映像，或給沒有
  Docker 的 CPU 機器、由 agent 下載並以 SHA-256 檢查的固定版本執行檔。內建的映像以 **digest 釘選**，寫成
  `repo:tag@sha256:…`：agent 拉取並執行的是 digest，registry 上的 tag 被改掉也改變不了實際執行的東西；介面顯示 tag。
  要升級引擎：改 tag，執行 `uv run --no-sync python scripts/pin_images.py --write`（從 Docker Hub 或 GHCR 取得多架構 digest，
  並檢查每個 variant 宣稱的平台都在），再檢查 diff；不加 `--write` 則檢查每個 digest 是否仍對應它的 tag。內建映像少了 digest
  測試就會失敗。admin 可在模型頁加自訂配方（同樣建議寫 digest）。1.3 為配方加上：`media` 區塊（引擎在對話中接受什麼：`inputs` 為 image、audio、video，以及 `files`）、參數的 `advanced: true`（部署表單收合的 Advanced 區塊）、帶 `if_model` 的 `optional_args`（`mmproj`，或模型接受的某種媒體）與 `{mmproj_file}` 佔位符，估算的 `vision_overhead` / `video_overhead`、文字轉語音用的 `tts` 區塊（引擎能產生的格式與預設格式、串流方式、列出聲音的路徑、預設聲音、gateway 替它補上的請求欄位、能否說中日韓文），以及兩階段引擎用的估算 `codec_overhead` 與 `{gpu_util_main}` / `{gpu_util_codec}` 佔位符。配方新增參數之前建立的部署，會以該參數的預設值產生。內建：

  | 配方 | 服務 | 執行在 | 固定版本 |
  |---|---|---|---|
  | `vllm` | chat（safetensors） | NVIDIA、AMD | `vllm/vllm-openai:v0.32.0`（CUDA 13.0，driver ≥ 580）、`-cu129`（≥ 575）、`vllm/vllm-openai-rocm:v0.32.0` |
  | `vllm-pooling` | embeddings、rerank | NVIDIA、AMD，預設共用 GPU | 同樣的映像，`--runner pooling` |
  | `llama-cpp` | chat（GGUF） | NVIDIA、AMD（Instinct 與 ROCm 映像有編譯的獨立顯卡用 ROCm；APU 與其他 AMD GPU 用 Vulkan，只給 render node）、CPU（容器或行程） | `ghcr.io/ggml-org/llama.cpp:server-{cuda,rocm,vulkan}-b12242` 與 `server-b12242`，行程用 release `b12242` |
  | `faster-whisper` | 語音轉文字 | NVIDIA、CPU，預設共用 GPU | `ghcr.io/speaches-ai/speaches:0.8.3-cuda` / `-cuda-12.4.1` / `-cpu` |
  | `speaches-tts`（1.3） | 文字轉語音（ONNX：Kokoro、Piper），英文 | 只用 CPU（容器）：即使在 CUDA 映像檔裡，ONNX Runtime 也是用 CPU 跑 Kokoro | speaches 的 CPU 映像 |
  | `vllm-omni-tts`（1.3） | 文字轉語音（safetensors：Qwen3-TTS） | NVIDIA，單張 GPU，預設共用 | `vllm/vllm-omni:v0.32.0`（CUDA 13.0，driver ≥ 580），`vllm serve <model> --omni` |
  | `vllm-omni-image`（1.3） | 圖片（diffusers pipeline） | NVIDIA，單張 GPU，預設共用 | 同樣的 `vllm/vllm-omni:v0.32.0` |
  | `sd-cpp`（1.3） | 圖片（GGUF、單一檔案） | NVIDIA（CUDA）、AMD（Vulkan，只給 render node）、CPU（行程） | 以 digest 固定的 `ghcr.io/leejet/stable-diffusion.cpp:master-{cuda,vulkan}`（tag 會持續移動），行程用 release `master-1043-3be2378`（.zip，glibc 2.39） |

  `llama-cpp` 在每個 variant 上也提供 `tts`（附 projector 的 TTS GGUF）。
- **估算**（每個 replica、每張 GPU）：safetensors = 參數量 × 每參數 bytes（FP8 時 embedding 仍是 16 bit）+ KV cache
  （2 × 層數 × KV heads × head 大小 × context × 同時序列數 × bytes）+ 配方的引擎 overhead + CUDA context（0.4 GiB，在 A100 上量的）；
  GGUF = 卸載到 GPU 的層的實際 tensor 大小 + 這些層的 KV cache + logits buffer + overhead；whisper = 模型檔 + overhead。
  宣告 `vram` 就以它為準（`local://` 模型必須宣告）。
  能讀圖片的模型（1.3）會加上配方的**視覺項**：vLLM 加 `vision_overhead`（1.5 GiB：vLLM 啟動時會用最大的圖片跑一次視覺編碼器並保留 encoder cache，之後才決定 KV cache 大小；視覺權重已含在模型權重裡），開啟影片時再加 `video_overhead`（1 GiB）；llama.cpp 加 projector 檔本身（整個載入，預設放到 GPU）與它的 buffer 用的 `vision_overhead`（在 CPU 上量測）。開啟圖片時，估算會註明圖片也會占用上下文 token。
  **兩階段語音引擎（1.3）：** vLLM-Omni 把 Qwen3-TTS 跑成 talker（語言模型，依它的 `talker_config` 像一般 safetensors 模型估算）與音訊解碼器（code2wav）兩段，放在同一張卡上，兩段各是一個 vLLM 引擎，各自檢查自己那份卡總量的 `gpu_memory_utilization`。估算加上配方的 `codec_overhead`（4.5 GiB：解碼器那段，vLLM-Omni 在 A100 40GB 上量到 3.8 GiB，再含它的 CUDA context），產生參數時再把保留量拆開：解碼器拿自己那份當它的比例，其餘給 talker（`--stage-overrides`）。speaches 的 TTS 模型和 Whisper 一樣是模型檔加 overhead。
  **圖片（1.3，method `diffusers`）：** 每個元件的權重（vLLM-Omni 以 16 bit 載入 pipeline，所以 float32 檔案算一半；stable-diffusion.cpp 照檔案原本的格式載入），加上部署允許的最大請求所需的量：引擎一次一起產生請求中所有圖片時（vLLM-Omni）為 `activation_per_mp` × *Largest side* 下的百萬像素 × *Images per request*，一張接一張產生時（stable-diffusion.cpp）則 × 1；再加一次 `vae_per_mp` × 百萬像素、overhead 與 CUDA context。開啟 *Offload to host memory* 時只算最大的元件。vLLM-Omni 的 diffusion pipeline 只有在 paged 模式才讀 `--gpu-memory-utilization`，所以沒有任何東西保留固定的比例：限制記憶體的是上限，由 gateway 強制執行，也在引擎重複設定（`--max-generated-image-size`）。stable-diffusion.cpp 依保留量拿到 `--max-vram`。在 CPU 上量測（stable-diffusion.cpp）：Stable Diffusion 1.5 與 Z-Image-Turbo 都隨圖片大小線性成長，權重之外每百萬像素約 6.3 GiB（它的 VAE 以 32 bit 解碼）。
- **排程**：先過濾（有對應硬體與 driver 的 variant、runtime、GPU allowlist、記憶體夠），再評分：**binpack**（剛好塞得下的那張卡優先，
  讓整張空著的卡留給大模型），同分時**已經快取該模型**的機器優先。記憶體是 agent 回報的卡上**空閒**量（卡可能被 Forgeyard 以外的工作佔用），
  減掉 Forgeyard 已保留但還沒反映在空閒量裡的部分，再減 `VRAM_HEADROOM_MIB`（512）；絕不用卡的總量。放不下時拒絕部署，
  並列出每台機器的原因與差多少（`wait: true` 則改為排隊等候）。同一個部署的多個 replica 會盡量放在**不同機器**上（優先於 binpack），
  讓 gateway 能在它們之間分流與容錯。這是預設行為，一直開著；只有在沒有別台機器放得下時，replica 才會放在同一台。
- **共用 GPU**（`share: vram`）：同一張卡上多個 replica，各自被限制在自己的保留量內；共用的卡不會再給獨占的 replica。
  vLLM 的 `--gpu-memory-utilization` 是卡**總**記憶體的比例，而且**空閒**量不夠就拒絕啟動，所以在共用卡上固定 0.9 永遠起不來：
  Forgeyard 把它設成（保留量 − CUDA context）÷ 總量，無條件捨去。同一張卡上的 replica 一個一個啟動（前一個 healthy 才啟動下一個），
  避免兩個引擎同時依同一份空閒量決定自己的大小。
- **模型快取**在每台機器上（`agent --model-cache DIR`）：以來源 + revision 為 key，目錄結構同 Hugging Face cache，該機器上所有 replica
  共用；由 agent 下載（可續傳、每個檔案以 SHA-256 或 git blob id 驗證、頁面顯示進度），以**唯讀**方式掛進容器（只掛該模型的目錄）；
  對行程也是唯讀檔案。下載和 server 走同一套防護：只接受 https 與公開位址、每次轉址都重新檢查（LAN mirror 用 `--allow-private-model-hosts`）。
- **權限**：admin 管目錄、配方與模型來源；admin 與 operator 部署；所有登入的人都能看部署並試用。

真機檢查：`scripts/accept_m3.py`。

## Gateway 與 API key

每個部署都在 **`https://<server>:7860/v1`** 對外，OpenAI 相容，和 console 同一個 port：`GET /v1/models`、
`POST /v1/chat/completions` 與 `/v1/completions`（串流原樣轉送）、`/v1/embeddings`、`/v1/rerank` 與 `/v1/score`（vLLM 的格式）、
`/v1/audio/transcriptions` 與 `/translations`（multipart）。`model` 填部署的 served name。任何 OpenAI client 都能用：

```python
import ssl

import httpx
from openai import OpenAI

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")   # server 的 CA，從 "Trust this server" 下載
client = OpenAI(base_url="https://<server>:7860/v1", api_key="fyk_…", http_client=httpx.Client(verify=ca))
client.chat.completions.create(model="qwen3-4b", messages=[{"role": "user", "content": "Hi"}])
```

用你自己的公開受信任憑證時，省略 `ca` 與 `http_client`。`curl --cacert forgeyard-ca.pem …` 與 `SSL_CERT_FILE=forgeyard-ca.pem`
也可以。server 用自己的 CA 時，API keys 頁上的範例會自動加上這幾行。

- **API key**（**API keys** 頁）：每個人自己建立；key 是 `fyk_` 加 43 個隨機字元，**只顯示一次**，同時給一段 curl 與 Python 範例；
  server 只存 SHA-256 與 12 字元的前綴。可選：到期時間、可用的模型（不選就是所有部署，包括之後新增的）、每分鐘請求數與 token 數、
  它的**雲端**設定與每月**預算**（見〈雲端供應商與分層路由〉）。撤銷、到期或帳號停用後，下一個請求就失效。admin 可以看與撤銷
  所有人的 key、改任何 key 的限制、雲端設定與預算，並設定**預設值**（key 沒設時套用）
  與**上限**（一般人替自己的 key 設的最大值）。建立、修改、撤銷 key 都記在稽核紀錄。
- **admin 設的限制會鎖定。** admin 替別人的 key 設定每分鐘請求數、token 數、雲端設定或每月預算後，key 的主人就不能再改這個值（403 `limit_locked`）；
  key 上會顯示鎖頭與設定它的 admin。主人仍然可以撤銷這把 key，也可以設定 admin 沒設的那個限制。admin 可以隨時修改鎖定的值，
  清空它就是**解鎖**（回到預設值，主人又能自己設定）。鎖定與解鎖都記在稽核紀錄（`api_key.changed` 的 `locked`／`unlocked`）。
  admin 自己的 key 不會被鎖。
- **`/v1` 只認 API key**：完全不讀 console 的 cookie（網頁沒辦法借用已登入的瀏覽器打 gateway），所以 console 的 CSRF 規則不套用；
  host 檢查（421）照樣套用。不送 CORS header。
- **路由：** 該模型 ready 的 replica 中，經這台 server 進行中請求最少的優先，同分輪流。連線失敗（還沒回任何東西給 client 之前）
  就換下一個，失敗的那個排到最後，直到它的 agent 回報它又 ready（最多 15 秒）。client 中途斷線會關掉往上游的連線，引擎就停止生成。
  逾時：連線 5 秒；等回應 header 或串流的下一段 600 秒（`GATEWAY_CONNECT_TIMEOUT`、`GATEWAY_READ_TIMEOUT`）。
- **`/v1/models`** 只列出符合以下條件的路由：這把 key 看得到它（它有在你網路內的目標，或 key 的雲端設定是 `on`）、key 的模型
  清單包含它，而且它能提供服務：某一層有應該在執行的部署（啟動中、重啟中都算）或啟用中的網路內供應商；或者有這把 key 能用的啟用中
  雲端目標：`on` 的 key 在路由不是 local only 時可以用；`fallback` 的 key 只有在雲端那層之前有本機層時才能用（跟實際呼叫時能用的層
  一樣）。所以停止的模型會從清單消失；`GET /v1/models/{id}` 回 404「…is stopped」，
  硬要呼叫則回 503 `no_ready_replica`「The deployment is stopped.」
- **錯誤**用 OpenAI 的格式 `{"error": {"message", "type", "param", "code"}}`，`type` 依狀態碼而定：400 請求內容不對或這個模型不走
  這個端點；401 沒有／錯誤／過期／已撤銷的 key，或帳號不是啟用狀態；402 預算（見下方）；403 這把 key 不能用這個模型；404 沒有這個
  模型或路徑；413 請求內容超過 `GATEWAY_MAX_BODY_MB`；421 不認得的主機名稱；429 超過限制，附 `Retry-After`；502 連不上 replica 或
  供應商、對方拒絕金鑰、重新導向或失敗；503 沒有 ready 的 replica（附每個 replica 的狀態）、沒有可用的目標，或讀不到本月花費；504
  逾時。每個 `code` 都列在 docs/guide/zh-TW/reference.md，並有測試確保清單完整。引擎自己的錯誤回應以原本的狀態碼與內容轉回（不含它的
  header）。串流開始後才中斷時，最後會送出一個 `upstream_stream_broken` 錯誤事件。
- **限制**以 key 計，滑動視窗（最近 60 秒），計數在這台 server 的記憶體裡（多台 server 共用限制之後再做）：每分鐘請求數（被拒絕的
  不算）與每分鐘 token 數（取自回應的 `usage`；串流 chat 由 gateway 向引擎要最後的 usage chunk，client 沒要的話再拿掉）；
  請求進行中先保留它的 `max_tokens`，避免同時送出的請求一起超過限制。
- **用量**（**Usage** 頁）：請求數、錯誤、因速率限制被拒的次數、prompt／completion／快取／推理 token、溢出的請求數、延遲 p50／p95，
  可依日期、模型、key、人、來源（API key 或 playground）或本機與雲端分組；**Cost** 分頁再依日期、key、人、模型或供應商顯示費用
  （見〈費用〉）。在記憶體裡計數，每 `USAGE_FLUSH_SECONDS`（5 秒）、關機時與讀取前寫入，每個 UTC 日期、來源、key、人、模型與目標
  一列。client 中途離開（499）不算錯誤。每個人看自己的，admin 看全部。部署頁顯示最近 7 天。
- **對話中的圖片、音訊與影片（1.3，** forgeyard/gateway/media.py**）。** vLLM 0.32 與 llama-server（b11977、b12242）會自己下載媒體區塊裡的任何 http(s) 網址，沒有私有位址過濾（vLLM 還會跟隨轉址），而且是從你的網路內部連出去。所以：
  - gateway 走過 `messages[].content[]`，把每個媒體區塊**重建**成同一種形式（`image_url` 與 `video_url` 帶 `data:` URI，`input_audio` 帶 base64），只保留檢查過的欄位；接受 `input_image`、`input_video` 與 `audio_url` 並轉換。文字區塊不變；其他類型與 `*_embeds` 一律拒絕（`invalid_content_part`）；
  - `http(s)` 網址**由 gateway 下載**，用 egress client（forgeyard/gateway/egress.py：`EGRESS_PROXY`、`EGRESS_CA_FILE`、`EGRESS_ALLOWED_HOSTS`）：名稱解析出的每個位址都要檢查，除非 `MEDIA_ALLOW_PRIVATE_HOSTS` 否則只接受公開位址，link-local 與 metadata 位址一律拒絕；連線直接連到檢查過的位址；轉址由 gateway 自己跟隨（最多 3 次），每次重新檢查；每個項目 `MEDIA_FETCH_MAX_MB`、20 秒；內容類型必須與區塊相符（以 `application/octet-stream` 回傳的圖片用 magic number 辨識）。資料取代網址。`file://` 與其他所有 scheme：`media_url_refused`；
  - 引擎本身也加固：`vllm` 與 `vllm-pooling` 帶 `--allowed-media-domains forgeyard.invalid`（清單為空時 vLLM 什麼主機都抓；一個什麼都比對不到的清單就全部拒絕）與 `VLLM_MEDIA_URL_ALLOW_REDIRECTS=0`，且不設 `--allowed-local-media-path`；llama-server 沒有這種開關，只能經由 key guard、以 gateway 送來的資料存取；
  - 路由會縮小（`fit_route`）到能處理請求內容的部署：模型的媒體（來自目錄項目）、配方的 `media.inputs`、部署的 `images`（圖片與音訊片段，4）與 `videos`（0：影片關閉）數量，`files` 只送給配方有宣告的（內建的都沒有：gateway 從不轉換檔案）。否則在下載或送出任何東西之前就回 `media_not_supported`、`too_many_media_items` 或 `file_input_not_supported`；
  - 1.3 中**媒體絕不送往供應商**（雲端與你網路內的都不送）：這些目標會被排除；只由供應商提供的路由回 `media_local_only`；
  - 溢出規則把每個媒體項目算成 1,600 token，而不是它的 base64 大小；
  - 用量會計算媒體項目（`media_items`）；圖片的 token 依引擎回報包含在 `prompt_tokens` 裡（vLLM 帶 `--enable-prompt-tokens-details`）。
- **每個部署的請求大小上限（1.3）。** 部署的 `max_body_mb`（vLLM 與 llama.cpp 為 64 MiB）就是它的上限；沒有這個參數的配方沿用 `GATEWAY_MAX_BODY_MB`。gateway 先讀到所有部署中最大的上限為止（最多每 5 秒從資料庫讀一次），知道模型之後再套用該路由自己的上限，媒體內嵌後再檢查一次（413 `request_too_large`）。
- **用量單位（1.3，migration 0012）。** `usage_daily` 為新模型類型的每種單位各加一欄：`media_items`、`characters`、`images`、`kilopixels`、`media_ms`、`bytes_in`、`bytes_out`、`busy_ms`；預設 0，所以舊的資料列意義不變。多模態對話會填 `media_items`；文字轉語音填 `characters` 與 `media_ms`；圖片填 `images`、`kilopixels` 與 `busy_ms`；其餘由 1.3 後續的類型填入。
- **文字轉語音（1.3，** forgeyard/gateway/speech.py**）。** `POST /v1/audio/speech` 接受 OpenAI 的請求格式。gateway 在送出任何東西之前先檢查（要有 `input`、OpenAI 的格式、`speed`、`stream_format`），並拒絕**聲音複製**：任何參考錄音欄位（`ref_audio`、`speaker_ref_b64` 等）、以資料或連結當作聲音、`task_type: "Base"`，以及上傳或刪除聲音，都回應 400 `voice_cloning_not_supported`（vLLM-Omni 的聲音登錄沒有分使用者，而 `ref_audio` 網址會讓引擎去抓任何東西）。路由會縮小到能處理這個請求的部署：在它的 *Longest input*（`max_input_chars`；0 代表文字和音訊編碼放在同一個上下文的引擎取上下文的四分之一，否則 4,096）之內、格式與串流格式在配方列表裡，而且 speaches 不收中日韓文（它已發布的版本會刪掉這些字）。接著每個部署拿到自己的請求內容：沒指定格式時用它的預設 `response_format`（gateway 才知道回來的是什麼）；沒有聲音、或用了模型沒有的 OpenAI 名稱時，改用配方的預設聲音（或引擎列出的第一個；聲音向副本詢問並保留 5 分鐘），並以 `X-Forgeyard-Voice` 標出；llama.cpp 另依文字的書寫系統補上 `lang`，並補上 `n_predict`（每字元十幀，至少 512，最多到上下文長度）。回應一塊一塊（原始音訊）或一個事件一個事件（vLLM-Omni 的 SSE）地轉給用戶端，並在途中量測長度：wav 與 pcm 看檔頭或標明的取樣率，flac、mp3、aac、opus 看各自的框架，SSE 看片段大小。用量計入有回應的請求的字元數與毫秒數，金鑰的 tpm 以一個字元算一個 token，送出前先預留。語音從不送往供應商（`openai-speech` 的路由不能加供應商目標）。`GET /v1/audio/voices?model=` 以一致的格式列出模型的聲音（不含 vLLM-Omni 上傳的聲音）與它能產生的格式。
- **圖片（1.3，** forgeyard/gateway/imagegen.py**）。** `POST /v1/images/generations` 接受 OpenAI 的請求，只以 base64 回應：`response_format: url` 回應 400 `response_format_not_supported`（圖片不保留，所以沒有網址），`stream` 也一樣。每個部署都有上限，各有能用的預設值（*Images per request* 4、*Largest side* 為模型自己的、*Most steps* 50、*Longest prompt* 4,000）；路由會縮小到請求符合上限的部署，否則回應最能說明問題的 400（`too_many_images`、`image_too_large`、邊長不是 16 的倍數（UNet 為 64）時的 `invalid_size`、`too_many_steps`、`prompt_too_long`）。引擎只拿到 Forgeyard 認得的欄位：對 vLLM-Omni 以欄位傳送並設 `response_format: b64_json`（它的 `lora` 會載入伺服器上的路徑）；對 sd-server 則放在提示裡、由 gateway 產生的一個 `<sd_cpp_extra_args>` 區塊（跳脫過、單行），用戶端不能自己送。沒指定尺寸時用部署的預設值，而且一定會送出。**佇列：**一個副本接 `concurrency` 個圖片請求（1）；其餘依序在 gateway 裡等待（每個路由與層級各有號碼牌，只有第一個能拿空出的位子），最多 `queue` 個（8）；再多一個就回應 429 `queue_full`，Retry-After 依請求耗時的移動平均計算；等待超過部署 `timeout` 的請求回應 503 `queue_timeout`；用戶端離開的請求會離開佇列，絕不會送到引擎。引擎的回應整個讀完，去掉額外內容（sd-server 的 `generation_info`、vLLM-Omni 的各階段耗時），每張圖片的尺寸從檔頭讀出：用量計入 `images`、`kilopixels` 與 `busy_ms`（副本的時間 × 部署占用的 GPU 數）。引擎錯誤以 OpenAI 的格式傳回（`engine_error`）。不計 token、不送雲端、不過濾提示。`/v1/images/edits` 與 `/variations` 回應 404 `endpoint_not_supported`（vLLM-Omni 的編輯路徑會去抓給它的任何網址；留待之後的版本）。
- **Playground**：在 console 裡用串流 chat（也能附上圖片，以 `data:` URI 送出），也能試 embeddings、rerank、語音轉文字、文字轉語音（**Speak**：模型的聲音與格式，附播放器）與圖片（**Image**：提示、部署接受的尺寸、張數，每張圖片附下載連結）；走同一套路由，以登入的使用者身分
  （cookie 與 CSRF，和 console 其他地方一樣）。不建立任何 API key，用量算在「console」。它的雲端設定對 admin 是 `on`，對其他人是
  server 允許的上限；個人的預算適用，沒有 key 的預算。
- **不能繞過 gateway 直接用 replica。** 每個模型 replica 都有一把**內部 key**，每次放置都換新。只有 gateway 與 *Try it* 會送出它；
  UI、API、spec 與 log 裡都看不到。agent 只把它存在記憶體，並用它的 **key guard** 把關：
  - **引擎只聽 loopback**，用只有 agent 知道的 port：行程綁 `127.0.0.1`（配方的 `{host}`），容器發布在 `127.0.0.1:<internal port>`。
  - agent 自己聽 replica **對外公布**的位址與 port（server 與 gateway 知道的那個），只有帶 `Authorization: Bearer <internal key>`
    的請求才轉給引擎，**每個路徑、每個 method** 都一樣（以固定時間比對 key）；其他一律 401 `replica_key_required`，到不了引擎。
    這補上引擎自己沒擋的地方：vLLM 0.32 只在 `/v1`、`/v2`、`/inference`、`/cohere` 檢查自己的 key，`/invocations`、`/rerank`、
    `/score`、`/pooling`、`/classify`、`/tokenize` 原本誰都能用。
  - 內容雙向串流、不暫存（SSE、chunked 與 multipart 上傳、大型回應）；client 斷線就關掉往引擎的連線；hop-by-hop header 會拿掉。
    引擎沒起來時 guard 直接關掉連線不回應，gateway 就改試下一個 replica。
  - guard 沒有任何開放的路徑，連 `/health` 也沒有：agent 直接到引擎的 loopback port 做 health check。行程引擎若被發現聽在
    loopback 以外（配方沒用 `{host}`），會被停掉並標為失敗。
  - 引擎仍然會從**環境變數**拿到 key（vLLM `VLLM_API_KEY`、llama.cpp `LLAMA_API_KEY`、speaches `API_KEY`；配方用 `api_key_env`
    指定變數名），絕不放在命令列，多一層防護。沒有 `api_key_env` 的配方一樣有 guard。
  - guard 在 agent 裡。**agent 停著的時候，它的 replica 從外面連不到**（引擎繼續跑）；agent 重啟後會接手它們，server 一送來 key
    就重新打開 guard。能在那台機器上執行程式的人（包括跑在那裡的 Forgeyard workload）連得到 loopback port；在那裡，引擎自己的 key
    仍然守著它的 `/v1` 路徑。
  - 舊版 agent 啟動的 replica，新 agent 拿到 key 後會重新啟動在 guard 後面（對外 port 不變）；M4 之前的則要重新放置才有 key
    （把部署停止再啟動）。一般 workload（不是模型）沒有 guard。

真機檢查：`scripts/accept_m4.py`。

## 雲端供應商與分層路由（1.2）

閘道也可以把請求送到 Forgeyard 以外的模型 API，但本機優先。2026-11-11 研究過 LiteLLM、OpenRouter、Portkey、Envoy 的 AI gateway、
Kong 與 GPUStack 之後決定。

- **上游只用 OpenAI 相容端點。** Forgeyard 以 OpenAI 的格式呼叫各供應商自己的相容端點（OpenAI、Anthropic `…/v1/`、Gemini
  `…/v1beta/openai/`、Azure OpenAI 的 v1 API、OpenRouter、任何 OpenAI 相容伺服器），回應原樣傳回。每種供應商有一張小小的
  **quirks 表**（`forgeyard/gateway/quirks.py`）處理差異：金鑰怎麼送（Bearer、Azure 的 `api-key`、Anthropic 的 `x-api-key` 與版本）、
  拿掉只有引擎才認得的參數（`chat_template_kwargs`、`top_k`、`guided_*`、llama.cpp 的取樣設定…）、OpenAI 與 Azure 的 `max_tokens`
  改名、Gemini 的 `models/` 前綴，以及怎麼讀回 `usage`（快取與推理 token；Anthropic 的永遠是空的）。不做原生翻譯：各端點少了什麼
  寫在文件裡（docs/guide/cloud-providers.md），而不是藏起來。**不用 LiteLLM**，不論當函式庫或 sidecar：它在 2026 年兩度在 PyPI
  上被植入惡意程式（3 月的 1.82.7/1.82.8，經由被入侵的 CI 工具；10 月的 1.107.0 與其 Docker 映像，會讀取 proxy 的憑證資料表），
  而且它自己的金鑰與預算系統會和 Forgeyard 的重疊。
- **供應商**（`providers`、`provider_models`；**Providers** 頁，只有 admin）。供應商包含種類、base URL、加密的金鑰、最多 16 個額外
  header（不是機密；用來帶金鑰的名稱、`Host` 與 `Cookie` 會被拒絕）、可選的回應逾時（1–3600 秒）、是否**在你的網路外**（依種類預設：
  除了 *OpenAI-compatible server* 都是），以及（只有在網路內時）是否允許純 http。它的每個模型提供 `openai-chat` 或
  `openai-embeddings`，可以額外拿掉參數（`drop_params`），並帶有價格。**Test connection** 帶著金鑰呼叫 `GET {base}/models`；
  **Import from /models** 列出可以加入的模型。模型還在路由裡的供應商不能刪除（409）。**Clone** 會複製它、它的模型、價格與預算，
  但不含金鑰，而且是停用狀態。

  | 種類 | 金鑰怎麼送 | 差異 |
  |---|---|---|
  | OpenAI | `Authorization: Bearer` | `max_tokens` 改送 `max_completion_tokens` |
  | Anthropic | Bearer、`x-api-key`、`anthropic-version: 2023-06-01` | 沒有 embeddings；用量沒有快取 token（`reports_cached` 為 false）；拿掉 `response_format: {"type": "text"}` |
  | Google Gemini | Bearer | 列出的 id 拿掉 `models/` |
  | Azure OpenAI | `api-key` | 要填自己的 base URL（v1 API）；`max_tokens` 改名 |
  | OpenRouter | Bearer | 保留 `top_k`、`min_p`、`repetition_penalty`；回報每個請求的費用 |
  | OpenAI-compatible server | Bearer，金鑰可省略 | 預設在網路內；什麼都不拿掉 |

  除了最後一種，每種都會在送出時拿掉只有引擎才認得的參數（`chat_template_kwargs`、`guided_*`、llama.cpp 的取樣設定、`top_k`…）。
- **讀回用量**（`quirks.read_usage`）：`prompt_tokens`、`completion_tokens`、快取（`prompt_tokens_details.cached_tokens`）、快取寫入
  （OpenRouter 的 `cache_write_tokens`）、推理（`completion_tokens_details.reasoning_tokens`）、回報的 `usage.cost`，以及 Anthropic
  的原生名稱（`input_tokens`、`output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`，此時 prompt 是三種輸入的總和）。
  **Gemini** 的相容端點不把思考 token 算進 `completion_tokens`，只算在 `total_tokens`：當 `total_tokens` 大於 prompt 加 completion，
  差額會加到 completion 與推理 token，因此和 Google 的計費一樣以輸出價格計價。快取 token 不超過 prompt，推理 token 不超過 completion。
  速率限制以同一份讀數的 prompt 加 completion 計算。
- **路由與目標。** 路由是一個 served name，加上分層的目標（`route_targets`）：本機部署與供應商的模型，每個目標有開關；供應商目標
  另有權重（1–100）。部署會建立自己的路由（第 0 層就是它自己），不能拿掉這個目標，刪除時一起刪；admin 可以加層，或自己建立路由
  （只有雲端，或多個部署共用一個名稱）。最多 8 層、32 個目標；served name 在所有路由中不能重複；供應商目標只能放在 chat 或
  embeddings 路由，模型也要相符。每個登入的人都能讀路由（含是否可用，以及種類：local、mixed 或 cloud）；admin 才能修改（稽核紀錄
  `route.created`、`route.changed`、`route.deleted`）。migration 0009 把既有的每條路由都變成一個第 0 層目標，upstream model
  名稱不變，所以行為完全和以前一樣。
- **溢出規則，只在第一個 byte 之前。** 這一層 *unavailable*（沒有就緒的副本、每個連線都失敗）、*busy*（每個就緒副本經由這台
  server 處理中的請求數都達到部署的 `concurrency`；名額是原子地取得的，兩個請求不會同時拿到最後一個；`wait_ms` 會先等名額）或
  *refusing*（429 或 503）時，才用下一層。*too long*（估計的提示加上 `max_tokens` 超過部署的 `context`）只有路由選擇時才算。
  其他 4xx、請求送出後逾時、副本拒絕內部金鑰、用戶端離開、或串流進行中，都絕不溢出。並行上限、`wait_ms`（0–10,000 毫秒，每 25
  毫秒檢查一次）與太長的檢查，只在後面還有東西可以接手時（同一層或之後的層）才適用；沒有 `concurrency` 的部署永遠不算忙碌，沒有
  `context` 的永遠放得下。提示以它的 UTF-8 大小（messages、prompt 或 input）÷ 3，加上 `max_completion_tokens` 或 `max_tokens` 估計。
  如果本機是忙碌、而之後每一層都失敗，請求最後還是會送到負載最輕的本機副本，不再受 `concurrency` 限制（由引擎排隊），
  並拿掉 `X-Forgeyard-Spilled`。沒有其他目標能取代的本機 429 或 503 會原樣轉回。
- **雲端層**依權重（加權洗牌）嘗試目標；回應 429 或 5xx、連不上、重新導向或被對外連線規則拒絕的目標會冷卻
  `PROVIDER_COOLDOWN_SECONDS`（30 秒，或依它的 `Retry-After`／`retry-after-ms`，最多 5 分鐘），排到最後才試，而不是跳過。沒有
  其他目標時（最後一層），不超過 `PROVIDER_RETRY_AFTER_CAP`（2 秒）的 `Retry-After` 會等一次。供應商的回應會變成：429 → 429
  `provider_rate_limited`（附它的 `Retry-After`）；5xx → 502 `provider_error`；401、403 或 407 → 立刻回 502 `provider_auth_failed`，
  不再試其他目標（絕不回 401 讓用戶端以為是自己的金鑰有問題）；重新導向 → 502 `provider_redirect`，絕不跟隨；其他 4xx 保留狀態碼與
  供應商的 `code`，訊息前加上供應商名稱，看起來像金鑰的字串會被拿掉。送出後逾時是 504 `provider_timeout`，不會改試別的。
- **誰可以把資料送出去。** 每把 API key 有 `cloud` = `off`（1.2 以前的每把 key，也是預設值）／`fallback`（只能當溢出目標：
  雲端目標只用在某一層之後，而那一層要有部署或網路內的供應商，所以不會和本機在同一層）／`on`（也包括只有雲端的模型）。對 `off`
  與 `fallback` 的 key，只有雲端的模型會回 404 且不會列出。admin 設定預設值與一般人替自己的 key 能設的上限（一開始取自
  `GATEWAY_CLOUD_DEFAULT` 與 `GATEWAY_CLOUD_MAX`，之後存在資料庫；預設值不能高於上限；稽核紀錄 `api_key_cloud.changed`）。
  修改它們不會改寫任何 key，但上限在每個請求都會套用：個人自己設得比上限高的 key，會照上限運作（key 清單裡的
  `effective_cloud`），直到上限調回來；admin 鎖定的值和 admin 自己的 key 不受上限限制。admin 替別人的 key 設的值會像限制一樣鎖定；使用者清空自己 key 的值時會拿到預設值。標示在你網路內的
  供應商（VPN 上的 vLLM）不算雲端。標示 `local_only` 的路由與帶 `X-Forgeyard-Local-Only: 1`（或 `true`、`yes`、`on`）的請求絕不
  使用雲端目標。只有 chat、completions 與 embeddings 會送到供應商。回應帶 `X-Forgeyard-Target`，溢出時另有 `X-Forgeyard-Spilled`
  （`unavailable`、`busy`、`refused`、`too_long` 或 `budget`），預算讓請求沒送到某個供應商時另有 `X-Forgeyard-Budget-Exhausted`。
- **對外連線。** 供應商用獨立的 HTTP client：驗證 TLS（可加 `EGRESS_CA_FILE`）、絕不跟隨重新導向、絕不用環境變數裡的 proxy
  （改用 `EGRESS_PROXY`）、`EGRESS_ALLOWED_HOSTS`。只允許 https；雲端供應商必須解析到公開位址；私有位址只給網路內的供應商，
  純 http 要 admin 為它開啟（而且絕不連到公開位址）；link-local、metadata（169.254.169.254、fd00:ec2::254、100.100.100.200）、
  multicast、保留與未指定位址一律拒絕；CGNAT（100.64.0.0/10）算私有位址。base URL 帶帳密、query 或 fragment 會被拒絕。每次請求前都
  會解析（快取 30 秒）並檢查名稱，連線直接連到檢查過的位址（TLS 仍然驗證名稱）。設定 `EGRESS_PROXY` 時由 proxy 解析名稱，所以只會
  檢查 IP 位址，限制名稱的是 `EGRESS_ALLOWED_HOSTS`。逾時：`PROVIDER_CONNECT_TIMEOUT`（10 秒）、`PROVIDER_READ_TIMEOUT`（300 秒，或
  供應商自己的設定）。
- **供應商金鑰**是 `EncryptedText("providers.api_key")`：只能寫入（API 只顯示最後四個字元）、絕不寫入記錄、稽核或回應、
  **Clone** 時絕不複製（複本沒有金鑰且停用），`secrets reset` 時會被丟棄。
- **用量**列記錄目標（`local` 或 `pm:<provider model id>`）、快取與推理 token、溢出的請求數與費用，所以能依供應商與模型顯示費用。
- **費用**（`forgeyard/gateway/pricing.py`），只用美元。請求在結束時計價一次；之後改價格絕不會改寫已經算過的數字。
  - 本機副本與你網路內的供應商費用是 0。
  - 上游回報的費用（`usage.cost`，OpenRouter）照用。
  - 否則：(prompt − 快取 − 快取寫入) × 輸入 + 快取 × 快取輸入 + 快取寫入 × 快取寫入價格 + completion × 輸出，價格單位是每百萬 token 的
    微美元。推理 token 包含在 completion 裡，只以輸出價格計一次。
  - 用到的 token 沒有輸入或輸出價格：費用未知（NULL，算作 *unpriced*），而不是 0。沒有快取或快取寫入價格：改用輸入價格，費用標為
    *estimated*；送到不回報快取 token 的種類（Anthropic）、有 prompt 的請求也一樣。
  - 完全沒有 token：上游拒絕或失敗時是 0（供應商不收這些錢）；上游有回答時是 unpriced（用量送達前就關閉的串流）。
  - 費用以皮美元加總，每列用量寫入時取整到微美元，餘數帶到下一次，所以大量很小的請求加起來仍然精確。
- **價格**存在供應商模型上：輸入、輸出、快取輸入與快取寫入，各自可選，並記錄來源（`manual` 或 `openrouter`）、時間，以及對應的
  OpenRouter id。只有網路外供應商的模型會計價。**Refresh prices from OpenRouter**（admin，`forgeyard/api/prices.py`）讀取 OpenRouter
  的公開模型清單（`OPENROUTER_MODELS_URL`，不送金鑰，走對外連線規則），回傳預覽：每個模型、它的對應、新舊價格。對應分成 `confirmed`
  （已存的 id）、`exact`（OpenRouter 供應商自己的 id）、`suggested`（種類的 OpenRouter 廠商加上原名、拿掉日期後綴的名稱，或把版本的
  `-` 換成 `.`；名稱本身有 `/` 時就用它）、`not_listed` 或 `none`；admin 可以輸入一個 id 來比較。**Apply** 會重新讀取清單，只寫入
  勾選、而且價格仍和預覽相同的列（否則以 `changed` 略過），只有要求時才覆蓋手動輸入的價格，並記錄 `provider.prices_refreshed`。
  OpenRouter 每 token 的字串會精確換算；負數（「依情況而定」）或讀不懂的價格算未知。沒有背景同步，也沒有內建的價格表。
- **預算**（`forgeyard/gateway/budgets.py`、`forgeyard/api/budgets.py`）：雲端請求每個 UTC 日曆月可以花的金額，單位微美元（最多
  10^15），設在 **API key**（主人設定，或 admin 設定並鎖定）、**個人**（admin，`user.budget_changed`）或**供應商**（admin，
  `provider.changed`）上；設為 null 就移除。只適用於網路外的供應商。
  - **帳本。** server 行程裡的一份記憶體內帳本，在鎖之下運作：本月每個 key、個人與供應商的已花費與已預留金額，單位皮美元。每個月
    第一次需要時，從 `usage_daily.cost_micros` 讀入（先寫入用量紀錄器還沒寫的部分；供應商的花費透過它的供應商模型計算），只要有
    預算適用，就會在路由之前於事件迴圈之外讀取。讀不到時，預算之下不送任何雲端請求（503 `budget_unavailable`）。
  - **預留。** 請求送到網路外的供應商之前，先估計它最多花多少：prompt 以 messages、prompt、input、tools 與 functions 的 UTF-8 大小
    ÷ 3 無條件進位，加上（`max_completion_tokens` 或 `max_tokens`，沒有就用 `BUDGET_DEFAULT_MAX_TOKENS` = 4096）× `n`（2–128）個
    completion token（embeddings 沒有），以模型的價格計算。這筆金額一次對每個適用的預算預留；只要已花費 + 已預留 + 這個請求超過其中
    一個預算，或模型沒有價格（OpenRouter 也一樣，因為它事後才回報費用），就略過這個目標，下一個目標、下一層或忙碌的本機副本仍然
    可以處理它。
  - **結算與釋放。** 請求結束時，預留換成它的實際費用，加到 key、個人與供應商上（和計入用量在同一把鎖之下，所以讀入時剛好算一次）。
    失敗或被拒絕的請求會釋放預留。有回答、費用卻未知的請求以它的預留計算，只存在記憶體裡，直到 server 重新啟動。
  - **402。** 沒有其他目標能處理時：`402`（type `insufficient_quota`），code 是 `budget_exhausted` 或 `budget_unpriced`，另附一個
    `budget` 物件（範圍、名稱、預算、已花費、已預留與這個請求的金額，單位微美元，以及月份與 `resets_at`），以及
    `X-Forgeyard-Budget-Exhausted`（`key`、`person`、`provider:<name>`）；由其他目標成功回答時也會帶這個 header。用 402 是因為
    OpenAI 的 SDK 會重試 429 但不重試 402，而在月底之前重試也沒有用。
  - **警示。** 預算到 80% 與 100% 時，每個範圍、月份與門檻各一次：`budget_alerts` 資料表（唯一，所以重啟後也只有一次），以及稽核
    事件 `budget.warning` 或 `budget.exhausted`（操作者 `forgeyard`）。在請求預留與結算時檢查，所以預算調低到本月花費以下時，下一個
    請求就會警示；花費加預留已超過 80% 時被拒絕的請求，算作到達 100%。和用量一起，每 `USAGE_FLUSH_SECONDS` 以及關機時寫入。
  - `GET /api/budgets`（admin：`?everyone=true`）回傳月份、`resets_at`，以及每個 key、個人與供應商的預算、已花費、已預留、使用比例
    與等級（`ok`、80% 起 `warning`、100% 起 `exhausted`；預算為 0 時是 exhausted）。API keys 頁、**Usage → Cost**（*Budgets this
    month*）與供應商的頁面會顯示它們。
- **1.2 不做：**Anthropic 或 Gemini 的原生翻譯與 `/v1/messages` 直通；Responses API、batch、檔案、圖片（1.3 中帶媒體的請求也只留在本機）；雲端的 rerank、score、
  語音；串流中途切換；雲端之間依成本、延遲或語意的路由；黏著路由；Entra ID、Vertex 或 Bedrock 認證。

## 帳號與登入

- **角色：** `admin` 管理使用者、機器與其 GPU allowlist、模型目錄、配方與模型來源、雲端供應商、路由與價格、key 的雲端設定，以及
  個人與供應商的預算，並看稽核紀錄；`operator` 執行 workload 與部署模型；`user` 可以瀏覽，並透過 playground 與自己的 API key
  使用已部署的模型、看自己的用量。
  **owner**（第一個帳號）永遠是 admin，不能被降級、停用或刪除，所以不會有人把管理權整個鎖死。
- **密碼：** argon2id；至少 12 字元；常見密碼（含尾巴加數字或符號的變形）與包含使用者名稱的密碼會被拒絕。
- **新使用者**由 admin 建立，admin 拿到一條**一次性連結**（72 小時）轉交對方，由本人自己設定密碼；
  admin 也可以用同樣方式發出重設密碼的連結（24 小時）。token 放在網址的 `#` 後面，不會進到伺服器 log 或 `Referer`。
- **自行註冊**預設關閉。`REGISTRATION=approval` 時可以申請帳號，會先處於 *pending*，等 admin 核准。
- **Session 存在伺服器端。** Cookie（`HttpOnly`、`SameSite=Strict`，HTTPS 時加 `Secure`）只放隨機 token，
  資料庫只存它的 SHA-256。登出、改密碼、停用帳號或 admin 強制登出後，下一個請求就失效——複製走的
  cookie 也不能用。閒置 2 小時、最長 7 天就結束，都在伺服器端檢查。*My account* 可以看到並登出每個 session。
- **速率限制：** 同一帳號失敗 5 次後，每次重試都要等待（2 秒起倍增，最多 5 分鐘）；同一 IP 15 分鐘內失敗 30 次也要等。
  不會永久鎖定，速率限制也絕不會把人登出。`X-Forwarded-For` 只採信 `TRUSTED_PROXIES`。
- **請求防護：** Host 不是自己的名稱回 **421**（防 DNS rebinding）；對 `/api` 的寫入必須帶 `X-Forgeyard: 1`，
  瀏覽器有送 `Origin`／`Referer` 時必須同源。
- **稽核紀錄：** 登入、登入失敗、所有帳號變更，以及機器、模型、配方、部署、workload、key、供應商、路由與價格的變更，還有預算
  警示，含操作者、對象與來源 IP。
- **靜態加密的機密：** Hugging Face token、replica 內部金鑰、workload 環境變數的值與雲端供應商的 API key 以 AES-256-GCM 加密儲存，金鑰不在資料庫裡
  （`data/secret.key`，權限 0600；或 `SECRET_KEY_FILE`／`FORGEYARD_SECRET_KEY`）。金鑰不見或不對時 server 拒絕啟動，
  而不是另做一把新的蓋過舊的機密。金鑰真的遺失時，`python -m forgeyard secrets reset --yes` 重新開始：token 與供應商金鑰要重新輸入，
  replica 換新金鑰重啟。密碼、session、API key 與加入 token 是雜湊，不是加密。
- UI 只在 **401** 時跳到登入頁（403、429、5xx 都不會），登入後回到原本那一頁。

## 容器映像檔

兩個映像檔都從這個 repository 建置，在推送版本 tag `vX.Y.Z` 時由 `.github/workflows/publish-images.yml`
發布到 Docker Hub：`docker.io/tonyhuang1101/forgeyard`（伺服器，`Dockerfile.server`）與
`docker.io/tonyhuang1101/forgeyard-agent`（`Dockerfile.agent`）。

- **名稱只寫在一個地方。**`forgeyard/install/images.py` 存放 Docker Hub 的 namespace（`DOCKERHUB_NAMESPACE`）
  與 repository 名稱。發布的 workflow 從這裡讀取（`scripts/publish_plan.py`），伺服器在 **Add a machine** 產生
  agent 的 `docker run` 時也是。namespace 是空的就代表還沒有發布：頁面會改用本機建置的
  `forgeyard-agent:<version>`，並顯示它的 `docker build`。
- **agent 的 tag 就是伺服器的版本**，所以機器上跑的永遠是和伺服器同版本的 agent。`AGENT_IMAGE` 可以換掉
  repository（鏡像站、私有 registry）、tag，或加上 digest；因為它會出現在使用者貼上的指令裡，啟動時會依
  Docker 的映像檔名稱語法檢查。
- **平台：**linux/amd64 與 linux/arm64，用 buildx 建置（arm64 在 runner 上透過 QEMU）。伺服器的網頁介面只在
  建置平台上的 node 階段建置一次：每個平台的產出都一樣。agent 的 ROCm 輔助套件（libdrm）和 ROCm 一樣只有
  amd64；NVML 則來自純 Python 套件，兩個平台都有。
- **伺服器映像檔**完全依照 `uv.lock` 安裝（不含 dev 群組，含 PostgreSQL），以 uid 10001 執行，把
  `DATA_DIR=/data` 放在 volume 裡，在 `0.0.0.0:7860` 監聽。它的 `HEALTHCHECK`
  （`forgeyard/install/healthcheck.py`）透過容器的 loopback 詢問 `/api/health`，不驗證憑證：請求不會離開容器，
  也不帶任何憑據；驗證反而會因為和健康無關的原因失敗（自己的憑證上寫的是對外的主機名稱）。
- **tag：**`X.Y.Z`、`X.Y` 與 `latest`；預發布版本只有 `X.Y.Z`。tag 和 `forgeyard/__init__.py` 的版本不符時，
  workflow 會停止。每個映像檔都有 OCI 標籤、provenance（`mode=max`）與 SBOM。推送後會用 digest 拉回 amd64
  映像檔，跑和 CI 相同的冒煙測試（`scripts/agent_image_smoke.sh`、`scripts/server_image_smoke.sh`）；
  digest 會寫進該次執行的摘要。
- **沒有 Docker Hub 的 secrets**（`DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`）或 namespace，或在分支上手動執行時，
  workflow 仍會為兩個平台建置兩個映像檔、對 amd64 映像檔跑冒煙測試，並以綠燈結束，用提示說明為什麼沒有推送。

## 設定與設定顧問（1.3）

- **啟動時驗證。** server 無法使用的值會讓它停止啟動，並指出設定名稱與可以填的值（`TLS`、`REGISTRATION`、`COOKIE_SECURE`、
  雲端等級、數字，以及 `TLS_CERT_FILE`／`TLS_KEY_FILE` 只設其中一個）。其他出乎意料的情況一律是警告，絕不拒絕：
  `forgeyard/settings_check.py` 找出 `.env` 或環境裡不是設定的名稱（和 agent 一樣附上「你是不是指」）、被另一個設定變得沒有作用的設定
  （`TLS=off` 時的 `TLS_*`、沒有自己的憑證時的 `TLS_CA_FILE`、兩個機密金鑰設定都有設）、有風險的值（離線時間少於 2.5 次心跳、
  允許私有位址的開關、HTTPS 下的 `COOKIE_SECURE=false`）、設定完成後還留著的設定用值，以及被資料庫覆蓋的環境變數初始值。
  server 啟動時把每一項寫進 log（`Settings: …`）。
- **設定顧問**（`forgeyard/advisor.py`、`GET /api/advisor`，只限 admin）在頁面要求時，對設定、資料庫與各 agent 最後一次的回報
  執行一串小檢查；它自己不儲存任何東西。一項檢查結果是 `{id, severity, title, detail, fix_url, fix_label, scope, pages}`：
  `scope` 指出對象（`machine:3`、`key:12`、`route:4`），`pages` 指出 UI 在哪些頁面以橫幅顯示它。*error* 是現在就壞了或不安全，
  *warning* 是可能會花錢、外洩或壞掉，*info* 是沒有作用或出乎意料。拋出例外的檢查會被記錄並略過，所以一個 bug 絕不會蓋掉其他結果。
  檢查清單在指南的參考資料（*Setup health checks*）；有測試確保它完整。
- **在哪裡顯示**（決策 D8）：**Get started → Setup health** 列出全部，頂端列顯示錯誤與警告的數量，每個頁面（Machines、API keys、
  Providers、Models、路由、部署、Users）以橫幅顯示自己的結果。一般 user 與 operator 都看不到。
- **延後提醒**是每位 admin、每項結果（`id@scope`）各自的，為期 30 天，存在使用者的 `preferences` JSON（`advisor_snoozed`），
  所以不需要 migration。error 不能延後。延後的結果如果仍然成立，30 天後會再出現。
- **機密金鑰備份**（決策 D5）：在 admin 勾選 *I have a backup of key id X* 之前一直是警告。勾選是一筆 `server_settings`
  （`secret_key_backup`：金鑰 id、誰、何時），和目前的金鑰 id 比對，所以換了新金鑰提醒就會回來。過時的 id 會被拒絕（409），
  金鑰本身絕不離開 server。
- **新設定的規則**寫在 `CONTRIBUTING.md`：預設值要能用、要有文件（有測試比對 `Settings`、`.env.example` 與參考資料）、
  除非必要都算進階設定，而且設錯有風險時要加一項顧問檢查。
