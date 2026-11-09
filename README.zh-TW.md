# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器，之後也包括租用的雲端機器。
每台機器上的 agent 回報硬體；你在上面跑 service、task 與開發環境，並用配方部署模型，統一走一個 OpenAI 相容的 gateway。

> **狀態：里程碑 M0（0.1.0，未發布）。** server、資料庫、Web UI 與登入已完成。Machines 與 Deployments
> 兩頁目前是空殼，要等 agent（M1）與 workload（M2）。

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
