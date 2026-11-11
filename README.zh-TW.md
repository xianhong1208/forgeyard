# Forgeyard

一個控制台管理你自己的 GPU（和 CPU）：同一個 LAN 或 VPN 上的 NVIDIA、AMD ROCm 與純 CPU 機器。每台機器上的 agent
回報硬體；你在上面跑服務與任務，用配方部署模型，統一走一個 OpenAI 相容的 `/v1` 閘道，有 API 金鑰、限流與用量統計。

[English](README.md)

> **狀態：1.1.1。** 1.0.0 是 MVP（里程碑 M0 到 M4，每一個都在真實的 8×A100 主機與一台 CPU 機器上驗收過）。1.1 加入首次使用清單與
> 推薦模型、開箱即用的 HTTPS、單行安裝 agent、agent 容器映像檔，以及這份使用指南。1.2 即將推出（已在 `dev`）：雲端供應商走同一個閘道、
> 本機優先：模型的副本忙碌或停擺時，可以溢出到 OpenAI、Anthropic、Gemini、Azure 或 OpenRouter，而且只限允許把資料送出去的金鑰；
> 每個雲端請求的費用以美元計算（價格手動輸入，或從 OpenRouter 讀取、確認差異後套用），並可依金鑰、人和供應商設定每月預算，
> 請求送出前先預留：預算用完時，請求改用本機。
> 見 [CHANGELOG.md](CHANGELOG.md) 與 [ROADMAP.md](ROADMAP.md)。

## 使用指南

安裝與使用需要知道的都在**[使用指南](docs/guide/zh-TW/README.md)**（[English](docs/guide/README.md)），
Forgeyard 自己也在 `/guide` 提供：

| | |
|---|---|
| [快速入門](docs/guide/zh-TW/quick-start.md) | 從零開始，大約十分鐘完成第一次 `/v1` 呼叫 |
| [概念](docs/guide/zh-TW/concepts.md) | 機器與 agent、GPU 存取、工作負載、模型、部署與閘道、金鑰、角色 |
| [操作指南](docs/guide/zh-TW/how-to.md) | AMD 與純 CPU 機器、用 Docker 跑 agent、共用 GPU、自己的憑證、升級、備份 |
| [雲端供應商](docs/guide/zh-TW/cloud-providers.md) | 把 OpenAI、Anthropic、Gemini、Azure、OpenRouter 或你自己的伺服器放到 `/v1` 後面；請求何時溢出；哪些金鑰可以使用雲端 |
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

## 已驗證的硬體

每個版本都在真實的機器上檢查過，不只是跑 CI。目前為止：

| 硬體 | 軟體 | 測過的內容 |
|---|---|---|
| NVIDIA A100 80GB PCIe（8 卡主機，只分給 Forgeyard 其中幾張） | driver 580、CUDA 13.0、Docker | 加入伺服器與 GPU 允許清單；服務和任務以程序或容器執行，各自只看到分配到的顯示卡；vLLM 0.32（Qwen3-4B 對話、Qwen3 embedding 與 reranker）、speaches 0.8.3（faster-whisper）；用官方 OpenAI SDK 呼叫閘道；以容器映像檔執行 agent |
| AMD Ryzen AI MAX+ 395 與 Radeon 8060S（Strix Halo，gfx1151 APU，96 GiB 顯示記憶體） | Ubuntu 24.04、ROCm 7.0.2、amd-smi 26.0.2 | 透過 `amdsmi` 函式庫偵測硬體（統一記憶體）、加入伺服器，並在連不到 Hugging Face 和 `ghcr.io` 的機器上用 llama.cpp 的 Vulkan 版本跑 Qwen3-1.7B GGUF，透過 `/v1` 提供服務 |
| 沒有 GPU 的 x86-64 | Linux | llama.cpp（Qwen3-1.7B GGUF）同時跑在兩個 agent 上：負載分配，一台的引擎掛掉時自動轉到另一台 |
| Google Gemini（`gemini-2.5-flash`） | 它的 OpenAI 相容端點 | 請求超過本機模型的 context 時溢出到雲端、串流、費用（含思考 tokens）與預算 |

還沒在真實硬體上檢查的：`vllm` 和 `llama-cpp` 配方的 ROCm 映像檔、獨立 AMD 顯示卡，以及在有 systemd 的主機上用單行指令安裝。歡迎回報在其他機器上的結果（見 [CONTRIBUTING.md](CONTRIBUTING.md)，英文）。

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

## 參與與安全

歡迎回報問題、提出想法或送 pull request，請先看 [CONTRIBUTING.md](CONTRIBUTING.md)（英文）。安全漏洞請依
[SECURITY.md](SECURITY.md)（英文）私下回報；那份文件也說明了 Forgeyard 對網路環境和使用者的假設。

## 授權

[Apache License 2.0](LICENSE)。
