# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器，之後也包括租用的雲端機器。
每台機器上的 agent 回報硬體；你在上面跑 service、task 與開發環境，並用配方部署模型，統一走一個 OpenAI 相容的 gateway。

> **狀態：里程碑 M0 與 M1（0.1.0，未發布）。** server、資料庫、Web UI 與登入已完成，機器可以透過 agent
> 加入並回報硬體。Deployments 頁要等 workload（M2）才有內容。

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

## 帳號與登入

- **角色：** `admin` 管理使用者、看稽核紀錄；`operator` 之後負責機器與部署；`user` 部署自己的工作。
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
