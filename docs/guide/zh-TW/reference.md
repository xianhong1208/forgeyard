# 參考資料

伺服器和 agent（代理程式）的每一個設定、命令列，以及各項資料存放的位置。

## 伺服器設定

伺服器從環境變數讀取設定，也可以從它啟動時所在資料夾裡的 `.env` 檔讀取（`.env.example` 列出了所有設定並附有註解）。修改後請重新啟動伺服器。

### 網路與位址

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HOST` | `127.0.0.1` | 監聽的位址。`0.0.0.0` 代表對網路開放 |
| `PORT` | `7860` | 連接埠，網頁介面、API、`/v1` 和 agent 都用這個 |
| `PUBLIC_URL` | — | 使用者和 agent 使用的位址，例如 `https://192.168.1.20:7860`。加入指令、設定密碼的連結和閘道位址都會用到它；它的主機一律會被接受，也會包含在憑證裡 |
| `ALLOWED_HOSTS` | — | 其他要回應的主機名稱，以逗號分隔。`localhost` 和 IP 位址一律可用；其他名稱會得到 421 |
| `TRUSTED_PROXIES` | — | 信任其 `X-Forwarded-For` 和 `X-Forwarded-Proto` 的代理，以逗號分隔的 IP |

### HTTPS

| 變數 | 預設值 | 用途 |
|---|---|---|
| `TLS` | `on` | `on`：以 Forgeyard 自己的 CA（建立在 `data/tls/`）或你的憑證提供 HTTPS。`off`：純 HTTP，只能用在處理 HTTPS 的代理後面 |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | 你自己的憑證和私鑰（兩個都設或都不設）；每 12 小時檢查一次，有變更就重新載入 |
| `TLS_CA_FILE` | — | 使用自己的憑證時：簽發它的 CA。agent 會釘選它，**Trust this server** 也會提供它 |
| `TLS_EXTRA_NAMES` | — | 要加進 Forgeyard 自己的伺服器憑證的其他名稱或 IP 位址，以逗號分隔 |

### 資料與機密

| 變數 | 預設值 | 用途 |
|---|---|---|
| `DATA_DIR` | `data` | 資料庫（SQLite）、`secret.key`、`tls/` 和設定權杖（setup token）的位置 |
| `DATABASE_URL` | — | 留空：SQLite，放在 `DATA_DIR`。PostgreSQL：`postgresql+psycopg://user:password@host:5432/forgeyard`（先執行 `uv sync --extra postgres`） |
| `SECRET_KEY_FILE` | `data/secret.key` | 加密儲存機密資料的金鑰。請和資料庫分開備份 |
| `FORGEYARD_SECRET_KEY` | — | 直接給金鑰本身（32 位元組的 base64），取代檔案 |

### 帳號與登入

| 變數 | 預設值 | 用途 |
|---|---|---|
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | — | 在第一次啟動時建立擁有者，取代設定頁面 |
| `FORGEYARD_SETUP_TOKEN` | — | 使用這個設定權杖，而不是隨機產生的 |
| `REGISTRATION` | `off` | `approval`：使用者可以申請帳號，申請會等待管理員核准 |
| `SESSION_IDLE_MINUTES` | `120` | 超過這段時間沒有任何請求就自動登出 |
| `SESSION_MAX_DAYS` | `7` | 無論如何，超過這段時間就自動登出 |
| `COOKIE_SECURE` | `auto` | `auto`：瀏覽器使用 HTTPS 時就使用 `Secure` cookie；設成 `true` 或 `false` 可強制指定 |
| `RATE_LIMIT_PER_MINUTE` | `600` | 每個位址每分鐘的 API 請求數（0：不限制） |
| `LOGIN_FREE_FAILURES` | `5` | 每個帳號登入失敗幾次後，之後每次嘗試都要等待（2 秒起，每次加倍，最多 5 分鐘） |
| `LOGIN_IP_FAILURES` | `30` | 每個位址在 15 分鐘內登入失敗幾次後，該位址就要等待 |

### 機器

| 變數 | 預設值 | 用途 |
|---|---|---|
| `AGENT_HEARTBEAT_SECONDS` | `10` | agent 送出心跳的頻率 |
| `AGENT_OFFLINE_SECONDS` | `30` | 超過這段時間沒收到任何訊息的機器會顯示離線 |

### 模型

| 變數 | 預設值 | 用途 |
|---|---|---|
| `HF_ENDPOINT` | `https://huggingface.co` | 從哪裡查詢和下載 Hugging Face 模型（可以填鏡像站） |
| `MODEL_ALLOW_PRIVATE_HOSTS` | `false` | 允許使用私有位址和純 http 的模型來源（區域網路內的鏡像站；agent 也需要設定 `allow_private_hosts`） |
| `MODEL_URL_HOSTS` | — | `https://` GGUF 來源允許的主機，以逗號分隔（管理員可以在 **Models** 修改） |
| `MODEL_LOCAL_DIRS` | — | `local://` 模型允許的目錄，以逗號分隔（管理員可以在 **Models** 修改） |
| `GPU_CONTEXT_MIB` | `400` | 副本的 CUDA context 在它本身之外額外佔用的 GPU 記憶體 |
| `VRAM_HEADROOM_MIB` | `512` | 放置副本時，每張卡一律保留不用的記憶體 |

### 閘道

| 變數 | 預設值 | 用途 |
|---|---|---|
| `GATEWAY_CONNECT_TIMEOUT` | `5` | 連線到副本的秒數上限，超過就改試下一個 |
| `GATEWAY_READ_TIMEOUT` | `600` | 副本回傳回應標頭或串流下一段內容的秒數上限 |
| `GATEWAY_MAX_BODY_MB` | `32` | 請求本文的大小上限（用來轉錄的音訊檔是最大的） |
| `GATEWAY_DEFAULT_RPM`, `GATEWAY_DEFAULT_TPM` | `0` | 沒有自己限制的金鑰，每分鐘的請求數和 token 數（0：不限制）。管理員可以在 **API keys** 修改 |
| `GATEWAY_MAX_RPM`, `GATEWAY_MAX_TPM` | `0` | 任何人能為自己的金鑰設定的最大值（0：沒有上限） |
| `USAGE_FLUSH_SECONDS` | `5` | 把記憶體中統計的用量寫入資料庫的頻率 |

### 雲端供應商

| 變數 | 預設值 | 用途 |
|---|---|---|
| `GATEWAY_CLOUD_DEFAULT` | `off` | 新 API 金鑰拿到的雲端設定：`off`、`fallback` 或 `on`。管理員可以在 **API keys** 修改（存在資料庫裡）；既有的金鑰維持原本的設定 |
| `GATEWAY_CLOUD_MAX` | `off` | 任何人能為自己的金鑰設定的最大值。管理員可以為任何金鑰設定任何值 |
| `EGRESS_PROXY` | 空白 | 送往供應商的請求改經由這個 proxy（`http://host:port`）。絕不使用環境變數裡的 proxy |
| `EGRESS_ALLOWED_HOSTS` | 空白 | 只允許這些供應商主機，以逗號分隔（`*.example.org` 代表子網域）。空白：不限 |
| `EGRESS_CA_FILE` | 空白 | 除了系統的 CA 之外，供應商連線額外信任的 CA 檔（例如會檢查 TLS 的公司 proxy） |
| `PROVIDER_ALLOW_PRIVATE_HOSTS` | `false` | 讓標示為在你網路外的供應商也能使用私有或 loopback 位址和純 http。只用於以替身伺服器測試；link-local 和 metadata 位址仍然一律拒絕 |
| `PROVIDER_CONNECT_TIMEOUT` | `10` | 連線到供應商的秒數上限，超過就改試下一個目標 |
| `PROVIDER_READ_TIMEOUT` | `300` | 供應商回傳回應標頭或串流下一段內容的秒數上限（每個供應商可以另外設定） |
| `PROVIDER_COOLDOWN_SECONDS` | `30` | 回應 429 或 5xx、或連不上的供應商目標，在這段時間內排到最後才嘗試（如果它的 `Retry-After` 更長就照它，最多 5 分鐘） |
| `PROVIDER_RETRY_AFTER_CAP` | `2` | 沒有其他目標可試時，不超過這個秒數的 `Retry-After` 會等一次再試 |
| `OPENROUTER_MODELS_URL` | `https://openrouter.ai/api/v1/models` | **Refresh prices from OpenRouter** 讀取價格的位置（只在管理員要求時讀取，不送出金鑰）。只有在用替身伺服器測試時才需要修改 |
| `BUDGET_DEFAULT_MAX_TOKENS` | `4096` | 預算會在雲端請求送出前先預留它的估計費用。沒有 `max_tokens` 的請求，以這個 token 數估計它的回應 |

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

`/v1` 底下的其他路徑都回 404 `unknown_endpoint`。

閘道如何處理一個請求：

- 從請求內容讀取 `model`，把請求送到這個模型的路由。模型在另一個端點提供時（例如把嵌入模型送到 `/v1/chat/completions`），回 400 `wrong_endpoint`。
- 把 `model` 換成副本或供應商預期的名稱，加上副本的內部金鑰或供應商的金鑰，你的其他東西一概不轉送：不轉送你的 API 金鑰，也不轉送你的其他標頭。
- `max_tokens`（或 `max_completion_tokens`）在請求到達時計入金鑰的每分鐘 token 數，也決定預算下這個請求最多可能花多少錢。
- 串流的對話或補全會多要一個最後的用量區塊（`stream_options.include_usage`），以便計算 token。如果你沒有要求，閘道會再把它拿掉。
- 請求內容最多 `GATEWAY_MAX_BODY_MB`（32 MB）。
- 你關閉連線時，閘道會立刻關閉往上游的請求，引擎就會停止生成。

### 標頭

| 標頭 | 方向 | 值 |
|---|---|---|
| `Authorization` | 請求 | `Bearer fyk_…`：你的 API 金鑰 |
| `X-Forgeyard-Local-Only` | 請求 | `1`（或 `true`、`yes`、`on`）：這個請求不使用任何雲端供應商。它絕不會放寬金鑰的權限 |
| `X-Forgeyard-Target` | 回應 | 由誰回答：`local:<deployment>#<replica>` 或 `provider:<provider>/<model>`。絕不包含金鑰 |
| `X-Forgeyard-Replica` | 回應 | 本機回答時：`<model>#<replica>` |
| `X-Forgeyard-Spilled` | 回應 | 請求離開第一層的原因：`unavailable`、`busy`、`refused`、`too_long` 或 `budget`。第一層回答時沒有這個標頭 |
| `X-Forgeyard-Budget-Exhausted` | 回應 | 讓請求無法送往雲端供應商的預算，以逗號分隔：`key`、`person` 或 `provider:<name>`。由其他目標成功回答時也會帶上 |
| `Retry-After` | 回應 | 要等待的秒數，出現在 429 `rate_limit_exceeded`，以及供應商有提供時的 429 `provider_rate_limited` |

### 錯誤

錯誤採用 OpenAI 的格式，每個 OpenAI 用戶端函式庫都讀得懂：

```json
{"error": {"message": "This API key was revoked.", "type": "authentication_error", "param": null, "code": "revoked_api_key"}}
```

`type` 依狀態碼而定（`invalid_request_error`、`authentication_error`、`insufficient_quota`、`permission_error`、`rate_limit_error`、`upstream_error`、`service_unavailable`）。請依 `code` 處理錯誤：

| 狀態碼 | `code` | 意思 | 怎麼做 |
|---|---|---|---|
| 400 | `invalid_json` | 請求內容不是 JSON | 送 JSON |
| 400 | `invalid_request` | 沒有 `model`、請求內容不是物件，或音訊不是以 `multipart/form-data` 送出 | 修正請求 |
| 400 | `wrong_endpoint` | 這個模型在另一個端點提供；訊息會寫出是哪一個 | 改用那個端點 |
| 400 | `context_length_exceeded` | 只出現在開啟 **Spill a prompt that is too long** 的路由：提示放不進本機的上下文，而且沒有其他目標能接手 | 縮短提示或降低 `max_tokens` |
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
| 413 | `request_too_large` | 請求內容大於 `GATEWAY_MAX_BODY_MB` | 送少一點，或調高設定 |
| 421 | `host_not_allowed` | 伺服器不回應網址裡的主機名稱 | 使用它的位址，或把名稱加進 `ALLOWED_HOSTS` |
| 429 | `rate_limit_exceeded` | 超過金鑰每分鐘的請求數或 token 數 | 等待 `Retry-After` 秒 |
| 429 | `provider_rate_limited` | 每個能提供服務的雲端目標都達到速率限制 | 有 `Retry-After` 就等待那麼多秒 |
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
| 504 | `upstream_timeout` | 副本在 `GATEWAY_READ_TIMEOUT` 內沒有回應（或沒有送出串流的下一段） | 降低 `max_tokens`，或調高設定 |
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

## 檔案與目錄

在伺服器上，位於它的資料夾裡：

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
