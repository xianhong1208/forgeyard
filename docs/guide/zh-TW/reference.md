# 參考資料

伺服器和 agent（代理程式）的每一個設定、命令列，以及各項資料存放的位置。

## 伺服器設定

伺服器從環境變數讀取設定，也可以從它啟動時所在資料夾裡的 `.env` 檔讀取（`.env.example` 列出了所有設定並附有註解，常用的排在最前面）。修改後請重新啟動伺服器。

伺服器啟動時會檢查它的設定：

- **無法使用的值會讓伺服器停止啟動**，並指出是哪個設定、可以填哪些值：`TLS`、`REGISTRATION`、`COOKIE_SECURE`、`GATEWAY_CLOUD_DEFAULT`、`GATEWAY_CLOUD_MAX`、不是數字的數字設定，以及 `TLS_CERT_FILE` 和 `TLS_KEY_FILE` 只設了其中一個。
- **其他出乎意料的情況都只記成警告**，並在[設定健康檢查](#setup-health-checks)顯示給管理員：不是設定的名稱（附上「你是不是指」）、被另一個設定變得沒有作用的設定、有風險的組合，以及還設著的[在 1.4 移除的設定](#removed-settings)（並指出改用什麼）。唯一的例外：已移除的 `SECRET_KEY_FILE` 如果現在會變成另一個金鑰檔，伺服器會停止啟動。

### 常用設定

大多數安裝只會設定其中幾個。這張表之後的都是進階設定：預設值幾乎適合所有人。

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HOST` | `127.0.0.1` | 監聽的位址。`0.0.0.0` 代表對網路開放，其他機器要加入就需要這樣設定 |
| `PORT` | `7860` | 連接埠，網頁介面、API、`/v1` 和 agent 都用這個 |
| `PUBLIC_URL` | — | 使用者和 agent 使用的位址，例如 `https://192.168.1.20:7860`。加入指令、設定密碼的連結和閘道位址都會用到它；它的主機一律會被接受，也會包含在憑證裡。留空：使用每個瀏覽器開啟 Forgeyard 時的位址；如果那是 `127.0.0.1`，其他機器就無法使用 |
| `DATA_DIR` | `data` | 資料庫（SQLite）、`secret.key`、`tls/` 和設定權杖（setup token）的位置。相對路徑是相對於伺服器啟動時所在的資料夾 |
| `DATABASE_URL` | — | 留空：SQLite，放在 `DATA_DIR`。PostgreSQL：`postgresql+psycopg://user:password@host:5432/forgeyard`（先執行 `uv sync --extra server --extra postgres`） |
| `TLS` | `on` | `on`：以 Forgeyard 自己的 CA（建立在 `data/tls/`）或你的憑證提供 HTTPS。`off`：純 HTTP，只能用在處理 HTTPS 的代理後面 |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | 你自己的憑證和私鑰：兩個都設或都不設（只設一個會停止啟動）。每 12 小時檢查一次，有變更就重新載入。`TLS=off` 時會被忽略 |
| `TLS_CA_FILE` | — | 使用自己的憑證時：簽發它的 CA。agent 會釘選它，**信任這台伺服器** 也會提供它。沒有自己的憑證時會被忽略 |
| `REGISTRATION` | `off` | `approval`：使用者可以申請帳號，申請會等待管理員核准 |
| `HF_ENDPOINT` | `https://huggingface.co` | 從哪裡查詢和下載 Hugging Face 模型（可以填鏡像站） |
| `EGRESS_PROXY` | — | 送往供應商的請求改經由這個 proxy（`http://host:port`）。絕不使用環境變數裡的 proxy |

### 網路與位址

| 變數 | 預設值 | 用途 |
|---|---|---|
| `ALLOWED_HOSTS` | — | 其他要回應的主機名稱，以逗號分隔。`localhost` 和 IP 位址一律可用；其他名稱會得到 421 |
| `TRUSTED_PROXIES` | — | 信任其 `X-Forwarded-For` 和 `X-Forwarded-Proto` 的代理，以逗號分隔的 IP |
| `METRICS_TOKEN` | — | `GET /metrics`（Prometheus）。留空：只有來自伺服器本機的直接請求可以讀取（其他得到 403）。有設定：每次收集都要 `Authorization: Bearer <token>`，不論從哪裡來（否則 401）。請看[指標](#metrics) |

### HTTPS

| 變數 | 預設值 | 用途 |
|---|---|---|
| `TLS_EXTRA_NAMES` | — | 要加進 Forgeyard 自己的伺服器憑證的其他名稱或 IP 位址，以逗號分隔。使用自己的憑證時會被忽略 |

`TLS`、`TLS_CERT_FILE`、`TLS_KEY_FILE` 和 `TLS_CA_FILE` 在[常用設定](#common-settings)裡。

從閘道到副本的流量（1.4.5）不論這些設定為何都會加密：`data/tls` 裡 Forgeyard 的 CA 會替每個 agent 簽發副本憑證（使用自己的憑證或 `TLS=off` 時，第一次用到才會在那裡建立）。沒有東西需要設定；請見[部署與閘道](concepts.md#deployments-and-the-gateway)。

### 資料與機密

| 變數 | 預設值 | 用途 |
|---|---|---|
| `FORGEYARD_SECRET_KEY_FILE` | `data/secret.key` | 加密儲存機密資料的金鑰（Hugging Face 權杖、供應商金鑰、副本金鑰、應用程式環境變數的值）。請和資料庫分開備份 |
| `FORGEYARD_SECRET_KEY` | — | 直接給金鑰本身（32 位元組的 base64），取代檔案。兩者只設一個：兩個都設的話，會使用這一個，並忽略檔案 |

### 帳號與登入

| 變數 | 預設值 | 用途 |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | 在第一次啟動時建立擁有者，取代設定頁面。只在那時讀取：之後請移除 |
| `SESSION_IDLE_MINUTES` | `120` | 超過這段時間沒有任何請求就自動登出 |
| `SESSION_MAX_DAYS` | `7` | 無論如何，超過這段時間就自動登出 |
| `COOKIE_SECURE` | `auto` | `auto`：瀏覽器使用 HTTPS 時就使用 `Secure` cookie；設成 `true` 或 `false` 可強制指定。其他任何值都會停止啟動 |
| `RATE_LIMIT_PER_MINUTE` | `600` | 每個位址每分鐘對網頁介面的 API（`/api`）的請求數（0：不限制）。閘道 `/v1` 則改用每把金鑰的限制 |
| `LOGIN_FREE_FAILURES` | `5` | 每個帳號登入失敗幾次後，之後每次嘗試都要等待（2 秒起，每次加倍，最多 5 分鐘） |
| `LOGIN_IP_FAILURES` | `30` | 每個位址在 15 分鐘內登入失敗幾次後，該位址就要等待 |

### 機器

| 變數 | 預設值 | 用途 |
|---|---|---|
| `AGENT_OFFLINE_SECONDS` | `30` | 超過這段時間沒收到任何訊息的機器會顯示離線。agent 每隔它的三分之一送出一次心跳（至少每 2 秒一次） |
| `AGENT_OFFLINE_MOVE_SECONDS` | `600` | 離線這麼久的機器（從最後一次心跳起算，伺服器啟動得更晚時從伺服器啟動起算），它上面沒有固定機器的部署，副本會重新放到放得下的機器上；固定在它上面的部署會等它。`0`：副本永遠等原本的機器（[詳細說明](how-to.md#when-a-machine-stays-offline)） |
| `IMAGE_UPLOAD_MAX_GB` | `64` | 管理員從機器頁面（**推論引擎的映像檔** → **上傳映像檔**）上傳到機器的映像檔最大大小，單位 GiB。檔案經由伺服器串流給 agent，不會被儲存 |
| `AGENT_IMAGE` | — | **加入機器** → **Docker** 指令裡的 agent 映像檔。留空：使用已發布的 `docker.io/tonyhuang1101/forgeyard-agent`，tag 是這台伺服器的版本；還沒有發布時則用本機建置的映像檔。可以設成鏡像站或私有 registry（`registry.example.org/forgeyard/forgeyard-agent`；tag 仍是伺服器的版本），或加上 `:TAG` 或 `@sha256:DIGEST` 指定特定版本。只接受單純的映像檔名稱 |

### 模型

| 變數 | 預設值 | 用途 |
|---|---|---|
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | 允許使用私有位址和純 http 的模型來源（區域網路內的鏡像站；agent 也需要設定 `allow_private_hosts`）。檢查推論引擎新版本時也適用 |
| `MODEL_URL_HOSTS` | — | `https://` GGUF 來源允許的主機，以逗號分隔。只是初始值：管理員一旦在 **模型 → 來源** 儲存過，就以資料庫為準。在那之前，面板會把它標示為 *from .env* |
| `MODEL_LOCAL_DIRS` | — | `local://` 模型允許的目錄，以逗號分隔。和 `MODEL_URL_HOSTS` 一樣只是初始值 |
| `GPU_CONTEXT_MIB` | `400` | 副本的 CUDA context 在它本身之外額外佔用的 GPU 記憶體 |
| `VRAM_HEADROOM_MIB` | `512` | 放置副本時，每張卡一律保留不用的記憶體 |

### 閘道

| 變數 | 預設值 | 用途 |
|---|---|---|
| `GATEWAY_CONNECT_TIMEOUT` | `5` | 連線到副本的秒數上限，超過就改試下一個 |
| `GATEWAY_READ_TIMEOUT` | `600` | 副本回傳非串流回應標頭，或串流第一段之後每一段的秒數上限（串流的第一批位元組：部署的 *等待首次回應*，預設 120 秒，請見[重試與等待第一個回應](#retries-and-the-wait-for-a-first-answer)） |
| `GATEWAY_MAX_BODY_MB` | `32` | 請求本文的大小上限（用來轉錄的音訊檔是最大的）。部署的配方有 *Largest request* 參數（`max_body_mb`，vLLM 與 llama.cpp 預設 64）時，改以該值為上限 |
| `GATEWAY_DEFAULT_RPM`, `GATEWAY_DEFAULT_TPM` | `0` | 沒有自己限制的金鑰，每分鐘的請求數和 token 數（0：不限制）。管理員可以在 **API 金鑰** 修改 |
| `GATEWAY_MAX_RPM`, `GATEWAY_MAX_TPM` | `0` | 任何人能為自己的金鑰設定的最大值（0：沒有上限） |

### 對話中的媒體

| 變數 | 預設值 | 用途 |
|---|---|---|
| `MEDIA_FETCH_MAX_MB` | `20` | 閘道為一個媒體網址（對話請求裡的 `https://` 圖片）最多下載多少，超過就拒絕（413 `media_too_large`） |
| `MEDIA_ALLOW_PRIVATE_HOSTS` | `false` | 允許媒體網址指向私有或 loopback 位址（區網內的圖片伺服器）。link-local 與 metadata 位址仍然拒絕 |

媒體網址也遵守下方的 `EGRESS_PROXY`、`EGRESS_ALLOWED_HOSTS` 與 `EGRESS_CA_FILE`。請見
[對話中的圖片、音訊與影片](#images-audio-and-video-in-chat)。

### 雲端供應商

| 變數 | 預設值 | 用途 |
|---|---|---|
| `GATEWAY_CLOUD_DEFAULT` | `off` | 新 API 金鑰拿到的雲端設定：`off`、`fallback` 或 `on`。管理員可以在 **API 金鑰** 修改（存在資料庫裡）；既有的金鑰維持原本的設定 |
| `GATEWAY_CLOUD_MAX` | `off` | 任何人能為自己的金鑰設定的最大值，每個請求也會套用到既有的金鑰。管理員可以為任何金鑰設定任何值 |
| `EGRESS_ALLOWED_HOSTS` | 空白 | 只允許這些供應商主機，以逗號分隔（`*.example.org` 代表子網域）。空白：不限 |
| `EGRESS_CA_FILE` | 空白 | 除了系統的 CA 之外，供應商連線額外信任的 CA 檔（例如會檢查 TLS 的公司 proxy） |
| `PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | 讓標示為在你網路外的供應商也能使用私有或 loopback 位址和純 http。只用於以替身伺服器測試；link-local 和 metadata 位址仍然一律拒絕 |
| `PROVIDER_CONNECT_TIMEOUT` | `10` | 連線到供應商的秒數上限，超過就改試下一個目標 |
| `PROVIDER_READ_TIMEOUT` | `300` | 供應商回傳回應標頭或串流下一段內容的秒數上限（每個供應商可以另外設定） |
| `BUDGET_DEFAULT_MAX_TOKENS` | `4096` | 預算會在雲端請求送出前先預留它的估計費用。沒有 `max_tokens` 的請求，以這個 token 數估計它的回應 |

`EGRESS_PROXY` 在[常用設定](#common-settings)裡。`GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、`GATEWAY_CLOUD_DEFAULT` 和 `GATEWAY_CLOUD_MAX` 也都只是初始值：管理員一旦在 **API 金鑰 → 金鑰的限制** 或 **雲端政策** 儲存過，就以資料庫為準；如果環境裡還留著另一個值，設定健康檢查會提醒。在管理員儲存之前，這些面板會把來自環境的值標示為 *from .env*。

### 已移除的設定

**在 1.4 移除。** 這些設定在 1.3.1 標為淘汰，現在已經不再讀取。如果伺服器的環境或 `.env` 裡還設著其中一個，啟動時會記下一則警告，說明它被忽略、改用什麼，設定健康檢查也會列出（`setting-removed`），直到你把它移除。

- `SECRET_KEY_FILE`：改用 `FORGEYARD_SECRET_KEY_FILE`，值不變，只是換成新名稱。如果 `SECRET_KEY_FILE` 指向 `data/secret.key` 以外的金鑰檔，而 `FORGEYARD_SECRET_KEY_FILE` 和 `FORGEYARD_SECRET_KEY` 都沒有設定，**伺服器會拒絕啟動**，而不是改用（或新建）另一把金鑰，並請你改名。你改好之前什麼都不會變動；`secrets reset` 也會拒絕執行。
- `AGENT_HEARTBEAT_SECONDS`：agent 每隔 `AGENT_OFFLINE_SECONDS` 的三分之一送出心跳，至少每 2 秒一次（預設是 10 秒）。
- `FORGEYARD_SETUP_TOKEN`：伺服器會印出一個隨機的設定權杖（設定完成前存在 `data/secrets.json`）。無人值守的第一次啟動請用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`。
- `USAGE_FLUSH_SECONDS`：記憶體中統計的用量每 5 秒寫入資料庫一次。
- `PROVIDER_COOLDOWN_SECONDS`：回應 429 或 5xx、或連不上的供應商目標，30 秒內排到最後才嘗試（如果它的 `Retry-After` 更長就照它，最多 5 分鐘）。
- `PROVIDER_RETRY_AFTER_CAP`：沒有其他目標可試時，不超過 2 秒的 `Retry-After` 會等一次再試。
- `OPENROUTER_MODELS_URL`：**從 OpenRouter 更新價格** 一律讀取 OpenRouter 的公開清單（只在管理員要求時讀取，不送出金鑰）。

### 單位、零與空白

| 種類 | 寫法 |
|---|---|
| 時間長度 | 單位是秒，除非名稱裡有 `MINUTES` 或 `DAYS`。路由的 *等待空出本機位置* 單位是毫秒 |
| 大小 | `_MIB` 是 MiB。`GATEWAY_MAX_BODY_MB` 和 agent 的 `log_max_mb` 雖然名稱是 MB，單位也是 MiB。在表單裡輸入的大小可以寫 `8Gi`（GiB）；`8G` 和 `8GB` 是十進位（10⁹ 位元組） |
| 金額 | 網頁介面裡是美元；API 裡是微美元（`_micros`）；價格以每百萬 token 計 |
| 頻率 | 以每分鐘計；`LOGIN_IP_FAILURES` 是計算 15 分鐘內的次數 |

| 設定 | `0` 代表 | 空白代表 |
|---|---|---|
| `GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、**金鑰的限制** | 不限制 | 不限制 |
| 金鑰自己的每分鐘請求數或 token 數 | 拒絕（至少為 1） | 伺服器的預設值 |
| 預算（金鑰、個人、供應商） | **$0：它適用的每個雲端請求都會被擋下** | 沒有預算 |
| `RATE_LIMIT_PER_MINUTE` | 關閉 | — |
| 路由的 *等待空出本機位置* | 立刻溢出 | — |

`/v1` 的標頭和錯誤碼列在[閘道（/v1）](#the-gateway-v1)。

## agent 設定

如果 `/etc/forgeyard/agent.toml` 存在，agent 會先讀取它（可用 `--config PATH` 或 `FORGEYARD_AGENT_CONFIG` 指定其他檔案），接著讀取 `FORGEYARD_*` 環境變數，最後是旗標。**旗標優先於環境變數，環境變數優先於檔案，檔案優先於預設值。** labels 會依照相同順序逐個 key 合併。

`forgeyard agent init` 會根據在機器上偵測到的內容寫出完整的檔案，並說明每個設定。`forgeyard agent config` 會顯示目前生效的設定以及每個值的來源，遇到有錯誤的檔案會拒絕並指出錯誤（未知的設定會附上最接近的正確名稱；型別錯誤；值不正確）。

| 設定（`agent.toml`） | 環境變數 | 旗標 | 預設值 | 用途 |
|---|---|---|---|---|
| `server` | `FORGEYARD_SERVER` | `--server` | — | 伺服器的位址，和 Machines 頁面上顯示的一樣。 |
| `ca_fingerprint` | `FORGEYARD_CA_FINGERPRINT` | `--ca-fingerprint` | — | 以 SHA-256 指紋釘選伺服器的憑證授權單位（取自加入指令）。只接受由它簽發的憑證。 |
| `ca_file` | `FORGEYARD_CA_FILE` | `--ca-file` | — | 或改為釘選這個 CA 檔案（公司的 CA、TLS 代理的 CA）。兩者不能同時使用。 |
| `name` | `FORGEYARD_NAME` | `--name` | — | 機器在伺服器上的名稱。預設：主機名稱。 |
| `state_dir` | `FORGEYARD_AGENT_STATE_DIR` | `--state-dir` | — | 存放機器的憑據、釘選的 CA 和副本資料的位置（權限 0700）。預設：以 root 執行時為 /var/lib/forgeyard-agent，否則為 ~/.local/state/forgeyard-agent。 |
| `log_level` | `FORGEYARD_LOG_LEVEL` | `-v` | `info` | info，或 debug 以顯示所有訊息（-v）。 |
| `labels` | `FORGEYARD_LABELS` | `--label` | — | 顯示在 Machines 頁面上的標籤，例如 { rack = "a" }。環境變數格式：rack=a,room=2。逐個 key 合併：旗標優先於環境變數，環境變數優先於檔案。 |
| `[workloads]` `enabled` | `FORGEYARD_WORKLOADS` | `--no-workloads` | `true` | false：只回報硬體，不執行任何東西。 |
| `[workloads]` `user` | `FORGEYARD_WORKLOAD_USER` | `--workload-user` | `forgeyard-run`（以 root 執行且帳號存在時） | agent 以 root 執行時，行程使用的無特權帳號。預設：forgeyard-run 帳號存在時就用它（agent 不會自己建立）；沒有的話，以 root 執行的 agent 會拒絕行程。 |
| `[workloads]` `work_dir` | `FORGEYARD_WORK_DIR` | `--work-dir` | — | 每個行程副本一個目錄。預設：放在狀態目錄旁邊（<state_dir>-work）。 |
| `[workloads]` `port_range` | `FORGEYARD_PORT_RANGE` | `--port-range` | `30000-32767` | 服務和模型副本使用的主機連接埠。 |
| `[workloads]` `bind_address` | `FORGEYARD_BIND_ADDRESS` | `--bind-address` | — | 公開的連接埠監聽的位址。預設：這台機器連往伺服器時使用的位址。 |
| `[workloads]` `container_engine` | `FORGEYARD_CONTAINER_ENGINE` | `--container-engine` | `auto` | auto（先試 docker，再試 podman）、docker、podman、none，或容器引擎 CLI 的路徑。 |
| `[workloads]` `limits` | `FORGEYARD_LIMITS` | `--limits` | `auto` | 行程的資源限制方式：systemd（有 CPU 和記憶體上限的 scope；需要在有 systemd 的主機上以 root 執行）、rlimit（沒有 systemd，例如在容器裡），或 auto。 |
| `[workloads]` `log_max_mb` | `FORGEYARD_LOG_MAX_MB` | `--log-max-mb` | `10` | 每個副本保留的輸出量，單位 MB。 |
| `[workloads]` `pull_stall_minutes` | `FORGEYARD_PULL_STALL_MINUTES` | `--pull-stall-minutes` | `5` | 連續這麼多分鐘沒有收到新的位元組時，拉取映像檔就算失敗（解開各層時是三倍）。持續有進度的慢速拉取絕不會被中止。 |
| `[models]` `cache` | `FORGEYARD_MODEL_CACHE` | `--model-cache` | `/srv/forgeyard-models`（以 root 執行時） | 下載並保存模型和推論引擎的位置。預設：以 root 執行時是 /srv/forgeyard-models，否則是 ~/.local/share/forgeyard-models。設成空白（""）：這台機器不提供任何模型。在 Docker 映像檔中，容器內外必須是相同路徑。 |
| `[models]` `local_dirs` | `FORGEYARD_MODEL_LOCAL_DIRS` | `--model-local-dir` | — | 這台機器上 local:// 模型可以使用的目錄。環境變數格式：以逗號分隔。 |
| `[models]` `allow_private_hosts` | `FORGEYARD_ALLOW_PRIVATE_MODEL_HOSTS` | `--allow-private-model-hosts` | `false` | 允許從私有位址和純 http 下載模型（僅限區域網路內的鏡像站）。 |

**有兩個預設值取決於機器**，和 `install.sh` 的設定一致：agent 以 root 執行時，模型快取是 `/srv/forgeyard-models`（否則是 `~/.local/share/forgeyard-models`）；agent 以 root 執行且 `forgeyard-run` 帳號存在時，行程以它執行。agent 不會自己建立這個帳號，`install.sh` 和 agent 映像檔會。`forgeyard agent config` 會把它們標示為 `(default)`。不想讓某台機器提供模型，就把快取設成空白（`--model-cache ""`、`FORGEYARD_MODEL_CACHE=` 或 `cache = ""`）。

`server`、`ca_fingerprint`、`ca_file` 和 `name` 是加入時使用的。機器加入之後，它的狀態目錄會記錄要連哪台伺服器、信任哪個 CA：檔案或環境變數裡如果有不同的值，會被回報並忽略；要更改請用 `forgeyard agent trust`。

加入權杖（join token）絕不會寫進檔案。請用 `--join-token` 或 `FORGEYARD_JOIN_TOKEN` 提供；它只會使用一次。如果機器已經加入過，權杖會被忽略（紀錄裡會寫明），所以同一個指令或容器可以重複啟動。

範例檔案：

```toml
server = "https://192.168.1.20:7860"
ca_fingerprint = "sha256:3f9a…"
name = "gpu-01"
labels = { rack = "a", room = "lab" }

[workloads]
user = "forgeyard-run"
port_range = "30000-32767"
container_engine = "docker"
limits = "auto"

[models]
cache = "/srv/forgeyard-models"
local_dirs = ["/data/models"]
```

### agent 讀取的其他變數

這些不是 `agent.toml` 裡的設定：

| 變數 | 用途 |
|---|---|
| `FORGEYARD_AGENT_CONFIG` | 設定檔，取代 `/etc/forgeyard/agent.toml`（等同 `--config PATH`） |
| `FORGEYARD_JOIN_TOKEN` | 一次性的加入權杖（等同 `--join-token`） |
| `DOCKER_HOST`、`DOCKER_CONFIG`、`CONTAINER_HOST`、`XDG_RUNTIME_DIR` | 傳給 `docker` 或 `podman` 指令，讓非預設的容器引擎 socket 或設定可以運作。agent 環境裡的其他東西都不會傳給推論引擎 |
| `ROCM_PATH`、`ROCM_HOME` | ROCm 不在 `/opt/rocm` 時的安裝位置（AMD 卡） |
| `FORGEYARD_AGENT_SIMULATION` | 設為 `1` 才允許 `--simulate-inventory FIXTURE`：agent 回報錄好的硬體資料，而不是自己的硬體。僅供開發和測試；伺服器會把這種機器標示為模擬 |

## 閘道（/v1）

程式以 API 金鑰呼叫 `https://<server>:7860/v1` 上的模型：`Authorization: Bearer fyk_…`。閘道不接受 cookie，也不送 CORS 標頭，所以網頁無法借用已登入的瀏覽器來呼叫它。

### 端點

| 端點 | 提供什麼 | 雲端供應商 |
|---|---|---|
| `GET /v1/models` | 這把金鑰可以使用的模型。以下模型不會列出：對這把金鑰不存在的模型（只有雲端，而金鑰的雲端設定不是 **允許**）、不在金鑰模型清單內的模型，以及部署全部停止的模型（除非還有這把金鑰可用的供應商能提供它） | — |
| `GET /v1/models/{model}` | 單一模型，格式相同。模型不存在、對這把金鑰隱藏或已停止時回 404 `model_not_found`；不在金鑰模型清單內時回 403 `model_not_allowed` | — |
| `POST /v1/chat/completions` | 對話模型（vLLM、llama.cpp、供應商的對話模型）。`stream: true` 會以 server-sent events 串流 | 是 |
| `POST /v1/completions` | 同樣的模型，使用單純的 `prompt` | 是 |
| `POST /v1/embeddings` | 嵌入模型 | 是 |
| `POST /v1/rerank` | 重排序模型（vLLM 的 rerank API） | 否 |
| `POST /v1/score` | 重排序模型（vLLM 的 score API） | 否 |
| `POST /v1/audio/transcriptions` | 語音轉文字（faster-whisper），以 `multipart/form-data` 送出，含 `model` 欄位 | 否 |
| `POST /v1/audio/translations` | 語音轉成英文文字，格式同上 | 否 |
| `POST /v1/audio/speech` | 文字轉語音（1.3）：OpenAI 的請求格式，音訊一邊產生一邊傳回。見[文字轉語音](#text-to-speech) | 否 |
| `GET /v1/audio/voices?model=<model>` | 語音模型的聲音、預設聲音、它能產生的格式、串流方式與輸入上限 | — |
| `POST /v1/audio/voices`、`DELETE /v1/audio/voices/{name}` | 上傳或刪除聲音（聲音複製）：1.3 一律回應 400 `voice_cloning_not_supported` | — |
| `POST /v1/images/generations` | 圖片生成（1.3）：OpenAI 的請求格式，圖片只以 base64（`b64_json`）傳回。見[圖片生成](#images) | 否 |
| `POST /v1/images/edits`、`POST /v1/images/variations` | 圖片編輯與變體：1.3 不提供，一律回應 404 `endpoint_not_supported` | — |
| `POST /v1/videos` | 影片生成（1.3）：要求一段影片，立刻回應這個工作。可用 JSON、`multipart/form-data`（OpenAI SDK 送的格式）或 URL 編碼表單。見[影片生成](#video) | 否 |
| `GET /v1/videos` | 這把金鑰自己的影片，最新的在前：`limit`（1 到 100，預設 20）、`after`（看過的最後一個 id）、`order`（`desc` 或 `asc`） | — |
| `GET /v1/videos/{id}` | 一個影片工作：它的狀態與進度 | — |
| `GET /v1/videos/{id}/content` | 完成的影片，`video/mp4`，從製作它的副本串流過來。只有 `variant=video`；`Range` 請求回應 206 | — |
| `DELETE /v1/videos/{id}` | 在它的副本上取消製作中的工作，或刪除完成的影片 | — |
| `POST /v1/videos/sync`、`POST /v1/videos/{id}/remix` | 不提供：一律回應 404 `endpoint_not_supported` | — |
| `POST /v1/vision/detect` | 物件偵測（1.4）：送一張圖片，傳回方框、標籤和分數；零樣本偵測模型（1.4.4）用 `labels` 告訴它要找什麼。見[視覺](#vision) | 否 |
| `POST /v1/vision/segment` | 用 SAM 2 分割（1.4）：送一張圖片和一個點或一個方框，傳回 PNG 遮罩 | 否 |
| `POST /v1/vision/classify` | 圖片分類（1.4.4）：送一張圖片，傳回最符合的類別和分數；零樣本分類器（CLIP、SigLIP）由 `labels` 給它可選的標籤 | 否 |
| `POST /v1/vision/depth` | 深度估計（1.4）：送一張圖片，傳回 16 位元深度圖 | 否 |
| `POST /v1/classify` | 文字分類（1.4.4）：送文字，傳回每段的標籤和機率。見[文字分類](#text-classification) | 否 |

`/v1` 底下的其他路徑都回 404 `unknown_endpoint`。

閘道如何處理一個請求：

- 從請求內容讀取 `model`，把請求送到這個模型的路由。模型在另一個端點提供時（例如把嵌入模型送到 `/v1/chat/completions`），回 400 `wrong_endpoint`。
- 把 `model` 換成副本或供應商預期的名稱，加上副本的內部金鑰或供應商的金鑰，你的其他東西一概不轉送：不轉送你的 API 金鑰，也不轉送你的其他標頭。
- `max_tokens`（或 `max_completion_tokens`）在請求到達時計入金鑰的每分鐘 token 數，也決定預算下這個請求最多可能花多少錢。文字轉語音則改成 `input` 的每個字元算一個 token（見[文字轉語音](#text-to-speech)）；圖片請求不計任何 token（見[圖片生成](#images)）。
- 串流的對話或補全會多要一個最後的用量區塊（`stream_options.include_usage`），以便計算 token。如果你沒有要求，閘道會再把它拿掉。
- 請求內容最多 `GATEWAY_MAX_BODY_MB`（32 MB），或模型自己的 *Largest request*（`max_body_mb`，vLLM 與 llama.cpp 部署預設 64 MB）。
- 你關閉連線時，閘道會立刻關閉往上游的請求，推論引擎就會停止生成。

### 重試與等待第一個回應

- **一個請求最多只執行一次。** 只有在請求確定沒有送到第一個副本時，閘道才會改試另一個副本（或供應商）：連線建立不起來（沒有程式在聽、被拒絕、`GATEWAY_CONNECT_TIMEOUT`）、沒有空出的連線，或副本的金鑰守門員回應它的推論引擎沒有在執行（503 帶 `X-Forgeyard-Not-Taken`，1.4.5 起的 agent）。請求送出之後才斷掉的連線（寫入請求時，或等待回應時）會回 502 `upstream_error`（供應商則為 `provider_error`），而且不會再送到別處：推論引擎可能已經開始處理，生成或付費的雲端請求會跑兩次。如果安全，請自己重試。1.4.5 之前的 agent 在推論引擎沒有執行時會直接關閉連線，現在會被當成這種斷線：請更新 agent，才能在 agent 回報推論引擎停止前的那幾秒內保有容錯移轉。
- **串流的第一個回應。** 串流請求（`stream: true`，或帶 `stream_format` 的語音）寫入副本之後，副本有部署的 *等待首次回應*（`first_answer_timeout`，預設 **120 秒**，5 到 3,600）送出狀態列和串流的第一批位元組。超過時請求會被關閉，推論引擎跟著停止，回應是 504 `first_answer_timeout`；它絕不會送到另一個副本。串流開始之後，之後每一段最多可以等 `GATEWAY_READ_TIMEOUT`（600 秒）；設得比它長的等待，第一批位元組也會照設定等。**非串流的回答就是整個回答，所以這個等待不適用**：它維持 `GATEWAY_READ_TIMEOUT`（504 `upstream_timeout`），長的非串流生成和長錄音的轉錄不會被提早切斷。圖片請求維持部署自己的 *Timeout*；影片工作也是用自己的；這兩種部署不接受 `first_answer_timeout`。它在部署和修改表單的 **進階** 裡；修改它不會重新啟動任何東西。

### 對話中的圖片、音訊與影片

`POST /v1/chat/completions` 接受 OpenAI 的內容區塊（content parts）。閘道接受什麼、轉送給推論引擎什麼：

| 區塊 | 接受的寫法 | 送給推論引擎的形式 |
|---|---|---|
| 文字 | `text`、`input_text`、`refusal` | 不變 |
| 圖片 | `image_url`（`{"url": …, "detail"}` 或單純字串）、`input_image` | 帶 `data:` URI 的 `image_url` |
| 音訊 | `input_audio`（`{"data": <base64>, "format"}`，或 `data:audio/…` URI）、`audio_url` | 帶 base64 `data` 的 `input_audio` |
| 影片 | `video_url`、`input_video` | 帶 `data:` URI 的 `video_url` |
| 檔案（PDF） | `file`，`file_data` 為 `data:` URI | 不變，只送給能接受檔案的配方（內建配方都不能） |

- 媒體網址可以是 `data:` URI（base64，且類型與區塊相符）或 `http(s)` 網址。**閘道會自己下載網址**，再以 `data:` URI 送給推論引擎：推論引擎從不自己抓任何東西。下載遵守 egress 規則：只連公開位址（開啟 `MEDIA_ALLOW_PRIVATE_HOSTS` 才允許私有位址；link-local 與 metadata 位址一律拒絕）、最多 3 次轉址且每次都重新檢查、每個項目最多 `MEDIA_FETCH_MAX_MB`、每個項目 20 秒，回應的類型也必須與區塊相符（圖片區塊要 `image/…`）。`file://` 與其他所有 scheme 回 400 `media_url_refused`。
- 請求只會送到能處理其內容的部署：模型接受這種媒體（從模型的 `config.json` 判斷，GGUF 則看它的 projector 檔），配方也接受，而且數量在部署的 *Images per request*（圖片與音訊片段，預設 4）與 *Videos per request*（預設 0：影片預設關閉）之內。否則回 400 `media_not_supported` 或 `too_many_media_items`。
- **1.3 中，帶媒體的請求絕不送往雲端供應商**（也不送往你網路內的供應商）。只由供應商提供的路由回 400 `media_local_only`；混合路由則不會溢出。
- 用量會計算媒體項目數（`GET /api/usage` 的 `media_items`）。圖片的 token 依推論引擎回報，包含在 `prompt_tokens` 中。

部署參數，位於部署表單的 **進階**（配方 `vllm` 與 `llama-cpp`）：

| 參數 | 預設值 | 用途 |
|---|---|---|
| `images`（*Images per request*） | `4` | 一個請求最多可帶的圖片與音訊片段數；0 表示關閉。以 `--limit-mm-per-prompt` 傳給 vLLM |
| `videos`（*Videos per request*） | `0` | 設為 1 以上才接受影片。在 vLLM 上，啟動時會為一段長影片預留記憶體 |
| `video_fps`（*Video frames per second*，僅 `llama-cpp`） | `1` | 影片每秒讀取幾格（`--video-fps`），用於沒有自己取樣率的模型；每一格大約和一張圖片一樣花 token。Qwen3-VL 不管這個值，固定每秒讀 2 格（llama.cpp b12242 與 1.0.0）：一段 6 秒的影片在 Qwen3-VL 2B 上用了 5,345 token。開啟影片的部署，上下文請設 8,192 以上 |
| `max_body_mb`（*Largest request*） | `64` | 閘道送給這個部署的最大請求，單位 MiB（超過回 413） |
| `image_tokens`（*Largest image*） | `1024` | 每張圖片最多縮成這麼多 token：Qwen3-VL 約 1024 × 1024 像素。`llama-cpp` 透過 `--image-max-tokens`；`vllm` 透過 `--mm-processor-kwargs` 的 `max_pixels`，適用於圖片 token 數會隨解析度增加的模型（Qwen-VL；Gemma 每張圖的 token 數固定）。不設的話，一張 5 MB 的照片在 llama.cpp 約 4,000 token（8 個 CPU 執行緒要 125 秒），在 vLLM 約 16,000 token，超過 8,192 token 的上下文。在這個設定出現之前加入的模型，要重新加入後 vLLM 才會套用上限 |

沒有這些參數的配方：4 張圖片或音訊片段、不接受影片、上限為 `GATEWAY_MAX_BODY_MB`。

### 文字轉語音

`POST /v1/audio/speech` 接受 OpenAI 的請求，回應的是音訊本身（不是 JSON），推論引擎一邊產生閘道就一邊轉送。三種推論引擎提供這項服務（配方 `vllm-omni-tts`、搭配 TTS GGUF 的 `llama-cpp`、`speaches-tts`）；只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 閘道怎麼處理 |
|---|---|
| `model` | 語音模型的名稱，和平常一樣 |
| `input` | 必填，最多為部署的 *Longest input*（`max_input_chars`；0 代表 vLLM-Omni 與 llama.cpp 取上下文長度的四分之一，預設即 1,024；speaches 為 4,096）。超過時回應 400 `input_too_long` |
| `voice` | 模型的聲音之一（`GET /v1/audio/voices`），名稱或 `{"id": …}` 皆可。模型沒有的 OpenAI 聲音名稱（`alloy`、`nova` 等），或沒有指定聲音時，改用配方的預設聲音或推論引擎列出的第一個；回應以 `X-Forgeyard-Voice` 標出（模型沒有內建聲音、只用它自己的一種聲音時為 `default`） |
| `response_format` | `mp3`、`opus`、`aac`、`flac`、`wav` 或 `pcm`，且必須是推論引擎能產生的；否則回應 400 `unsupported_response_format`。沒指定時用推論引擎的預設（vLLM-Omni 與 llama.cpp 為 `wav`，speaches 為 `mp3`），不是 OpenAI 的 `mp3` |
| `stream_format` | `audio`：一邊產生一邊傳原始音訊（串流的 WAV 會宣告未知長度）。`sse`：OpenAI 的 `speech.audio.delta` / `speech.audio.done` 事件（僅 vLLM-Omni）。沒指定時傳回整個檔案。推論引擎不支援時回應 400 `stream_format_not_supported`。`stream: true` 等同 `stream_format: audio` |
| `speed` | 0.25 到 4，原樣轉送（llama.cpp 只接受 1.0，speaches 的 Kokoro 為 0.5 到 2） |
| `instructions` | 原樣轉送：vLLM-Omni 的 Qwen3-TTS 會用它（風格與情緒），其他推論引擎忽略 |
| `language`、`lang`、`sample_rate`、`seed` 等 | 原樣轉送。對 llama.cpp，若請求沒給，閘道會依文字的書寫系統設定 `lang`（`zh`、`ja`、`ko`），並設定 `n_predict`（每個字元十個音訊幀，至少 512，最多到上下文長度） |

**1.3 不提供聲音複製。** 參考錄音（`ref_audio`、`ref_text`、`speaker_ref_b64`、`speaker_embedding` 等）、以音訊資料或連結當作聲音、Qwen3-TTS 的 `task_type: "Base"`，以及上傳或刪除聲音，都回應 400 `voice_cloning_not_supported`，任何內容都不會送到推論引擎。

每個有回應的請求會計入（`GET /api/usage` 與 **用量** 頁面）：

| 單位 | 怎麼算 |
|---|---|
| `characters` | 輸入的字元數（Unicode 碼位），送出前就算好 |
| `media_ms` | 音訊長度（毫秒），在轉送時讀出：`wav` 看檔頭（串流時算檔頭之後的位元組），`pcm` 依大小與回應標明的取樣率（`x-audio-sample-rate`，或類型裡的 `rate=`；否則用該聲音自己的取樣率），`flac` 看 STREAMINFO，`mp3` 與 `aac` 數音框，`opus` 看最後一個 Ogg 頁；SSE 依 `pcm` 片段的大小。其他回應不計 |
| tokens | 只計推論引擎回報的：vLLM-Omni 的 `x-vllm-omni-input-tokens` 與 `-output-tokens`，或 `speech.audio.done` 的 usage |

**限制：** rpm 和所有請求一樣；**tpm 以 `input` 的每個字元算一個 token**，在請求到達時先預留（15 個字元的請求需要 15 個 token 的額度）。預算只管雲端花費，而語音從不送往雲端。金鑰的模型清單同樣適用於語音和聲音清單。

`GET /v1/audio/voices?model=<model>` 回應
`{"object": "list", "model", "voices": [{"id", "name", "language"?, "gender"?}], "default", "formats",
"default_format", "stream_formats", "max_input_chars"}`，向一個就緒的副本詢問，保留 5 分鐘。上傳到推論引擎的聲音一律不列出。

### 圖片生成

`POST /v1/images/generations` 接受 OpenAI 的請求，回應 OpenAI 的 JSON，圖片以 base64 傳回。兩種推論引擎提供這項服務（配方 `vllm-omni-image` 在 NVIDIA 上跑 diffusers pipeline，`sd-cpp` 在 AMD（Vulkan）、NVIDIA 和 CPU 上跑 stable-diffusion.cpp）；只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 閘道怎麼處理 |
|---|---|
| `model` | 圖片模型的名稱，和平常一樣 |
| `prompt` | 必填，最多為部署的 *Longest prompt*（`max_prompt_chars`，4,000）。超過時回應 400 `prompt_too_long`。提示裡的推論引擎設定（`<sd_cpp_extra_args>`）會被拒絕：請改用下面的欄位 |
| `n` | 1 到部署的 *Images per request*（`max_images`，預設 4）；超過時回應 400 `too_many_images`。沒指定時為 1 |
| `size` | `WIDTHxHEIGHT`；`auto` 或沒指定時用部署的 *Default size*（模型自己的：Stable Diffusion 1.5 為 512x512，SDXL、Z-Image 與 FLUX 為 1024x1024）。每一邊最多 *Largest side*（`max_side`，0 = 模型自己的）；超過時回應 400 `image_too_large`。每一邊必須是 16 的倍數（diffusion transformer）或 64 的倍數（Stable Diffusion 的 UNet）；否則回應 400 `invalid_size` |
| `response_format` | 不指定或 `b64_json`。**`url` 會被拒絕**，回應 400 `response_format_not_supported`：Forgeyard 不保留它產生的圖片，所以沒有網址可給。請解碼 `data[].b64_json` |
| `output_format` | `png`（預設）、`jpeg` 或 `webp`；其他回應 400 `unsupported_output_format`。jpeg 與 webp 可用 `output_compression`（0 到 100） |
| `num_inference_steps` | 去噪步數，最多為部署的 *Most steps*（`max_steps`，50）；超過時回應 400 `too_many_steps`。沒指定時用部署的 *Default steps*，否則用模型自己的 |
| `negative_prompt`、`guidance_scale`、`seed` | 轉送：對 vLLM-Omni 以欄位轉送，對 stable-diffusion.cpp 則放進提示裡的 `<sd_cpp_extra_args>`（由閘道產生）。沒有 `seed` 時，每個請求都是一張新圖片 |
| `true_cfg_scale` | 只有 vLLM-Omni（Qwen-Image）使用；stable-diffusion.cpp 忽略它 |
| `quality`、`style`、`background`、`moderation`、`user` | 接受但忽略 |
| `stream` | 拒絕（400 `streaming_not_supported`）：圖片做好才會回應 |

其他東西都不會送到推論引擎：推論引擎會接受、但 Forgeyard 不提供的欄位（vLLM-Omni 的 `lora`，它帶的是伺服器上的路徑；它的 `extra_params` 與系統提示）都會被拿掉。

回應：`{"created", "data": [{"b64_json"}], "output_format", "size"}`。推論引擎自己附加的內容（stable-diffusion.cpp 帶著提示與設定的 `generation_info`、vLLM-Omni 的各階段耗時）不會轉送。推論引擎的錯誤會以 OpenAI 的格式傳回，帶著它的狀態碼和 `engine_error` 這個 code。

**佇列。** 部署的每個副本同時處理 *Requests at once*（`concurrency`，1）個請求；其他請求在閘道裡等待，先到先處理，最多 *Queue length*（`queue`，8）個。再多一個就回應 429 `queue_full` 並附上 `Retry-After`（依它最近的請求花了多久計算）。等待超過部署 *Timeout* 的請求回應 503 `queue_timeout`；副本接手之後，閘道最多等待這個 *Timeout*（`timeout`：vLLM-Omni 為 600 秒，stable-diffusion.cpp 為 1,800 秒）取得圖片，之後回應 504 `upstream_timeout`。在等待中放棄的用戶端會離開佇列；它的請求絕不會送到推論引擎。

**不過濾提示。** 閘道只檢查請求的格式與部署的上限，不檢查提示要求畫什麼。如果部署需要安全過濾，請在它前面加一層。

每個有回應的請求會計入（`GET /api/usage` 與 **用量** 頁面）：

| 單位 | 怎麼算 |
|---|---|
| `images` | 回應裡的圖片數 |
| `kilopixels` | 寬 x 高 / 1,000，從每張圖片自己的檔頭讀出（PNG、JPEG、WebP），否則用請求的尺寸 |
| `busy_ms` | 副本處理這個請求的時間（從送出到收到回應的最後一個位元組），乘以部署占用的 GPU 數（CPU 上為 1）：也就是 GPU 時間 |
| tokens | 不計。金鑰的每分鐘 token 數不適用；每分鐘請求數照樣適用 |

### 影片生成

`POST /v1/videos` 接受 OpenAI 的 Videos 請求，並立刻回應一個**工作**；影片在一個副本上製作，也由它保存。輪詢 `GET /v1/videos/{id}` 直到 `status` 是 `completed` 或 `failed`，再取回 `GET /v1/videos/{id}/content`。由一種推論引擎提供（配方 `vllm-omni-video`，NVIDIA 上的 vLLM-Omni）；只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 閘道怎麼處理 |
|---|---|
| `model` | 影片模型的名稱，和平常一樣 |
| `prompt` | 必填，最多為部署的 *Longest prompt*（`max_prompt_chars`，2,000）。超過時回應 400 `prompt_too_long` |
| `seconds` | 整數秒，數字或字串（`"5"`）都可以，1 到部署的 *Longest video*（`max_seconds`，5）；超過時回應 400 `video_too_long`。沒指定時用 *Default length*（`seconds`，5）。影格數是秒數 x 每秒影格數，再取到模型會做的數量（Wan：4k + 1，所以 16 fps 的 5 秒是 81 格） |
| `size` | `WIDTHxHEIGHT`；沒指定時用 *Default size*（模型自己的：Wan2.1 為 832x480，Wan2.2 為 1280x704）。每一邊最多 *Largest side*（`max_side`，0 = 模型自己的長邊），而且每格像素不超過預設尺寸放大到該邊時的像素；超過時回應 400 `video_too_large`。邊長是 16 的倍數（Wan2.2-5B 為 32）；否則回應 400 `invalid_size` |
| `num_inference_steps` | 去噪步數，最多為 *Most steps*（`max_steps`，50）；超過時回應 400 `too_many_steps`。沒指定時用 *Default steps*，否則用模型自己的 |
| `negative_prompt`、`guidance_scale`、`seed` | 轉送 |
| `input_reference`（以及 vLLM-Omni 的 `image_reference`、`video_reference`、`audio_reference`） | 拒絕，回應 400 `input_reference_not_supported`：1.3 只支援文字轉影片 |
| `quality`、`user` | 接受但忽略 |

其他東西都不會送到推論引擎：閘道只送出提示、模型、尺寸、影格數、每秒影格數和上面的欄位（絕不送 vLLM-Omni 的 `lora`、`extra_params` 或影格插補的路徑）。

**工作**採用 OpenAI 的格式：`{"id", "object": "video", "model", "status", "progress", "created_at",
"completed_at", "expires_at", "size", "seconds", "quality", "remixed_from_video_id", "error"}`。

- `id` 是 Forgeyard 自己的（`video_` 加上 32 個隨機十六進位數字），絕不是推論引擎的。別的金鑰的 id 回應 404 `video_not_found`，和不存在的 id 完全一樣。
- `status` 是 `queued`、`in_progress`、`completed` 或 `failed`。`progress` 是推論引擎回報的值：vLLM-Omni 在影片做好之前都是 0，做好後是 100。
- `seconds` 是字串，和 OpenAI 的 API 一樣。`expires_at` 在工作結束時設定：部署的 *Keep results* 之後。
- 工作 `failed` 時的 `error`（`{"code", "message"}`）：

| `error.code` | 意思 |
|---|---|
| `generation_failed` | 推論引擎做影片失敗；後面附上它的訊息 |
| `timed_out` | 在部署的 *Longest job*（`timeout`）內沒有做完；閘道已在副本上取消它 |
| `replica_restarted` | 副本的推論引擎重新啟動：它的工作（放在記憶體裡）也跟著不見了 |
| `replica_gone` | 副本被停止、重新配置或刪除 |
| `video_lost` | 副本已經不知道這個工作（它的推論引擎重新啟動過） |

不保存提示。工作的資料列在結果過期一週後刪除。

**內容。** `GET /v1/videos/{id}/content` 從製作它的副本串流 MP4，以 `video/mp4` 加上 `Content-Disposition: inline` 傳回。`variant=thumbnail` 和 `variant=spritesheet` 回應 400 `variant_not_supported`（推論引擎只做影片）。還沒做好的工作回應 409 `video_not_ready`，失敗的回應 409 `video_failed`，過期的回應 410 `video_expired`，副本後來重新啟動或被停止的回應 410 `video_lost`。

**刪除。** `DELETE /v1/videos/{id}` 在副本上取消製作中的工作，或刪除完成的影片，並移除這個工作：`{"id", "object": "video.deleted", "deleted": true}`。

**同時的工作數。** 一個副本一次做一段影片；其他的在它上面等待。閘道把工作送到工作最少的就緒副本。一個部署最多同時有 *Jobs at once*（`queue`，8）個在等待或製作中的工作，其中來自同一把金鑰（或試用區裡同一個人）的最多 *Jobs per key*（`jobs_per_key`，2）個。再多一個就回應 429 `queue_full` 或 `too_many_jobs` 並附上 `Retry-After`（依它最近的影片花了多久計算）。金鑰的每分鐘請求數會計入每個要求的工作；輪詢、列出和下載都不算。

**過期。** 結果在 *Keep results*（`keep_minutes`，1,440）內可以取回；之後伺服器會在副本上刪除它。推論引擎自己也會在那之後一小時刪除它的檔案（`VLLM_OMNI_SERVER_STORAGE__FILE_TTL`），而且檔案放在副本的容器裡，所以停止或重新啟動的副本不會留下任何檔案。

部署參數（配方 `vllm-omni-video`）：

| 參數 | 預設值 | 作用 |
|---|---|---|
| `default_size`（*Default size*） | `auto` | 沒指定尺寸的請求用這個尺寸；`auto` 是模型自己的 |
| `seconds`（*Default length*） | `5` | 沒指定長度的請求用這個長度 |
| `max_seconds`（*Longest video*） | `5` | 更長的請求：400。記憶體估算依它計算 |
| `max_side`（*Largest side*，進階） | `0` | 影片最寬或最高的像素數；0 是模型自己的 |
| `fps`（*Frames per second*，進階） | `0` | 0 是模型自己的（Wan2.1 為 16，Wan2.2 為 24） |
| `steps`、`max_steps`（進階） | `0`、`50` | 預設步數（0：模型自己的）與請求最多能要的步數 |
| `queue`（*Jobs at once*，進階） | `8` | 部署上等待或製作中的工作數 |
| `jobs_per_key`（*Jobs per key*，進階） | `2` | 其中單一金鑰或單一使用者的工作數 |
| `keep_minutes`（*Keep results*，進階） | `1440` | 完成的影片可以取回多久 |
| `timeout`（*Longest job*，進階） | `3600` | 從要求開始算的秒數；到時還在製作的工作會被取消並回報失敗 |
| `max_prompt_chars`（*Longest prompt*，進階） | `2000` | 更長的提示：400 |
| `offload`（*Offload to host memory*，進階） | `none` | `model`：卡上只放正在工作的元件（`--enable-cpu-offload`）；較慢 |

**每個工作在結束時計入一次**（`GET /api/usage` 與 **用量** 頁面）：

| 單位 | 怎麼算 |
|---|---|
| 請求數 | 每個工作一個。完成的算有回應；失敗的算一個錯誤，其他什麼都不算；製作中被刪除的算 499（不是錯誤）。被拒絕的請求（400、404、429）在被拒絕時計入 |
| `media_ms` | 影片的長度（毫秒），用推論引擎回報的值（否則用影格數 / 每秒影格數） |
| `busy_ms` | 從看到工作進入製作中到它做完的時間，乘以部署占用的 GPU 數：也就是 GPU 時間 |
| tokens | 不計 |

試用區的 **影片** 面板使用 `/api/playground/videos`（`POST`、`GET`）、`GET
/api/playground/videos/options?model=<model>`、`GET` 與 `DELETE /api/playground/videos/{id}`，以及 `GET
/api/playground/videos/{id}/content`：規則相同，以登入的使用者身分（session cookie 與主控台的 `X-Forgeyard: 1` 標頭），只能看到自己的工作，計為 console。工作存放在伺服器的資料庫裡（資料表 `video_jobs`，migration `0014`）。

### 視覺

`POST /v1/vision/detect`、`/v1/vision/segment`、`/v1/vision/depth`（1.4）和 `/v1/vision/classify`（1.4.4，圖片分類，任務 `image-classify`）接受一張圖片給視覺模型，回應 JSON。這是 Forgeyard 自己的 API（OpenAI SDK 沒有）：任何 HTTP 用戶端都能用。由 Forgeyard 自己的視覺伺服器提供（配方 `vision`，映像檔是以 digest 固定的 `forgeyard-vision`：NVIDIA 上用 CUDA 版，其他用 CPU 版），模型來自機器的模型快取。只由本機部署提供，絕不送往雲端供應商。

請求是 `multipart/form-data`，帶 `model`、檔案欄位 `image` 和各選項欄位；或是 JSON，帶 `model`、`image`（base64 或 `data:` URI）和選項。PNG、JPEG 或 WebP，最多 20 MiB、4,000 萬像素。閘道檢查有沒有模型和圖片，只換掉 `model`，其餘原樣交給副本；別種任務的模型回 400 `wrong_endpoint`（訊息會寫出它的端點）。

| 端點 | 選項 | 回應 |
|---|---|---|
| `detect` | `threshold`（0 到 1；0.5，零樣本偵測模型 0.3，OWL 0.1）、`max_detections`（1 到 1,000，100）；`labels`（1.4.4）：零樣本偵測模型要找的東西，名稱清單或以逗號分隔的字串，最多 32 個、每個最多 64 個字元 | `detections`：`[{"label", "score", "box": [x0, y0, x1, y1]}]`，分數高的在前，單位是圖片的像素；零樣本偵測模型的 `label` 是送出的名稱之一，原樣傳回 |
| `segment` | `points`（`[[x, y], …]`，最多 16 個）配 `labels`（1 在內、0 在外；省略時全是 1），或 `box`（`[x0, y0, x1, y1]`）；`multimask`（`true`：三個候選遮罩） | `masks`：`[{"score", "area", "box", "png"}]`，最好的在前；每個遮罩是和圖片同尺寸的 1 位元 PNG，base64 |
| `depth` | 無 | `depth_png`（和圖片同尺寸的 16 位元灰階 PNG，base64，越近越亮）、`min`、`max`、`unit`（相對深度，不是公尺） |
| `classify` | `top_k`（1 到 1,000；5，零樣本分類器是它的全部標籤）；`labels`（只有零樣本分類器能用，而且一定要給：可選的標籤，格式同 `detect`）；`template`（零樣本：`This is a photo of {}.`，用一個 `{}` 放標籤，最多 200 個字元） | `labels`：`[{"label", "score"}]`，最好的在前；`multi_label`（`true`：每個分數各自獨立，用 sigmoid，SigLIP 和多標籤分類器就是這樣；`false`：分數加起來是 1） |

每個回應另有 `model`、`width`、`height`（圖片的）和 `ms`（模型花的時間）。視覺伺服器的錯誤（`{"error": {"message", "code"}}`）照原狀態碼傳回：400 `invalid_image`、`invalid_option`、`missing_prompt`（分割時沒給點或方框）、`labels_required`（零樣本偵測模型沒給 `labels`）、`labels_not_supported`（固定類別的偵測模型或分類器收到 `labels`；兩者都是 1.4.4）、413 `image_too_large`。

**零樣本偵測模型（1.4.4，#142）**（Grounding DINO、MM-Grounding-DINO 與 LLMDet、OWL-ViT、OWLv2）找的是每個請求說的東西，而不是固定的類別清單：目錄標示 *找出你指定的東西*（`vision.zero_shot`）。送簡單的名稱，`labels: ["cat", "remote control"]`（或表單欄位 `labels=cat, remote control`）；重複和空的名稱會被去掉。視覺伺服器依各家族的方式詢問（Grounding DINO 是一句小寫提示「cat. remote control.」，每個方框取分數最高的那個名稱；OWL 是每個名稱一個查詢），所以名稱會照送出的樣子傳回。它們的分數比固定類別的偵測模型低：從 0.3（Grounding DINO）或 0.1 到 0.2（OWLv2）開始。它們需要 1.4.4 起的視覺映像檔，內建配方已經使用（固定在 `forgeyard-vision` 1.4.4rc1）；自訂配方指定較舊的映像檔時，部署會回 422 `vision_image_too_old`（本機建置照原樣接受）。

每個有回應的請求會計入（`GET /api/usage` 和 **用量** 頁）：

| 單位 | 怎麼算 |
|---|---|
| `images` | 1：看的那張圖片 |
| `kilopixels` | 寬 x 高 / 1,000，取自回應 |
| `busy_ms` | 副本處理它的時間 |
| tokens | 不計。金鑰的每分鐘 token 數不適用；每分鐘請求數和模型清單照常適用 |

一個副本一次處理一張圖片，其餘依序等待；要更高吞吐量就多開副本。哪些模型算視覺模型、授權規則，見[內建配方](#built-in-recipes)。

### 文字分類

`POST /v1/classify`（1.4.4）把文字送給文字分類模型，以 vLLM 的格式回應。這是 Forgeyard 自己的 API（OpenAI SDK 沒有）。推論引擎是 vLLM 的 pooling runner（配方 `vllm-pooling`）；閘道把請求送到副本的 `/classify`。只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 說明 |
|---|---|
| `model` | 模型名稱 |
| `input` | 一段文字，或 1 到 2,048 段文字；每段都不能是空的。不接受 token id |
| `truncate_prompt_tokens` | 選用：把每段文字截到這麼多 token（至少 1），或 -1 代表模型的最大值。沒有它時，比部署的 context 長的文字會被推論引擎拒絕 |
| `use_activation` | 選用，`true` 或 `false`：`false` 傳回原始分數（logits）而不是機率 |

閘道只送出這些欄位（其他欄位會被丟掉）；`messages`（vLLM 的對話格式）回 400。其他種類的模型回 400 `wrong_endpoint`，並說出它的端點（對話模型還會說明怎麼用結構化輸出分類）。

回應：`data` 依序每段文字一項：`index`、`label`（最可能的）、`probs`（每個標籤的機率，順序和模型的 `config.json` 相同；`multi_label_classification` 模型各自評分每個標籤，所以加起來不一定是 1）和 `num_classes`；`usage` 裡有 `prompt_tokens`。標籤名稱在目錄項目上（`GET /api/models/<name>` 的 `classify.label_names`）。

| 單位 | 怎麼算 |
|---|---|
| tokens | 推論引擎回應裡的 `prompt_tokens`，和嵌入一樣。金鑰的每分鐘 token 數照常適用 |

### 標頭

| 標頭 | 方向 | 值 |
|---|---|---|
| `Authorization` | 請求 | `Bearer fyk_…`：你的 API 金鑰 |
| `X-Forgeyard-Local-Only` | 請求 | `1`（或 `true`、`yes`、`on`）：這個請求不使用任何雲端供應商。它絕不會放寬金鑰的權限 |
| `X-Forgeyard-Target` | 回應 | 由誰回答：`local:<deployment>#<replica>` 或 `provider:<provider>/<model>`。絕不包含金鑰 |
| `X-Forgeyard-Replica` | 回應 | 本機回答時：`<model>#<replica>` |
| `X-Forgeyard-Spilled` | 回應 | 請求離開第一層的原因：`unavailable`、`busy`、`refused`、`too_long` 或 `budget`。第一層回答時沒有這個標頭 |
| `X-Forgeyard-Voice` | 回應 | 文字轉語音：請求沒指定聲音、或指定了模型沒有的 OpenAI 聲音時，閘道所選的聲音；模型只有自己的一種聲音時為 `default` |
| `X-Forgeyard-Dropped-Params` | 回應 | Forgeyard 在送往回答的供應商途中拿掉的請求參數（只有推論引擎才有的，例如 `top_k`、`chat_template_kwargs`；模型自己的拿掉清單），以逗號分隔。該供應商回錯誤時也會帶上。沒有拿掉任何參數時沒有這個標頭（[結構化輸出](cloud-providers.md#when-a-request-spills)永遠不會被拿掉：這種請求會略過那個供應商） |
| `X-Forgeyard-Budget-Exhausted` | 回應 | 讓請求無法送往雲端供應商的預算，以逗號分隔：`key`、`person` 或 `provider:<name>`。由其他目標成功回答時也會帶上 |
| `Retry-After` | 回應 | 要等待的秒數，出現在 429 `rate_limit_exceeded`、供應商有提供時的 429 `provider_rate_limited`、503 `local_busy`（1），以及原樣轉回的副本或供應商自己的 429 或 503 |

### 錯誤

錯誤採用 OpenAI 的格式，每個 OpenAI 用戶端函式庫都讀得懂：

```json
{"error": {"message": "This API key was revoked.", "type": "authentication_error", "param": null, "code": "revoked_api_key"}}
```

`type` 依狀態碼而定（`invalid_request_error`、`authentication_error`、`insufficient_quota`、`permission_error`、`rate_limit_error`、`upstream_error`、`service_unavailable`）。請依 `code` 處理錯誤：

| 狀態碼 | `code` | 意思 | 怎麼做 |
|---|---|---|---|
| 400 | `invalid_json` | 請求內容不是 JSON | 送 JSON |
| 400 | `invalid_request` | 沒有 `model`、請求內容不是物件，或音訊不是以 `multipart/form-data` 送出；語音請求沒有 `input`、`speed` 不在 0.25 到 4 之間、`stream_format` 是別的值，或 `GET /v1/audio/voices` 沒帶 `model` | 修正請求 |
| 400 | `wrong_endpoint` | 這個模型在另一個端點提供；訊息會寫出是哪一個 | 改用那個端點 |
| 400 | `context_length_exceeded` | 只出現在開啟 **提示詞太長時溢出** 的路由：提示放不進本機的上下文，而且沒有其他目標能接手 | 縮短提示或降低 `max_tokens` |
| 400 | `invalid_content_part` | 內容區塊不是帶 `type` 的物件、閘道不接受的類型（`*_embeds`、未知類型），或媒體區塊沒有網址 | 使用[對話中的圖片、音訊與影片](#images-audio-and-video-in-chat)列出的區塊 |
| 400 | `invalid_media_data` | `data:` URI 不是 base64，或類型與區塊不符（圖片區塊帶 `data:text/plain`），或 `file` 區塊沒有 `data:` URI | 送出 `data:<type>;base64,…` |
| 400 | `media_url_refused` | 媒體網址的 scheme 不是 http(s) 或 `data:`（例如 `file://`）、帶有帳密，或主機解析到私有、loopback、link-local 或 metadata 位址（轉址後也一樣）；`input_audio.data` 裡放網址 | 送公開網址或 `data:` URI |
| 400 | `media_fetch_failed` | 閘道無法下載媒體網址：沒有回應、狀態碼不是 200、超過 3 次轉址、超過 20 秒，或類型與區塊不符 | 檢查網址，或改以 `data:` URI 送出檔案 |
| 400 | `media_not_supported` | 模型不接受這種媒體（把圖片送給純文字模型、*Videos per request* 為 0 時送影片）；訊息會說它接受什麼 | 改用能處理的模型，或在部署上開啟影片 |
| 400 | `too_many_media_items` | 圖片（或音訊片段、影片）多於部署的 *Images per request* 或 *Videos per request* | 少送一些，或調高參數 |
| 400 | `file_input_not_supported` | 把 `file` 區塊（PDF）送給配方不接受檔案的模型；閘道不會轉換檔案 | 改送頁面圖片或文字 |
| 400 | `media_local_only` | 把帶媒體的請求送給只由供應商提供的模型 | 改用本機模型 |
| 400 | `input_too_long` | 文字轉語音：`input` 超過部署的 *Longest input* | 把文字拆開，或調高 `max_input_chars` |
| 400 | `unsupported_response_format` | 文字轉語音：`response_format` 不是 OpenAI 的格式，或推論引擎不產生這種格式；訊息會列出它能產生的格式 | 改用其中一種 |
| 400 | `stream_format_not_supported` | 文字轉語音：推論引擎不提供這種 `stream_format`（llama.cpp 或 speaches 的 `sse`） | 拿掉它，或用 `audio` |
| 400 | `language_not_supported` | 文字轉語音：把中文、日文或韓文送給 speaches，它會把這些字刪掉 | 改用 Qwen3-TTS 部署 |
| 400 | `voice_cloning_not_supported` | 文字轉語音：帶了參考錄音、以音訊或連結當聲音、`task_type: "Base"`，或上傳聲音。1.3 不提供 | 使用模型自己的聲音 |
| 400 | `response_format_not_supported` | 圖片：`response_format: url`。圖片只以 base64 傳回 | 拿掉它，或要求 `b64_json` |
| 400 | `streaming_not_supported` | 圖片與視覺：`stream: true` | 拿掉它 |
| 400 | `missing_image` | 視覺：沒有檔案欄位 `image`，或 JSON 沒有 `image` | 附上圖片 |
| 400 | `labels_required` | 視覺（1.4.4）：沒告訴零樣本偵測模型要找什麼，或沒給零樣本分類器它的標籤 | 送 `labels` |
| 400 | `labels_not_supported` | 視覺（1.4.4）：固定類別的偵測模型或分類器收到 `labels` | 拿掉 `labels`，或改用零樣本模型 |
| 400 | `too_many_images` | 圖片：`n` 超過部署的 *Images per request* | 少要幾張，或調高 `max_images` |
| 400 | `image_too_large` | 圖片：某一邊超過部署的 *Largest side* | 要小一點的尺寸，或調高 `max_side` |
| 400 | `invalid_size` | 圖片：`size` 不是 `WIDTHxHEIGHT`（每邊 64 到 8,192），或某一邊不是模型接受的倍數（16 或 64；訊息會說明）。影片：同上，每邊 64 到 2,048，16 或 32 的倍數 | 使用類似訊息建議的尺寸 |
| 400 | `unsupported_output_format` | 圖片：`output_format` 不是 png、jpeg 或 webp，或是推論引擎不產生的格式 | 改要 png |
| 400 | `too_many_steps` | 圖片與影片：`num_inference_steps` 超過部署的 *Most steps* | 少一點步數，或調高 `max_steps` |
| 400 | `prompt_too_long` | 圖片與影片：`prompt` 或 `negative_prompt` 超過部署的 *Longest prompt* | 縮短它，或調高 `max_prompt_chars` |
| 400 | `video_too_long` | 影片：`seconds` 超過部署的 *Longest video* | 要短一點的秒數，或調高 `max_seconds` |
| 400 | `video_too_large` | 影片：某一邊超過 *Largest side*，或每格像素比預設尺寸放大到該邊時還多 | 要小一點的尺寸，或調高 `max_side` |
| 400 | `input_reference_not_supported` | 影片：帶了 `input_reference`（或 `image_reference`、`video_reference`、`audio_reference`）：1.3 只支援文字轉影片 | 改在提示裡描述 |
| 400 | `variant_not_supported` | 影片：`variant=thumbnail` 或 `spritesheet`；推論引擎只做影片 | 改用 `variant=video` |
| 4xx/5xx | `engine_error` | 圖片與影片：推論引擎拒絕了請求或處理失敗；會轉送它的狀態碼和訊息 | 看訊息；記憶體不足的錯誤代表要用小一點的尺寸或少幾張圖片 |
| 400 | `structured_output_not_supported` | 請求要求結構化輸出（JSON 的 `response_format`、`structured_outputs`、`guided_*`、`grammar`、`json_schema`、strict 工具），但提供這個模型的每個供應商都會拿掉或忽略它。訊息會列出它們和參數 | 拿掉這個參數，或改用接受它的目標（OpenAI 相容伺服器、本機部署） |
| 400 | `cloud_only_model` | 只有雲端供應商提供這個模型，但請求帶了 `X-Forgeyard-Local-Only`，或路由設為 **僅本機** | 拿掉標頭，或改用本機模型 |
| 401 | `missing_api_key` | 沒有 `Authorization: Bearer` 標頭 | 送出金鑰 |
| 401 | `invalid_api_key` | 不是 Forgeyard 金鑰，或沒有這把金鑰 | 檢查金鑰 |
| 401 | `revoked_api_key` | 金鑰已被撤銷 | 建立新金鑰 |
| 401 | `expired_api_key` | 金鑰已過期 | 建立新金鑰 |
| 401 | `account_disabled` | 金鑰的擁有者已停用或正在等待核准 | 詢問管理員 |
| 402 | `budget_exhausted` | 每月預算讓請求無法送往任何能提供服務的供應商，本機也無法接手。`budget` 物件會說明是哪個預算、何時重置 | 等到月底、提高預算，或改用本機模型。不要重試 |
| 402 | `budget_unpriced` | 有預算適用，但供應商的模型沒有價格，所以無法預留它的費用 | 由管理員設定它的價格 |
| 403 | `model_not_allowed` | 金鑰只能使用其他模型 | 改用允許的模型，或修改金鑰的模型清單 |
| 404 | `model_not_found` | 對這把金鑰沒有這個模型：它不存在，或它只在雲端而金鑰的雲端設定不是 **允許**。在 `GET /v1/models/{model}` 也包括已停止的模型 | 查看 `GET /v1/models` |
| 404 | `unknown_endpoint` | 閘道沒有這個路徑 | 使用上面列出的端點 |
| 404 | `endpoint_not_supported` | `/v1/images/edits`、`/v1/images/variations`、`/v1/videos/sync` 或 `/v1/videos/{id}/remix`：1.3 不提供 | 改用 `/v1/images/generations`，或 `/v1/videos` 的工作 |
| 404 | `video_not_found` | 影片：這把金鑰沒有這段影片（別的金鑰的 id 也一樣回應） | 查看 `GET /v1/videos` |
| 409 | `video_not_ready` | 影片：取還在 `queued` 或 `in_progress` 的工作的內容 | 輪詢到 `completed` |
| 409 | `video_failed` | 影片：取失敗工作的內容 | 看工作的 `error`，重新要求 |
| 410 | `video_expired` | 影片：結果超過部署的 *Keep results*，已被刪除 | 重新要求 |
| 410 | `video_lost` | 影片：製作它的副本重新啟動或被停止，它的檔案也跟著不見了 | 重新要求 |
| 413 | `request_too_large` | 請求內容（媒體網址下載並內嵌後）大於模型的 *Largest request* 或 `GATEWAY_MAX_BODY_MB` | 送少一點或縮小圖片，或調高參數或設定 |
| 413 | `media_too_large` | 媒體網址的檔案大於 `MEDIA_FETCH_MAX_MB` | 送小一點的檔案，或調高設定 |
| 421 | `host_not_allowed` | 伺服器不回應網址裡的主機名稱 | 使用它的位址，或把名稱加進 `ALLOWED_HOSTS` |
| 429 | `rate_limit_exceeded` | 超過金鑰每分鐘的請求數或 token 數 | 等待 `Retry-After` 秒 |
| 429 | `provider_rate_limited` | 每個能提供服務的雲端目標都達到速率限制 | 有 `Retry-After` 就等待那麼多秒 |
| 429 | `queue_full` | 圖片：每個副本都在忙，部署的佇列也滿了。影片：部署已經有 *Jobs at once* 個工作 | 等待 `Retry-After` 秒 |
| 429 | `too_many_jobs` | 影片：這把金鑰（或這個人）在部署上已經有 *Jobs per key* 段影片在等待或製作中 | 等待 `Retry-After` 秒，或刪掉一段 |
| 502 | `upstream_unreachable` | 連不上任何副本，或每個副本的推論引擎都沒有在執行 | 通常是 agent 或推論引擎重新啟動了，會自己恢復。檢查部署 |
| 502 | `upstream_error` | 請求送出之後，或回應傳送到一半時，和副本的連線斷了。不會送到另一個副本，以免跑兩次 | 安全的話再重試 |
| 502 | `replica_refused_key` | 副本拒絕了閘道的內部金鑰 | 停止再啟動部署 |
| 502 | `provider_auth_failed` | 供應商拒絕了 Forgeyard 給它的金鑰。絕不會以 401 轉給你，免得你懷疑自己的金鑰 | 由管理員修正供應商的金鑰 |
| 502 | `provider_blocked` | 外連規則在送出前就拒絕了供應商的位址 | 由管理員檢查供應商的 base URL 和 `EGRESS_*` |
| 502 | `provider_redirect` | 供應商回應了重新導向，而重新導向一律不跟隨 | 由管理員檢查供應商的 base URL |
| 502 | `provider_unreachable` | 連不上供應商 | 稍後重試 |
| 502 | `provider_error` | 供應商回應 5xx，或送往它的請求失敗 | 稍後重試 |
| 503 | `no_ready_replica` | 模型存在，但沒有就緒的副本：下載中、啟動中、已停止，或它的機器離線。訊息會列出每個副本的狀態 | 等待，或啟動部署 |
| 503 | `model_waking` | 1.5.0：模型的部署原本在休眠（*閒置時休眠*），請求喚醒了它，但它沒有在部署的 `wake_wait`（預設 5 分鐘）內就緒，或已經有太多請求（256 個）在等它。`Retry-After`：它上次啟動的時間減掉已經過去的時間 | 照給的時間後重試；給用戶端更長的逾時，或給路由一個雲端分層（[閒置時休眠](#sleep-when-idle)） |
| 503 | `cannot_wake` | 1.5.0：模型原本在休眠，現在無法喚醒；訊息會說明原因：*Cannot be placed: …*（沒有機器還有空間）、*it is pinned to …, which is offline*，或推論引擎失敗（*replica 0 on …: backoff …*）。立刻回應，不會等完等待時間 | 釋出記憶體、讓機器恢復上線，或查看部署的事件；部署保持喚醒，能啟動時就會啟動 |
| 503 | `local_busy` | 每個副本都達到它的 *Concurrent requests*，也沒有其他目標能接手。閘道接著還是會把它送到本機副本，由推論引擎排隊處理，所以通常不會看到它。帶 `Retry-After: 1` | 等 `Retry-After` 秒後重試 |
| 503 | `no_target` | 路由的每個目標都停用了，或沒有目標提供這個端點 | 由管理員檢查路由 |
| 503 | `budget_unavailable` | 無法讀取本月的花費，所以在預算下不送出任何雲端請求 | 稍後重試 |
| 503 | `queue_timeout` | 圖片：請求等待空出副本的時間超過部署的 *Timeout* | 稍後重試，或加一個副本 |
| 504 | `first_answer_timeout` | 只限串流請求：副本接下了請求，但沒有在部署的 *等待首次回應*（預設 120 秒）內送出狀態列和第一批位元組。請求已停止，絕不會送到另一個副本 | 稍後重試，或調高部署的等待（長提示在第一個 token 之前要花不少時間） |
| 504 | `upstream_timeout` | 副本在 `GATEWAY_READ_TIMEOUT` 內沒有送出非串流的回應，或串流的下一段；圖片則是在部署的 *Timeout* 內沒有回應；影片則是副本在 60 秒內沒有接下工作 | 降低 `max_tokens`，或調高設定；圖片則用小一點的尺寸或少一點步數 |
| 504 | `provider_timeout` | 供應商沒有及時回應（`PROVIDER_READ_TIMEOUT`，或供應商自己的逾時設定） | 稍後重試 |

另外兩種情況：

- **推論引擎或供應商本身的錯誤**會以它自己的狀態碼轉給你。副本的回應原封不動轉送（例如提示太長時 vLLM 回的 400）。供應商的其他 4xx 保留它的狀態碼和 `code`，訊息前面加上供應商的名稱，看起來像金鑰的內容會被移除。沒有其他副本或層級能接手時，副本的 429 或 503 會轉給你。
- **串流開始後才中斷**時，狀態碼已經無法更改。閘道會送出最後一個事件結束它，也就是錯誤碼為 `upstream_stream_broken` 的錯誤：`data: {"error": {…, "code": "upstream_stream_broken"}}`。

## 指標

`GET /metrics` 以 Prometheus 的文字格式（`text/plain`）回應，不快取。怎麼收集：[用 Prometheus 收集 Forgeyard 的指標](how-to.md#scrape-forgeyard-with-prometheus)。

| 回應 | 什麼時候 |
|---|---|
| 200 | `METRICS_TOKEN` 留空，且請求直接來自伺服器本機（loopback 位址，沒有 `X-Forwarded-For`、`Forwarded` 或 `X-Real-IP` 標頭）；或 `METRICS_TOKEN` 有設定，且請求帶著 `Authorization: Bearer <METRICS_TOKEN>` |
| 401 `metrics_token_required` | `METRICS_TOKEN` 有設定，但請求沒有 Bearer 權杖或帶了別的（兩種情況回應相同），附 `WWW-Authenticate: Bearer realm="metrics"` |
| 403 `metrics_local_only` | `METRICS_TOKEN` 留空，而請求來自其他位址或經過 proxy |
| 421 | 請求的主機名稱不是伺服器會回應的名稱（`ALLOWED_HOSTS`），所有路徑都一樣 |

閘道的指標計算的是用量統計所計算的請求（`/v1`、試用區、`/x/` 和影片工作），在請求結束時計入：

| 指標 | 類型 | 標籤 | 是什麼 |
|---|---|---|---|
| `forgeyard_gateway_requests_total` | counter | `endpoint`、`model`、`status`、`source`、`target` | 已結束的請求。`endpoint`：`chat/completions`、`embeddings`、`audio/speech`、`videos`、`x`（直通）… `status`：HTTP 狀態碼（499：用戶端離開了）。`source`：`api`（API 金鑰）或 `console`（試用區）。`target`：`local` 或 `provider` |
| `forgeyard_gateway_request_duration_seconds` | histogram | `endpoint`、`model` | 從請求到達到結束；影片工作是從建立到結束 |
| `forgeyard_gateway_time_to_first_byte_seconds` | histogram | `endpoint`、`model` | 從請求到達，到收到副本或供應商回應的第一批位元組：串流是第一個區塊，其他是它的狀態列。只計有收到回應的請求 |
| `forgeyard_gateway_tokens_total` | counter | `model`、`direction` | 推論引擎或供應商回報的 token：`in` 是提示，`out` 是產生的內容 |
| `forgeyard_gateway_spills_total` | counter | `model`、`reason` | 離開路由第一層的請求，以及原因：`unavailable`、`busy`、`waking`（1.5.0）、`refused`、`too_long`、`budget` |
| `forgeyard_gateway_waiting` | gauge | `model` | 現在正在排隊等本機空位的請求（路由所有層加總） |
| `forgeyard_gateway_wait_seconds` | histogram | `model` | 每個請求在隊伍裡等了多久，才拿到空位或繼續往下走 |
| `forgeyard_gateway_waking` | gauge | `model` | 1.5.0：現在正在等休眠中的模型喚醒的請求 |
| `forgeyard_gateway_wake_wait_seconds` | histogram | `model` | 1.5.0：它們每個等了多久（之後被服務，或放棄） |
| `forgeyard_deployment_wakes_total` | counter | `deployment`、`by` | 1.5.0：被喚醒的休眠部署，`by` 是 `request` 或 `person`（Start、Wake） |
| `forgeyard_autoscale_total` | counter | `deployment`、`direction` | 1.5.0：自動調整加上（`up`）和排空後移除（`down`）的副本數 |

模型部署的副本就緒時（1.5.0）：

| 指標 | 類型 | 標籤 | 是什麼 |
|---|---|---|---|
| `forgeyard_replica_start_seconds` | histogram | `recipe`、`model` | 啟動花了多久：從 agent 開始啟動它（模型檔案、映像檔、推論引擎）到第一次健康檢查通過。agent 比 1.5.0 舊時只有推論引擎的部分（[查看模型啟動花了多久](how-to.md#see-how-long-a-model-takes-to-start)） |

每次收集時從資料庫讀取：

| 指標 | 標籤 | 是什麼 |
|---|---|---|
| `forgeyard_replicas` | `deployment`、`state` | 每個模型部署各狀態的副本數：`pending`、`starting`、`running`、`healthy`、`unhealthy`、`backoff`、`exited`、`failed`、`stopping`、`stopped` |
| `forgeyard_deployment_replicas` | `deployment`、`kind` | 1.5.0：每個部署的副本數，`desired`（現在應該執行的副本數；停止或休眠時是 0）、`ready`、`draining`（自動調整正在移除的），以及開啟自動副本數時的 `wanted`（負載要求的數量，在範圍內） |
| `forgeyard_deployment_load` | `deployment` | 1.5.0：開啟自動副本數時，最後一次取樣的處理中加上等待空位的請求數 |
| `forgeyard_machines` | `status` | `online`、`offline` 和 `revoked` 的機器數 |
| `forgeyard_machine_up` | `machine` | 機器的 agent 連線中時是 1，否則是 0 |
| `forgeyard_gpu_memory_total_bytes`、`forgeyard_gpu_memory_free_bytes` | `machine`、`gpu` | 一張卡的記憶體和可用記憶體，依 agent 最近一次回報（`gpu`：它的編號） |
| `forgeyard_gpu_memory_reserved_bytes` | `machine`、`gpu` | Forgeyard 的副本在這張卡上保留的量：也就是放置時計算的量 |
| `forgeyard_machine_memory_total_bytes`、`forgeyard_machine_memory_free_bytes`、`forgeyard_machine_memory_reserved_bytes` | `machine` | 機器的 RAM，同上（CPU 副本，以及 GPU 副本留在 CPU 上的層） |
| `forgeyard_cloud_spend_usd` | `provider` | 每個供應商這個 UTC 月份的花費，依預算的計算方式 |
| `forgeyard_provider_budget_usd` | `provider` | 供應商的每月預算（有設定時） |
| `forgeyard_budgets_reached` | `scope`、`threshold` | 這個月有幾個 `key`、`person` 和 `provider` 預算達到 `80` 或 `100` 百分比（絕不列出是哪些） |
| `forgeyard_build_info` | `version` | 永遠是 1：伺服器的版本 |

另外還有 `process_*`（伺服器的 CPU 時間、記憶體、開啟的檔案）。`model` 標籤是閘道有路由的服務名稱：請求一個沒有人提供的模型，會計為 `_unknown`，絕不會用用戶端送來的名稱；`recipe` 標籤是配方的名稱。沒有任何標籤包含 API 金鑰、金鑰的 id 或名稱、使用者或提示內容。伺服器重新啟動時計數器會從 0 開始。

## 應用程式（已淘汰）

應用程式，也就是不是模型的 workload（1.4，#88），已經離開主控台，**1.5 會移除**，並以 migration 移除剩下的應用程式。在那之前，它們仍可透過 API 使用，並會明白告知：

| 已淘汰 | 內容 |
|---|---|
| `/api/workloads` 裡屬於應用程式的部分 | `POST /api/workloads`；以及應用程式的 `GET`、`PUT …/gateway`、`POST …/stop`、`POST …/start`、`DELETE` 和 `GET …/replicas/<n>/logs`；清單裡有應用程式時的 `GET /api/workloads` |
| `GET /api/workloads/templates` | 1.3 的範本 |
| `/x/<name>/…` | [直通](#the-passthrough-x)，以及主控台自己送到公開視覺應用程式的 `POST /api/playground/vision/<name>/<task>` |
| 金鑰的 `workloads` 與 `workload_list` | `/api/keys` 仍會接受並回傳，但主控台不再顯示，`GET /api/keys` 也不再列出金鑰可以指定的應用程式 |

- **標頭。** 這些路由的每個回應（包括錯誤）都帶 `Deprecation`（RFC 9745：宣布淘汰的日期，`@<epoch>`），以及一個 `rel="deprecation"` 的 `Link`，指向這一節。沒有 `Sunset` 標頭：1.5 還沒有日期。
- **紀錄。** 伺服器對三類（應用程式、範本、`/x/`）各自最多每小時記一次警告，並寫出是哪個請求。
- **設定健康檢查。** 只要還有任何應用程式，`apps-deprecated`（警告）就會列出它們的名稱。

模型部署的 workload（runtime 為 `model`）沒有淘汰：模型就是這樣跑的，讀取它（`GET /api/workloads/<id>`、它的紀錄）不會帶這些標頭。

```http
HTTP/1.1 200 OK
Deprecation: @1794787200
Link: </guide/en/reference#apps-deprecated>; rel="deprecation"; type="text/html"
```

替代做法：把要跑的東西當成模型在 **服務中** 部署；內建推論引擎跑不了的模型，加一個自訂推論引擎（**模型 → 推論引擎**，一份 recipe）。然後用 `DELETE /api/workloads/<id>` 刪除每個應用程式。[試用區的視覺模型](#vision)取代 1.3 的視覺範本。

## 直通（/x/）

**1.4 起淘汰，1.5 移除**（[應用程式（已淘汰）](#apps-deprecated)）。程式以 **Apps** 設定允許的 API 金鑰，在 `https://<server>:7860/x/<name>/<path>?<query>` 呼叫公開的應用程式。和 `/v1` 一樣只接受 `Authorization: Bearer fyk_…`：不接受 cookie、不接受放在查詢字串裡的金鑰、不送 CORS 標頭。

### 路徑與方法

| 項目 | 規則 |
|---|---|
| 方法 | `GET`、`HEAD`、`POST`、`PUT`、`PATCH`、`DELETE`、`OPTIONS` |
| 前綴 | `/x/<name>` 會被拿掉：`/x/<name>/a/b?c=1` 到應用程式那邊是 `/a/b?c=1`，只有 `/x/<name>` 則是 `/` |
| 路徑 | 原封不動、逐位元組轉送。以下情況回 400 `invalid_path` 拒絕，絕不正規化：`.` 或 `..` 路徑段、編碼過的 `.`、`/` 或 `\`（`%2e`、`%2f`、`%5c`）、反斜線、控制字元（原始或編碼過的）以及非 ASCII 位元組 |
| 查詢字串 | 原封不動轉送 |
| 不轉送 | WebSocket（`Upgrade` 會被拿掉）、gRPC、到應用程式的 HTTP/2 |

### 請求與回應標頭

| 標頭 | 方向 | 會怎樣 |
|---|---|---|
| `Authorization` | 請求 | 檢查你的 API 金鑰後拿掉；應用程式收到的是副本的內部金鑰，由 agent 的金鑰守門員檢查後拿掉 |
| `Cookie`、`Forwarded`、`X-Forwarded-*`、`X-Real-IP`、`X-Forgeyard`、`X-Forgeyard-*` | 請求 | 拿掉 |
| `X-Forwarded-For`、`X-Forwarded-Proto`、`X-Forwarded-Host` | 請求 | 重新設定：用戶端的位址、`https` 或 `http`，以及它呼叫的主機 |
| `X-Forwarded-Prefix` | 請求 | `/x/<name>` |
| `X-Request-Id` | 雙向 | 每個請求一個新 id，送給應用程式，也回給用戶端 |
| 逐跳標頭（`Connection` 和它列出的標頭、`Keep-Alive`、`TE`、`Trailer`、`Transfer-Encoding`、`Upgrade`、`Expect`、`Proxy-*`） | 雙向 | 拿掉 |
| `Set-Cookie`、`Access-Control-*`、`Strict-Transport-Security`、`Clear-Site-Data`、`Alt-Svc`、`Service-Worker-Allowed`、`Report-To`、`NEL`、`Date`、`Server` | 回應 | 拿掉（伺服器會送它自己的 `Date` 和 `Server`） |
| `Content-Security-Policy` | 回應 | 一律是 `sandbox; default-src 'none'; frame-ancestors 'none'` |
| `X-Content-Type-Options`、`X-Frame-Options`、`Cache-Control`、`Cross-Origin-Resource-Policy`、`Referrer-Policy` | 回應 | 一律是 `nosniff`、`DENY`、`no-store`、`same-origin` 和 `no-referrer` |
| `Location`、`Content-Location` | 回應 | 路徑，或指向應用程式自己的位址，會改寫到 `/x/<name>` 底下；其他網站和相對參照不變。絕不跟隨轉址 |
| `X-Forgeyard-Target` | 回應 | `workload:<name>#<replica>` |
| `Content-Encoding`、`Content-Length` 和其他標頭 | 回應 | 照樣轉送：本文以原始位元組、一到就轉送（壓縮的本文維持壓縮；SSE 和 chunked 回應會串流） |

### 限制與逾時

| 限制 | 預設值 | 在哪裡設定 |
|---|---|---|
| 最大請求本文 | 32 MiB（超過回 413 `request_too_large`） | 應用程式：**Largest request (MiB)**，1 到 1,024 |
| 等回應每一段的時間 | 600 秒（第一個位元組之前逾時回 504 `upstream_timeout`） | 應用程式：**Read timeout (s)**，1 到 3,600 |
| 連到副本 | `GATEWAY_CONNECT_TIMEOUT`（5 秒）；連不到的副本會跳過，改試下一個 | 伺服器 |
| 每個副本同時處理的請求 | 不限（每個副本都滿了時回 429 `workload_busy`） | 應用程式 |
| 每把金鑰同時進行的請求 | 不限（429 `key_busy`）；在試用區裡以每個人計算 | 應用程式 |
| 每分鐘請求數 | 金鑰自己的限制（429 `rate_limit_exceeded`）；每分鐘 token 數不適用 | 金鑰 |

每個 429 都帶 `Retry-After`。用戶端離開時，到應用程式的請求會立刻關閉。

**用量** 把每個請求記在模型 `x:<name>` 底下：請求數、錯誤（包括回應本身的 5xx）、被限制的請求、送進的位元組（請求本文）和送出的位元組（實際送出的回應本文）、忙碌時間（從送出請求到回應結束）和延遲。

### /x/ 的錯誤

閘道產生的錯誤和 `/v1` 的格式相同。應用程式本身的回應，包括 401 或 5xx，會原樣轉送。

| 狀態碼 | `code` | 意思 | 怎麼處理 |
|---|---|---|---|
| 400 | `invalid_path` | 路徑有 `.` 路徑段、編碼過的 `.`、`/` 或 `\`、反斜線、控制字元或非 ASCII 位元組 | 不要送這些 |
| 401 | `missing_api_key`、`invalid_api_key`、`revoked_api_key`、`expired_api_key`、`account_disabled` | 和 `/v1` 相同 | 和 `/v1` 相同 |
| 403 | `workloads_not_allowed` | 金鑰的 **Apps** 設定是 *None* | 修改金鑰的設定 |
| 404 | `workload_not_found` | 對這把金鑰來說沒有這個公開的應用程式：不存在、沒有公開，或不在金鑰的清單裡 | 檢查名稱和金鑰的清單 |
| 413 | `request_too_large` | 本文超過應用程式的 **Largest request** | 少送一點，或調高限制 |
| 429 | `rate_limit_exceeded` | 超過金鑰的每分鐘請求數 | 等 `Retry-After` 秒 |
| 429 | `workload_busy` | 每個副本進行中的請求都達到 **Requests at once per replica** | 等 `Retry-After` 秒，或增加副本 |
| 429 | `key_busy` | 這把金鑰對這個應用程式進行中的請求達到 **Requests at once per key** | 等其中一個完成 |
| 502 | `upstream_unreachable` | 連不到任何副本（它的程式停了或正在重新啟動） | 看應用程式的輸出 |
| 502 | `upstream_error` | 送到副本的請求失敗 | 重試 |
| 502 | `replica_refused_key` | 副本的金鑰守門員拒絕了閘道的內部金鑰 | 停止再啟動應用程式 |
| 503 | `no_ready_replica` | 應用程式已停止、沒有副本就緒，或有副本還不在金鑰守門員後面；訊息會說是哪一種 | 啟動它、等一下，或更新 agent |
| 504 | `upstream_timeout` | 在應用程式的 **Read timeout** 內沒有回應 | 調高逾時，或檢查應用程式 |

已經開始的回應如果中斷，狀態碼就無法再改：連線會在本文完整之前關閉，所以用戶端絕不會把被截斷的本文當成完整的。

### 從試用區呼叫

1.4 起，console 的試用區只提供視覺模型（在它唯一的模型選單裡；Vision 分頁已移除），`GET /api/playground/vision/options` 也不再列出應用程式（`workloads`）。下面的路由在 1.5 之前仍能送到公開的視覺應用程式（已淘汰），就像 1.3 的 Vision 分頁那樣：透過自己的路由，從不經過 `/x/`：這些路由用的是 console 的登入（它的 cookie，`POST` 時還要從 console 自己的來源送出 `X-Forgeyard: 1` 標頭），而 `/x/` 仍然只接受 API 金鑰。任何已登入的人都可以使用。視覺應用程式是指以 `--task detect`、`--task segment` 或 `--task depth` 啟動的公開服務，每個[範本](#app-templates)都是這樣。

| 路由 | 做什麼 |
|---|---|
| `GET /api/playground/vision/options` | `models`：視覺模型（見[視覺](#vision)）；`tasks`：視覺任務 |
| `POST /api/playground/vision/<name>/<task>` | 把本文（圖片，multipart）送到應用程式自己的 `POST /v1/<task>`，不會送到別處：沒有其他路徑、方法或查詢字串 |

之後請求依照上面 `/x/` 的規則：同樣的副本和金鑰守門員、請求與回應標頭（cookie 絕不會送到應用程式）、應用程式的 **Largest request**、**Requests at once per replica** 和 **Requests at once per key**（以每個人計算）、它的逾時，以及同樣的錯誤。任務和應用程式的不同，或應用程式不是公開的視覺應用程式，回 404 `workload_not_found`；視覺伺服器沒有的任務回 404 `unknown_task`。用量記在 `x:<name>` 底下，來源是 *console*，算在登入的人身上。

## 應用程式範本

**1.4 起淘汰，1.5 移除。**範本執行伺服器版本的 `docker.io/tonyhuang1101/forgeyard-vision`：tag `X.Y.Z`（CPU，linux/amd64 與 linux/arm64）或 `X.Y.Z-cuda`（CUDA 13.0 的 torch，linux/amd64，NVIDIA 驅動 580 或更新）。它由儲存庫裡的 `images/vision` 建置，相依套件以雜湊值固定，模型以固定的 revision 包在裡面，並以無特權的使用者執行。`GET /api/workloads/templates` 會列出範本，以及每個範本產生的應用程式規格。

| 端點 | 接受 | 回傳 |
|---|---|---|
| `GET /` | | `task`、`model`、`revision`、`licence`、`device`（`cpu` 或 `cuda`）、`endpoint` |
| `GET /health` | | 模型載入後回 200 |
| `POST /v1/detect` | `image`；`threshold`（0.5）、`max_detections`（100） | `detections`：`label`、`score`、`box` `[x0, y0, x1, y1]`（像素） |
| `POST /v1/segment` | `image`；`points` `[[x, y], …]` 加上 `labels`（1 在裡面，0 在外面），或 `box` `[x0, y0, x1, y1]`；`multimask`（true） | `masks`：`score`、`area`、`box`、`png`（1 位元 PNG，base64），最好的在前 |
| `POST /v1/depth` | `image` | `depth_png`（16 位元 PNG，base64，和圖片同樣大小，越近越亮）、`min`、`max`：相對深度，不是公尺 |

圖片以 multipart 檔案欄位 `image` 送出（其他欄位當表單欄位，可以是 JSON 值），或以 JSON 本文 `{"image": "<base64 或 data: URI>", …}` 送出；PNG、JPEG 或 WebP，最多 20 MiB、4000 萬像素。每個回應也都有 `model`、`revision`、`width`、`height` 和 `ms`。伺服器的錯誤是 `{"error": {"message", "code"}}`，狀態碼 400（`invalid_image`、`missing_image`、`missing_prompt`、`invalid_option`、`invalid_json`）或 413（`image_too_large`）；經過 `/x/` 時會原樣轉送。

## 應用程式的環境變數

應用程式的副本從空的環境開始，只拿到它的規格宣告的變數，再加上：

| 變數 | 值 |
|---|---|
| `FORGEYARD_WORKLOAD` | 應用程式的名稱 |
| `FORGEYARD_REPLICA` | 副本的 id |
| `FORGEYARD_REPLICA_INDEX` | 副本的編號，從 0 開始 |
| `PORT`、`PORT_<NAME>` | 每個宣告的連接埠：行程拿到它必須監聽的主機連接埠；容器拿到它自己的連接埠（推論引擎會把主機連接埠對應過去）。`PORT` 是第一個 |
| `CUDA_VISIBLE_DEVICES`、`NVIDIA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER` | NVIDIA：只有它自己的卡的 UUID。沒有 GPU 的副本看不到任何卡 |
| `ROCR_VISIBLE_DEVICES`、`HIP_VISIBLE_DEVICES` | AMD：只有它自己的卡 |
| `PATH`、`HOME`、`LANG`、`USER`、`LOGNAME` | 只有行程：基本的 `PATH`、以它自己的目錄當 `HOME`、`LANG=C.UTF-8`，以及執行行程的帳號 |

規格裡設定 GPU 變數（也包括 `HIP_VISIBLE_DEVICES` 和 `GPU_DEVICE_ORDINAL`）、`PORT`，或任何以 `FORGEYARD_` 或 `PORT_` 開頭的名稱，都會被拒絕。在行程的參數和環境變數的值裡，`${PORT}` 和 `${PORT_<NAME>}` 會換成連接埠。在行程的參數裡，`${BIND_ADDRESS}` 會換成它應該監聽的位址：在 agent 的金鑰守門員後面（公開在 `/x/` 的應用程式）是 `127.0.0.1`，否則是 agent 自己的位址。

## 透過 API 修改部署

`PATCH /api/deployments/{id}`，限管理員和操作員，使用主控台的登入工作階段（和 **編輯** 頁面送出的請求相同；寫入請求還要帶 `X-Forgeyard: 1` 標頭）。沒有送的欄位維持原值。

| 欄位 | 值 |
|---|---|
| `params` | 要修改的參數：recipe 自己的（`context`、`concurrency`…）、`gpus`、`share`（`exclusive` 或 `vram`）、`vram`（每個裝置的記憶體，例如 `"20Gi"`）、`extra_args`（清單）、`first_answer_timeout`（秒，5 到 3,600；圖片和影片部署不適用；只改它不會重新啟動任何東西），（1.5.0）`sleep_when_idle`、`idle_minutes` 和 `wake_wait`（[閒置時休眠](#sleep-when-idle)），以及自動副本數的 `autoscale`、`min_replicas`、`max_replicas`、`autoscale_target`、`scale_up_seconds` 和 `scale_down_minutes`（[自動副本數](#automatic-replicas)）；修改這些不會重新啟動任何東西。只有送出的名稱會改變；送 `null` 的名稱會回到 recipe 的預設值（`vram`：回到估算值） |
| `replicas` | 1 到 16。開啟自動副本數時（1.5.0），只能是它現在的數量（否則 422 `replicas_automatic`）：改 `min_replicas` 和 `max_replicas`（[自動副本數](#automatic-replicas)） |
| `machine_id` | 機器的 id 會把部署固定在那台機器；`null` 讓它變成自動 |
| `wait` | `true`：即使目前放不下也儲存；重新啟動的副本會等到有空間 |
| `dry_run` | `true`：只檢查。回應內容相同，但沒有 `deployment`，也不會儲存任何東西 |
| `recipe` | （1.5.2）它的推論引擎的其他版本：它執行的、或被複製來源的內建配方，或是那個內建配方的複本（`based_on`），而且要用同樣的協定提供這個模型。名稱、對外的模型名稱、金鑰和路由都不變；會用它重新估算；它不接受的參數會被拿掉（`dropped`） |

模型、名稱和對外的模型名稱都不能改（422）。回應：

| 欄位 | 內容 |
|---|---|
| `estimate` | 新的估算 |
| `placement` | 每個副本會放在哪裡，和部署時一樣；`previous` 標示留在原機器上的副本；獨占的模型只需要它那張卡一半以下的記憶體時，會有 `share_hint` |
| `changes` | 每個會改變的參數、`replicas` 和 `machine_id`：`{name: {before, after}}`；沒有任何改變時是 `{}` |
| `restart` | `rolling`（一次一個副本）、`all`（唯一的副本重新啟動）、`none`、`on_start`（已停止：啟動時才套用），或（1.5.0）`on_wake`（休眠中：喚醒時才套用） |
| `restart_text` | 用一句話說明同一件事 |
| `recipe` | （1.5.2）它會執行的推論引擎 |
| `dropped` | （1.5.2）它原本有、但這個推論引擎不接受的參數：會被拿掉 |
| `outage` | （1.5.2）`restart` 為 `all`（唯一執行中的副本會重新啟動）時：`{seconds, source, first_start, image_missing, avoid, cloud, text}`。`seconds` 是模型大約多久不會回應，依據是 `source`：`last_start`（它自己的）、`engine`（同一個推論引擎其他部署最近幾次啟動時間的中間值）或 `typical`。`first_start`：推論引擎換了，新版本第一次啟動可能比較久。`image_missing`：副本所在、還沒有新推論引擎映像檔的機器。`avoid`：`add_replica`、`autoscale`（開啟自動副本數時是 `min_replicas`），路由有雲端層時再加上 `cloud`。其他情況是 `null` |
| `deployment` | 儲存後的部署（dry run 時沒有） |

| 狀態 | `code` | 什麼時候 |
|---|---|---|
| 409 | `does_not_fit` | 部署正在執行，而新設定目前哪裡都放不下；`machines` 會像部署時一樣逐台說明原因。不會儲存任何東西 |
| 409 | `recipe_gone` | 它的 recipe 已經不存在 |
| 422 | `invalid_params` | 有不認得的參數，或超出範圍 |
| 422 | `cannot_estimate` | 無法為這個模型估算新參數 |
| 422 | `engine_not_related`、`incompatible`、`engine_not_compatible` | （1.5.2）`recipe` 不是它的內建推論引擎或那個推論引擎的複本；不能提供這個模型；或是用別的協定提供它，路由會因此改變 |
| 404 | `not_found`、`machine_not_found`、`recipe_not_found` | 沒有這個部署、沒有 `machine_id` 指定的機器，或沒有 `recipe` 指定的推論引擎 |
| 403 | | 不是管理員或操作員 |

每次儲存的修改都會以 `deployment.changed` 記入稽核紀錄，包含改了什麼和重新啟動的方式（1.5.2：換了推論引擎時，還有 `recipe: {from, to}`、`engine_versions: {from, to}` 和 `dropped`）。`GET /api/deployments/{id}` 另外提供（1.5.2）`engine`（`{name, title, version, builtin, based_on}`）和 `engines`（它可以換過去的版本，含 `current`）、 `pinned`（選定的機器，或 `null`）、`rollout`（副本重新啟動期間：`{generation, updated, total, next}`），以及每個副本的 `not_picked_up`：副本被放到一台已連線的機器上，而它的 agent 超過 2 分鐘都沒有回報這個副本時才會有（[副本一直停在 pending](troubleshooting.md#a-replica-stays-pending)），和（1.4）`placement_note`：當一台無法取得所需檔案的機器影響了副本的放置位置時，說明副本為什麼在這台機器上，例如 *cpu-01 could not reach huggingface.co; placed on cpu-02 instead*（[機器連不到 Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face)）。部署的 `workload`（與 `GET /api/workloads/{id}` 相同）會反過來指出它：`deployment: {id, name}`。在 `placement` 裡，每台機器和每個已放置的副本會列出 `needs`（在那裡要下載的東西：`model`、`engine`、`image`），如果那台機器在過去一小時內連不到其中某個來源，還會有 `unreachable`。

（1.5.0）每個部署（列表裡和單獨查詢時）還有 `last_start`：最近一次啟動花了多久，`{seconds, phases, source, at, replica}`，取自它的事件。`phases` 是 agent 分得出來的各部分：`model`、`waiting`、`image` 和 `engine`，加起來就是 `seconds`；`source` 是 `agent`，或是 `server`（agent 比 1.5.0 舊，由伺服器只量了推論引擎的部分）。1.5.0 之後還沒有副本就緒過時是 `null`（[查看模型啟動花了多久](how-to.md#see-how-long-a-model-takes-to-start)）。

### 閒置時休眠

1.5.0（[概念](concepts.md#sleep-when-idle)）。每個部署都有的三個參數，在部署時（`POST /api/deployments` 的 `params`）或修改時設定；它們由伺服器讀取，所以修改它們不會重新啟動任何東西：

| 參數 | 值 |
|---|---|
| `sleep_when_idle` | `true` 開啟；`false`（或 `null`，或不送）關閉。預設關閉，1.5.0 之前建立的部署也一樣。只有開啟時才會留在 `params` 裡 |
| `idle_minutes` | 沒有請求多少分鐘後休眠：1 到 1,440，不送時是 **30**。休眠關閉時也會保留，等開啟時使用 |
| `wake_wait` | 請求等休眠中的模型喚醒多少秒，之後回應 503 `model_waking`：10 到 1,800，不送時是 **300** |

其他值都是 422 `invalid_params`（`"yes"`、`1`、`0`、`1441`、`1.5`；`wake_wait` 的 `9` 或 `1801`）。

休眠中的部署，它的 workload 的 `desired_state` 是 **`asleep`**，`status` 是 `asleep`（在 agent 確認之前是 `stopping`），和 `running`、`stopped` 並列；它的副本是 `stopped`。讀取 `desired_state` 的腳本應該把不認得的值當作沒有在執行。`GET /api/deployments` 和 `GET /api/deployments/{id}` 會給每個部署一個 `sleep` 物件：

| 欄位 | 內容 |
|---|---|
| `on`、`idle_minutes`、`wake_wait` | 設定（沒有設定時是 30 分鐘和 300 秒） |
| `asleep` | 它現在是否在休眠 |
| `last_request_at` | 這台伺服器啟動以來看到的最後一個送給它的請求，或 `null` |
| `since` | 它的閒置時間從什麼時候起算：最後一個請求、最後一次啟動或修改、副本變成就緒，或伺服器啟動的時間，取最晚的那個 |
| `sleeps_at` | 如果沒人使用，它什麼時候會休眠（只在開啟休眠且正在執行時），每 30 秒檢查一次 |
| `held_by` | 雖然可能已經閒置夠久，它現在為什麼不休眠：*a request is in flight*、*a replica is starting*、*an edit is restarting its replicas*、*a video is waiting or being made*、*a finished video is kept until …*；其他時候是 `null` |
| `waking` | 請求（或 **立即喚醒**）喚醒了它、還沒有副本就緒時：`{since, by, waiting, expected_seconds}`：喚醒開始的時間、誰喚醒它（*a request (key laptop)*、*a request from the Playground (owner)*，或那個人的使用者名稱）、有多少請求在等它，以及預計啟動要多久（上次啟動的時間，沒有量過時是 30 秒）。其他時候是 `null`（記在這台伺服器的記憶體裡：重新啟動後是 `null`） |
| `asleep_since` | 從什麼時候開始休眠（它的 *Asleep* 事件），或 `null` |
| `wakes` | 過去 24 小時被喚醒的次數（依它的事件） |

對休眠中的部署送 `POST /api/deployments/{id}/start` 會喚醒它（事件 *Woken by …*，以 `workload.started`（帶 `woken: true`）和 `deployment.started` 記入稽核紀錄）；`POST …/stop` 會讓它變成停止（事件 *Stopped by … (it was asleep)*）。進入休眠的事件是 *Asleep: no requests for N min; frees …*，不記入稽核紀錄。對休眠中的部署送 `POST …/try` 會回應 409 `model_asleep`。經過閘道的請求（包括 `POST /v1/videos`）會喚醒它：事件 *Woken by a request (key …)*（不記入稽核紀錄），然後等待就緒的副本、轉到金鑰可以用的雲端分層（`X-Forgeyard-Spilled: waking`），或回應 503 `cannot_wake` / `model_waking`（[錯誤](#errors)）。`/v1/models`、健康檢查和主控台的頁面不會喚醒它，已停止的部署也不會被喚醒。

**降版：**比 1.5.0 舊的伺服器會把 `asleep` 當作停止：模型不會列出，請求回應 503 `no_ready_replica` *The deployment is stopped*，按 Start 會啟動它。

### 自動副本數

1.5.0（[概念](concepts.md#automatic-replicas)）。除了影片部署以外每個部署都有的參數，在部署時（`POST /api/deployments` 的 `params`）或修改時設定；它們由伺服器讀取，所以修改它們不會重新啟動任何東西：

| 參數 | 值 |
|---|---|
| `autoscale` | `true` 開啟自動副本數；`false`（或 `null`，或不送）關閉。預設關閉，1.5.0 之前建立的部署也一樣。只有開啟時才會留在 `params` 裡。影片部署會拒絕它（422） |
| `min_replicas` | 最少幾個副本：1 到 16，不送時是 **1**；只有 `sleep_when_idle` 開啟時可以是 0（這時只會透過休眠降到 0，醒著時至少 1 個） |
| `max_replicas` | 最多幾個：1 到 16，不送時是 **2**；至少是 `min_replicas` |
| `autoscale_target` | 一個副本承擔的負載，是它 `concurrency`（recipe 沒有時算 1）的百分比：20 到 100，不送時是 **80** |
| `scale_up_seconds` | 負載要高於目標多久才多一個副本：10 到 600，不送時是 **60**。達到目標兩倍時是 10 秒 |
| `scale_down_minutes` | 少一個副本也夠用要持續多久才移除一個：1 到 120，不送時是 **10** |

`autoscale` 關閉時，`min_replicas`、`max_replicas` 和其他參數也會保留，等開啟時使用。其他值都是 422 `invalid_params`。開啟 `autoscale` 的部署會啟動 `min_replicas` 個副本（至少 1 個），不管 `replicas` 寫多少；開啟它或修改範圍的修改，會立刻把副本數調整進範圍內。開啟時，修改不能設定另一個 `replicas` 數字（422 `replicas_automatic`）。

負載是正在它的副本上處理的請求（不含正在排空的副本），加上在閘道隊伍裡等它所在路由分層空位的請求（分層有好幾個部署時平分）和它的圖片佇列，由伺服器每 2 秒取樣一次。`scale_up_seconds` 內的平均值高於 `就緒副本數 × concurrency × autoscale_target %`，或 10 秒內的平均值至少是它的兩倍時，加一個副本；不會超過 `max_replicas`，只在沒有副本正在啟動時，而且只在現在有線上機器（固定機器時是那台）放得下時。上一次變動之後的 `scale_down_minutes` 內，平均值都不超過少一個副本在目標下能承擔的量、而且少一個也不會馬上又要多加一個時，移除一個；不會低於 `min_replicas`（至少 1）。移除的是最新的副本：閘道先停止送請求給它，等到它上面沒有處理中的請求（最多 30 分鐘），然後刪掉它那一列，由它的 agent 停止它。修改正在重新啟動副本、某個副本所在的機器離線，或部署啟動、被喚醒或修改後的 2 分鐘內，都不會變動。在休眠部署的喚醒隊伍裡等的請求不算進負載：喚醒本來就會啟動它的副本，等有副本就緒，它們會進到一般流程，在那裡計入。

`GET /api/deployments` 和 `GET /api/deployments/{id}` 會給每個部署一個 `autoscale` 物件：

| 欄位 | 內容 |
|---|---|
| `on`、`min`、`max`、`target`、`scale_up_seconds`、`scale_down_minutes` | 設定（沒有設定時是預設值） |
| `capacity` | 它的 `concurrency`（沒有時是 1） |
| `replicas`、`ready` | 它現在的副本數（沒有結束的）和就緒的副本數 |
| `load`、`per_replica` | `scale_up_seconds` 內的平均負載，以及每個就緒（沒在排空）副本的負載；取樣前是 `null` |
| `wanted` | 負載要求的副本數（在範圍內） |
| `draining` | 正在排空的副本：`{replica, since, in_flight}`，或 `null` |
| `room` | 想多加的那一個為什麼現在放不下（*Wants 3 replicas, room for 2: …*），或 `null` |
| `paused` | 它現在為什麼不調整（*an edit is restarting its replicas*、*a replica's machine is offline*、*it was started, woken or changed less than 2 minutes ago*），或 `null` |
| `last` | 這台伺服器做的最後一個決定 `{at, text}`，或 `null` |

每個決定都是部署的一個事件（kind `scaled`）：*Scaled up to N: …*、*Wants N replicas, room for M: …*（放不下期間只記一次）、*Draining replica N: …*、*Kept replica N: …*、*Removed replica N after draining for … s*、*Asleep with N replicas (its minimum)*。它們不記入稽核紀錄。負載樣本、排空和決定都記在伺服器的記憶體裡：重新啟動後時間窗重新開始，進行中的排空不會移除任何副本。

## 透過 API 查看機器的模型快取

和其他 `/api` 請求一樣使用主控台的登入工作階段（寫入請求還要帶 `X-Forgeyard: 1` 標頭）。所有登入的人都可以查看；管理員和操作員可以刪除。

| 請求 | 做什麼 |
|---|---|
| `GET /api/machines/{id}/cache` | 那台機器快取裡的模型，以它的 agent 最近一次的回報為準 |
| `POST /api/machines/{id}/cache/remove`，內容 `{"key": "<key>"}` | 刪除一個項目：一個 revision、其中一個 GGUF 檔、一個 URL 模型或一個推論引擎 |
| `POST /api/machines/{id}/cache/remove-partial` | 刪除未完成的下載留下的檔案 |

回應有 `free_bytes` 和 `total_bytes`（快取所在的磁碟）、`used_bytes`（快取在上面占了多少，共用的檔案只算一次）、`partial_bytes` 和 `partial_files`、`sizes`（早於 1.4 的 agent 為 false，它只列出 key），以及 `remove_note`（目前為什麼不能刪除，或 `null`）。`models` 列出每個項目：

| 欄位 | 內容 |
|---|---|
| `key` | 刪除時要送的值：Hugging Face revision 是 `hub/models--<org>--<name>@<commit>`，從 URL 下載的單一檔案是 `url/<id>` |
| `kind` | `hf` 或 `url` |
| `repo`、`revision`、`file` | repository 和 commit，或 URL 模型的檔名 |
| `bytes`、`files` | 大小和檔案數。兩個 revision 共用的檔案兩邊都會算 |
| `parts` | 有 GGUF 檔的 revision：每個檔案（分割的模型只算一次，包含所有部分）各有自己的 `key`、`bytes`、`catalog` 和 `used_by`。刪掉最後一個就會刪掉整個 revision |
| `catalog` | 它對應的目錄模型 |
| `used_by` | 那台機器上使用它的部署（或應用程式） |

`engines` 列出推論引擎的壓縮檔（`key` 為 `engine/<壓縮檔 SHA-256 的前 16 個十六進位數字>`、`name`、`bytes`、`used_by`）。刪除的回應有 `freed_bytes`（磁碟上釋出的空間）和刪除後的清單（`cache`）。

| 狀態 | `code` | 什麼時候 |
|---|---|---|
| 409 | `model_in_use` | 那台機器上有部署正在使用它；`in_use` 會列出是哪些。agent 在刪除前也會再檢查一次 |
| 409 | `cache_busy` | 那裡正在下載這個模型（刪除未完成的下載時：正在進行任何下載） |
| 409 | `machine_offline`、`agent_too_old`、`agent_no_answer` | agent 沒有連線、不支援刪除（1.4 之前），或一分鐘內沒有回應 |
| 409 | `cache_remove_failed` | agent 刪不掉；訊息會說明原因 |
| 404 | `not_in_cache` | 那台機器沒有回報這個 key |
| 403 | | 不是管理員或操作員 |

每次刪除都會以 `model.cache_removed` 記入稽核紀錄，包含機器、key、模型和 `freed_bytes`。背後是 agent 協定的 feature `cache_admin`（1.4 起的 agent）：agent 列出快取和大小、依要求刪除，並在那台機器上有副本使用這些檔案、或這些檔案正在下載時拒絕。

## 透過 API 先下載模型

與其他 `/api` 請求一樣，用 console 的登入工作階段（寫入時也要送 `X-Forgeyard: 1` header）。所有登入的人都可以查看；admin 和 operator 可以開始和取消下載。

| 請求 | 作用 |
|---|---|
| `GET /api/downloads/targets?model_id=<id>&recipe=<name>&engine=true` | 每台機器，以及它能不能下載這個模型（和配方的推論引擎） |
| `POST /api/downloads` 加上 `{"model_id": <id>, "machine_ids": [<id>, …], "engine": true, "recipe": "<name>"}` | 在每台能下載的機器上開始下載；`engine` 預設開啟，`recipe` 預設是第一個能跑這個模型的配方 |
| `GET /api/downloads/targets?recipe=<name>` | （1.5.2）不帶 `model_id`：每台機器，以及它能不能只下載這個推論引擎 |
| `POST /api/downloads` 加上 `{"recipe": "<name>", "machine_ids": [<id>, …]}` | （1.5.2）不帶 `model_id`：只下載推論引擎（每台機器會執行的變體的映像檔或程式） |
| `GET /api/downloads?model_id=<id>&machine_id=<id>&engine=<name>` | 先列出進行中的下載，再列出已結束的（最近 100 筆）；`engine`：只列出單獨下載那個推論引擎的 |
| `POST /api/downloads/{id}/cancel` | 取消一個進行中的下載；它的 agent 會立刻停止，並保留已在磁碟上的部分 |
| `PUT /api/machines/{id}/fetch-via` 加上 `{"fetch_via": "auto"}` | 只限 admin：那台機器怎麼取得模型和推論引擎的壓縮檔：`auto`（直接下載，遇到網路錯誤後改經由伺服器）、`server`（一律經由伺服器）或 `direct`（永不經由伺服器）。它的 agent 會立刻收到新的方式 |

`targets` 裡的每台機器有 `state`：`ok`、`cached`（已經在那裡）、`downloading`、`too_old`（1.4 以前的 agent）、`no_cache`、`offline`、`no_space`（模型加上 1 GiB 的保留空間超過快取磁碟的可用空間）、`revoked` 或（1.5.2，只下載推論引擎時）`no_engine`（它沒有能在那裡執行的變體），並以 `note` 說明原因；`need_bytes` 和 `free_bytes`；有推論引擎時還有 `variant` 和 `engine`（`kind` 為 `archive` 或 `image`、`name`、`cached`），沒有配方的變體能在那裡執行時則是 `engine_note`；以及 `via`：模型會怎麼取得（`direct`、`auto` 或 `server`），和 `from_machine`（1.4.1）：會經由伺服器把模型送過來的機器，沒有則為 null。變體是那台機器會執行的那一個：機器有變體支援、且允許使用的 GPU 時用 GPU 變體，否則用 CPU 變體，而且是它的 runtime 能執行的。

一筆下載有 `id`、`model`（只下載推論引擎時為 null）、`engine_only`（1.5.2）、`machine`（含 `connected`）、`file`、`recipe`（null：只下載模型）、`state`（`queued`、`downloading`、`done`、`failed` 或 `cancelled`）、`download`（進度，格式與部署副本的相同；資料經由伺服器時帶有 `via: "server"`）、`reason`、`via`（`direct`，經由伺服器下載過則是 `server`，模型經由伺服器從另一台機器的快取取得則是 `machine`，並帶 `source: {id, name}`）、`requested_by`、`created_at`、`finished_at` 和 `waiting`（機器離線）。已結束的保留 30 天，每台機器最多 20 筆。

| 狀態碼 | `code` | 何時 |
|---|---|---|
| 409 | `nothing_to_download` | 沒有機器能下載；`skipped` 列出每台機器的 `state` 和 `reason` |
| 409 | `download_finished` | 取消一個已經結束的下載 |
| 422 | `local_model` | 本機模型是機器上的目錄：沒有東西要下載 |
| 422 | `recipe_not_compatible`、`bad_fetch_via` | 配方不能跑這個模型；`fetch_via` 不是這三個之一 |
| 422 | `model_or_engine` | （1.5.2）沒有指定模型也沒有指定推論引擎，或沒有模型卻送了 `engine: false` |
| 404 | `not_found` | 沒有這個模型、機器或下載 |
| 403 | | 不是 admin 或 operator（`fetch-via` 要 admin） |

稽核事件：`model.download_requested`（每台機器一筆，含模型和推論引擎）、`model.download_cancelled`、（1.5.2）`engine.download_requested`（每台機器一筆，含 `recipe`、`version`、`variant` 和 `engine`，也就是映像檔或壓縮檔）和 `engine.download_cancelled`、`node.fetch_via_changed`，以及記下 URL 模型 SHA-256 時的 `model.checksum_recorded`：從 URL 下載的單一 GGUF 檔沒有公布的 checksum，所以會把第一個 agent 在完整下載後算出的值記在目錄項目裡（模型 metadata 的 `files[0].sha256`，並有 `sha256_from`（哪台機器）和 `sha256_at`），之後每次下載都必須相符。之後回報的不同值只會記到 log，不會取代它。

背後是 agent 協定的 feature `prefetch`（1.4 起的 agent 和伺服器）。伺服器把每筆排隊中或下載中的列放在 `desired.downloads`，與副本並列：`{id, model, engine?, image?, image_ids?}`（Hugging Face 模型的區塊會帶伺服器用於受限 repository 的 token，和副本一樣只存在記憶體）。agent 用副本下載的同一套程式抓取（相同的鎖、從部分檔案續傳、checksum 和取消；映像檔用 1.3.1 的 pull 進度），不啟動任何東西，同時最多跑兩個，並回報 `status.downloads`（`{id, state, reason, download}`）；下一份清單裡沒有的下載會被取消。Migration `0015` 新增資料表 `model_downloads` 和欄位 `nodes.fetch_via`（所有既有機器都是 `auto`）。

（1.5.2）只下載推論引擎，就是一筆沒有模型的列：它在 `desired.downloads` 裡的項目沒有 `model` 區塊，只有 `engine` 壓縮檔（含 SHA-256）或 `image`（含 `image_ids`，機器經由伺服器取得映像檔時還有 `image_relay`；複本的映像檔依 `image_pins` 裡的 ID）。有 `prefetch` 的 agent 原本就能處理。推論引擎的頁面（`GET /api/recipes/{name}`）多了 `can_download`、`downloads`（這個推論引擎的下載，最新的在前）和每台機器的 `download`（那裡進行中的下載）；複本還有 `caught_up`：內建推論引擎現在已經是複本的版本或更新時為 `{builtin, title, version, same}`，否則為 `null`。Migration `0017_engine_downloads` 讓 `model_downloads.model_id` 可以是 null（既有的列都不變；在 SQLite 上會重建這個資料表，因為沒有其他資料表參照它，所以是安全的）；降版時會刪除只下載推論引擎的列，並把它改回 NOT NULL。

### 經由伺服器下載（轉送）

agent 協定的 feature `relay`（1.4 起的 agent 和伺服器）。對列出它的 agent，在 `fetch_via` 為 `auto` 或 `server` 的機器上，desired 副本或下載的每個模型區塊（Hugging Face 或 URL）和推論引擎的壓縮檔區塊都會帶 `relay: {path, mode}`：`mode` 為 `server`（一律經由伺服器；設成這樣，或是 `auto` 且過去一小時內記得連不到那個主機）或 `auto`（先直接下載，遇到網路錯誤後改經由伺服器）。`server` 的模型區塊不帶 `token`。agent 之後向它自己的伺服器下載，絕不會連到伺服器指定的其他主機：

| 請求 | 回傳 |
|---|---|
| `GET /agent/v1/relay/{replica\|download}/{id}/model/{檔案路徑}` | Hugging Face 模型的一個檔案 |
| `GET /agent/v1/relay/{replica\|download}/{id}/model` | URL 模型的檔案 |
| `GET /agent/v1/relay/{replica\|download}/{id}/engine` | process 變體的推論引擎壓縮檔 |

`{id}` 是 desired 清單裡副本或下載的 id。請求帶機器憑證（`Authorization: Bearer …`），以及最多一個 `Range: bytes=N-`（或 `N-M`），會轉給來源；回應是來源的 200 或 206，含 `Content-Length` 和 `Content-Range`，以串流傳送，不會存在任何地方。瀏覽器（帶 `Origin` 的請求）一律拒絕。

伺服器只轉送那台機器**目前** desired 狀態裡有的東西：放在那裡且應該執行的副本，或排隊中、下載中的那台機器的下載，而且只限那個模型列出的檔案。來源網址由伺服器自己從目錄項目或配方組出來，使用它自己的 Hugging Face 權杖（或部署的權杖）和它自己的模型來源規則（`MODEL_ALLOW_PRIVATE_HOSTS`、URL 主機）。它每 2 秒檢查一次轉送是否還需要，不需要時就關閉。

| 狀態碼 | `code` | 什麼時候 |
|---|---|---|
| 401 | `credential_refused` | 沒有憑證，或憑證已撤銷 |
| 403 | `relay_off` | 機器設成 `direct`，或它的 agent 不支援 `relay` |
| 403 | `browser_refused` | 請求帶有 `Origin` |
| 403 | `source_refused` | 來源拒絕存取（受限模型，而且沒有可用的權杖） |
| 403 | `source_blocked` | 伺服器的規則不允許來源的位址 |
| 404 | `relay_not_wanted` | 機器的 desired 狀態裡沒有那個 id、檔案或推論引擎 |
| 416 | `bad_range`、`range_not_satisfiable` | 超過一個範圍，或超過結尾 |
| 429 | `relay_busy` | 那台機器已經有兩個轉送，或全部已有四個；`Retry-After: 5` |
| 502 | `source_unreachable`、`source_failed` | 伺服器連不到來源，或來源回了錯誤 |

agent 遇到 401、403 和 404 會停止（並顯示伺服器的說明），遇到 429 會等待且不算一次重試，其餘的則像網路錯誤一樣重試。這時它的進度會帶 `download.via: "server"`。

**從另一台機器**（協定 feature `relay_source`，1.4.1 起的 agent 和伺服器）。伺服器開啟模型檔的來源之前，會先問另一台有這個檔案的機器。候選機器是：已連線、agent 列出 `relay_source`、回報的快取（`model_cache.cached`）裡有這個模型的 revision（URL 模型則是它的 key，並且在 `model_cache.models` 裡列出和伺服器記錄相同的 SHA-256），而且沒有因為這個檔案被標記的其他機器。列出那個 GGUF 檔本身 key 的機器優先，其次是正在送的檔案比較少的，再其次是最近一次心跳較新的。提出要求的機器，它的請求完全不變。

伺服器透過 WebSocket 傳給選中的機器 `relay_send {request_id, path, item, offset, length}`。`item` 依伺服器持有的 digest 描述檔案：`{kind: "hf", repo, revision, file, blob, size}`（`blob` 是 LFS 檔的 SHA-256，否則是它的 git blob id）或 `{kind: "url", dir, sha256, size}`。agent 只在它自己磁碟上找到的模型裡找這個檔案（revision 的標記檔列出這個檔案、snapshot 連結指向那個 blob、快取裡大小相符的一般檔案；URL 模型的標記檔有那個 SHA-256），送的期間持有模型檔案的鎖（這時移除會回 `busy`），然後 POST 第 `offset` 到 `offset + length - 1` 個位元組：

| 請求 | 作用 |
|---|---|
| `POST /agent/v1/relay-send/{ticket}` | 這些位元組，帶機器憑證，`Content-Length` = `length`；全部轉出去之後回 `{"ok": true, "bytes": n}` |

每個回應都會關閉連線（`Connection: close`），所以傳送被提早結束時（提出要求的機器離開了、下載被取消），來源會立刻停止寫入。

之後它回 `relay_sent {request_id, ok, code, reason, bytes}`，`code` 是 `not_found`、`busy`（已經在送兩個）、`corrupt`（它送出的完整檔案和 digest 不符）或 `failed`。伺服器透過最多 64 段的佇列把內容轉進提出要求的機器的回應；`Content-Length` 和 `Content-Range` 由伺服器依檔案大小產生。回 `not_found` 或 `busy`，或 10 秒內沒有送出任何東西（之後 10 分鐘內不會再被要求送這個檔案）的機器會被跳過，改問下一台，最後才照上面的方式從網際網路取得。完整的檔案在經過時會計算 hash，最後一段保留到相符才送出：副本不符的機器在一小時內不會再被要求送這個檔案，稽核紀錄會記下 `node.relay_copy_refused`（那台機器、檔案、提出要求的機器和兩個 digest），部署的事件也會寫出來，而提出要求的機器對這個檔案的下一次請求會收到完整檔案（200，忽略它的 `Range`），所以會從頭重新下載。每台機器同時最多送兩個。

| 狀態 | `code` | 時機 |
|---|---|---|
| 401 | `credential_refused` | 沒有憑證，或憑證已撤銷 |
| 403 | `browser_refused` | 請求帶有 `Origin` |
| 404 | `send_not_found` | 這台機器沒有那個 ticket 的傳送（別台機器的憑證、已結束的 ticket） |
| 409 | `send_claimed`、`send_ended` | 同一個 ticket 第二次 POST；傳送在全部位元組轉出前就結束了 |
| 400 | `send_length` | `Content-Length` 不是要求的長度 |

這時經由伺服器的進度會帶 `download.source`，也就是送出檔案的機器名稱（由伺服器填入，agent 不能設定）；下載的 `via` 會變成 `machine`，並帶 `source: {id, name}`。Download to… 的 targets 裡，`from_machine` 會寫出哪台機器會經由伺服器把模型送給這台機器。

**推論引擎的映像檔**（協定 feature `relay_images`，1.4 起的 agent 和伺服器）。對同時列出它的 agent，desired 裡的副本（在 `spec` 旁邊）或下載，如果推論引擎的映像檔以 digest 釘選、並記錄在 `forgeyard/recipes/images.lock.json`，就會帶 `image_relay: {path, mode}`，模式相同；`path` 是 `/agent/v1/relay/{replica|download}/{id}/image`。只用 tag 指定的映像檔永遠不會有。

| 請求 | 回傳內容 |
|---|---|
| `GET /agent/v1/relay/{replica\|download}/{id}/image/manifest` | 機器平台（依它的 agent 回報的 `platform.arch`）記錄的 manifest，也就是 registry 原本的位元組，附 `Docker-Content-Digest` 和 `Forgeyard-Platform`（`linux/amd64`） |
| `GET /agent/v1/relay/{replica\|download}/{id}/image/blobs/{digest}` | 那份 manifest 的 config 或其中一層，依 digest；`Range` 同上 |

伺服器依記錄的 digest 取得 manifest，只有位元組的雜湊等於它、而且列的是記錄的 config 時才接受，並把它留在記憶體。blob 從 registry 的 API 取得，使用匿名 pull token（Docker Hub、GHCR、quay.io 和其他會發 token 的 registry），套用和模型檔相同的對外連線規則；registry 轉址到 CDN 時不會帶 token。完整的 blob 經過時會計算雜湊，最後一段要雜湊相符才會送出。agent 會把 manifest 的 digest 和記錄的 id 比對、每個 blob 和它的 digest 比對、config 的 content id 和記錄的比對、它的架構和機器的比對，然後載入一個同時含 OCI image layout（`oci-layout`、`index.json`、`blobs/sha256/…`）和傳統 `manifest.json` 的封存檔，加上 `repo:tag`，再對它執行 `image_ids` 檢查。它的進度是 `phase: "image"` 的 `download`，帶 `layers`、`layers_done`、`via: "server"`，載入時另有 `unpacking`。

| 狀態 | `code` | 何時 |
|---|---|---|
| 403 | `relay_off` | 機器設成 `direct`，或它的 agent 不支援 `relay_images` |
| 404 | `relay_not_in_image` | 那個 digest 不是那份 manifest 的 config 或任何一層 |
| 409 | `relay_image_not_pinned` | 映像檔沒有以 digest 釘選並記錄 id：請改用上傳 |
| 409 | `relay_no_platform` | 映像檔沒有那台機器架構的版本 |
| 502 | `source_failed` | registry 的 manifest 雜湊不等於記錄的 digest，或列的是別的 config |

### 把映像檔上傳到機器

`POST /api/machines/{id}/images`（admin）。內容是 `docker save` 或 `podman save` 寫出的檔案，可以用 gzip 壓縮，必須有 `Content-Length`（沒有時回 411），最大為 `IMAGE_UPLOAD_MAX_GB`（超過回 413）；`X-Filename`（URL 編碼）是稽核紀錄裡的檔名。伺服器送給 agent `image_load {request_id, path, size, name}`（協定 feature `image_load`）；agent 用它的憑證取得 `GET /agent/v1/image-upload/{ticket}`（只能一次；其他機器的憑證得到 404），把它串流進 `docker load` / `podman load`，再回覆 `image_loaded {request_id, ok, reason, images: [{id, tags, content, os, architecture}]}`。回給瀏覽器的是 `{ok, bytes, images}`，每個映像檔帶 `matches: [{recipe, variant, image, how}]`：`how` 是 `checked`（它的 id 或 content id 是那個 recipe 釘選映像檔記錄的）或 `tag`（名稱和只用 tag 指定的 recipe 映像檔相同）。

| 狀態 | `code` | 何時 |
|---|---|---|
| 409 | `machine_offline`、`agent_cannot_load` | agent 沒有連線，或沒有 容器引擎、或比 1.4 舊 |
| 409 | `upload_busy` | 那台機器已經有上傳在進行（全部兩個時回 429） |
| 400 | `upload_short`、`upload_broken` | 內容提早結束或中斷 |
| 504 | `agent_no_fetch`、`agent_stalled` | agent 30 秒內沒有開始取得，或 2 分鐘都沒有再取 |
| 422 | `image_load_failed` | 容器引擎無法載入（附 agent 的說明） |

稽核事件 `node.image_uploaded`（檔名、大小、`ok`、載入的映像檔或失敗原因）。

### 從另一台機器複製映像檔

`GET /api/machines/{id}/images`（管理員）會向那台機器的 agent 詢問它的容器引擎裡的映像檔（協定 feature `image_save`，1.4.1 起的 agent：`image_list {request_id}`，以 `image_listed {request_id, ok, reason, images}` 回答），回傳 `{machine_id, images: [{id, tags, content, size, created, os, architecture, matches}]}`，最多 256 個。

`POST /api/machines/{id}/images/copy`，內容 `{"source_id": 2, "image": "sha256:…"}`（管理員），把其中一個複製到機器 `id`。伺服器傳給來源 `image_save {request_id, path, image}`；它的 agent 以自己的 tag 儲存 id 為 `image` 的映像檔（必須是它自己的：絕不使用伺服器給的名稱），用 `docker save`（Podman 有多個 tag 時用 `podman save --multi-image-archive`），再以 chunked 方式 POST 到 `/agent/v1/image-send/{ticket}`，帶它的憑證（只能一次；別台機器的憑證會得到 404，第二次 POST 是 409，瀏覽器是 403；每個回應都會關閉連線，所以傳送被提早結束的來源會立刻停止）。伺服器像上傳一樣把它轉進目標機器的 `image_load`（不帶 `size`；給目標機器的回應是 chunked，來源中斷時絕不會正常結束），來源再回 `image_saved {request_id, ok, reason, bytes, image}`。回應是 `{ok, bytes, images, source: {id, name}, saved}`，每個映像檔帶 `matches` 和 `same_as_source`（它的 content id，沒有則用 id，和來源的相同；沒有可比對的則為 null）。

| 狀態 | `code` | 時機 |
|---|---|---|
| 422 | `bad_image`、`same_machine` | 映像檔不是用 id 指定；來源就是目標 |
| 409 | `source_offline`、`source_cannot_save` | 來源沒有連線，或沒有 容器引擎、或比 1.4.1 舊 |
| 409 | `source_busy` | 來源已經在送一個映像檔 |
| 409 | `machine_offline`、`agent_cannot_load`、`upload_busy` | 目標機器，同上傳（全部兩個時是 429） |
| 409 | `image_save_failed` | 來源在送出任何位元組之前就無法儲存（它的說明）：不是它的映像檔，或它的推論引擎失敗 |
| 504 | `source_no_send` | 來源 2 分鐘內沒有開始送 |
| 400 | `upload_broken` | 來源送到一半停了，或伺服器停掉一個 2 分鐘都沒送資料的來源；什麼都沒有載入。來源自己的回覆說明了原因（`docker save failed: …`）時，訊息會包含它的說明（伺服器最多等 10 秒，#215） |
| 413 | `upload_too_large` | 映像檔超過 `IMAGE_UPLOAD_MAX_GB` |
| 422 | `image_load_failed` | 目標機器的容器引擎無法載入（agent 的說明） |

稽核事件 `node.image_copied`（目標機器；`from_machine`、要求的映像檔、大小、`ok`、載入的映像檔和它們的 content id，或失敗原因）。在任何位元組送出前就被拒絕的不會記錄。

### 透過 API 查看機器的推論引擎映像檔

`GET /api/machines/{id}/engine-images`（所有登入的人，1.4.2）會向那台機器的 agent 詢問它的容器引擎裡的映像檔（`image_list`，同上），回傳 `{machine_id, listed, note, images, others, free_bytes, total_bytes, can_remove, remove_note}`。`images` 只包含 recipe 指定的映像檔，由大到小，每個是列出的內容再加上 `matches`、`used_by`（那台機器上副本正在執行它的部署：`[{name, kind, id}]`）、`agent`（agent 從它執行）和 `remove_note`（目前為什麼不能刪除，或 null）。`others` 是容器引擎裡其他映像檔的數量。`free_bytes` / `total_bytes`：容器引擎存放映像檔的位置，agent 看得到那個目錄時才有，否則為 null。agent 離線、不能列出（沒有 容器引擎、比 1.4.1 舊）或失敗時，`listed` 為 false 並帶 `note`。

`POST /api/machines/{id}/engine-images/remove`，內容 `{"image": "sha256:…"}`（管理員），刪除其中一個。伺服器會再列一次那台機器的映像檔，在詢問 agent 之前先做檢查；接著傳送 `image_remove {request_id, image}`（協定 feature `image_remove`，1.4.2 起的 agent）。agent 只刪除它自己清單裡的映像檔，絕不刪除它本身執行的映像檔、它的副本使用的映像檔，或容器引擎裡任何 container 使用的映像檔；它執行 `docker rmi` / `podman rmi`，不加 `-f`（映像檔有好幾個 tag 時以那些 tag 刪除），並回答 `image_removed {request_id, ok, code, reason, in_use, image, freed_bytes, free_bytes, total_bytes}`。1.4.2 的 agent 的 `image_listed` 還會帶 `agent_image`（它執行的映像檔的 id）和 `free_bytes` / `total_bytes`。回應是 `{ok, image, freed_bytes, free_bytes, total_bytes}`：`freed_bytes` 是映像檔的大小（最多釋出這麼多：其他映像檔共用的 layer 會保留）；`free_bytes` / `total_bytes` 是之後的可用空間，agent 看得到時才有。

| 狀態 | `code` | 時機 |
|---|---|---|
| 422 | `bad_image` | 映像檔不是用 id 指定 |
| 409 | `machine_offline`、`agent_too_old` | agent 沒有連線，或比 1.4.2 舊 |
| 404 | `image_not_found` | 那台機器（已經）沒有列出這個 id |
| 409 | `not_an_engine_image` | 沒有 recipe 指定這個映像檔：它不屬於 Forgeyard，不能刪除 |
| 409 | `agent_image` | agent 從這個映像檔執行 |
| 409 | `image_in_use` | 那台機器上有部署正在使用它（`in_use` 列出它們），或容器引擎裡有 container 正在使用它（agent 的說明） |
| 409 | `image_remove_failed` | 容器引擎沒有刪除它（它的說明） |
| 504 | `agent_no_answer` | agent 5 分鐘內沒有回答 |

稽核事件 `node.image_removed`（映像檔、它的 tag、指定它的 recipe、`ok`、釋出的位元組，或 agent 的 `code` 和原因），每次要求 agent 刪除都會記錄；在那之前就被拒絕的不會記錄。

## 設定健康檢查的項目

管理員會在 **開始使用 → 設定健康檢查** 看到這些檢查結果，頂端列會顯示錯誤和警告的數量，相關的頁面也會以橫幅顯示。一般使用者和 operator 都看不到。每項檢查都在頁面要求時，根據設定、資料庫和各 agent 最後一次的回報執行。*錯誤*會一直顯示到修好為止；*警告*或*提示*（API 裡是 `info`）管理員可以延後 30 天，只對自己隱藏。使用方式：[檢查設定健康狀態](how-to.md#check-the-setup-health)。

| Id | 嚴重程度 | 何時出現 |
|---|---|---|
| `setting-unknown` | 警告 | `.env` 裡的某個名稱，或環境裡開頭像設定（`GATEWAY_`、`PROVIDER_`、`EGRESS_`…）或和某個設定非常接近的名稱，並不是設定。它會說出你可能想設的是哪一個 |
| `tls-setting-ignored` | 警告 | `TLS=off` 時設了 `TLS_*`；沒有自己的憑證卻設了 `TLS_CA_FILE`；使用自己的憑證時設了 `TLS_EXTRA_NAMES` |
| `setting-removed` | 警告 | 還設著一個[在 1.4 移除的設定](#removed-settings)，所以它被忽略。這項發現會指出改用什麼，或改成怎麼運作 |
| `secret-key-both` | 警告 | `FORGEYARD_SECRET_KEY` 和 `FORGEYARD_SECRET_KEY_FILE` 都有設定 |
| `heartbeat-flapping` | 警告 | `AGENT_OFFLINE_SECONDS` 小於 2.5 次心跳（小於 5 秒：最短的心跳是 2 秒） |
| `private-hosts-allowed` | 警告 | `MODEL_ALLOW_PRIVATE_HOSTS`、`PROVIDER_ALLOW_PRIVATE_HOSTS` 或 `MEDIA_ALLOW_PRIVATE_HOSTS` 已開啟 |
| `cookie-not-secure` | 警告 | 伺服器使用 HTTPS，但設定了 `COOKIE_SECURE=false` |
| `admin-password-left` | 警告 | 擁有者已經存在，`ADMIN_PASSWORD` 卻還設定著 |
| `env-seed-ignored` | 提示 | 某個初始值設定（`MODEL_URL_HOSTS`、`MODEL_LOCAL_DIRS`、`GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、`GATEWAY_CLOUD_*`）和管理員儲存的值不同，而儲存的值優先 |
| `secret-key-backup` | 警告 | 還沒有管理員確認已備份目前的機密金鑰。按 **下載備份…**（用通關密語加密的檔案，見[金鑰備份檔](#the-key-backup-file)）或勾選 **我已經備份了識別碼 … 的金鑰** 就會消失；金鑰更換後會再出現 |
| `host-loopback` | 錯誤 | `HOST` 是 loopback 位址，卻有機器從其他位址加入（而且前面沒有代理） |
| `public-url-unset` | 提示 | 其他機器連得到這台伺服器，`PUBLIC_URL` 卻是空白。如果你是以 `127.0.0.1` 或 `localhost` 開啟頁面，則是警告 |
| `certificate-name-missing` | 錯誤 | 你自己的憑證沒有包含 `PUBLIC_URL` 的主機 |
| `certificate-expiring` | 警告 | 你自己的憑證在 30 天內到期，或 Forgeyard 的 CA 在 90 天內到期。14 天內到期則是錯誤 |
| `registrations-pending` | 提示 | 有人在等待核准 |
| `agent-outdated` | 警告 | 某台機器的 agent 比伺服器舊。如果它的協定比較舊，則是錯誤 |
| `replica-traffic-plain` | 警告 | 送往某台機器的流量沒有加密：它的 agent 比 1.4.5 舊，閘道以純 HTTP 連它；修正連結會開啟它的更新指令（[更多](troubleshooting.md#traffic-to-a-machine-is-not-encrypted)） |
| `replica-certificate` | 錯誤 | 某台機器的 agent 以 TLS 提供副本，卻沒有這台伺服器簽發的憑證，或憑證已過期、沒有在更新（剩不到 3 天）：連不到它的副本（[做法](troubleshooting.md#a-machine-has-no-certificate-for-its-replicas)） |
| `gpus-shared-no-allowlist` | 警告 | 某台機器的顯示卡佔用的記憶體比 Forgeyard 在那裡保留的多出至少 1 GiB，而且沒有人選擇它的 GPU 存取 |
| `machine-no-models` | 提示 | 某個上線的 agent 沒有模型快取，所以不會有模型放到那裡。如果它有 GPU，則是警告 |
| `apps-deprecated` | 警告 | 有應用程式（不是模型的 workload）：它們、它們的 API 和 `/x/` 會在 1.5 移除 |
| `process-workloads-off` | 提示 | 某個上線的 agent 拒絕行程；檢查結果會說明原因 |
| `model-cache-full` | 警告 | 某台機器的模型快取磁碟剩餘空間不到 10% |
| `machine-simulated` | 警告 | 某台機器回報的是模擬的硬體 |
| `machine-offline-with-replicas` | 警告 | 某台機器離線超過 5 分鐘，上面還有放置的副本 |
| `machine-loopback-address` | 警告 | 不是伺服器主機的機器把副本公開在 loopback 位址上（透過 tunnel 或 proxy），而且它的連接埠範圍沒有轉送到伺服器的主機（[做法](troubleshooting.md#a-machine-publishes-its-replicas-on-a-loopback-address)） |
| `machine-gpu-access-lost` | 錯誤 | 線上機器的新行程打不開它的 GPU（agent 每 5 分鐘以workload 帳號檢查一次；只用 `--gpus` 啟動的 agent 容器，在主機執行 `systemctl daemon-reload` 後會失去這份存取權）。這段期間不會把行程類 workload放到那些 GPU 上；修正連結會開啟機器的更新指令（[做法](troubleshooting.md#new-processes-on-a-machine-cannot-open-its-gpus)） |
| `replica-margin-low` | 警告 | 某張有副本在執行的顯示卡，剩餘空間少於 `VRAM_HEADROOM_MIB`（至少 256 MiB） |
| `deployment-waiting` | 警告 | 某個副本等待機器已超過 15 分鐘 |
| `deployment-could-share` | 說明 | 正在執行的獨占副本保留的記憶體不到 24 GiB 以上卡片的一半：改用 Share VRAM，其餘空間就能放其他模型 |
| `deployment-idle-awake` | 說明 | 1.5.0：關閉 *閒置時休眠* 的執行中部署 3 天沒有請求（用量的最後一天、伺服器啟動後看到的請求，或從部署、啟動或修改起算），而且佔著 GPU 記憶體：要讓它閒置時休眠嗎？ |
| `deployment-wakes-often` | 說明 | 1.5.0：開啟 *閒置時休眠* 的部署在過去 24 小時被喚醒超過 10 次，而且上次啟動超過一分鐘：把閒置時間調長 |
| `extra-args-override` | 警告 | 部署的額外推論引擎參數重複了 Forgeyard 會設定的旗標（記憶體、上下文、位址、金鑰） |
| `gated-model-no-token` | 提示 | 某個受限（gated）Hugging Face 模型的部署沒有自己的權杖，伺服器也沒有。下載被拒絕後會變成錯誤 |
| `key-cloud-without-budget` | 警告 | 某把金鑰可以使用雲端（也包括透過伺服器的預設值），而且會用到某個供應商，卻沒有任何預算適用：金鑰、它的所屬個人和供應商都沒有預算 |
| `cloud-model-unpriced` | 錯誤 | 已經有預算存在時，某條路由的雲端模型沒有輸入或輸出價格：它的請求會回應 402 `budget_unpriced` |
| `cloud-model-cost-unknown` | 提示 | 同樣的情況但沒有預算：它的費用未知 |
| `provider-no-key` | 錯誤 | 某個已啟用、而且種類需要 API 金鑰的供應商沒有金鑰 |
| `provider-blocked-by-egress` | 錯誤 | 某個供應商的主機不在 `EGRESS_ALLOWED_HOSTS` 裡 |
| `provider-inside-public-address` | 警告 | 某個標示為在你網路內的供應商，只解析得到公開位址 |
| `cloud-default-open` | 提示 | 新金鑰拿到的設定高於 **永不**，或使用者可以把自己的金鑰設為 **允許**。如果某把金鑰同時沒有預算，則是警告 |
| `cloud-unreachable` | 提示 | 路由有雲端目標，卻沒有任何金鑰可以使用雲端 |
| `route-cloud-before-local` | 警告 | 某條路由的雲端層排在它的第一個本機層之前 |
| `route-cloud-never-used` | 提示 | 勾選了 **僅本機** 的路由有雲端目標，或某條溢出規則在本機層之後沒有其他層 |
| `route-no-target` | 錯誤 | 某條路由沒有任何目標能回應：每個目標都被停用，或它的供應商被停用 |
| `route-local-stopped` | 警告 | 某條同時有本機層和雲端層的路由，它的本機部署全部停止 |
| `budget-zero` | 提示 | 某個預算是 $0，會擋下它適用的每個雲端請求 |
| `prices-stale` | 提示 | 某條路由使用的模型，來自 OpenRouter 的價格已經超過 60 天 |
| `keys-idle-never-expire` | 提示 | 永不過期的金鑰已經 90 天沒有使用 |

## 命令列

### 伺服器

| 指令 | 用途 |
|---|---|
| `uv run python main.py` | 啟動伺服器（等同 `python -m forgeyard serve`，或單獨的 `python -m forgeyard`）。資料庫的變更會在啟動時執行 |
| `python -m forgeyard secrets reset --yes` | 機密金鑰遺失時使用：產生一把新的，並捨棄舊金鑰加密的內容（請先停止伺服器）。沒有 `--yes` 時只說明會捨棄什麼 |
| `python -m forgeyard secrets restore FILE` | 從主控台下載的金鑰備份放回機密金鑰和 CA（請先停止伺服器）。會詢問通關密語。`--data-dir DIR`：寫到另一個資料夾；`--force`：取代那裡已有的不同金鑰或 CA（舊檔案保留為 `<名稱>.replaced-<時間>`）；`--passphrase-file PATH`：從檔案的第一行讀取通關密語。通關密語錯誤、檔案損壞或那裡已有不同的金鑰時，結束代碼為 2，什麼都不寫入 |
| `python -m forgeyard --version` | 顯示版本 |

### 金鑰備份檔

**下載備份…**（管理員，1.4.2）呼叫 `POST /api/secret-key/backup`（JSON：`password`、`passphrase`、`passphrase_confirm`），回應 `forgeyard-key-backup-<金鑰 id>-<YYYYMMDD>.fykey`，帶 `Content-Disposition: attachment` 和 `Cache-Control: no-store`。密碼的檢查和登入一樣（相同的延遲機制；密碼錯誤回應 401 `wrong_password`，並以 `secret_key.backup_failed` 記入稽核紀錄）；通關密語需要 12 到 1024 個字元、兩種以上不同的字元，而且不能和密碼相同（422）。`GET /api/secret-key` 會說明備份裡有什麼。檔案：

| 位元組 | 內容 |
|---|---|
| 0–7 | `FYKEYBK` 加一個零位元組 |
| 8–9 | N，標頭的長度（big-endian） |
| 10 … 10+N | 標頭，UTF-8 JSON：`format` `forgeyard-key-backup`、`version` 1、`kdf`（`name` `argon2id`、`memory_kib` 65536、`iterations` 3、`lanes` 4、`salt`：16 個隨機位元組，base64）、`cipher`（`name` `aes-256-gcm`、`nonce`：12 個隨機位元組，base64）、`kid`、`created_at` |
| 其餘 | AES-256-GCM 密文，後面接 16 位元組的 tag |

32 位元組的加密金鑰是通關密語（正規化為 Unicode NFC、以 UTF-8 編碼）用標頭的 salt 和參數做 Argon2id（RFC 9106）得到的。附加資料（associated data）是密文之前的所有位元組，所以標頭被改過和密文被改過一樣會失敗。明文是 JSON：`kid`、`secret_key`（base64url，和 `secret.key` 相同）、`tls`（`{"ca.key": PEM, "ca.crt": PEM}`，沒有 Forgeyard 自己的 CA 時為 `null`）、`created_at` 和 `server_version`。`secrets restore` 只讀取第 1 版，拒絕低於上述值、或高於 1 GiB 記憶體、16 次、16 lanes 的 KDF 參數，並在寫入任何東西之前檢查金鑰和它的 id 相符、CA 的金鑰和它的憑證相符。

### agent

用單行安裝裝好後，agent 的指令是 `forgeyard`（在 `/usr/local/bin`）；從原始碼目錄執行則是 `uv run --no-sync python -m forgeyard`。每個指令都接受 [agent 設定](#agent-settings)的旗標，放在指令名稱之前或之後都可以，`-v`（`--verbose`）則顯示除錯輸出。`--fingerprint` 是 `--ca-fingerprint` 的另一個名稱。在 agent 的 Docker 映像檔裡，指令本身已經是 `forgeyard agent`，所以只要加上後面的部分（`… forgeyard-agent:<version> inventory`）。

| 指令 | 用途 |
|---|---|
| `forgeyard agent` | 執行 agent（服務執行的是 `forgeyard agent --config /etc/forgeyard/agent.toml`）。如果有加入權杖但還沒有憑據，會先加入；`--rejoin` 會以新機器的身分重新加入 |
| `forgeyard agent init [--print] [--force]` | 根據偵測到的內容（GPU、Docker 或 Podman、systemd、執行行程的帳號）寫出 `/etc/forgeyard/agent.toml`，並印出內容。`--print` 只印出不寫入。在這裡給的旗標（`--server`、`--ca-fingerprint`、`--model-cache`…）會寫進檔案 |
| `forgeyard agent join --join-token fyj_…` | 用檔案裡的設定加入，儲存憑據後結束。已經加入過：不做任何事；`--rejoin` 會以新機器的身分重新加入 |
| `forgeyard agent config [--get KEY]` | 列出所有生效中的設定和它們的來源；`--get models.cache` 只印出一個值 |
| `forgeyard agent trust --server https://… [--ca-fingerprint …]` | 把已加入的機器切換到另一個位址或 CA，並保留它的憑據 |
| `forgeyard agent inventory [--json] [--raw]` | agent 偵測到的內容（GPU、CPU、記憶體），不需要加入 |
| `forgeyard agent runtimes [--json]` | 這台機器能執行什麼（行程、容器、容器內的 NVIDIA），不需要加入 |
| `forgeyard --version` | agent 的版本 |

### 安裝腳本

`curl … https://<server>/install.sh | sudo sh -s -- OPTIONS`：

| 選項 | 用途 |
|---|---|
| `--join fyj_…` | 加入權杖（第一次安裝）。也可以寫成 `--join-token` |
| `--ca-fingerprint sha256:…` | 伺服器 CA 的指紋：在下載任何東西之前檢查，之後由 agent 釘選。也可以寫成 `--fingerprint` |
| `--ca-file PEM` | 改為釘選這個 CA 檔案 |
| `--server URL` | 伺服器的位址（預設：提供這個腳本的伺服器） |
| `--name NAME`, `--label KEY=VALUE` | 機器的名稱和標籤（第一次安裝） |
| `--model-cache DIR` | 模型存放的位置（預設 `/srv/forgeyard-models`） |
| `--workload-user USER` | 行程使用的帳號（預設 `forgeyard-run`，不存在時會自動建立） |
| `--no-start` | 安裝並加入，但不啟動服務 |
| `--rejoin` | 以新機器的身分重新加入 |
| `--uninstall [--purge]` | 移除服務和程式；`--purge` 會連設定和憑據一起刪除 |
| `-h`、`--help` | 列出選項 |

再執行一次時，它會把 agent 升級到伺服器的版本，並保留設定和憑據。它需要 x86_64 或 aarch64 的 Linux、root 權限、`curl`，以及能連上 PyPI 以下載 agent 的少數幾個相依套件。它會優先使用已安裝的 `uv`，否則使用系統上已有的 Python 3.13 或更新版本，再不然就把 `uv` 安裝到 `/opt/forgeyard/bin`，它會自備 Python。沒有 systemd 的話，它會安裝好所有東西，並印出啟動 agent 的方法。

它下載的東西都會經過檢查（1.4.5）：agent 和它的相依套件清單來自伺服器，各自對照腳本裡寫明的 SHA-256；相依套件從 PyPI 下載，版本固定為這個伺服器版本測試過的版本，每個檔案都要符合它公開的雜湊值（`--require-hashes`，不會連帶安裝其他套件）；它安裝的 `uv` 是固定的版本（0.9.24），從 GitHub 下載並對照 SHA-256，而不是執行安裝腳本。透過鏡像站連到 PyPI 的機器（`UV_INDEX_URL`、`PIP_INDEX_URL`）拿到的仍是同樣的檔案，否則就不安裝。

## 內建配方

配方的 variant 依序嘗試；第一個 `when` 符合某張卡（或 CPU）、而且機器上有它的執行環境的 variant，就用來執行副本。部署不能自己選 variant；只放你要的 variant 的自訂配方可以。

| 配方 | Variant | 執行在 | 映像檔或 release |
|---|---|---|---|
| `vllm`、`vllm-pooling` | `cuda`、`cuda-12.9`、`rocm` | NVIDIA（compute capability 7.0 以上，driver 580 以上；`cuda-12.9` 為 575 以上）；AMD `gfx90a`、`gfx942`、`gfx950`、`gfx1100`、`gfx1101`、`gfx1150`、`gfx1151`、`gfx1200`、`gfx1201` | `vllm/vllm-openai:v0.32.0`、`-cu129`、`vllm/vllm-openai-rocm:v0.32.0` |
| `llama-cpp` | `cuda` | NVIDIA，compute capability 5.0 以上，driver 570 以上 | `ghcr.io/ggml-org/llama.cpp:server-cuda-v1.0.0-b12359-b1b770d`（CUDA 12.8） |
| | `rocm` | AMD `gfx908`、`gfx90a`、`gfx942`、`gfx1030`、`gfx1100`、`gfx1101`、`gfx1102`、`gfx1200`、`gfx1201` | `…:server-rocm-v1.0.0-b12359-b1b770d`（ROCm 7.2.1） |
| | `vulkan` | 其他所有 AMD GPU：APU（`gfx1151`、`gfx1150`、`gfx1103`…）與 ROCm 映像檔沒有編譯的卡。只給那張卡的 render node，不給 `/dev/kfd` | `…:server-vulkan-v1.0.0-b12359-b1b770d`（Mesa RADV） |
| | `cpu`、`cpu-process` | x86_64 與 aarch64 用容器；x86_64 沒有 Docker 時用行程 | `…:server-v1.0.0-b12359-b1b770d`；release `v1.0.0`（它的封存檔就是 build `b12359` 的）的 `llama-b12359-bin-ubuntu-x64.tar.gz`，用它的 SHA-256 檢查（glibc 2.34 以上：Ubuntu 22.04 以後、Debian 12 以後） |
| `faster-whisper` | `cuda`、`cuda-12.4`、`cpu` | NVIDIA、CPU | `ghcr.io/speaches-ai/speaches:0.8.3-*` |
| `speaches-tts`（1.3） | `cpu` | CPU（容器），任何機器都可以 | 與 faster-whisper 相同的 `ghcr.io/speaches-ai/speaches:0.8.3-cpu` 映像檔。只用 CPU：Kokoro 和 Piper 跑在 ONNX Runtime 上，用 CPU 產生語音的速度就比播放還快；在 CUDA 映像檔裡 Kokoro 其實也是用 CPU 跑 |
| `vllm-omni-tts`（1.3） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU） | `vllm/vllm-omni:v0.32.0`（CUDA 13.0） |
| `vllm-omni-image`（1.3） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU） | 同樣的 `vllm/vllm-omni:v0.32.0` |
| `vllm-omni-video`（1.3） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU） | 同樣的 `vllm/vllm-omni:v0.32.0` |
| `sd-cpp`（1.3） | `cuda` | NVIDIA，compute capability 5.0 以上、驅動程式 560 以上 | `ghcr.io/leejet/stable-diffusion.cpp:master-cuda`（build `3bad892`，CUDA 12.6） |
| | `vulkan` | 所有 AMD GPU 與 APU。只給那張卡的 render node，不給 `/dev/kfd` | `…:master-vulkan`（build `32d516c`，Mesa RADV） |
| | `cpu-process` | x86_64，以行程執行、不需要 Docker（glibc 2.39 以上：Ubuntu 24.04、Debian 13） | release `master-1043-3be2378` 的 `sd-master-3be2378-bin-Linux-Ubuntu-24.04-x86_64.zip`，用它的 SHA-256 檢查。需要 1.3 版 agent（它會解開 `.zip`） |
| `vision`（1.4） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU，依記憶體共用） | Forgeyard 自己的 `docker.io/tonyhuang1101/forgeyard-vision:1.4.4rc1-cuda`（torch +cu130） |
| | `cpu` | x86_64 與 aarch64，在容器中，任何機器都行（AMD 機器也是：還沒有 ROCm 版） | `…/forgeyard-vision:1.4.4rc1`（linux/amd64 與 arm64） |

`vllm` 和 `vllm-pooling`（1.5.0）宣告了 `engine_cache: {env: VLLM_CACHE_ROOT, enabled: true}`：vLLM 的編譯快取會在兩次啟動之間保留，放在副本所在機器上每個副本各自的目錄裡，所以重新啟動或被喚醒時會載入第一次啟動編譯好的結果（[在兩次啟動之間保留 vLLM 的編譯快取](how-to.md#keep-vllms-compile-cache-between-starts)）。自訂配方可以用 `enabled: false` 關閉它，或用 `enabled: true` 為其他推論引擎開啟它（只限容器 variant）。

`vllm-pooling` 只有在序列分類模型（`…ForSequenceClassification`）只有一個輸出時才把它當 reranker 服務，和 vLLM 的判斷一樣：`config.json` 的 `id2label` 只有一項，或 `num_labels` 為 1（1.4.3，#147）。兩個以上標籤的是文字分類模型（任務 `classify`，1.4.4）：用 reranker 檢查或加入它會回 422 `cannot_inspect`，訊息列出它的標籤。語言模型型的 reranker（Qwen3-Reranker）不受影響。

當作文字分類模型時（1.4.4，#144），`vllm-pooling` 接受 vLLM 0.32.0 原生支援、附分類頭的架構：`BertForSequenceClassification`、`RobertaForSequenceClassification`、`XLMRobertaForSequenceClassification`、`ModernBertForSequenceClassification`、`GteNewForSequenceClassification`、`GPT2ForSequenceClassification`、`JambaForSequenceClassification`、`LlamaBidirectionalForSequenceClassification`。vLLM 的 pooling runner 會自己為它們選 classify 任務（不需要參數）。加入時會被拒絕並說明原因（422 `cannot_inspect`）的有：只有一個輸出（reranker）、迴歸頭、零樣本（NLI）模型（標籤像 entailment、contradiction），以及其他架構（DeBERTa、DistilBERT、BART：1.5 的 transformers 推論引擎）。沒有 `config.json` 可讀的儲存庫（GGUF、ONNX）回 422 `not_a_text_classifier`。以其他種類加入的模型不能改成文字分類模型（它的標籤沒有被讀取：請重新加入），文字分類模型也不能改成 reranker。

部署沒有指定 context 時，如果模型本身的最大長度比配方的預設短，就從模型的最大值開始（1.4.4）：512 個 token 的 BERT 分類模型會得到 `--max-model-len 512`，而不是 8192。RoBERTa 和 XLM-RoBERTa 模型的位置從 padding index 之後開始算，所以 514 個位置嵌入只放得下 512 個 token（BGE-M3：8192）。

`vllm` 會傳 `--max-num-batched-tokens`，也就是 vLLM 一個步驟處理的 token 數（1.4.5，#201）：2,048，最多 *Concurrent requests* × *Context length*；Gemma 4 12B、26B-A4B 和 31B 在部署接受圖片、音訊或影片時是 2,496（vLLM 不會把它們的一張圖片或一段影片拆到兩個步驟，最長的是 32 格的影片，2,496 個 token）。這就是 vLLM 0.32.0 在 A100 和 70 GB 以下的卡上自己會選的值。讓它自己選的話，在 H100、H200 上會是 8,192，160 GB 以上是 16,384，而估算是在選卡之前做的，無從得知：滑動視窗層（Gemma、gpt-oss）每個請求除了視窗，還要存兩個這樣的步驟，所以 Gemma 4 12B 在 16,384 × 1 時估了 5.25 GiB 的 KV cache，vLLM 實際只用了 2.09 GiB。現在兩者在每張卡上都一致。在 H100、H200 上，長的提示詞會分成比 vLLM 自己選的更多、更小的步驟讀完。

`llama-cpp` 也提供文字轉語音：搭配 TTS GGUF 與它的 projector，上面每種 variant 都可以。

`llama-cpp` 跑的是 llama.cpp **1.0.0**（1.3.1），這是它第一個使用語意化版本號的 release。這個 release
的映像檔與封存檔就是同一個 commit 的每日建置 `b12359`，所以映像檔 tag 同時寫出版本、build 和 commit
（`server-vulkan-v1.0.0-b12359-b1b770d`），`llama-server --version` 顯示 `1.0.0-dev (build 12359)`。1.3.0
以前跑的是 build `b12242`。配方傳的每個參數在兩者都一樣。它的 *KV cache type*（`kv_cache`，在
**進階** 裡）預設是 `f16`；`f8_e4m3`（FP8，llama.cpp 1.0.0 新增）讓 KV cache 每個值只用一個位元組而不是兩個，
所以估算的 KV cache 減半：Qwen3-4B 在上下文 8,192 時，每個序列從 1.125 GiB 變成 0.5625 GiB（每多一個同時請求少
576 MiB）。FP8 在 CPU、CUDA 和 ROCm 上有對應的 kernel；llama.cpp 沒有 Vulkan 的版本，所以在 Vulkan 上請保持 `f16`。
有些架構不接受它（MLA 模型，例如 DeepSeek），這時副本會啟動失敗：改回 `f16` 即可。

stable-diffusion.cpp 只用每次建置都會移動的 tag（`master-vulkan`、`master-cuda`）發布映像檔。配方用 digest 釘住檢查過的那些 build；`scripts/pin_images.py` 會保留它們（tag 已經移動只會當成資訊回報），除非執行時加上 `--move-rolling`。它之後的 Linux release 是在 Ubuntu 26.04（glibc 2.43）上建置的，所以 CPU 行程用的是較早的 build；它的 OpenAI API 是一樣的。`sd-server` 沒有 API 金鑰：沒有金鑰守門的 agent（1.1 之前）不會分到 `sd-cpp` 副本。第一個釘選的 CUDA build（`465adf3`）在 A100 上跑 Z-Image-Turbo 會當掉（[#43](https://github.com/xianhong1208/forgeyard/issues/43)），已換成 build `3bad892`。

每個映像檔也都用 digest 釘選（`tag@sha256:…`）；頁面上顯示的是 tag。
`scripts/pin_images.py` 會在 `forgeyard/recipes/images.lock.json` 記錄每個映像檔的正確副本可能顯示的 id（index、manifest 和 config 的 digest，以及每個平台的內容 id），所以在連不到 registry 的機器上用 `docker load` 載入的副本會被檢查並使用：[離線機器](how-to.md#offline-machines)。

Variant 的欄位：`name`、`when`（`vendor`：`nvidia`、`amd` 或 `cpu`；`arch`：AMD 的 gfx target 或 CPU 架構；`compute_capability` 與 `driver`：像 `>=8.0` 這樣的比較）、`runtime`（`container` 或 `process`）、`image` 與 `entrypoint`（容器）、行程用的 `engine`（`url`、`sha256`、`binary`：agent 下載的 release）或 `command`（已安裝的程式）、取代配方設定的 `args` 與 `env`，以及 `amd_kfd`（AMD 容器；預設 `true`：給 `/dev/kfd` 和那張卡的 render node；`false`：只給 render node，給只用 Vulkan 的推論引擎），以及 `unsupported`（1.3.1，這個 variant 不能執行的參數值，例如 `{kv_cache: [f8_e4m3]}`：帶有這些值的部署不會被放到這個 variant 上，放置結果會說明原因）。

`vision` 配方（1.4）用 `--task {task}`（模型的任務，專屬的佔位符）和 `--model {model_path}` 執行 Forgeyard 自己的視覺伺服器。它的映像檔和其他推論引擎一樣以 digest 固定（1.4.1），所以載入的副本會用記錄的 id 檢查，連不到 registry 的機器也能經由伺服器取得。升級伺服器不會改變它：某個 release 改了視覺伺服器時，固定的映像檔會在之後的版本移到新映像檔。自訂配方仍可用 `forgeyard_image: vision`（或 `vision-cuda`）代替 `image`，指定和伺服器同版本、只有 tag 的映像檔（1.4.0 的 `vision` 配方就是這樣寫的）；這種映像檔不會被檢查，也不會經由伺服器轉送。模型的 `config.json` 寫的是 `…ForObjectDetection` 架構（detect：RT-DETR、D-FINE、不用 timm backbone 的 DETR…）、SAM 2 或 2.1（segment），或 `…ForDepthEstimation`（depth：Depth Anything、DPT…）時，就是視覺模型。Hugging Face 上的視覺模型只接受寬鬆授權（Apache-2.0、MIT、BSD）：其他授權或沒有授權回 422 `not_a_vision_model`（Depth Anything V2 Base 與 Large 是 CC-BY-NC-4.0，Ultralytics 的 YOLO 權重是 AGPL-3.0）。零樣本（開放詞彙）偵測模型，也就是用 `model_type` 或架構認出的 transformers 零樣本偵測模型（Grounding DINO、MM-Grounding-DINO 與 LLMDet、OmDet-Turbo、OWL-ViT、OWLv2），是標上 `zero_shot` 的 detect 模型（1.4.4，#142；1.4.3 會拒絕它們，1.4.3 之前加入的靠目錄存下的 config 認出，不需要遷移）。OmDet-Turbo 和用 timm backbone 的 DETR 一樣被拒絕：它的 transformers 版本經由 timm 載入 backbone（加入時，或部署 1.4.3 之前加入的時候，回 422 `not_a_vision_model`）。圖片分類器（1.4.4，#141，任務 `image-classify`）是 `config.json` 寫的是 `…ForImageClassification` 架構（固定類別：ViT、ConvNeXt V2、DINOv2…；timm 權重 `timm_wrapper` 會被拒絕）或 CLIP 類架構（標上 `zero_shot`：CLIP、SigLIP、SigLIP 2、ALIGN、AltCLIP、Chinese-CLIP、MetaCLIP 2）的模型，以 `vision-classify` 提供。零樣本偵測模型和圖片分類器需要 1.4.4 起的視覺映像檔：配方的 `forgeyard-vision` 映像檔比較舊時，部署或估算會回 422 `vision_image_too_old`，寫出兩個版本（`1.4.4rc1` 這類預先發布版算作它之後的正式版，也就是 1.4.4）；其他名稱的映像檔（本機建置）照原樣接受。CLIP 儲存庫裡 transformers 權重旁邊的 OpenCLIP 權重（`open_clip_*`）不會被讀取，也不會下載。估算：檔案大小，GPU 上再加 1 GiB（和 CUDA context），CPU 上再加 1.25 GiB 記憶體，用三個推薦模型量的（CPU 上峰值 0.66 到 1.12 GiB）；Grounding DINO 和 MM-Grounding-DINO 再加 512 MiB（文字編碼器和 800 像素的特徵：0.64 GiB 的檔案在 CPU 上峰值 2.24 GiB，1.4.4）。

影片配方（1.3）有一個 `video` 區塊：`request: vllm-omni`（vLLM-Omni 的非同步工作）和 `frames_step`（4：推論引擎做 k x 4 + 1 格）。它只提供 `video` 任務，以 `openai-videos` 提供，它的 `{keep_seconds}` 佔位符是部署的 `keep_minutes` 再加一小時。

### 內建配方的新版本

1.5.2（#95）。`POST /api/engine-updates/check`（管理員）會立刻向每個內建配方自己的映像檔儲存庫查詢（`GET /v2/<repo>/tags/list`，並跟著 `Link: rel="next"` 往下讀：GHCR 每頁回 1,000 個標籤；對候選標籤用 `HEAD` 取得 digest，Docker Hub 不把它算成一次拉取），以及 GitHub 的發布版本（匿名：每個位址每小時 60 次；一次檢查大約用 5 次）。它走伺服器的模型來源規則（https 和公開位址；`MODEL_ALLOW_PRIVATE_HOSTS`），每個請求最多 20 秒、整體最多 90 秒，每個儲存庫最多讀 30 頁標籤。就算某個 registry 失敗，它也會回 200 和結果（那個推論引擎標成 `could_not_check`）；另一個檢查正在進行時回 409 `check_running`。`vision` 配方不檢查。

`GET /api/engine-updates`（所有登入的人）回傳 `{"result": …, "can_check": bool}`，第一次檢查之前 `result` 是 null。結果存在一筆 `server_settings`（`engine_updates`，不需要資料庫遷移），每次檢查都會以 `recipe.updates_checked` 記進稽核紀錄，附上每個配方找到的新版本。

```json
{"checked_at": "…", "checked_by": "admin", "seconds": 21.4, "github_requests": 5,
 "engines": {"vllm": {"recipe": "vllm", "version": "0.32.0", "kind": "release", "status": "newer",
   "newest": "0.34.0", "errors": [], "candidates": [
     {"version": "0.34.0", "kind": "release",
      "release": {"tag": "v0.34.0", "url": "https://github.com/vllm-project/vllm/releases/tag/v0.34.0", "published_at": "…"},
      "variants": [
        {"name": "cuda", "available": true, "tag": "v0.34.0", "digest": "sha256:…", "image": "vllm/vllm-openai:v0.34.0@sha256:…"},
        {"name": "cuda-12.9", "available": false, "why": {"code": "no_image", "tag": "v0.34.0-cu129", "last": "0.32.0", "text": "…"}}],
      "mentions": [{"flag": "--gpu-memory-utilization", "lines": 5, "excerpts": ["…"]}]}]}}}
```

| 欄位 | 值 |
|---|---|
| `status` | `newer`、`newer_build`（會移動的標籤指向較新的映像檔）、`up_to_date`、`partly_checked`、`could_not_check`、`not_checked`（視覺映像檔） |
| `kind` | `release`（標籤帶版本號）、`rolling`（只有會移動的標籤：stable-diffusion.cpp）、`own` |
| `candidates` | 最新的在前，最多 8 個：比配方的 `version` 大的穩定版本，以各變體的標籤形式找到（內建標籤把版本號換掉；llama.cpp 的 `-b<build>-<commit>` 可有可無）。候選版本、每日建置和其他標籤都不會符合 |
| `variants[].why.code` | `no_image`、`tag_unversioned`、`registry_error`、`digest_failed`；行程變體：`installed_command`、`not_github`、`release_unknown`、`no_release`、`no_archive`、`no_sha256`、`github_error`；會移動的標籤：`not_moved`、`not_newer`、`other_build` |
| `mentions` | 版本說明提到的、配方的 `--參數`（兩個字以上的也比對 `snake_case` 寫法）和環境變數，每個最多三行；讀不到說明時改成 `notes`（`no_notes`、`github_error`） |
| `upstream` | `release_without_image`：GitHub 上有較新的穩定版本，但映像檔還沒發布 |

`GET /api/engine-updates` 也會回傳 `signatures`：每個內建配方的推論引擎專案提供什麼簽章（`{"signed": true, "kind": "github-attestation", "repository": "ggml-org/llama.cpp"}`，或 `{"signed": false, "text": …}`）。

#### 試用版本：推論引擎的複本

`POST /api/recipes/{name}/try {"version": "0.34.0", "name": "vllm-0-34-0"}`（管理員；`name` 可省略）會以內建配方 `{name}` 為基礎，為上次檢查找到的版本建立一個自訂配方，回傳 201，附上新配方、未包含的變體和簽章。它只接受版本，不接受映像檔：每個變體的標籤取自儲存的檢查結果，並再次比對內建變體自己的標籤形式；registry 和儲存庫一律取自內建變體的映像檔。每個包含的映像檔都會重新查詢 digest（檢查之後標籤移動了就回 409 `digest_moved`），用 digest 從 registry 讀取它的 ID，做法和 `scripts/pin_images.py` 為 lock 檔案做的一樣（`forgeyard/workloads/pinning.py`），專案有提供簽章時也會驗證。行程壓縮檔的 SHA-256 會再向 GitHub 讀一次。

| 回應 | 代碼 | 什麼時候 |
|---|---|---|
| 409 | `check_again` | 沒有那個版本的檢查結果、檢查之後內建配方變了，或儲存的結果無法使用 |
| 409 | `name_taken`、`builtin_name`、`not_builtin`、`try_running`、`digest_moved` | 代碼本身就說明了原因（`digest_moved` 會附上 `variant`） |
| 422 | `invalid_name`、`nothing_to_pin`、`signature_failed`、`signature_missing` | 簽章驗證不過，或是會簽章的專案沒有那個 digest 的建置證明 |
| 502 | `cannot_verify`、`registry_error`、`github_error` | 連不到 GitHub、Sigstore 的信任根或 registry，或（`cannot_verify`）這台伺服器沒有安裝 sigstore：什麼都不會建立。`cannot_verify` 會附上 `can_override` |

加上 `"allow_unverified": true` 時，即使簽章無法檢查也會建立複本（只限 `cannot_verify`，而且 `can_override` 為 true 時）：每個有簽章的映像檔和壓縮檔都會記下 `{"status": "not_verified", "reason", "overridden_by", "at"}`，主控台會標示為 *未驗證*，`recipe.created` 也會附上 `"unverified_override": true` 和那些變體（`not_verified`）。簽章驗證不過，或是會簽章的專案找不到簽章時，一律不能略過。

複本就是內建配方的 YAML 文字，加上開頭的註解，換掉 `name`、`title`（`vLLM 0.34.0`）、`version`、新的映像檔（`repo:tag@sha256:…`）和壓縮檔，刪掉未包含的變體，再加上兩個欄位（只用於自訂配方）：

| 欄位 | 意思 |
|---|---|
| `based_on` | 它來自哪個內建配方（`engine` 必須相同）。目錄模型為那個配方設定的 `engine_args` 也會套用到複本，除非模型有以複本名稱設定的項目 |
| `copy` | `from_version`、`kind`（`release` 或 `build`）、`made_at`、`made_by`、`left_out`（`variant`、`code`、`text`、`params`）、`archives`（`variant`、`release`、`signature`） |

會以 `recipe.created` 記進稽核紀錄，附上 `based_on`、`from_version`、`version`、`images`、`left_out` 和 `signatures`。複本的修改和刪除，和其他自訂配方一樣。

**`image_pins`**（資料庫遷移 `0016_image_pins`，只新增）：每個釘選的映像檔參照一筆（`ref` 是主鍵），指名同一個映像檔的複本共用：`entry`（`{"index", "platforms": {"linux/amd64": {"manifest", "config", "content"}}}`，和 `forgeyard/recipes/images.lock.json` 的格式相同）、`signature`、`recorded_by`、`recorded_at`。所有讀取記錄 ID 的地方都會先讀 lock 檔案，再讀這個資料表（`images.entry_for`）：經由伺服器轉送、隨副本或下載送出的 ID（給用 `docker load` 載入的映像檔用）、機器的推論引擎映像檔和移除、推論引擎頁面上的「已在機器上」。伺服器把這些資料列放在記憶體裡（啟動時載入，每次變更後重新載入）；配方修改或刪除時，沒有任何自訂配方再指名的資料列會被移除。降回 0015 會刪掉這個資料表：之後複本會直接以 digest 拉取映像檔。`GET /api/recipes/{name}` 會多回傳 `pins`：每個變體對應資料列的 `signature`、`recorded_at` 和 `recorded_by`。

**簽章。** 只在伺服器上驗證（agent 執行的是伺服器釘選的 digest），使用 [sigstore-python](https://github.com/sigstore/sigstore-python)（Apache-2.0）。它是伺服器專用的 extra（`uv sync --extra server`；伺服器映像檔已包含）：agent 的 wheel 和映像檔都不帶它，沒有安裝它的伺服器無法檢查簽章（`cannot_verify`）。

| 推論引擎 | 提供的內容 | 檢查的方式 |
|---|---|---|
| llama.cpp | 映像檔和發布壓縮檔的 GitHub 建置證明（Sigstore bundle 裡的 SLSA provenance） | 從 `GET https://api.github.com/repos/ggml-org/llama.cpp/attestations/sha256:<hex>`（匿名）取得 bundle：憑證鏈到 Fulcio，而且在 Rekor 記錄這筆資料時有效（inclusion proof 和 checkpoint）、DSSE 簽章、OIDC 簽發者是 `https://token.actions.githubusercontent.com`、來源儲存庫正確、簽章的是同一個儲存庫裡的 workflow 檔案、內容是 SLSA provenance v1，而且對象就是那個 digest |
| vLLM、vLLM-Omni、speaches | 沒有 | 記成 `{"status": "unsigned", "note": …}` |
| stable-diffusion.cpp | 沒有簽章的 BuildKit provenance | 記成未簽章 |

驗證通過的映像檔或壓縮檔會記下 `{"status": "verified", "repository", "workflow", "ref", "commit", "run", "predicate", "issuer", "log_index", "signed_at"}`。信任根（Fulcio 的憑證、Rekor 和 CT log 的公鑰）來自 Sigstore 的 TUF 儲存庫 `https://tuf-repo-cdn.sigstore.dev`，經由同樣的網路規則更新（保留 5 分鐘，metadata 存在 `DATA_DIR/sigstore/`）；第一次更新從 sigstore-python 內附的根開始。更新失敗就是 `cannot_verify`。只連得到自己 registry 的伺服器（使用鏡像站的隔離環境）無法驗證有簽章的推論引擎的新版本：管理員仍可以選擇照樣建立複本，它會一直標示為 *未驗證*。

### 透過 API 試跑

1.5.2（#95）。試跑會在一台機器上用一個推論引擎暫時部署一個目錄中的模型，大約幾分鐘，檢查完再移除（[操作指南](how-to.md#try-out-a-new-engine-version-before-switching)）。

| 請求 | 誰可以 | 內容 |
|---|---|---|
| `POST /api/recipes/{name}/trials {"model", "machine_id", "params"?, "timeout_minutes"?, "dry_run"?}` | 管理員、操作員 | 回傳 201 `{"trial": …}`。沒有給 `params` 時，沿用這個模型在內建推論引擎上的部署的參數（`params_from`），沒有的話用推論引擎的預設值；`gpus` 依機器上的變體決定。休眠、自動調整副本數和 `first_answer_timeout` 會被拿掉。`timeout_minutes` 是啟動的時限（預設 30，可以設 5 到 180）；量測和送出請求另外有 15 分鐘。`dry_run`：回傳 200，內容是 `estimate`、`placement`、`params`、`params_from`、`engine_on`（機器上有沒有它的映像檔）、`running` 和 `limit`，不會建立任何東西 |
| `GET /api/recipes/{name}/trials` | 所有登入的人 | 最近 50 次，最新的在前面，以及 `can_run`；配方刪除之後也還查得到 |
| `GET /api/engine-trials/{id}` | 所有登入的人 | 單一次試跑，包含 `events`（暫時部署最後 15 筆事件）、`log_tail`（副本最後的輸出）、`requests` 和 `memory`（每一次讀數） |
| `POST /api/engine-trials/{id}/cancel` | 管理員、操作員 | 移除它的部署；試跑結束後回傳 409 `not_running` |

拒絕的情況：404 `not_found`、`model_not_found`、`machine_not_found`；422 `incompatible`（推論引擎無法提供這個模型）、`invalid_params`；409 `machine_offline`、`does_not_fit`（附上 `machines` 和 `estimate`，和部署時一樣）、`trial_running`（每個推論引擎一個）、`too_many_trials`（一台伺服器四個）。試跑進行期間，`DELETE` 它的配方或模型會回傳 409 `trial_running`。

```json
{"trial": {"id": 7, "recipe": "vllm-0-34-0", "builtin": "vllm", "engine_version": "0.34.0",
  "model": {"id": 1, "name": "qwen3-0.6b", "task": "chat"}, "machine": {"id": 2, "name": "gpu-01"},
  "gpus": [{"key": "nvidia:GPU-…", "index": 4, "model": "NVIDIA A100 80GB PCIe"}],
  "params": {"context": 4096, "concurrency": 4, "gpus": 1, "share": "vram"}, "params_from": "chat",
  "estimate": 5948108800, "reserved": 5948108800, "state": "passed", "phase": null,
  "start": {"seconds": 95.2, "phases": {"image": 2.1, "engine": 93.1}, "source": "agent"},
  "measured": 5551245721, "p50_ms": 120, "p95_ms": 180, "first_answer": "A GPU is a processor …",
  "checks": [{"id": "memory", "label": "Memory", "value": 5551245721, "expected": "at most 5.54 GiB (reserved; estimate 5.54 GiB)",
              "ok": true, "note": "Approximate: …", "codes": [{"code": "approximate"}]}],
  "why": null, "summary": "Passed: ready in 95 s, 6/6 answers, p50 120 ms, +5.17 GiB of 5.54 GiB reserved.",
  "comparison": {"builtin": "vllm", "deployment": {"id": 3, "name": "chat", "reserved": 5948108800, "start_seconds": 112.0,
                                                    "same_machine": true}, "trial": null}}}
```

| 欄位 | 值 |
|---|---|
| `state` | `running`、`passed`（沒有檢查未通過）、`failed`、`cancelled` |
| `phase` | 進行中時：`starting`、`measuring`（就緒後讀取記憶體用量）、`asking`、`settling`（回應之後再讀一次） |
| `checks[].id` | `started`、`memory`、`answers`、`latency`、`same_answer`；`ok` 是 `true`、`false` 或 `null`（未檢查，說明會寫原因） |
| `checks[].codes` | 說明的內容代碼，供主控台翻譯：`slower`、`log`、`timeout_reason`、`not_run`、`approximate`、`approximate_ram`、`cards`、`after_answers`、`over`、`not_measured`、`went_down`、`first_answer`、`error`、`not_asked`（`why`：`video`、`none`）、`no_address`、`no_answer`、`same`、`different` |
| `why` | 未通過或提早結束的原因：`start_failed`、`unplaced`、`timeout`（`phase`、`minutes`）、`gone`、`error`、`restarted`、`stopped`、`not_ready`、`checks`（`checks`：未通過的檢查）、`cancelled`（`who`） |

記憶體檢查會取機器對副本那張卡（或 RAM）的讀數：推論引擎啟動前和就緒後，來得及的話各取三次（agent 會隨心跳送出讀數）。如果最大的差值（包含回應之後的那一次讀數）比副本預留的量多出 5% 以上，就算未通過。如果那張卡也有其他工作在使用（啟動前已經用了超過 512 MiB，或上面還有其他 Forgeyard 副本），量測結果只能當作參考。

**資料。** 資料表 `engine_trials`（migration `0018_engine_trials`，只有新增：降版時會刪除這個資料表，所以請先讓試跑結束或取消）：每次試跑一列，包含配方和它的內建推論引擎（`builtin`）、`engine`、`engine_version`、`model_id` 和 `model_name`、`task`、`node_id` 和 `machine_name`（模型和機器刪除後名稱仍會保留）、`gpus`、`params`、`estimate`、`reserved`、`deployment_id`（暫時部署存在期間）、`requested_by`、`created_at`、`ready_at`、`finished_at`、`deadline_at`、`state`、`phase`、`checks`、`comparison`、`measurements` 和 `summary`。暫時部署是一般的 `deployments` 資料列，只是沒有閘道路由，名稱是 `trial-<id>-<4 個十六進位字元>`，並固定在那台機器上；所有不該看到它的清單、背景迴圈和檢查，都會排除 `engine_trials` 指到的部署。伺服器啟動時如果還有 `running` 的試跑，會把它標示為 `failed`（`why.code` 是 `restarted`）並移除它的部署。稽核紀錄的動作是 `engine_trial.started`、`engine_trial.finished`（`state`、未通過的 `failed` 檢查）和 `engine_trial.cancelled`。

## 容器映像檔

每個版本都會在 Docker Hub 發布兩個映像檔，由 CI 從該版本的 tag 建置（還有視覺伺服器的 `docker.io/tonyhuang1101/forgeyard-vision`，`X.Y.Z` 與 `X.Y.Z-cuda`，由 `vision` 配方以 digest 固定）：

| 映像檔 | 內容 | 平台 |
|---|---|---|
| `docker.io/tonyhuang1101/forgeyard` | 伺服器（`Dockerfile.server`） | linux/amd64、linux/arm64 |
| `docker.io/tonyhuang1101/forgeyard-agent` | agent（`Dockerfile.agent`） | linux/amd64、linux/arm64（AMD 顯示卡只支援 amd64） |

- **tag**：`1.5.1`（就是那個版本）、`1.5`（它最新的修補版本）、`latest`（最新的正式版本）。預發布版本只會有它自己的版本號。每個映像檔都帶有 OCI 標籤（原始碼、授權、版本、revision）、建置來源證明（provenance）和 SBOM。
- **伺服器映像檔**以帳號 `forgeyard`（uid 10001）執行 `python main.py`，設定 `DATA_DIR=/data`（一個 volume）、`HOST=0.0.0.0` 和 `PORT=7860`（對外公開），並有針對 `/api/health` 的健康檢查。見[用 Docker 執行伺服器](how-to.md#run-the-server-in-docker)。
- **agent 映像檔**在 `tini` 底下執行 `forgeyard agent`，身分是 root（它要操作主機的 Docker），設定 `FORGEYARD_LIMITS=rlimit`、執行行程的帳號 `forgeyard-run`（uid 990），以及掛載在 `/var/lib/forgeyard-agent` 的 volume。見[用 Docker 執行 agent](how-to.md#run-the-agent-in-docker)。
- **伺服器指定哪個 agent 映像檔**：[`AGENT_IMAGE`](#machines)，否則就是伺服器版本的已發布映像檔。還沒有任何發布（`forgeyard/install/images.py` 裡的 namespace 是空的）而且沒有設定 `AGENT_IMAGE` 時，會指定本機建置的 `forgeyard-agent:<version>`，**加入機器** 會說明怎麼建置。

## 檔案與目錄

在伺服器上，位於它的資料夾裡（伺服器映像檔則是在 `/data` volume 裡，沒有 `data/` 這一層）：

| 路徑 | 內容 |
|---|---|
| `.env` | 設定 |
| `data/forgeyard.db` | 資料庫（SQLite） |
| `data/secret.key` | 加密儲存機密資料的金鑰（0600） |
| `data/tls/ca.crt`, `ca.key` | Forgeyard 的憑證授權單位（金鑰為 0600） |
| `data/tls/server.crt`, `server.key` | 伺服器憑證，會自動更新 |
| `data/secrets.json` | 一次性的設定權杖，設定完成前會一直存在 |

在用單行安裝裝好的機器上：

| 路徑 | 內容 |
|---|---|
| `/etc/forgeyard/agent.toml` | agent 的設定 |
| `/var/lib/forgeyard-agent/` | 憑據（`credential.json`，0600）、釘選的 CA（`server-ca.pem`）、副本的資料與紀錄 |
| `/var/lib/forgeyard-agent-work/` | 每個行程副本各一個工作目錄 |
| `/srv/forgeyard-models/` | 模型快取；（1.5.0）其中的 `engine-cache/` 存放保留編譯快取的配方的推論引擎編譯快取 |
| `/opt/forgeyard/` | agent 程式：它的 Python 環境和下載下來的 wheel |
| `/usr/local/bin/forgeyard` | `forgeyard` 指令 |
| `/etc/systemd/system/forgeyard-agent.service` | 服務（`KillMode=process`、`Restart=always`） |

## 連接埠

| 連接埠 | 位置 | 用途 |
|---|---|---|
| `7860` | 伺服器 | 網頁介面、API、`/v1`、agent 的 WebSocket、`/install.sh`（單一連接埠，HTTPS） |
| `30000–32767` | 機器 | 服務和模型副本公開的連接埠（agent 的 `port_range`）；伺服器必須連得到這些連接埠 |

agent 本身不需要任何對內的連接埠：它們會主動連到伺服器。
