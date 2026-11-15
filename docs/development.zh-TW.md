# 開發

[English](development.md)

如何建置與測試 Forgeyard，以及程式碼裡各個東西放在哪裡。要回報問題或送出變更，請先看
[CONTRIBUTING.md](../CONTRIBUTING.md)（英文）；要安裝與使用 Forgeyard，請看[使用指南](guide/zh-TW/README.md)。

需要 Python 3.13 與 [uv](https://docs.astral.sh/uv/)、建置 UI 用的 Node 22.12+，只有建置容器映像檔時才需要 Docker。

## 建置與測試

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
docker build -f Dockerfile.server -t forgeyard:dev . && scripts/server_image_smoke.sh forgeyard:dev
```

資料表變更一律走 Alembic（`forgeyard/migrations`）；啟動時執行 `upgrade head`，model 與 migration 不一致時會有測試失敗。測試編號（`FY-…`）在整個專案中唯一。

## 程式碼結構

| 路徑 | 內容 |
|---|---|
| `forgeyard/app.py` | FastAPI 應用程式：防護、路由、背景工作（用量、警示、憑證更新）、建置好的 UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]`、`secrets reset` 與 `forgeyard agent …` |
| `forgeyard/config.py` | 伺服器的每個設定（環境變數與 `.env`） |
| `forgeyard/db.py`, `forgeyard/migrations/` | 資料庫模型；Alembic migration，啟動時執行 |
| `forgeyard/api/` | HTTP API：`auth`（初始設定、登入、工作階段、連結）、`users`、`audit_log`、`machines`、`workloads`、`onboarding`（Get started）、`tls`（下載 CA）、`install`（`/install.sh` 與 wheel）、`guide` |
| `forgeyard/api/models.py`, `deployments.py` | 模型目錄、recipe、來源；部署、估算、試用 |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` 與 playground（它的 Vision 分頁經過 `forgeyard/gateway/vision.py`）；API 金鑰、它的限制、雲端設定與預算，以及用量 |
| `forgeyard/api/providers.py` | 雲端供應商與它的模型：新增、測試、匯入、手動價格、複製 |
| `forgeyard/api/routing.py` | 路由與它分層的目標 |
| `forgeyard/api/prices.py` | 從 OpenRouter 更新價格：預覽與套用 |
| `forgeyard/api/budgets.py` | 個人與供應商的每月預算，以及本月的花費 |
| `forgeyard/gateway/proxy.py` | 把請求依路由的各層轉送：副本、供應商、溢出、串流 |
| `forgeyard/gateway/keys.py`, `limits.py`, `balancer.py` | API 金鑰檢查與雲端設定；每分鐘請求數與 token 數；副本順序與供應商冷卻 |
| `forgeyard/gateway/quirks.py` | 各種供應商的差異：金鑰怎麼送、拿掉或改名的參數、怎麼讀回用量 |
| `forgeyard/gateway/where.py` | 依種類和位址判斷新的供應商是否在你的網路外 |
| `forgeyard/gateway/egress.py` | 供應商用的 HTTP client：TLS、不跟隨重新導向、位址檢查、`EGRESS_*` |
| `forgeyard/gateway/pricing.py` | 依用量與價格計算請求的費用；OpenRouter 的清單與價格對應 |
| `forgeyard/gateway/budgets.py` | 預算帳本：預留、結算、釋放、80% 與 100% 警示、402 |
| `forgeyard/gateway/media.py` | 對話中的圖片、音訊與影片：重建內容區塊、依 egress 規則下載媒體網址 |
| `forgeyard/gateway/speech.py` | 文字轉語音：檢查請求、拒絕聲音複製、每種引擎拿到什麼、音訊長度 |
| `forgeyard/gateway/imagegen.py` | 圖片生成：檢查請求、每個部署的上限、每種引擎拿到什麼、佇列、回應與圖片尺寸 |
| `forgeyard/gateway/videogen.py`、`videojobs.py` | 影片生成：檢查請求、每個部署的上限、工作物件；工作留在它的副本上、清掃器、過期、內容與用量 |
| `forgeyard/gateway/passthrough.py` | `/x/<name>` 的通用直通：拒絕哪些路徑、拿掉與強制哪些標頭、以原始位元組轉送回應、限制與用量 |
| `forgeyard/gateway/usage.py`, `errors.py` | 在記憶體計數再寫入資料庫的用量；OpenAI 格式的錯誤 |
| `forgeyard/machines/` | agent 協定、加入與串流端點、即時連線、離線偵測 |
| `forgeyard/workloads/` | 工作負載的規格、放置與 GPU 允許清單、期望狀態與目前狀態 |
| `forgeyard/models/` | 模型來源、有 SSRF 防護的下載器、檢查（safetensors、GGUF、文字以外能接受什麼、授權）、估算、recipe、排程器、路由、推薦清單、伺服器的 Hugging Face 權杖 |
| `forgeyard/recipes/` | 內建的引擎 recipe（YAML），映像檔以 digest 固定 |
| `forgeyard/security/` | 密碼、工作階段、速率限制、host／CSRF middleware、HTTPS 與 CA、靜態加密的機密、`secrets reset` |
| `forgeyard/agent/` | agent：硬體偵測（NVML、AMD SMI、CPU）、連線、狀態、信任（`agent trust`） |
| `forgeyard/agent/config.py`, `setup.py` | agent 的設定（`agent.toml`、`FORGEYARD_*`、旗標）與 `agent init` |
| `forgeyard/agent/models.py` | agent 的模型快取：下載、驗證、引擎 |
| `forgeyard/agent/workloads/` | agent 的協調迴圈、行程與容器執行環境、副本的金鑰守門、紀錄、健康檢查 |
| `forgeyard/install/` | 由執行中程式碼建置的 agent wheel、`install.sh`、Machines 頁上的指令、已發布映像檔的名稱（`images.py`）、伺服器映像檔的健康檢查 |
| `forgeyard/guide.py`, `docs/guide/` | 使用指南（en、zh-TW），在 `/guide` 提供 |
| `docs/design.md`, `docs/https-and-trust.md` | 每個部分如何運作；HTTPS、信任與機密 |
| `Dockerfile.agent`, `scripts/agent_image_smoke.sh` | agent 的容器映像檔與它的 CI 冒煙測試 |
| `Dockerfile.server`, `scripts/server_image_smoke.sh` | 伺服器的容器映像檔與它的 CI 冒煙測試 |
| `.github/workflows/publish-images.yml`, `scripts/publish_plan.py` | 推送版本 tag 時，把兩個映像檔發布到 Docker Hub |
| `scripts/accept_all.py` | 完整的回歸驗收：在你自己的伺服器和機器上檢查每一項已發布的功能（[做法](guide/zh-TW/how-to.md#從頭到尾檢查一台伺服器)） |
| `scripts/accept_m3.py`, `accept_m4.py`, `accept_mm.py` | 在實際機器上的驗收，透過 API 與官方 OpenAI SDK |
| `scripts/gpu_probe.py` | 只透過 CUDA driver 檢查工作負載看到哪張 NVIDIA GPU |
| `scripts/accept_tts.py` | 文字轉語音在真實硬體上的驗收（也是 `accept_all.py` 的 `tts` 群組） |
| `scripts/accept_img.py` | 圖片生成在真實硬體上的驗收（也是 `accept_all.py` 的 `img` 群組） |
| `images/vision/` | 工作負載範本（偵測、分割、深度）背後的 vision server：Dockerfile、固定版本的模型與相依套件、smoke test |
| `scripts/accept_x.py` | `/x/` 直通在真實硬體上的驗收（也是 `accept_all.py` 的 `passthrough` 群組） |
| `scripts/check_recommended.py`, `pin_images.py` | 對照 Hugging Face 檢查推薦模型；以 digest 固定 recipe 的映像檔 |
| `web/` | React + TypeScript + Vite 的 UI，建置到 `forgeyard/static` |
