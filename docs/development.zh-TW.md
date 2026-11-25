# 開發

[English](development.md)

如何建置與測試 Forgeyard，以及程式碼裡各個東西放在哪裡。要回報問題或送出變更，請先看
[CONTRIBUTING.md](../CONTRIBUTING.md)（英文）；要安裝與使用 Forgeyard，請看[使用指南](guide/zh-TW/README.md)。

需要 Python 3.13 與 [uv](https://docs.astral.sh/uv/)、建置 UI 用的 Node 22.12+，只有建置容器映像檔時才需要 Docker。

## 建置與測試

```bash
uv sync --extra server
uv run --no-sync python -m pytest -q          # 後端測試（SQLite）；卡住的測試 5 分鐘後失敗，
                                               # 並印出每個執行緒的 stack（pytest-timeout）
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

執行 `uv lock`（或 Dependabot 更新 `uv.lock`）之後，請執行 `uv run --no-sync python -m forgeyard.install.requirements`：它會重寫 install.sh 安裝 agent 相依套件時使用的清單（含雜湊值，`forgeyard/install/agent-requirements*.txt`）；清單和 lock 不一致時 FY-INST-020 會失敗。

**Dependency audit** workflow（`.github/workflows/dependency-audit.yml`）會在每個 pull request 和推送到 dev、main 時執行：用 pip-audit 檢查 uv.lock 的執行期套件（所有 extra，不含 dev 群組），並對網頁 UI 執行 `npm audit --omit=dev`。它不是必要的檢查：沒有人動過的套件被公布新的弱點時，不該擋住其他工作；但結果變紅，就表示有相依套件要另開 pull request 更新。在本機執行同樣的檢查：

```bash
uv export --locked --no-dev --all-extras --format requirements-txt -o /tmp/requirements-audit.txt
uvx --from pip-audit==2.10.1 pip-audit -r /tmp/requirements-audit.txt --disable-pip
cd web && npm audit --omit=dev
```

## 以 digest 固定的推論引擎映像檔

內建配方的每個容器 variant 都以 `repo:tag@sha256:<index digest>` 指定映像檔，`forgeyard/recipes/images.lock.json`
記下正確副本會有的 id（每個平台的 manifest、config 與 content id）。agent 靠它檢查 `docker load` 載入的副本，伺服器也靠它把映像檔轉送給連不到 registry 的機器。
`scripts/pin_images.py` 對照 registry 檢查這兩者（有任何不同就以 1 結束）；`--write` 重新解析 tag，改寫配方與 lock。

- **升級推論引擎：**改配方裡的 tag，執行 `pin_images.py --write`，檢查 diff，跑測試。
- **Forgeyard 自己的視覺映像檔**（`images/vision`，配方 `vision` 與 1.5.4 起的 `transformers`）也一樣固定（1.4.1）。release
  會用該版的程式碼發布 `forgeyard-vision:X.Y.Z` 與 `X.Y.Z-cuda`，所以 digest 要在打 tag 之後才知道：**改過 `images/vision`
  的 release 之後**，另開一個小 PR 進 `dev`，把 `forgeyard/recipes/vision.yaml` **和** `forgeyard/recipes/transformers.yaml`
  （同一個映像檔）的兩個 tag 改成新版本，再執行 `pin_images.py --write`（FY-VIS-001、FY-VIS-019 與 FY-TFM-001 寫著固定的
  reference；FY-TFM-004 在固定的映像檔到 1.5.4 之前預期會被舊映像檔拒絕）。沒改到它的 release 就保留原本的固定：單是升級伺服器，視覺模型執行的映像檔永遠不會變。

## 發布之前

發布步驟寫在 [CONTRIBUTING.md](../CONTRIBUTING.md#releasing)（英文）。其中一步是守住映像檔的發布：
`.github/workflows/dockerhub-login.yml`（*Docker Hub login*）在每次推送到 `dev` 與 `main` 時執行，用 repository secrets
`DOCKERHUB_USERNAME` 與 `DOCKERHUB_TOKEN` 登入 Docker Hub，再以 `scripts/dockerhub_check.py` 確認這個 token 可以推送到
`forgeyard`、`forgeyard-agent` 與 `forgeyard-vision`（Docker Hub 的 token 服務會回答它給了哪些權限；不推送任何東西），然後登出。
沒有設定 secrets 時它只發出警告，仍然是綠燈。**打 tag 之前，它在 `main` 上最近一次的執行必須是綠燈且沒有警告**
（`gh run list --workflow dockerhub-login.yml --branch main --limit 1`）；否則重新輸入兩個 secrets（username 是 Docker Hub
帳號名稱，token 是 *Read & Write* 的 access token），再手動執行一次（*Actions → Docker Hub login → Run workflow*）。
它不是必要的檢查，也不在 pull request 上執行。

## 寫不會時好時壞的測試

CI 跑在小機器上（兩個核心、常常很忙；PostgreSQL 那個 job 最慢），所以在這裡會過的測試，到那裡偶爾會失敗。
目前為止幾乎每一次都是測試固定等一段時間就以為事情發生了，或是在需要的資料到達之前就讀了頁面。

- **等狀態，不要等時間。** `tests/waits.py` 有 `wait_until(predicate, what, timeout=10)`，非同步測試用
  `await until(...)`：狀態一到就回傳，超過期限才失敗並說出 `what`，所以期限可以放寬。要先有一個請求在處理中，
  就在執行緒裡送出它，再等假的 engine 或 app 收到（FY-IGEN-007、FY-X-014、FY-PGV-008），不要
  `time.sleep(0.2)`。接著讓假的那一方一直留著它，直到測試放行（`threading.Event`），不要只留固定的時間：
  在忙碌的 runner 上，下一個請求可能比那段時間更晚才到，發現第一個已經結束了（FY-PGV-008 留了 1 秒，#204；
  FY-RT-002 與 FY-X-014 也是，#212）。
- **不要替程式同時做的兩件事計時。** gateway 同時送出的兩個請求，可能以任一順序到達假的 engine，而「0.5 秒內」
  在慢的 runner 上會失敗。改成把它們留住、檢查狀態：FY-FW-008 證明第二個請求在第一個仍在處理時就拿到名額，
  因為兩者在同一時刻都被留在假 engine（`FakeEngine.held`）；並把等待隊伍的安全網重查時間設得比測試還長，
  只有真正的喚醒才會通過（#212）。
- **先把狀態準備好，再做可能搶先的那一步。** 一個必須還在進行中的假工作：先讓假 engine 卡住再送出，而不是送出後才設定（FY-VID-005）。
- **Fixture 要看起來是現在的。** 測試期間 app 的 liveness 檢查每 2 秒跑一次，所以 `make_node` 會說它的機器剛剛才回報過；
  沒有 heartbeat 的機器在慢的測試裡會變成離線，下一次部署就得到 409。
- **結束 process 不是瞬間的。** 用 `tests/leaks.py` 的 `kill_group()` 結束 process group，它會等到每個成員都消失；
  否則每個測試之後的遺留檢查會看到一個還在結束中的 process。
- **時間本身就是測試對象時，sleep 沒問題**：逾時、停滯、「0.5 秒內什麼都不會發生」。即使如此，不該及時到達的東西
  要用 `threading.Event` 留住，不要用比逾時稍長的 sleep，這樣能結束請求的就只有逾時：一個被拖慢、晚了才去看的 gateway，
  會發現 sleep 之後的回應已經在那裡，就把它傳下去（FY-RT-006、FY-FA-006、FY-FA-010、FY-X-009）。下限只斷言程式保證的
  （「至少等了 `wait_ms`」）；上限只用來分辨「逾時」與「永遠不會」，要放寬，並在註解寫出理由。FY-RT-003 的 `< 1.5 秒`
  在 1.63 秒失敗過；現在改成檢查溢出到雲端的回應回來時，第一個請求仍被留著（#226）。
- **UI 測試：API 的東西都比較晚到，而且順序不定。** 來自 fetch 的東西（資料到了才顯示的表單、另一個 endpoint 提供的
  select 選項、回應到達時才設定的值）一律用 `await screen.findBy…` 或 `waitFor`，`getBy…` 只用在已知已經在頁面上的東西。
  儲存之後，先等它的訊息出現再點下一步。`npm run test:jitter` 讓每個假 fetch 隨機延遲 0–150 ms 再回應
  （`web/src/test/fetchJitter.ts`）：寫完 UI 測試後跑幾次。
- **在 CI 之前先在負載下試**：把測試固定在兩個核心上並讓它們保持忙碌，例如
  `for i in 1 2 3 4; do taskset -c 2,3 sh -c 'while :; do :; done' & done`，再
  `taskset -c 2,3 uv run --no-sync python -m pytest -q`（或 `npx vitest run`），之後把那四個迴圈結束掉。

**CI 在測試以外的地方卡住時。** pytest-timeout 會讓卡住 5 分鐘的測試失敗並指出是哪一個，但管不到收集測試、session 的
hook 和直譯器結束；留下來的非 daemon 執行緒會讓 Python 停在結束那一步一直等。所以 CI 這樣跑 pytest：
`uv run --no-sync timeout --signal=ABRT --kill-after=60s 40m python -X faulthandler -m pytest -q`。40 分鐘後（這一步平常
要 18–23 分鐘）Python 會印出每個執行緒的 stack，這一步隨即失敗，不必等到 job 的 45 分鐘上限把它取消、連 log 都沒留下
（#286）。一定要加 `-X faulthandler`：pytest 在直譯器結束前就會關掉自己的 handler。在 job 的 log 裡搜尋
`Fatal Python error: Aborted`，下面列出的執行緒就是卡住的位置。FY-CIHANG-001 會檢查 workflow 裡每個 pytest 步驟都保有這層包裝。
想在本機試，把同一行的上限改短（`timeout --signal=ABRT 10s …`）即可。

## 程式碼結構

| 路徑 | 內容 |
|---|---|
| `forgeyard/app.py` | FastAPI 應用程式：防護、路由、背景工作（用量、警示、憑證更新）、建置好的 UI |
| `forgeyard/cli.py` | `python -m forgeyard [serve]`、`secrets reset` 與 `forgeyard agent …` |
| `forgeyard/config.py` | 伺服器的每個設定（環境變數與 `.env`） |
| `forgeyard/db.py`, `forgeyard/migrations/` | 資料庫模型；Alembic migration，啟動時執行 |
| `forgeyard/api/` | HTTP API：`auth`（初始設定、登入、工作階段、連結）、`users`、`audit_log`、`machines`、`workloads`、`onboarding`（Get started）、`tls`（下載 CA）、`install`（`/install.sh` 與 wheel）、`guide` |
| `forgeyard/api/models.py`, `deployments.py` | 模型目錄、recipe、來源；部署、估算、試用 |
| `forgeyard/api/gateway.py`, `keys.py` | `/v1/*` 與 playground（它的視覺面板經過 `forgeyard/gateway/vision.py`）；API 金鑰、它的限制、雲端設定與預算，以及用量 |
| `forgeyard/api/providers.py` | 雲端供應商與它的模型：新增、測試、匯入、手動價格、複製 |
| `forgeyard/api/routing.py` | 路由與它分層的目標 |
| `forgeyard/api/prices.py` | 從 OpenRouter 更新價格：預覽與套用 |
| `forgeyard/api/budgets.py` | 個人與供應商的每月預算，以及本月的花費 |
| `forgeyard/gateway/proxy.py` | 把請求依路由的各層轉送（`Job`、`forward`、`record`）；下列各部分的名稱也都能從它匯入 |
| `forgeyard/gateway/route.py`, `body.py`, `fit.py` | 路由與它的目標、誰能呼叫、請求能用哪些層；讀取並解析請求本文；能接下它的目標（媒體、大小、語音、圖片） |
| `forgeyard/gateway/upstream.py`, `replicas.py`, `cloud.py` | 連到副本的 HTTP client、哪些情況可以重試、用戶端斷線；一層裡的副本（並行數、排隊、第一個回應）；它的供應商（順序、預算、差異） |
| `forgeyard/gateway/streaming.py`, `answer.py` | 串流回應（SSE、音訊）；把回應轉回去並計算用量 |
| `forgeyard/gateway/keys.py`, `limits.py`, `balancer.py` | API 金鑰檢查與雲端設定；每分鐘請求數與 token 數；副本順序與供應商冷卻 |
| `forgeyard/gateway/quirks.py` | 各種供應商的差異：金鑰怎麼送、拿掉或改名的參數、怎麼讀回用量 |
| `forgeyard/gateway/where.py` | 依種類和位址判斷新的供應商是否在你的網路外 |
| `forgeyard/gateway/egress.py` | 供應商用的 HTTP client：TLS、不跟隨重新導向、位址檢查、`EGRESS_*` |
| `forgeyard/gateway/pricing.py` | 依用量與價格計算請求的費用；OpenRouter 的清單與價格對應 |
| `forgeyard/gateway/budgets.py` | 預算帳本：預留、結算、釋放、80% 與 100% 警示、402 |
| `forgeyard/gateway/media.py` | 對話中的圖片、音訊與影片：重建內容區塊、依 egress 規則下載媒體網址 |
| `forgeyard/gateway/speech.py` | 文字轉語音：檢查請求、拒絕聲音複製、每種推論引擎拿到什麼、音訊長度 |
| `forgeyard/gateway/imagegen.py` | 圖片生成：檢查請求、每個部署的上限、每種推論引擎拿到什麼、佇列、回應與圖片尺寸 |
| `forgeyard/gateway/videogen.py`、`videojobs.py` | 影片生成：檢查請求、每個部署的上限、工作物件；工作留在它的副本上、清掃器、過期、內容與用量 |
| `forgeyard/gateway/usage.py`, `errors.py` | 在記憶體計數再寫入資料庫的用量；OpenAI 格式的錯誤 |
| `forgeyard/machines/` | agent 協定、加入與串流端點、即時連線、離線偵測 |
| `forgeyard/workloads/` | 工作負載的規格、放置與 GPU 允許清單、期望狀態與目前狀態 |
| `forgeyard/models/` | 模型來源、有 SSRF 防護的下載器、檢查（safetensors、GGUF、文字以外能接受什麼、授權）、估算、recipe、排程器、路由、推薦清單、伺服器的 Hugging Face 權杖 |
| `forgeyard/recipes/` | 內建的推論引擎 recipe（YAML），映像檔以 digest 固定 |
| `forgeyard/security/` | 密碼、工作階段、速率限制、host／CSRF middleware、HTTPS 與 CA、靜態加密的機密、`secrets reset` |
| `forgeyard/agent/` | agent：硬體偵測（NVML、AMD SMI、CPU）、連線、狀態、信任（`agent trust`）、它的執行方式（`how_it_runs.py`，用於更新指令） |
| `forgeyard/agent/config.py`, `setup.py` | agent 的設定（`agent.toml`、`FORGEYARD_*`、旗標）與 `agent init` |
| `forgeyard/agent/models.py` | agent 的模型快取：下載、驗證、推論引擎 |
| `forgeyard/agent/workloads/` | agent 的協調迴圈、行程與容器執行環境、副本的金鑰守門員、紀錄、健康檢查 |
| `forgeyard/agent/workloads/supervisor.py`, `state.py`, `runtime.py` | 協調迴圈：迴圈本身、期望清單與回報；它在記憶體裡的狀態；每種執行環境都實作的 `Runtime` 協定（以及 `ContainerEngine`） |
| `forgeyard/agent/workloads/lifecycle.py`, `fetching.py`, `checks.py`, `keyguards.py`, `admin.py` | 協調迴圈的各部分：副本的啟動、結束、停止與移除；啟動前的模型、推論引擎與映像檔；健康檢查；金鑰守門；管理員的快取與映像檔請求 |
| `forgeyard/install/` | 由執行中程式碼建置的 agent wheel、`install.sh`、Machines 頁上的指令、agent 的更新指令與是否落後（`update.py`）、已發布映像檔的名稱（`images.py`）、伺服器映像檔的健康檢查 |
| `forgeyard/guide.py`, `docs/guide/` | 使用指南（en、zh-TW），在 `/guide` 提供 |
| `docs/design.md`, `docs/https-and-trust.md` | 每個部分如何運作；HTTPS、信任與機密 |
| `Dockerfile.agent`, `scripts/agent_image_smoke.sh` | agent 的容器映像檔與它的 CI 冒煙測試 |
| `Dockerfile.server`, `scripts/server_image_smoke.sh` | 伺服器的容器映像檔與它的 CI 冒煙測試 |
| `.github/workflows/publish-images.yml`, `scripts/publish_plan.py` | 推送版本 tag 時，把兩個映像檔發布到 Docker Hub |
| `.github/workflows/dockerhub-login.yml`, `scripts/dockerhub_check.py` | 每次推送到 `dev` 與 `main` 時，確認 Docker Hub secrets 能登入且可以推送（[發布之前](#發布之前)） |
| `scripts/accept_all.py` | 完整的回歸驗收：在你自己的伺服器和機器上檢查每一項已發布的功能（[做法](guide/zh-TW/how-to.md#從頭到尾檢查一台伺服器)） |
| `scripts/accept_m3.py`, `accept_m4.py`, `accept_mm.py` | 在實際機器上的驗收，透過 API 與官方 OpenAI SDK |
| `scripts/gpu_probe.py` | 只透過 CUDA driver 檢查行程看到哪張 NVIDIA GPU |
| `scripts/accept_tts.py` | 文字轉語音在真實硬體上的驗收（也是 `accept_all.py` 的 `tts` 群組） |
| `scripts/accept_img.py` | 圖片生成在真實硬體上的驗收（也是 `accept_all.py` 的 `img` 群組） |
| `images/vision/` | `vision` 配方執行的 vision server（偵測、分割、深度、圖片分類），1.5.4 起也是 `transformers` 配方的（圖片嵌入、文字分類）：Dockerfile、固定版本的模型與相依套件、smoke test 和它的迷你測試模型（`tinymodels.py`） |
| `scripts/accept_scale.py` | 閒置時休眠、喚醒和自動副本數在真實硬體上的驗收（也是 `accept_all.py` 的 `scale` 群組；[做法](guide/zh-TW/how-to.md#檢查休眠喚醒和自動副本數)） |
| `scripts/check_recommended.py`, `pin_images.py` | 對照 Hugging Face 檢查推薦模型；以 digest 固定 recipe 的映像檔 |
| `web/` | React + TypeScript + Vite 的 UI，建置到 `forgeyard/static` |
