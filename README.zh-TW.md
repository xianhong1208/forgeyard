# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器，之後也包括租用的雲端機器。
每台機器上的 agent 回報硬體；你在上面跑 service、task 與開發環境，並用配方部署模型，統一走一個 OpenAI 相容的 gateway。

> **狀態：里程碑 M0、M1 與 M2（0.1.0，未發布）。** server、資料庫、Web UI 與登入已完成；機器透過 agent
> 加入並回報硬體；可以在機器上跑 service 與 task，以容器或程序執行，而且只拿得到分配給它的 GPU。
> 模型部署在 M3。

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
replica 會停在 *pending* 並寫明原因，一有空間就自動放上去。真正的排程器（評分、VRAM 估算、binpack）在 M3。

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
  `--limits auto|systemd|rlimit`、`--log-max-mb`、`--no-workloads`。`python -m forgeyard agent runtimes
  [--workload-user …]` 不加入 server，只印出這台機器能用哪些 runtime。

workload 的真機驗收步驟見 [docs/acceptance-m2.md](docs/acceptance-m2.md)（英文）。

## 帳號與登入

- **角色：** `admin` 管理使用者、機器與其 GPU allowlist，並看稽核紀錄；`operator` 執行 workload；`user` 只能看
  （M4 會拿到 API key）。
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
