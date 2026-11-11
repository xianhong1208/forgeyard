# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器。每台機器上的 agent
回報硬體；你在上面跑服務與任務，用配方部署模型，統一走一個 OpenAI 相容的 `/v1` 閘道，有 API 金鑰、限流與用量統計。

[English](README.md)

> **狀態：1.1.0。** 1.0.0 是 MVP（里程碑 M0 到 M4，每一個都在真實的 8×A100 主機與一台 CPU 機器上驗收過）。1.1 加入首次使用清單與
> 推薦模型、開箱即用的 HTTPS、單行安裝 agent、agent 容器映像檔，以及這份使用指南。見 [CHANGELOG.md](CHANGELOG.md)
> 與 [ROADMAP.md](ROADMAP.md)。

## 使用指南

安裝與使用需要知道的都在**[使用指南](docs/guide/zh-TW/README.md)**（[English](docs/guide/README.md)），
Forgeyard 自己也在 `/guide` 提供：

| | |
|---|---|
| [快速入門](docs/guide/zh-TW/quick-start.md) | 從零開始，大約十分鐘完成第一次 `/v1` 呼叫 |
| [概念](docs/guide/zh-TW/concepts.md) | 機器與 agent、GPU 存取、工作負載、模型、部署與閘道、金鑰、角色 |
| [操作指南](docs/guide/zh-TW/how-to.md) | AMD 與純 CPU 機器、用 Docker 跑 agent、共用 GPU、自己的憑證、升級、備份 |
| [疑難排解](docs/guide/zh-TW/troubleshooting.md) | 在真實機器上遇過的問題與解法 |
| [參考資料](docs/guide/zh-TW/reference.md) | 伺服器與 agent 的所有設定、指令、檔案與連接埠 |

各部分的運作細節：[docs/design.zh-TW.md](docs/design.zh-TW.md)。HTTPS、信任與加密儲存的機密：
[docs/https-and-trust.md](docs/https-and-trust.md)（英文）。

## 簡介

伺服器（Python 3.13 與 [uv](https://docs.astral.sh/uv/)；建置 UI 需要 Node 22.12+）：

```bash
cd web && npm ci && npm run build && cd ..
uv sync
HOST=0.0.0.0 PUBLIC_URL=https://192.168.1.20:7860 uv run python main.py
```

它一啟動就用自己的憑證授權單位提供 HTTPS，印出 `/setup` 用的一次性設定權杖，所有資料都放在 `data/`
（要備份資料庫、`data/secret.key` 與 `data/tls/`）。

加入機器：**Machines → Add a machine** 會給你一行安裝指令。它先用指紋核對伺服器的 CA，再把與伺服器同版本的
agent 裝成 systemd 服務：

```bash
curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
  sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
  echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
  curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
  sudo sh -s -- --join fyj_… --ca-fingerprint sha256:3f9a…
```

也可以用 agent 映像檔跑一個 `docker run`（`docker build -f Dockerfile.agent -t forgeyard-agent:<版本> .`），
或在 Forgeyard 目錄裡搭配 `/etc/forgeyard/agent.toml` 執行。接著在 **Models** 部署模型，用 API 金鑰和任何 OpenAI
客戶端呼叫 `https://192.168.1.20:7860/v1`。

**從 1.0 升級：** 執行 `uv sync`，重新啟動伺服器，再到每台用一般 HTTP 加入的機器上執行一次 `agent trust` 指令；見
[docs/https-and-trust.md](docs/https-and-trust.md#upgrading-from-10-agents-that-joined-over-plain-http)。

## 開發

```bash
uv sync
uv run --no-sync python -m pytest -q          # 後端測試（SQLite）
uv run --no-sync ruff check .
TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/db uv run --no-sync python -m pytest -q
                                               # 同一套測試跑在 PostgreSQL 上——會清空該資料庫
cd web
npm run dev                                   # UI 在 :5173，API 轉到 https://127.0.0.1:7860
npm run lint && npm run typecheck && npm test && npm run build
cd ..
shellcheck -s sh forgeyard/install/install.sh  # 安裝腳本維持 POSIX sh
docker build -f Dockerfile.agent -t forgeyard-agent:dev . && scripts/agent_image_smoke.sh forgeyard-agent:dev
```

資料表變更一律走 Alembic（`forgeyard/migrations`）；啟動時執行 `upgrade head`，model 與 migration 不一致時
會有測試失敗。測試編號（`FY-…`）在整個專案中唯一。
