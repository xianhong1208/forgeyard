# 參考資料

伺服器和 agent（代理程式）的每一個設定、命令列，以及各項資料存放的位置。

## 伺服器設定

伺服器從環境變數讀取設定，也可以從它啟動時所在資料夾裡的 `.env` 檔讀取（`.env.example` 列出了所有設定並附有註解，常用的排在最前面）。修改後請重新啟動伺服器。

伺服器啟動時會檢查它的設定：

- **無法使用的值會讓伺服器停止啟動**，並指出是哪個設定、可以填哪些值：`TLS`、`REGISTRATION`、`COOKIE_SECURE`、`GATEWAY_CLOUD_DEFAULT`、`GATEWAY_CLOUD_MAX`、不是數字的數字設定，以及 `TLS_CERT_FILE` 和 `TLS_KEY_FILE` 只設了其中一個。
- **其他出乎意料的情況都只記成警告**，並在[Setup health](#setup-health-checks)顯示給管理員：不是設定的名稱（附上「你是不是指」）、被另一個設定變得沒有作用的設定，以及有風險的組合。

### 常用設定

大多數安裝只會設定其中幾個。這張表之後的都是進階設定：預設值幾乎適合所有人。

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HOST` | `127.0.0.1` | 監聽的位址。`0.0.0.0` 代表對網路開放，其他機器要加入就需要這樣設定 |
| `PORT` | `7860` | 連接埠，網頁介面、API、`/v1` 和 agent 都用這個 |
| `PUBLIC_URL` | — | 使用者和 agent 使用的位址，例如 `https://192.168.1.20:7860`。加入指令、設定密碼的連結和閘道位址都會用到它；它的主機一律會被接受，也會包含在憑證裡。留空：使用每個瀏覽器開啟 Forgeyard 時的位址；如果那是 `127.0.0.1`，其他機器就無法使用 |
| `DATA_DIR` | `data` | 資料庫（SQLite）、`secret.key`、`tls/` 和設定權杖（setup token）的位置。相對路徑是相對於伺服器啟動時所在的資料夾 |
| `DATABASE_URL` | — | 留空：SQLite，放在 `DATA_DIR`。PostgreSQL：`postgresql+psycopg://user:password@host:5432/forgeyard`（先執行 `uv sync --extra postgres`） |
| `TLS` | `on` | `on`：以 Forgeyard 自己的 CA（建立在 `data/tls/`）或你的憑證提供 HTTPS。`off`：純 HTTP，只能用在處理 HTTPS 的代理後面 |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | 你自己的憑證和私鑰：兩個都設或都不設（只設一個會停止啟動）。每 12 小時檢查一次，有變更就重新載入。`TLS=off` 時會被忽略 |
| `TLS_CA_FILE` | — | 使用自己的憑證時：簽發它的 CA。agent 會釘選它，**Trust this server** 也會提供它。沒有自己的憑證時會被忽略 |
| `REGISTRATION` | `off` | `approval`：使用者可以申請帳號，申請會等待管理員核准 |
| `HF_ENDPOINT` | `https://huggingface.co` | 從哪裡查詢和下載 Hugging Face 模型（可以填鏡像站） |
| `EGRESS_PROXY` | — | 送往供應商的請求改經由這個 proxy（`http://host:port`）。絕不使用環境變數裡的 proxy |

### 網路與位址

| 變數 | 預設值 | 用途 |
|---|---|---|
| `ALLOWED_HOSTS` | — | 其他要回應的主機名稱，以逗號分隔。`localhost` 和 IP 位址一律可用；其他名稱會得到 421 |
| `TRUSTED_PROXIES` | — | 信任其 `X-Forwarded-For` 和 `X-Forwarded-Proto` 的代理，以逗號分隔的 IP |

### HTTPS

| 變數 | 預設值 | 用途 |
|---|---|---|
| `TLS_EXTRA_NAMES` | — | 要加進 Forgeyard 自己的伺服器憑證的其他名稱或 IP 位址，以逗號分隔。使用自己的憑證時會被忽略 |

`TLS`、`TLS_CERT_FILE`、`TLS_KEY_FILE` 和 `TLS_CA_FILE` 在[常用設定](#common-settings)裡。

### 資料與機密

| 變數 | 預設值 | 用途 |
|---|---|---|
| `SECRET_KEY_FILE` | `data/secret.key` | 加密儲存機密資料的金鑰。請和資料庫分開備份 |
| `FORGEYARD_SECRET_KEY` | — | 直接給金鑰本身（32 位元組的 base64），取代檔案。兩者只設一個：兩個都設的話，會使用這一個，並忽略檔案 |

### 帳號與登入

| 變數 | 預設值 | 用途 |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | 在第一次啟動時建立擁有者，取代設定頁面。只在那時讀取：之後請移除 |
| `FORGEYARD_SETUP_TOKEN` | — | 使用這個設定權杖，而不是隨機產生的（設定完成後就沒有作用） |
| `SESSION_IDLE_MINUTES` | `120` | 超過這段時間沒有任何請求就自動登出 |
| `SESSION_MAX_DAYS` | `7` | 無論如何，超過這段時間就自動登出 |
| `COOKIE_SECURE` | `auto` | `auto`：瀏覽器使用 HTTPS 時就使用 `Secure` cookie；設成 `true` 或 `false` 可強制指定。其他任何值都會停止啟動 |
| `RATE_LIMIT_PER_MINUTE` | `600` | 每個位址每分鐘對網頁介面的 API（`/api`）的請求數（0：不限制）。閘道 `/v1` 則改用每把金鑰的限制 |
| `LOGIN_FREE_FAILURES` | `5` | 每個帳號登入失敗幾次後，之後每次嘗試都要等待（2 秒起，每次加倍，最多 5 分鐘） |
| `LOGIN_IP_FAILURES` | `30` | 每個位址在 15 分鐘內登入失敗幾次後，該位址就要等待 |

### 機器

| 變數 | 預設值 | 用途 |
|---|---|---|
| `AGENT_HEARTBEAT_SECONDS` | `10` | agent 送出心跳的頻率 |
| `AGENT_OFFLINE_SECONDS` | `30` | 超過這段時間沒收到任何訊息的機器會顯示離線。請至少設為心跳間隔的 2.5 倍，否則機器會在上線和離線之間反覆跳動 |
| `AGENT_IMAGE` | — | **Add a machine** → **Docker** 指令裡的 agent 映像檔。留空：使用已發布的 `docker.io/tonyhuang1101/forgeyard-agent`，tag 是這台伺服器的版本；還沒有發布時則用本機建置的映像檔。可以設成鏡像站或私有 registry（`registry.example.org/forgeyard/forgeyard-agent`；tag 仍是伺服器的版本），或加上 `:TAG` 或 `@sha256:DIGEST` 指定特定版本。只接受單純的映像檔名稱 |

### 模型

| 變數 | 預設值 | 用途 |
|---|---|---|
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | 允許使用私有位址和純 http 的模型來源（區域網路內的鏡像站；agent 也需要設定 `allow_private_hosts`） |
| `MODEL_URL_HOSTS` | — | `https://` GGUF 來源允許的主機，以逗號分隔。只是初始值：管理員一旦在 **Models → Model sources** 儲存過，就以資料庫為準 |
| `MODEL_LOCAL_DIRS` | — | `local://` 模型允許的目錄，以逗號分隔。和 `MODEL_URL_HOSTS` 一樣只是初始值 |
| `GPU_CONTEXT_MIB` | `400` | 副本的 CUDA context 在它本身之外額外佔用的 GPU 記憶體 |
| `VRAM_HEADROOM_MIB` | `512` | 放置副本時，每張卡一律保留不用的記憶體 |

### 閘道

| 變數 | 預設值 | 用途 |
|---|---|---|
| `GATEWAY_CONNECT_TIMEOUT` | `5` | 連線到副本的秒數上限，超過就改試下一個 |
| `GATEWAY_READ_TIMEOUT` | `600` | 副本回傳回應標頭或串流下一段內容的秒數上限 |
| `GATEWAY_MAX_BODY_MB` | `32` | 請求本文的大小上限（用來轉錄的音訊檔是最大的）。部署的配方有 *Largest request* 參數（`max_body_mb`，vLLM 與 llama.cpp 預設 64）時，改以該值為上限 |
| `GATEWAY_DEFAULT_RPM`, `GATEWAY_DEFAULT_TPM` | `0` | 沒有自己限制的金鑰，每分鐘的請求數和 token 數（0：不限制）。管理員可以在 **API keys** 修改 |
| `GATEWAY_MAX_RPM`, `GATEWAY_MAX_TPM` | `0` | 任何人能為自己的金鑰設定的最大值（0：沒有上限） |
| `USAGE_FLUSH_SECONDS` | `5` | 把記憶體中統計的用量寫入資料庫的頻率 |

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
| `GATEWAY_CLOUD_DEFAULT` | `off` | 新 API 金鑰拿到的雲端設定：`off`、`fallback` 或 `on`。管理員可以在 **API keys** 修改（存在資料庫裡）；既有的金鑰維持原本的設定 |
| `GATEWAY_CLOUD_MAX` | `off` | 任何人能為自己的金鑰設定的最大值，每個請求也會套用到既有的金鑰。管理員可以為任何金鑰設定任何值 |
| `EGRESS_ALLOWED_HOSTS` | 空白 | 只允許這些供應商主機，以逗號分隔（`*.example.org` 代表子網域）。空白：不限 |
| `EGRESS_CA_FILE` | 空白 | 除了系統的 CA 之外，供應商連線額外信任的 CA 檔（例如會檢查 TLS 的公司 proxy） |
| `PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | 讓標示為在你網路外的供應商也能使用私有或 loopback 位址和純 http。只用於以替身伺服器測試；link-local 和 metadata 位址仍然一律拒絕 |
| `PROVIDER_CONNECT_TIMEOUT` | `10` | 連線到供應商的秒數上限，超過就改試下一個目標 |
| `PROVIDER_READ_TIMEOUT` | `300` | 供應商回傳回應標頭或串流下一段內容的秒數上限（每個供應商可以另外設定） |
| `PROVIDER_COOLDOWN_SECONDS` | `30` | 回應 429 或 5xx、或連不上的供應商目標，在這段時間內排到最後才嘗試（如果它的 `Retry-After` 更長就照它，最多 5 分鐘） |
| `PROVIDER_RETRY_AFTER_CAP` | `2` | 沒有其他目標可試時，不超過這個秒數的 `Retry-After` 會等一次再試 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | **Refresh prices from OpenRouter** 讀取價格的位置（只在管理員要求時讀取，不送出金鑰）。只有在用替身伺服器測試時才需要修改 |
| `BUDGET_DEFAULT_MAX_TOKENS` | `4096` | 預算會在雲端請求送出前先預留它的估計費用。沒有 `max_tokens` 的請求，以這個 token 數估計它的回應 |

`EGRESS_PROXY` 在[常用設定](#common-settings)裡。`GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、`GATEWAY_CLOUD_DEFAULT` 和 `GATEWAY_CLOUD_MAX` 也都只是初始值：管理員一旦在 **API keys → Limits for keys** 或 **Cloud for keys** 儲存過，就以資料庫為準；如果環境裡還留著另一個值，Setup health 會提醒。

### 單位、零與空白

| 種類 | 寫法 |
|---|---|
| 時間長度 | 單位是秒，除非名稱裡有 `MINUTES` 或 `DAYS`。路由的 *Wait for a free local slot* 單位是毫秒 |
| 大小 | `_MIB` 是 MiB。`GATEWAY_MAX_BODY_MB` 和 agent 的 `log_max_mb` 雖然名稱是 MB，單位也是 MiB。在表單裡輸入的大小可以寫 `8Gi`（GiB）；`8G` 和 `8GB` 是十進位（10⁹ 位元組） |
| 金額 | 網頁介面裡是美元；API 裡是微美元（`_micros`）；價格以每百萬 token 計 |
| 頻率 | 以每分鐘計；`LOGIN_IP_FAILURES` 是計算 15 分鐘內的次數 |

| 設定 | `0` 代表 | 空白代表 |
|---|---|---|
| `GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、**Limits for keys** | 不限制 | 不限制 |
| 金鑰自己的每分鐘請求數或 token 數 | 拒絕（至少為 1） | 伺服器的預設值 |
| 預算（金鑰、個人、供應商） | **$0：它適用的每個雲端請求都會被擋下** | 沒有預算 |
| `RATE_LIMIT_PER_MINUTE` | 關閉 | — |
| 路由的 *Wait for a free local slot* | 立刻溢出 | — |

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
| `[workloads]` `user` | `FORGEYARD_WORKLOAD_USER` | `--workload-user` | — | agent 以 root 執行時，行程類工作負載使用的無特權帳號（此時為必填）。 |
| `[workloads]` `work_dir` | `FORGEYARD_WORK_DIR` | `--work-dir` | — | 每個行程副本一個目錄。預設：放在狀態目錄旁邊（<state_dir>-work）。 |
| `[workloads]` `port_range` | `FORGEYARD_PORT_RANGE` | `--port-range` | `30000-32767` | 服務和模型副本使用的主機連接埠。 |
| `[workloads]` `bind_address` | `FORGEYARD_BIND_ADDRESS` | `--bind-address` | — | 公開的連接埠監聽的位址。預設：這台機器連往伺服器時使用的位址。 |
| `[workloads]` `container_engine` | `FORGEYARD_CONTAINER_ENGINE` | `--container-engine` | `auto` | auto（先試 docker，再試 podman）、docker、podman、none，或容器引擎 CLI 的路徑。 |
| `[workloads]` `limits` | `FORGEYARD_LIMITS` | `--limits` | `auto` | 行程類工作負載的資源限制方式：systemd（有 CPU 和記憶體上限的 scope；需要在有 systemd 的主機上以 root 執行）、rlimit（沒有 systemd，例如在容器裡），或 auto。 |
| `[workloads]` `log_max_mb` | `FORGEYARD_LOG_MAX_MB` | `--log-max-mb` | `10` | 每個副本保留的輸出量，單位 MB。 |
| `[models]` `cache` | `FORGEYARD_MODEL_CACHE` | `--model-cache` | — | 下載並保存模型和引擎的位置。沒有設定的話，這台機器不提供任何模型。在 Docker 映像檔中，容器內外必須是相同路徑。 |
| `[models]` `local_dirs` | `FORGEYARD_MODEL_LOCAL_DIRS` | `--model-local-dir` | — | 這台機器上 local:// 模型可以使用的目錄。環境變數格式：以逗號分隔。 |
| `[models]` `allow_private_hosts` | `FORGEYARD_ALLOW_PRIVATE_MODEL_HOSTS` | `--allow-private-model-hosts` | `false` | 允許從私有位址和純 http 下載模型（僅限區域網路內的鏡像站）。 |

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
| `DOCKER_HOST`、`DOCKER_CONFIG`、`CONTAINER_HOST`、`XDG_RUNTIME_DIR` | 傳給 `docker` 或 `podman` 指令，讓非預設的引擎 socket 或設定可以運作。agent 環境裡的其他東西都不會傳給引擎 |
| `ROCM_PATH`、`ROCM_HOME` | ROCm 不在 `/opt/rocm` 時的安裝位置（AMD 卡） |
| `FORGEYARD_AGENT_SIMULATION` | 設為 `1` 才允許 `--simulate-inventory FIXTURE`：agent 回報錄好的硬體資料，而不是自己的硬體。僅供開發和測試；伺服器會把這種機器標示為模擬 |

## 閘道（/v1）

程式以 API 金鑰呼叫 `https://<server>:7860/v1` 上的模型：`Authorization: Bearer fyk_…`。閘道不接受 cookie，也不送 CORS 標頭，所以網頁無法借用已登入的瀏覽器來呼叫它。

### 端點

| 端點 | 提供什麼 | 雲端供應商 |
|---|---|---|
| `GET /v1/models` | 這把金鑰可以使用的模型。以下模型不會列出：對這把金鑰不存在的模型（只有雲端，而金鑰的雲端設定不是 **Allowed**）、不在金鑰模型清單內的模型，以及部署全部停止的模型（除非還有這把金鑰可用的供應商能提供它） | — |
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

`/v1` 底下的其他路徑都回 404 `unknown_endpoint`。

閘道如何處理一個請求：

- 從請求內容讀取 `model`，把請求送到這個模型的路由。模型在另一個端點提供時（例如把嵌入模型送到 `/v1/chat/completions`），回 400 `wrong_endpoint`。
- 把 `model` 換成副本或供應商預期的名稱，加上副本的內部金鑰或供應商的金鑰，你的其他東西一概不轉送：不轉送你的 API 金鑰，也不轉送你的其他標頭。
- `max_tokens`（或 `max_completion_tokens`）在請求到達時計入金鑰的每分鐘 token 數，也決定預算下這個請求最多可能花多少錢。文字轉語音則改成 `input` 的每個字元算一個 token（見[文字轉語音](#text-to-speech)）；圖片請求不計任何 token（見[圖片生成](#images)）。
- 串流的對話或補全會多要一個最後的用量區塊（`stream_options.include_usage`），以便計算 token。如果你沒有要求，閘道會再把它拿掉。
- 請求內容最多 `GATEWAY_MAX_BODY_MB`（32 MB），或模型自己的 *Largest request*（`max_body_mb`，vLLM 與 llama.cpp 部署預設 64 MB）。
- 你關閉連線時，閘道會立刻關閉往上游的請求，引擎就會停止生成。

### 對話中的圖片、音訊與影片

`POST /v1/chat/completions` 接受 OpenAI 的內容區塊（content parts）。閘道接受什麼、轉送給引擎什麼：

| 區塊 | 接受的寫法 | 送給引擎的形式 |
|---|---|---|
| 文字 | `text`、`input_text`、`refusal` | 不變 |
| 圖片 | `image_url`（`{"url": …, "detail"}` 或單純字串）、`input_image` | 帶 `data:` URI 的 `image_url` |
| 音訊 | `input_audio`（`{"data": <base64>, "format"}`，或 `data:audio/…` URI）、`audio_url` | 帶 base64 `data` 的 `input_audio` |
| 影片 | `video_url`、`input_video` | 帶 `data:` URI 的 `video_url` |
| 檔案（PDF） | `file`，`file_data` 為 `data:` URI | 不變，只送給能接受檔案的配方（內建配方都不能） |

- 媒體網址可以是 `data:` URI（base64，且類型與區塊相符）或 `http(s)` 網址。**閘道會自己下載網址**，再以 `data:` URI 送給引擎：引擎從不自己抓任何東西。下載遵守 egress 規則：只連公開位址（開啟 `MEDIA_ALLOW_PRIVATE_HOSTS` 才允許私有位址；link-local 與 metadata 位址一律拒絕）、最多 3 次轉址且每次都重新檢查、每個項目最多 `MEDIA_FETCH_MAX_MB`、每個項目 20 秒，回應的類型也必須與區塊相符（圖片區塊要 `image/…`）。`file://` 與其他所有 scheme 回 400 `media_url_refused`。
- 請求只會送到能處理其內容的部署：模型接受這種媒體（從模型的 `config.json` 判斷，GGUF 則看它的 projector 檔），配方也接受，而且數量在部署的 *Images per request*（圖片與音訊片段，預設 4）與 *Videos per request*（預設 0：影片預設關閉）之內。否則回 400 `media_not_supported` 或 `too_many_media_items`。
- **1.3 中，帶媒體的請求絕不送往雲端供應商**（也不送往你網路內的供應商）。只由供應商提供的路由回 400 `media_local_only`；混合路由則不會溢出。
- 用量會計算媒體項目數（`GET /api/usage` 的 `media_items`）。圖片的 token 依引擎回報，包含在 `prompt_tokens` 中。

部署參數，位於部署表單的 **Advanced**（配方 `vllm` 與 `llama-cpp`）：

| 參數 | 預設值 | 用途 |
|---|---|---|
| `images`（*Images per request*） | `4` | 一個請求最多可帶的圖片與音訊片段數；0 表示關閉。以 `--limit-mm-per-prompt` 傳給 vLLM |
| `videos`（*Videos per request*） | `0` | 設為 1 以上才接受影片。在 vLLM 上，啟動時會為一段長影片預留記憶體 |
| `video_fps`（*Video frames per second*，僅 `llama-cpp`） | `1` | 影片每秒讀取幾格（`--video-fps`），用於沒有自己取樣率的模型；每一格大約和一張圖片一樣花 token。Qwen3-VL 不管這個值，固定每秒讀 2 格（llama.cpp b12242）：一段 6 秒的影片在 Qwen3-VL 2B 上用了 5,345 token。開啟影片的部署，上下文請設 8,192 以上 |
| `max_body_mb`（*Largest request*） | `64` | 閘道送給這個部署的最大請求，單位 MiB（超過回 413） |
| `image_tokens`（*Largest image*） | `1024` | 每張圖片最多縮成這麼多 token：Qwen3-VL 約 1024 × 1024 像素。`llama-cpp` 透過 `--image-max-tokens`；`vllm` 透過 `--mm-processor-kwargs` 的 `max_pixels`，適用於圖片 token 數會隨解析度增加的模型（Qwen-VL；Gemma 每張圖的 token 數固定）。不設的話，一張 5 MB 的照片在 llama.cpp 約 4,000 token（8 個 CPU 執行緒要 125 秒），在 vLLM 約 16,000 token，超過 8,192 token 的上下文。在這個設定出現之前加入的模型，要重新加入後 vLLM 才會套用上限 |

沒有這些參數的配方：4 張圖片或音訊片段、不接受影片、上限為 `GATEWAY_MAX_BODY_MB`。

### 文字轉語音

`POST /v1/audio/speech` 接受 OpenAI 的請求，回應的是音訊本身（不是 JSON），引擎一邊產生閘道就一邊轉送。三種引擎提供這項服務（配方 `vllm-omni-tts`、搭配 TTS GGUF 的 `llama-cpp`、`speaches-tts`）；只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 閘道怎麼處理 |
|---|---|
| `model` | 語音模型的名稱，和平常一樣 |
| `input` | 必填，最多為部署的 *Longest input*（`max_input_chars`；0 代表 vLLM-Omni 與 llama.cpp 取上下文長度的四分之一，預設即 1,024；speaches 為 4,096）。超過時回應 400 `input_too_long` |
| `voice` | 模型的聲音之一（`GET /v1/audio/voices`），名稱或 `{"id": …}` 皆可。模型沒有的 OpenAI 聲音名稱（`alloy`、`nova` 等），或沒有指定聲音時，改用配方的預設聲音或引擎列出的第一個；回應以 `X-Forgeyard-Voice` 標出（模型沒有內建聲音、只用它自己的一種聲音時為 `default`） |
| `response_format` | `mp3`、`opus`、`aac`、`flac`、`wav` 或 `pcm`，且必須是引擎能產生的；否則回應 400 `unsupported_response_format`。沒指定時用引擎的預設（vLLM-Omni 與 llama.cpp 為 `wav`，speaches 為 `mp3`），不是 OpenAI 的 `mp3` |
| `stream_format` | `audio`：一邊產生一邊傳原始音訊（串流的 WAV 會宣告未知長度）。`sse`：OpenAI 的 `speech.audio.delta` / `speech.audio.done` 事件（僅 vLLM-Omni）。沒指定時傳回整個檔案。引擎不支援時回應 400 `stream_format_not_supported`。`stream: true` 等同 `stream_format: audio` |
| `speed` | 0.25 到 4，原樣轉送（llama.cpp 只接受 1.0，speaches 的 Kokoro 為 0.5 到 2） |
| `instructions` | 原樣轉送：vLLM-Omni 的 Qwen3-TTS 會用它（風格與情緒），其他引擎忽略 |
| `language`、`lang`、`sample_rate`、`seed` 等 | 原樣轉送。對 llama.cpp，若請求沒給，閘道會依文字的書寫系統設定 `lang`（`zh`、`ja`、`ko`），並設定 `n_predict`（每個字元十個音訊幀，至少 512，最多到上下文長度） |

**1.3 不提供聲音複製。** 參考錄音（`ref_audio`、`ref_text`、`speaker_ref_b64`、`speaker_embedding` 等）、以音訊資料或連結當作聲音、Qwen3-TTS 的 `task_type: "Base"`，以及上傳或刪除聲音，都回應 400 `voice_cloning_not_supported`，任何內容都不會送到引擎。

每個有回應的請求會計入（`GET /api/usage` 與 **Usage** 頁面）：

| 單位 | 怎麼算 |
|---|---|
| `characters` | 輸入的字元數（Unicode 碼位），送出前就算好 |
| `media_ms` | 音訊長度（毫秒），在轉送時讀出：`wav` 看檔頭（串流時算檔頭之後的位元組），`pcm` 依大小與回應標明的取樣率（`x-audio-sample-rate`，或類型裡的 `rate=`；否則用該聲音自己的取樣率），`flac` 看 STREAMINFO，`mp3` 與 `aac` 數音框，`opus` 看最後一個 Ogg 頁；SSE 依 `pcm` 片段的大小。其他回應不計 |
| tokens | 只計引擎回報的：vLLM-Omni 的 `x-vllm-omni-input-tokens` 與 `-output-tokens`，或 `speech.audio.done` 的 usage |

**限制：** rpm 和所有請求一樣；**tpm 以 `input` 的每個字元算一個 token**，在請求到達時先預留（15 個字元的請求需要 15 個 token 的額度）。預算只管雲端花費，而語音從不送往雲端。金鑰的模型清單同樣適用於語音和聲音清單。

`GET /v1/audio/voices?model=<model>` 回應
`{"object": "list", "model", "voices": [{"id", "name", "language"?, "gender"?}], "default", "formats",
"default_format", "stream_formats", "max_input_chars"}`，向一個就緒的副本詢問，保留 5 分鐘。上傳到引擎的聲音一律不列出。

### 圖片生成

`POST /v1/images/generations` 接受 OpenAI 的請求，回應 OpenAI 的 JSON，圖片以 base64 傳回。兩種引擎提供這項服務（配方 `vllm-omni-image` 在 NVIDIA 上跑 diffusers pipeline，`sd-cpp` 在 AMD（Vulkan）和 CPU 上跑 stable-diffusion.cpp，不支援 NVIDIA：#43）；只由本機部署提供，絕不送往雲端供應商。

| 欄位 | 閘道怎麼處理 |
|---|---|
| `model` | 圖片模型的名稱，和平常一樣 |
| `prompt` | 必填，最多為部署的 *Longest prompt*（`max_prompt_chars`，4,000）。超過時回應 400 `prompt_too_long`。提示裡的引擎設定（`<sd_cpp_extra_args>`）會被拒絕：請改用下面的欄位 |
| `n` | 1 到部署的 *Images per request*（`max_images`，預設 4）；超過時回應 400 `too_many_images`。沒指定時為 1 |
| `size` | `WIDTHxHEIGHT`；`auto` 或沒指定時用部署的 *Default size*（模型自己的：Stable Diffusion 1.5 為 512x512，SDXL、Z-Image 與 FLUX 為 1024x1024）。每一邊最多 *Largest side*（`max_side`，0 = 模型自己的）；超過時回應 400 `image_too_large`。每一邊必須是 16 的倍數（diffusion transformer）或 64 的倍數（Stable Diffusion 的 UNet）；否則回應 400 `invalid_size` |
| `response_format` | 不指定或 `b64_json`。**`url` 會被拒絕**，回應 400 `response_format_not_supported`：Forgeyard 不保留它產生的圖片，所以沒有網址可給。請解碼 `data[].b64_json` |
| `output_format` | `png`（預設）、`jpeg` 或 `webp`；其他回應 400 `unsupported_output_format`。jpeg 與 webp 可用 `output_compression`（0 到 100） |
| `num_inference_steps` | 去噪步數，最多為部署的 *Most steps*（`max_steps`，50）；超過時回應 400 `too_many_steps`。沒指定時用部署的 *Default steps*，否則用模型自己的 |
| `negative_prompt`、`guidance_scale`、`seed` | 轉送：對 vLLM-Omni 以欄位轉送，對 stable-diffusion.cpp 則放進提示裡的 `<sd_cpp_extra_args>`（由閘道產生）。沒有 `seed` 時，每個請求都是一張新圖片 |
| `true_cfg_scale` | 只有 vLLM-Omni（Qwen-Image）使用；stable-diffusion.cpp 忽略它 |
| `quality`、`style`、`background`、`moderation`、`user` | 接受但忽略 |
| `stream` | 拒絕（400 `streaming_not_supported`）：圖片做好才會回應 |

其他東西都不會送到引擎：引擎會接受、但 Forgeyard 不提供的欄位（vLLM-Omni 的 `lora`，它帶的是伺服器上的路徑；它的 `extra_params` 與系統提示）都會被拿掉。

回應：`{"created", "data": [{"b64_json"}], "output_format", "size"}`。引擎自己附加的內容（stable-diffusion.cpp 帶著提示與設定的 `generation_info`、vLLM-Omni 的各階段耗時）不會轉送。引擎的錯誤會以 OpenAI 的格式傳回，帶著它的狀態碼和 `engine_error` 這個 code。

**佇列。** 部署的每個副本同時處理 *Requests at once*（`concurrency`，1）個請求；其他請求在閘道裡等待，先到先處理，最多 *Queue length*（`queue`，8）個。再多一個就回應 429 `queue_full` 並附上 `Retry-After`（依它最近的請求花了多久計算）。等待超過部署 *Timeout* 的請求回應 503 `queue_timeout`；副本接手之後，閘道最多等待這個 *Timeout*（`timeout`：vLLM-Omni 為 600 秒，stable-diffusion.cpp 為 1,800 秒）取得圖片，之後回應 504 `upstream_timeout`。在等待中放棄的用戶端會離開佇列；它的請求絕不會送到引擎。

**不過濾提示。** 閘道只檢查請求的格式與部署的上限，不檢查提示要求畫什麼。如果部署需要安全過濾，請在它前面加一層。

每個有回應的請求會計入（`GET /api/usage` 與 **Usage** 頁面）：

| 單位 | 怎麼算 |
|---|---|
| `images` | 回應裡的圖片數 |
| `kilopixels` | 寬 x 高 / 1,000，從每張圖片自己的檔頭讀出（PNG、JPEG、WebP），否則用請求的尺寸 |
| `busy_ms` | 副本處理這個請求的時間（從送出到收到回應的最後一個位元組），乘以部署占用的 GPU 數（CPU 上為 1）：也就是 GPU 時間 |
| tokens | 不計。金鑰的每分鐘 token 數不適用；每分鐘請求數照樣適用 |

### 標頭

| 標頭 | 方向 | 值 |
|---|---|---|
| `Authorization` | 請求 | `Bearer fyk_…`：你的 API 金鑰 |
| `X-Forgeyard-Local-Only` | 請求 | `1`（或 `true`、`yes`、`on`）：這個請求不使用任何雲端供應商。它絕不會放寬金鑰的權限 |
| `X-Forgeyard-Target` | 回應 | 由誰回答：`local:<deployment>#<replica>` 或 `provider:<provider>/<model>`。絕不包含金鑰 |
| `X-Forgeyard-Replica` | 回應 | 本機回答時：`<model>#<replica>` |
| `X-Forgeyard-Spilled` | 回應 | 請求離開第一層的原因：`unavailable`、`busy`、`refused`、`too_long` 或 `budget`。第一層回答時沒有這個標頭 |
| `X-Forgeyard-Voice` | 回應 | 文字轉語音：請求沒指定聲音、或指定了模型沒有的 OpenAI 聲音時，閘道所選的聲音；模型只有自己的一種聲音時為 `default` |
| `X-Forgeyard-Budget-Exhausted` | 回應 | 讓請求無法送往雲端供應商的預算，以逗號分隔：`key`、`person` 或 `provider:<name>`。由其他目標成功回答時也會帶上 |
| `Retry-After` | 回應 | 要等待的秒數，出現在 429 `rate_limit_exceeded`、供應商有提供時的 429 `provider_rate_limited`，以及原樣轉回的副本或供應商自己的 429 或 503 |

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
| 400 | `context_length_exceeded` | 只出現在開啟 **Spill a prompt that is too long** 的路由：提示放不進本機的上下文，而且沒有其他目標能接手 | 縮短提示或降低 `max_tokens` |
| 400 | `invalid_content_part` | 內容區塊不是帶 `type` 的物件、閘道不接受的類型（`*_embeds`、未知類型），或媒體區塊沒有網址 | 使用[對話中的圖片、音訊與影片](#images-audio-and-video-in-chat)列出的區塊 |
| 400 | `invalid_media_data` | `data:` URI 不是 base64，或類型與區塊不符（圖片區塊帶 `data:text/plain`），或 `file` 區塊沒有 `data:` URI | 送出 `data:<type>;base64,…` |
| 400 | `media_url_refused` | 媒體網址的 scheme 不是 http(s) 或 `data:`（例如 `file://`）、帶有帳密，或主機解析到私有、loopback、link-local 或 metadata 位址（轉址後也一樣）；`input_audio.data` 裡放網址 | 送公開網址或 `data:` URI |
| 400 | `media_fetch_failed` | 閘道無法下載媒體網址：沒有回應、狀態碼不是 200、超過 3 次轉址、超過 20 秒，或類型與區塊不符 | 檢查網址，或改以 `data:` URI 送出檔案 |
| 400 | `media_not_supported` | 模型不接受這種媒體（把圖片送給純文字模型、*Videos per request* 為 0 時送影片）；訊息會說它接受什麼 | 改用能處理的模型，或在部署上開啟影片 |
| 400 | `too_many_media_items` | 圖片（或音訊片段、影片）多於部署的 *Images per request* 或 *Videos per request* | 少送一些，或調高參數 |
| 400 | `file_input_not_supported` | 把 `file` 區塊（PDF）送給配方不接受檔案的模型；閘道不會轉換檔案 | 改送頁面圖片或文字 |
| 400 | `media_local_only` | 把帶媒體的請求送給只由供應商提供的模型 | 改用本機模型 |
| 400 | `input_too_long` | 文字轉語音：`input` 超過部署的 *Longest input* | 把文字拆開，或調高 `max_input_chars` |
| 400 | `unsupported_response_format` | 文字轉語音：`response_format` 不是 OpenAI 的格式，或引擎不產生這種格式；訊息會列出它能產生的格式 | 改用其中一種 |
| 400 | `stream_format_not_supported` | 文字轉語音：引擎不提供這種 `stream_format`（llama.cpp 或 speaches 的 `sse`） | 拿掉它，或用 `audio` |
| 400 | `language_not_supported` | 文字轉語音：把中文、日文或韓文送給 speaches，它會把這些字刪掉 | 改用 Qwen3-TTS 部署 |
| 400 | `voice_cloning_not_supported` | 文字轉語音：帶了參考錄音、以音訊或連結當聲音、`task_type: "Base"`，或上傳聲音。1.3 不提供 | 使用模型自己的聲音 |
| 400 | `response_format_not_supported` | 圖片：`response_format: url`。圖片只以 base64 傳回 | 拿掉它，或要求 `b64_json` |
| 400 | `streaming_not_supported` | 圖片：`stream: true` | 拿掉它 |
| 400 | `too_many_images` | 圖片：`n` 超過部署的 *Images per request* | 少要幾張，或調高 `max_images` |
| 400 | `image_too_large` | 圖片：某一邊超過部署的 *Largest side* | 要小一點的尺寸，或調高 `max_side` |
| 400 | `invalid_size` | 圖片：`size` 不是 `WIDTHxHEIGHT`（每邊 64 到 8,192），或某一邊不是模型接受的倍數（16 或 64；訊息會說明） | 使用類似訊息建議的尺寸 |
| 400 | `unsupported_output_format` | 圖片：`output_format` 不是 png、jpeg 或 webp，或是引擎不產生的格式 | 改要 png |
| 400 | `too_many_steps` | 圖片：`num_inference_steps` 超過部署的 *Most steps* | 少一點步數，或調高 `max_steps` |
| 400 | `prompt_too_long` | 圖片：`prompt` 或 `negative_prompt` 超過部署的 *Longest prompt* | 縮短它，或調高 `max_prompt_chars` |
| 4xx/5xx | `engine_error` | 圖片：引擎拒絕了請求或處理失敗；會轉送它的狀態碼和訊息 | 看訊息；記憶體不足的錯誤代表要用小一點的尺寸或少幾張圖片 |
| 400 | `cloud_only_model` | 只有雲端供應商提供這個模型，但請求帶了 `X-Forgeyard-Local-Only`，或路由設為 **Local only** | 拿掉標頭，或改用本機模型 |
| 401 | `missing_api_key` | 沒有 `Authorization: Bearer` 標頭 | 送出金鑰 |
| 401 | `invalid_api_key` | 不是 Forgeyard 金鑰，或沒有這把金鑰 | 檢查金鑰 |
| 401 | `revoked_api_key` | 金鑰已被撤銷 | 建立新金鑰 |
| 401 | `expired_api_key` | 金鑰已過期 | 建立新金鑰 |
| 401 | `account_disabled` | 金鑰的擁有者已停用或正在等待核准 | 詢問管理員 |
| 402 | `budget_exhausted` | 每月預算讓請求無法送往任何能提供服務的供應商，本機也無法接手。`budget` 物件會說明是哪個預算、何時重置 | 等到月底、提高預算，或改用本機模型。不要重試 |
| 402 | `budget_unpriced` | 有預算適用，但供應商的模型沒有價格，所以無法預留它的費用 | 由管理員設定它的價格 |
| 403 | `model_not_allowed` | 金鑰只能使用其他模型 | 改用允許的模型，或修改金鑰的模型清單 |
| 404 | `model_not_found` | 對這把金鑰沒有這個模型：它不存在，或它只在雲端而金鑰的雲端設定不是 **Allowed**。在 `GET /v1/models/{model}` 也包括已停止的模型 | 查看 `GET /v1/models` |
| 404 | `unknown_endpoint` | 閘道沒有這個路徑 | 使用上面列出的端點 |
| 404 | `endpoint_not_supported` | `/v1/images/edits` 或 `/v1/images/variations`：1.3 不提供 | 改用 `/v1/images/generations` |
| 413 | `request_too_large` | 請求內容（媒體網址下載並內嵌後）大於模型的 *Largest request* 或 `GATEWAY_MAX_BODY_MB` | 送少一點或縮小圖片，或調高參數或設定 |
| 413 | `media_too_large` | 媒體網址的檔案大於 `MEDIA_FETCH_MAX_MB` | 送小一點的檔案，或調高設定 |
| 421 | `host_not_allowed` | 伺服器不回應網址裡的主機名稱 | 使用它的位址，或把名稱加進 `ALLOWED_HOSTS` |
| 429 | `rate_limit_exceeded` | 超過金鑰每分鐘的請求數或 token 數 | 等待 `Retry-After` 秒 |
| 429 | `provider_rate_limited` | 每個能提供服務的雲端目標都達到速率限制 | 有 `Retry-After` 就等待那麼多秒 |
| 429 | `queue_full` | 圖片：每個副本都在忙，部署的佇列也滿了 | 等待 `Retry-After` 秒 |
| 502 | `upstream_unreachable` | 連不上任何副本 | 通常是 agent 或引擎重新啟動了，會自己恢復。檢查部署 |
| 502 | `upstream_error` | 回應傳送到一半時，和副本或供應商的連線斷了 | 重試 |
| 502 | `replica_refused_key` | 副本拒絕了閘道的內部金鑰 | 停止再啟動部署 |
| 502 | `provider_auth_failed` | 供應商拒絕了 Forgeyard 給它的金鑰。絕不會以 401 轉給你，免得你懷疑自己的金鑰 | 由管理員修正供應商的金鑰 |
| 502 | `provider_blocked` | 外連規則在送出前就拒絕了供應商的位址 | 由管理員檢查供應商的 base URL 和 `EGRESS_*` |
| 502 | `provider_redirect` | 供應商回應了重新導向，而重新導向一律不跟隨 | 由管理員檢查供應商的 base URL |
| 502 | `provider_unreachable` | 連不上供應商 | 稍後重試 |
| 502 | `provider_error` | 供應商回應 5xx，或送往它的請求失敗 | 稍後重試 |
| 503 | `no_ready_replica` | 模型存在，但沒有就緒的副本：下載中、啟動中、已停止，或它的機器離線。訊息會列出每個副本的狀態 | 等待，或啟動部署 |
| 503 | `local_busy` | 每個副本都達到它的 *Concurrent requests*，也沒有其他目標能接手。閘道接著還是會把它送到本機副本，由引擎排隊處理，所以通常不會看到它 | 重試 |
| 503 | `no_target` | 路由的每個目標都停用了，或沒有目標提供這個端點 | 由管理員檢查路由 |
| 503 | `budget_unavailable` | 無法讀取本月的花費，所以在預算下不送出任何雲端請求 | 稍後重試 |
| 503 | `queue_timeout` | 圖片：請求等待空出副本的時間超過部署的 *Timeout* | 稍後重試，或加一個副本 |
| 504 | `upstream_timeout` | 副本在 `GATEWAY_READ_TIMEOUT` 內沒有回應（或沒有送出串流的下一段）；圖片則是在部署的 *Timeout* 內 | 降低 `max_tokens`，或調高設定；圖片則用小一點的尺寸或少一點步數 |
| 504 | `provider_timeout` | 供應商沒有及時回應（`PROVIDER_READ_TIMEOUT`，或供應商自己的逾時設定） | 稍後重試 |

另外兩種情況：

- **引擎或供應商本身的錯誤**會以它自己的狀態碼轉給你。副本的回應原封不動轉送（例如提示太長時 vLLM 回的 400）。供應商的其他 4xx 保留它的狀態碼和 `code`，訊息前面加上供應商的名稱，看起來像金鑰的內容會被移除。沒有其他副本或層級能接手時，副本的 429 或 503 會轉給你。
- **串流開始後才中斷**時，狀態碼已經無法更改。閘道會送出最後一個事件結束它，也就是錯誤碼為 `upstream_stream_broken` 的錯誤：`data: {"error": {…, "code": "upstream_stream_broken"}}`。

## 工作負載的環境變數

工作負載的副本從空的環境開始，只拿到它的規格宣告的變數，再加上：

| 變數 | 值 |
|---|---|
| `FORGEYARD_WORKLOAD` | 工作負載的名稱 |
| `FORGEYARD_REPLICA` | 副本的 id |
| `FORGEYARD_REPLICA_INDEX` | 副本的編號，從 0 開始 |
| `PORT`、`PORT_<NAME>` | 每個宣告的連接埠：行程拿到它必須監聽的主機連接埠；容器拿到它自己的連接埠（引擎會把主機連接埠對應過去）。`PORT` 是第一個 |
| `CUDA_VISIBLE_DEVICES`、`NVIDIA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER` | NVIDIA：只有它自己的卡的 UUID。沒有 GPU 的副本看不到任何卡 |
| `ROCR_VISIBLE_DEVICES`、`HIP_VISIBLE_DEVICES` | AMD：只有它自己的卡 |
| `PATH`、`HOME`、`LANG`、`USER`、`LOGNAME` | 只有行程：基本的 `PATH`、以它自己的目錄當 `HOME`、`LANG=C.UTF-8`，以及工作負載帳號 |

規格裡設定 GPU 變數（也包括 `HIP_VISIBLE_DEVICES` 和 `GPU_DEVICE_ORDINAL`）、`PORT`，或任何以 `FORGEYARD_` 或 `PORT_` 開頭的名稱，都會被拒絕。在行程的參數和環境變數的值裡，`${PORT}` 和 `${PORT_<NAME>}` 會換成連接埠。

## Setup health 檢查

管理員會在 **Get started → Setup health** 看到這些檢查結果，頂端列會顯示錯誤和警告的數量，相關的頁面也會以橫幅顯示。一般使用者和 operator 都看不到。每項檢查都在頁面要求時，根據設定、資料庫和各 agent 最後一次的回報執行。*錯誤*會一直顯示到修好為止；*警告*或*提示*（API 裡是 `info`）管理員可以延後 30 天，只對自己隱藏。使用方式：[檢查設定健康狀態](how-to.md#check-the-setup-health)。

| Id | 嚴重程度 | 何時出現 |
|---|---|---|
| `setting-unknown` | 警告 | `.env` 裡的某個名稱，或環境裡開頭像設定（`GATEWAY_`、`PROVIDER_`、`EGRESS_`…）或和某個設定非常接近的名稱，並不是設定。它會說出你可能想設的是哪一個 |
| `tls-setting-ignored` | 警告 | `TLS=off` 時設了 `TLS_*`；沒有自己的憑證卻設了 `TLS_CA_FILE`；使用自己的憑證時設了 `TLS_EXTRA_NAMES` |
| `secret-key-both` | 警告 | `FORGEYARD_SECRET_KEY` 和 `SECRET_KEY_FILE` 都有設定 |
| `heartbeat-flapping` | 警告 | `AGENT_OFFLINE_SECONDS` 小於 2.5 × `AGENT_HEARTBEAT_SECONDS` |
| `private-hosts-allowed` | 警告 | `MODEL_ALLOW_PRIVATE_HOSTS`、`PROVIDER_ALLOW_PRIVATE_HOSTS` 或 `MEDIA_ALLOW_PRIVATE_HOSTS` 已開啟 |
| `cookie-not-secure` | 警告 | 伺服器使用 HTTPS，但設定了 `COOKIE_SECURE=false` |
| `admin-password-left` | 警告 | 擁有者已經存在，`ADMIN_PASSWORD` 卻還設定著 |
| `setup-token-left` | 提示 | 設定完成後，`FORGEYARD_SETUP_TOKEN` 還設定著 |
| `env-seed-ignored` | 提示 | 某個初始值設定（`MODEL_URL_HOSTS`、`MODEL_LOCAL_DIRS`、`GATEWAY_DEFAULT_*`、`GATEWAY_MAX_*`、`GATEWAY_CLOUD_*`）和管理員儲存的值不同，而儲存的值優先 |
| `secret-key-backup` | 警告 | 還沒有管理員確認已備份目前的機密金鑰。勾選 **I have a backup of key id …** 就會消失；金鑰更換後會再出現 |
| `host-loopback` | 錯誤 | `HOST` 是 loopback 位址，卻有機器從其他位址加入（而且前面沒有代理） |
| `public-url-unset` | 提示 | 其他機器連得到這台伺服器，`PUBLIC_URL` 卻是空白。如果你是以 `127.0.0.1` 或 `localhost` 開啟頁面，則是警告 |
| `certificate-name-missing` | 錯誤 | 你自己的憑證沒有包含 `PUBLIC_URL` 的主機 |
| `certificate-expiring` | 警告 | 你自己的憑證在 30 天內到期，或 Forgeyard 的 CA 在 90 天內到期。14 天內到期則是錯誤 |
| `registrations-pending` | 提示 | 有人在等待核准 |
| `agent-outdated` | 警告 | 某台機器的 agent 比伺服器舊。如果它的協定比較舊，則是錯誤 |
| `gpus-shared-no-allowlist` | 警告 | 某台機器的顯示卡佔用的記憶體比 Forgeyard 在那裡保留的多出至少 1 GiB，而且沒有人選擇它的 GPU 存取 |
| `machine-no-models` | 提示 | 某個上線的 agent 沒有模型快取，所以不會有模型放到那裡。如果它有 GPU，則是警告 |
| `process-workloads-off` | 提示 | 某個上線的 agent 拒絕行程類工作負載；檢查結果會說明原因 |
| `model-cache-full` | 警告 | 某台機器的模型快取磁碟剩餘空間不到 10% |
| `machine-simulated` | 警告 | 某台機器回報的是模擬的硬體 |
| `machine-offline-with-replicas` | 警告 | 某台機器離線超過 5 分鐘，上面還有放置的副本 |
| `replica-margin-low` | 警告 | 某張有副本在執行的顯示卡，剩餘空間少於 `VRAM_HEADROOM_MIB`（至少 256 MiB） |
| `deployment-waiting` | 警告 | 某個副本等待機器已超過 15 分鐘 |
| `extra-args-override` | 警告 | 部署的額外引擎參數重複了 Forgeyard 會設定的旗標（記憶體、上下文、位址、金鑰） |
| `key-cloud-without-budget` | 警告 | 某把金鑰可以使用雲端（也包括透過伺服器的預設值），而且會用到某個供應商，卻沒有任何預算適用：金鑰、它的所屬個人和供應商都沒有預算 |
| `cloud-model-unpriced` | 錯誤 | 已經有預算存在時，某條路由的雲端模型沒有輸入或輸出價格：它的請求會回應 402 `budget_unpriced` |
| `cloud-model-cost-unknown` | 提示 | 同樣的情況但沒有預算：它的費用未知 |
| `provider-no-key` | 錯誤 | 某個已啟用、而且種類需要 API 金鑰的供應商沒有金鑰 |
| `provider-blocked-by-egress` | 錯誤 | 某個供應商的主機不在 `EGRESS_ALLOWED_HOSTS` 裡 |
| `provider-inside-public-address` | 警告 | 某個標示為在你網路內的供應商，只解析得到公開位址 |
| `cloud-default-open` | 提示 | 新金鑰拿到的設定高於 **Never**，或使用者可以把自己的金鑰設為 **Allowed**。如果某把金鑰同時沒有預算，則是警告 |
| `cloud-unreachable` | 提示 | 路由有雲端目標，卻沒有任何金鑰可以使用雲端 |
| `route-cloud-before-local` | 警告 | 某條路由的雲端層排在它的第一個本機層之前 |
| `route-cloud-never-used` | 提示 | 勾選了 **Local only** 的路由有雲端目標，或某條溢出規則在本機層之後沒有其他層 |
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
| `python -m forgeyard --version` | 顯示版本 |

### agent

用單行安裝裝好後，agent 的指令是 `forgeyard`（在 `/usr/local/bin`）；從原始碼目錄執行則是 `uv run --no-sync python -m forgeyard`。每個指令都接受 [agent 設定](#agent-settings)的旗標，放在指令名稱之前或之後都可以，`-v`（`--verbose`）則顯示除錯輸出。`--fingerprint` 是 `--ca-fingerprint` 的另一個名稱。在 agent 的 Docker 映像檔裡，指令本身已經是 `forgeyard agent`，所以只要加上後面的部分（`… forgeyard-agent:<version> inventory`）。

| 指令 | 用途 |
|---|---|
| `forgeyard agent` | 執行 agent（服務執行的是 `forgeyard agent --config /etc/forgeyard/agent.toml`）。如果有加入權杖但還沒有憑據，會先加入；`--rejoin` 會以新機器的身分重新加入 |
| `forgeyard agent init [--print] [--force]` | 根據偵測到的內容（GPU、Docker 或 Podman、systemd、工作負載帳號）寫出 `/etc/forgeyard/agent.toml`，並印出內容。`--print` 只印出不寫入。在這裡給的旗標（`--server`、`--ca-fingerprint`、`--model-cache`…）會寫進檔案 |
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
| `--workload-user USER` | 行程類工作負載使用的帳號（預設 `forgeyard-run`，不存在時會自動建立） |
| `--no-start` | 安裝並加入，但不啟動服務 |
| `--rejoin` | 以新機器的身分重新加入 |
| `--uninstall [--purge]` | 移除服務和程式；`--purge` 會連設定和憑據一起刪除 |
| `-h`、`--help` | 列出選項 |

再執行一次時，它會把 agent 升級到伺服器的版本，並保留設定和憑據。它需要 x86_64 或 aarch64 的 Linux、root 權限、`curl`，以及能連上 PyPI 以下載 agent 的少數幾個相依套件。它會優先使用已安裝的 `uv`，否則使用系統上已有的 Python 3.13 或更新版本，再不然就把 `uv` 安裝到 `/opt/forgeyard/bin`，它會自備 Python。沒有 systemd 的話，它會安裝好所有東西，並印出啟動 agent 的方法。

## 內建配方

配方的 variant 依序嘗試；第一個 `when` 符合某張卡（或 CPU）、而且機器上有它的執行環境的 variant，就用來執行副本。部署不能自己選 variant；只放你要的 variant 的自訂配方可以。

| 配方 | Variant | 執行在 | 映像檔或 release |
|---|---|---|---|
| `vllm`、`vllm-pooling` | `cuda`、`cuda-12.9`、`rocm` | NVIDIA（compute capability 7.0 以上，driver 580 以上；`cuda-12.9` 為 575 以上）；AMD `gfx90a`、`gfx942`、`gfx950`、`gfx1100`、`gfx1101`、`gfx1150`、`gfx1151`、`gfx1200`、`gfx1201` | `vllm/vllm-openai:v0.32.0`、`-cu129`、`vllm/vllm-openai-rocm:v0.32.0` |
| `llama-cpp` | `cuda` | NVIDIA，compute capability 5.0 以上，driver 570 以上 | `ghcr.io/ggml-org/llama.cpp:server-cuda-b12242`（CUDA 12.8） |
| | `rocm` | AMD `gfx908`、`gfx90a`、`gfx942`、`gfx1030`、`gfx1100`、`gfx1101`、`gfx1102`、`gfx1200`、`gfx1201` | `…:server-rocm-b12242`（ROCm 7.2.1） |
| | `vulkan` | 其他所有 AMD GPU：APU（`gfx1151`、`gfx1150`、`gfx1103`…）與 ROCm 映像檔沒有編譯的卡。只給那張卡的 render node，不給 `/dev/kfd` | `…:server-vulkan-b12242`（Mesa RADV） |
| | `cpu`、`cpu-process` | x86_64 與 aarch64 用容器；x86_64 沒有 Docker 時用行程 | `…:server-b12242`；release `b12242` 的 `llama-b12242-bin-ubuntu-x64.tar.gz`，用它的 SHA-256 檢查 |
| `faster-whisper` | `cuda`、`cuda-12.4`、`cpu` | NVIDIA、CPU | `ghcr.io/speaches-ai/speaches:0.8.3-*` |
| `speaches-tts`（1.3） | `cpu` | CPU（容器），任何機器都可以 | 與 faster-whisper 相同的 `ghcr.io/speaches-ai/speaches:0.8.3-cpu` 映像檔。只用 CPU：Kokoro 和 Piper 跑在 ONNX Runtime 上，用 CPU 產生語音的速度就比播放還快；在 CUDA 映像檔裡 Kokoro 其實也是用 CPU 跑 |
| `vllm-omni-tts`（1.3） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU） | `vllm/vllm-omni:v0.32.0`（CUDA 13.0） |
| `vllm-omni-image`（1.3） | `cuda` | NVIDIA，compute capability 7.5 以上、驅動程式 580 以上（單張 GPU） | 同樣的 `vllm/vllm-omni:v0.32.0` |
| `sd-cpp`（1.3） | `vulkan` | 所有 AMD GPU 與 APU。只給那張卡的 render node，不給 `/dev/kfd` | `ghcr.io/leejet/stable-diffusion.cpp:master-vulkan`（build `32d516c`，Mesa RADV） |
| | `cpu-process` | x86_64，以行程執行、不需要 Docker（glibc 2.39 以上：Ubuntu 24.04、Debian 13） | release `master-1043-3be2378` 的 `sd-master-3be2378-bin-Linux-Ubuntu-24.04-x86_64.zip`，用它的 SHA-256 檢查。需要 1.3 版 agent（它會解開 `.zip`） |

`llama-cpp` 也提供文字轉語音：搭配 TTS GGUF 與它的 projector，上面每種 variant 都可以。

stable-diffusion.cpp 只用每次建置都會移動的 tag（`master-vulkan`）發布映像檔。配方用 digest 釘住檢查過的那個 build；`scripts/pin_images.py` 會保留它（tag 已經移動只會當成資訊回報），除非執行時加上 `--move-rolling`。它之後的 Linux release 是在 Ubuntu 26.04（glibc 2.43）上建置的，所以 CPU 行程用的是較早的 build；它的 OpenAI API 是一樣的。`sd-server` 沒有 API 金鑰：沒有金鑰守門的 agent（1.1 之前）不會分到 `sd-cpp` 副本。沒有 NVIDIA variant（[#43](https://github.com/xianhong1208/forgeyard/issues/43)）：官方的 CUDA 映像檔在 A100 上跑 Z-Image-Turbo 會當掉，所以 NVIDIA 顯示卡用 `vllm-omni-image` 產生圖片；在只有 NVIDIA 的機器上用 GPU 部署 GGUF 圖片模型會被拒絕（*no allowed GPU that sd-cpp supports*），選 **Runs on** → **CPU only** 則會在 CPU 上執行。

每個映像檔也都用 digest 釘選（`tag@sha256:…`）；頁面上顯示的是 tag。
`scripts/pin_images.py` 會在 `forgeyard/recipes/images.lock.json` 記錄每個映像檔的正確副本可能顯示的 id（index、manifest 和 config 的 digest，以及每個平台的內容 id），所以在連不到 registry 的機器上用 `docker load` 載入的副本會被檢查並使用：[離線機器](how-to.md#offline-machines)。

Variant 的欄位：`name`、`when`（`vendor`：`nvidia`、`amd` 或 `cpu`；`arch`：AMD 的 gfx target 或 CPU 架構；`compute_capability` 與 `driver`：像 `>=8.0` 這樣的比較）、`runtime`（`container` 或 `process`）、`image` 與 `entrypoint`（容器）、行程用的 `engine`（`url`、`sha256`、`binary`：agent 下載的 release）或 `command`（已安裝的程式）、取代配方設定的 `args` 與 `env`，以及 `amd_kfd`（AMD 容器；預設 `true`：給 `/dev/kfd` 和那張卡的 render node；`false`：只給 render node，給只用 Vulkan 的引擎）。

## 容器映像檔

每個版本都會在 Docker Hub 發布兩個映像檔，由 CI 從該版本的 tag 建置：

| 映像檔 | 內容 | 平台 |
|---|---|---|
| `docker.io/tonyhuang1101/forgeyard` | 伺服器（`Dockerfile.server`） | linux/amd64、linux/arm64 |
| `docker.io/tonyhuang1101/forgeyard-agent` | agent（`Dockerfile.agent`） | linux/amd64、linux/arm64（AMD 顯示卡只支援 amd64） |

- **tag**：`1.2.0`（就是那個版本）、`1.2`（它最新的修補版本）、`latest`（最新的正式版本）。預發布版本只會有它自己的版本號。每個映像檔都帶有 OCI 標籤（原始碼、授權、版本、revision）、建置來源證明（provenance）和 SBOM。
- **伺服器映像檔**以帳號 `forgeyard`（uid 10001）執行 `python main.py`，設定 `DATA_DIR=/data`（一個 volume）、`HOST=0.0.0.0` 和 `PORT=7860`（對外公開），並有針對 `/api/health` 的健康檢查。見[用 Docker 執行伺服器](how-to.md#run-the-server-in-docker)。
- **agent 映像檔**在 `tini` 底下執行 `forgeyard agent`，身分是 root（它要操作主機的 Docker），設定 `FORGEYARD_LIMITS=rlimit`、工作負載帳號 `forgeyard-run`（uid 990），以及掛載在 `/var/lib/forgeyard-agent` 的 volume。見[用 Docker 執行 agent](how-to.md#run-the-agent-in-docker)。
- **伺服器指定哪個 agent 映像檔**：[`AGENT_IMAGE`](#machines)，否則就是伺服器版本的已發布映像檔。還沒有任何發布（`forgeyard/install/images.py` 裡的 namespace 是空的）而且沒有設定 `AGENT_IMAGE` 時，會指定本機建置的 `forgeyard-agent:<version>`，**Add a machine** 會說明怎麼建置。

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
| `/srv/forgeyard-models/` | 模型快取 |
| `/opt/forgeyard/` | agent 程式：它的 Python 環境和下載下來的 wheel |
| `/usr/local/bin/forgeyard` | `forgeyard` 指令 |
| `/etc/systemd/system/forgeyard-agent.service` | 服務（`KillMode=process`、`Restart=always`） |

## 連接埠

| 連接埠 | 位置 | 用途 |
|---|---|---|
| `7860` | 伺服器 | 網頁介面、API、`/v1`、agent 的 WebSocket、`/install.sh`（單一連接埠，HTTPS） |
| `30000–32767` | 機器 | 服務和模型副本公開的連接埠（agent 的 `port_range`）；伺服器必須連得到這些連接埠 |

agent 本身不需要任何對內的連接埠：它們會主動連到伺服器。
