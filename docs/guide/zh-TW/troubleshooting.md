# 疑難排解

在實際機器上遇過的問題，每一個都說明你會看到什麼、原因，以及該怎麼做。請先從 agent（代理程式）自己的紀錄看起：用安裝方式裝的機器執行 `journalctl -u forgeyard-agent -n 50`，Docker 則執行 `docker logs --tail 50 forgeyard-agent`。

## 瀏覽器對憑證發出警告

**你會看到：** 開啟 Forgeyard 時出現 *Your connection is not private*（Chrome）或 *Warning: Potential Security Risk Ahead*（Firefox）。

**原因：** Forgeyard 在第一次啟動時建立了自己的憑證授權單位（CA），而你的瀏覽器還不認得它。連線無論如何都有加密；只是瀏覽器還無法分辨這台伺服器和冒充者。

**做法：**

1. 先略過警告一次（*Advanced* → *Proceed* / *Accept the risk*）。
2. 開啟 **Trust this server**（信任此伺服器，在登入頁面或帳號選單裡，或直接開 `/trust`）。
3. 把那裡顯示的指紋，和伺服器啟動紀錄裡的指紋（`HTTPS with Forgeyard's own CA. CA fingerprint sha256:…`）或加入指令裡的指紋比對。
4. 下載 CA，並依照你裝置對應的步驟安裝。關掉瀏覽器再重新開啟。

如果一台之前已經信任 CA 的裝置又出現警告，代表伺服器的 CA 換了（它的 `data/tls/` 遺失或被替換）。信任新的指紋之前，請先和管理員確認。

如果你用一個不在憑證裡的名稱開啟伺服器，瀏覽器也會發出警告。憑證涵蓋 `PUBLIC_URL` 的主機、`ALLOWED_HOSTS`、`TLS_EXTRA_NAMES`、主機名稱，以及機器自己的位址；請把缺少的名稱加到 `TLS_EXTRA_NAMES`，然後重新啟動伺服器。

## agent 顯示離線

**你會看到：** 在 **Machines** 上機器顯示 *offline*，並附上 *last heartbeat … ago*。

**做法：** 查看 agent 的紀錄。最後幾行會說明原因：

| 紀錄內容 | 原因 | 做法 |
|---|---|---|
| `Connection to https://… lost or refused: [Errno 111] Connection refused` | 伺服器沒有在執行，或不在那個位址或連接埠上 | 啟動伺服器；檢查 `HOST=0.0.0.0` 和防火牆 |
| `… is not the server this agent trusts: certificate verify failed` | 伺服器的 CA 換了，或中間有別的東西代替伺服器回應 | 如果 CA 是刻意更換的：用新的指紋執行 `sudo forgeyard agent trust --server https://… --ca-fingerprint sha256:…`，然後重新啟動 agent |
| `The server refused this machine's credential (revoked or removed)` | 管理員撤銷或移除了這台機器 | 用新的權杖重新加入：`sudo forgeyard agent join --rejoin --join-token fyj_…` |
| `The server does not answer to the host name in --server` | agent 用了一個伺服器不接受的名稱連線（HTTP 421） | 把這個名稱加到伺服器的 `ALLOWED_HOSTS`，或改用它的 IP 位址 |
| `If the server now uses HTTPS …, run once: forgeyard agent trust …` | agent 是透過純 HTTP 加入的（Forgeyard 1.0） | 執行它顯示的 `agent trust` 指令；機器會保留它的憑據 |
| `Connected to … as '…'` 之後就沒有其他訊息 | agent 沒問題；機器已經恢復上線 | 重新整理頁面 |

如果服務本身沒有在執行（`systemctl status forgeyard-agent` 顯示 *failed*），失敗訊息上方的紀錄會指出哪個設定有誤，例如 `/etc/forgeyard/agent.toml has 1 problem: - [workloads] limits wants one of auto, systemd, rlimit`。用以下指令檢查設定：

```bash
sudo forgeyard agent config
```

它會印出每個設定、它的值，以及值的來源（旗標、環境變數、檔案或預設值）。

兩個使用相同憑據的 agent（例如複製了狀態目錄）會互相把對方擠掉：伺服器只保留最新的連線，並以 *another agent connected with this credential* 關閉較舊的那條。

## 安裝程式拒絕指紋

**你會看到：** `forgeyard-ca.der: FAILED` 和 `sha256sum: WARNING: 1 computed checksum did NOT match`，或安裝程式顯示 `Refused: the CA … sent has fingerprint sha256:…, not sha256:…`。

**原因：** 機器收到的 CA 不是伺服器的。可能是指令來自另一台伺服器、伺服器的 CA 在指令產生後換了，或是機器和伺服器之間有別的東西代替伺服器回應。

**做法：** 在 **Machines** 建立一個新的加入指令，並把指紋和伺服器的啟動紀錄比對。如果兩者一致，機器卻仍然拿到別的 CA，代表網路上有東西擋在中間（例如會檢查 TLS 的代理）：請從沒有這個代理的網路安裝，或者如果你信任那個代理，用 `--ca-file` 釘選它的 CA。

## 拉取映像檔失敗：error getting credentials

**你會看到：** 部署的副本失敗，顯示 `docker: error getting credentials - err: exit status
255`（或 `pull access denied`），但你自己執行 `docker pull` 卻沒問題。

**原因：** 你的 Docker 設定指定了一個憑據輔助程式（`~/.docker/config.json` 裡的 `credsStore`），而它在 agent 執行的環境裡無法運作：例如沒有登入工作階段的桌面鑰匙圈，或 agent 的容器裡沒有這個輔助程式。

**Forgeyard 會怎麼處理：** 拉取映像檔以這種方式失敗時，agent 會不使用輔助程式，以匿名方式再拉一次。所有內建的引擎映像檔都是公開的，所以這樣就夠了。

**做法：** 如果還是失敗（私有映像檔，或機器無法連上網路）：

- 自己在機器上拉取一次映像檔：`docker pull vllm/vllm-openai:v0.32.0`；之後 agent 就會在本機找到它；
- 或修好輔助程式（`docker-credential-<name> list` 必須能以 agent 執行時的使用者身分正常執行）。

## Hugging Face 下載很慢

**你會看到：** 部署長時間停在 *downloading*；它的頁面會顯示進度和速度。

**原因：** agent 直接從 Hugging Face 下載模型，速度取決於機器的網路能跑多快。在其中一台測試機器上只有 3–4 MiB/s，所以 16 GB 的模型要花一個多小時。

**做法：**

- 等待：下載會從中斷的地方繼續，而且每台機器的每個模型只會下載一次；
- 使用鏡像站：在伺服器上把 `HF_ENDPOINT` 設成鏡像站的位址。如果鏡像站在你的區域網路內，還要在伺服器上設定 `MODEL_ALLOW_PRIVATE_HOSTS=true`，並在每個 agent 的 `agent.toml` 的 `[models]` 底下設定 `allow_private_hosts = true`；
- 從另一台已經有這個模型的機器複製模型快取（結構相同，請看[搬移模型快取](how-to.md#move-a-model-cache)）；
- 或把檔案放到機器上的某個目錄，再以 `local://` 模型的方式加入。

## vLLM 在共用的卡上無法啟動

**你會看到：** vLLM 副本啟動後不久就失敗，輸出的最後類似 `ValueError: Free memory on device (…) is less than desired GPU memory utilization (…)`。

**原因：** vLLM 的 `--gpu-memory-utilization` 是卡片**總**記憶體的比例，而 vLLM 要求這麼多記憶體是**可用**的才肯啟動。Forgeyard 會根據副本自己保留的記憶體量計算這個值，讓它能和別人的使用並存；但如果在放置和啟動之間，有其他人佔用了這張卡的記憶體，就會不夠用。

**做法：**

- 到 **Machines** 查看這張卡：以它的可用記憶體為準；
- 停止再啟動這個部署：它會依照當下的可用記憶體重新放置；
- 或降低它的需求：縮短 **Context length**、減少 **Concurrent requests**，或換一個較小的模型。部署頁面會在你部署前顯示新的估算。

## 部署放不下

**你會看到：** *Deploy* 回應 *Nothing can take a replica that needs … GiB per GPU*，接著每台機器一行說明。

**原因：** Forgeyard 只會把副本放在估算結果放得進**目前**可用記憶體的地方，而且只用 Forgeyard 可以使用的卡。與其啟動一個注定會失敗的東西，它寧可拒絕。每台機器的那一行會說明它為什麼沒被選上：

| 內容 | 意思 |
|---|---|
| `no allowed GPU that vllm supports` | GPU 允許清單上沒有任何卡，或沒有適合那張卡和驅動程式的 recipe（配方）變體 |
| `needs 14.38 GiB per GPU; available: #4 12.07 GiB, … (2.31 GiB short)` | 最適合的那張卡可用記憶體還差這麼多 |
| `needs 1 GPU it can have; in use: #4 (qwen)` | 卡都被不共用的副本佔走了 |
| `needs 3.08 GiB of RAM, 1.20 GiB available (1.88 GiB short)` | 這是 CPU 模型，而可用的 RAM 不夠 |
| `no container runtime (…)` | recipe 需要 Docker 或 Podman，但這台機器沒有 |
| `its agent does not serve models` | 這個 agent 沒有設定模型快取（設定裡的 `[models] cache`） |
| `offline` | 這台機器的 agent 沒有連線 |

**做法：** 釋出卡上的記憶體（停掉某些東西）、允許更多卡（[GPU 存取權](how-to.md#restrict-gpus-on-a-shared-machine)）、降低上下文長度或並行數、選擇量化過或較小的模型，或者先部署，讓它等到有空間為止（API 的 `"wait": true`）。

## 來自 /v1 的錯誤：401、403、429、503

閘道以 OpenAI 的格式回應錯誤：`{"error": {"message", "type", "param", "code"}}`。`code` 會說明發生了什麼事。

### 401

`missing_api_key`、`invalid_api_key`、`expired_api_key`、`revoked_api_key`、`account_disabled`：沒有帶金鑰（請送出 `Authorization: Bearer fyk_…`）、金鑰錯誤、過期或已撤銷，或金鑰擁有者的帳號已停用。請到 **API keys** 檢查金鑰；需要的話建立一把新的。

### 403

`model_not_allowed`：這把金鑰被限制只能用某些模型，而這個模型不在其中。金鑰的擁有者（或管理員）可以在 **API keys** 修改清單。

### 429

`rate_limit_exceeded`：超過這把金鑰每分鐘的請求數或 token 數。請等待 `Retry-After` 標頭裡的秒數；限制是以最近 60 秒計算。管理員為你的金鑰設定的限制是鎖定的；如果要修改，請找管理員。

### 503

`no_ready_replica`：模型存在，但它沒有任何一個副本處於就緒狀態：還在下載或啟動、已停止，或所在的機器離線。錯誤訊息會列出每個副本的狀態。請查看這個部署的頁面。

其他錯誤：`404 model_not_found`（沒有這個模型名稱的部署：請查看 `/v1/models`）、`502`（連不到某個副本，或副本拒絕了它的內部金鑰：通常是因為它的 agent 重新啟動了；幾秒內就會恢復）、`504`（在 `GATEWAY_READ_TIMEOUT` 內沒有回應，預設 600 秒）。

## 在沒有 systemd 的容器裡執行 agent

**你會看到：** 行程類工作負載無法啟動，agent 的紀錄裡提到 `systemd-run` 或 `Failed to connect to bus`。

**原因：** 在 `limits = "auto"` 的情況下，如果 agent 以 root 執行而且找到 systemd，就會使用 systemd scope。在和主機共用 `/run` 的容器裡，它可能找得到 systemd 的檔案，卻無法和 systemd 溝通。

**做法：** 改用 rlimit。在 `/etc/forgeyard/agent.toml` 裡設定：

```toml
[workloads]
limits = "rlimit"
```

或使用 `--limits rlimit`，或 `FORGEYARD_LIMITS=rlimit`（agent 的 Docker 映像檔已經設定好了）。這樣行程類工作負載會以每個行程為單位套用資源限制，CPU 用量只會被計算，不會被設上限。

## 回報問題時要附上的資訊

```bash
forgeyard --version
sudo forgeyard agent config
sudo forgeyard agent inventory --raw --json
sudo forgeyard agent runtimes
journalctl -u forgeyard-agent -n 200 --no-pager
```

分享之前，請先移除任何私人資訊（主機名稱、位址）。
