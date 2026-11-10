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
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | 你自己的憑證和私鑰（兩個都設或都不設）；檔案變更時會重新載入 |
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

## 命令列

### 伺服器

| 指令 | 用途 |
|---|---|
| `uv run python main.py` | 啟動伺服器（等同 `python -m forgeyard serve`）。資料庫的變更會在啟動時執行 |
| `python -m forgeyard secrets reset --yes` | 機密金鑰遺失時使用：產生一把新的，並捨棄舊金鑰加密的內容（請先停止伺服器） |
| `python -m forgeyard --version` | 顯示版本 |

### agent

用單行安裝裝好後，agent 的指令是 `forgeyard`（在 `/usr/local/bin`）；從原始碼目錄執行則是 `uv run --no-sync python -m forgeyard`。

| 指令 | 用途 |
|---|---|
| `forgeyard agent` | 執行 agent（服務執行的是 `forgeyard agent --config /etc/forgeyard/agent.toml`）。如果有加入權杖但還沒有憑據，會先加入 |
| `forgeyard agent init [--print] [--force]` | 根據偵測到的內容（GPU、Docker 或 Podman、systemd、工作負載帳號）寫出 `/etc/forgeyard/agent.toml`，並印出內容。`--print` 只印出不寫入。在這裡給的旗標（`--server`、`--ca-fingerprint`、`--model-cache`…）會寫進檔案 |
| `forgeyard agent join --join-token fyj_…` | 用檔案裡的設定加入，儲存憑據後結束。已經加入過：不做任何事；`--rejoin` 會以新機器的身分重新加入 |
| `forgeyard agent config [--get KEY]` | 列出所有生效中的設定和它們的來源；`--get models.cache` 只印出一個值 |
| `forgeyard agent trust --server https://… [--ca-fingerprint …]` | 把已加入的機器切換到另一個位址或 CA，並保留它的憑據 |
| `forgeyard agent inventory [--json] [--raw]` | agent 偵測到的內容（GPU、CPU、記憶體），不需要加入 |
| `forgeyard agent runtimes` | 這台機器能執行什麼（行程、容器、容器內的 NVIDIA），不需要加入 |

### 安裝腳本

`curl … https://<server>/install.sh | sudo sh -s -- OPTIONS`：

| 選項 | 用途 |
|---|---|
| `--join fyj_…` | 加入權杖（第一次安裝） |
| `--ca-fingerprint sha256:…` | 伺服器 CA 的指紋：在下載任何東西之前檢查，之後由 agent 釘選 |
| `--ca-file PEM` | 改為釘選這個 CA 檔案 |
| `--server URL` | 伺服器的位址（預設：提供這個腳本的伺服器） |
| `--name NAME`, `--label KEY=VALUE` | 機器的名稱和標籤（第一次安裝） |
| `--model-cache DIR` | 模型存放的位置（預設 `/srv/forgeyard-models`） |
| `--workload-user USER` | 行程類工作負載使用的帳號（預設 `forgeyard-run`，不存在時會自動建立） |
| `--no-start` | 安裝並加入，但不啟動服務 |
| `--rejoin` | 以新機器的身分重新加入 |
| `--uninstall [--purge]` | 移除服務和程式；`--purge` 會連設定和憑據一起刪除 |

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
