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

**原因：** agent 直接從 Hugging Face 下載模型，速度取決於機器的網路能跑多快。以 4 MiB/s 計算，16 GB 的模型要花一個多小時。

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
| `needs 14.38 GiB per GPU; available: #0 12.07 GiB, … (2.31 GiB short)` | 最適合的那張卡可用記憶體還差這麼多 |
| `needs 1 GPU it can have; in use: #1 (qwen)` | 卡都被不共用的副本佔走了 |
| `needs 3.08 GiB of RAM, 1.20 GiB available (1.88 GiB short)` | 這是 CPU 模型，而可用的 RAM 不夠 |
| `no container runtime (…)` | recipe 需要 Docker 或 Podman，但這台機器沒有 |
| `its agent does not serve models` | 這個 agent 沒有設定模型快取（設定裡的 `[models] cache`） |
| `offline` | 這台機器的 agent 沒有連線 |

**做法：** 釋出卡上的記憶體（停掉某些東西）、允許更多卡（[GPU 存取權](how-to.md#restrict-gpus-on-a-shared-machine)）、降低上下文長度或並行數、選擇量化過或較小的模型，或者先部署，讓它等到有空間為止（API 的 `"wait": true`）。

## 來自 /v1 的錯誤：401、403、429、503

閘道以 OpenAI 的格式回應錯誤：`{"error": {"message", "type", "param", "code"}}`。`code` 會說明發生了什麼事。

### 401

`missing_api_key`、`invalid_api_key`、`expired_api_key`、`revoked_api_key`、`account_disabled`：沒有帶金鑰（請送出 `Authorization: Bearer fyk_…`）、金鑰錯誤、過期或已撤銷，或金鑰擁有者的帳號已停用。請到 **API keys** 檢查金鑰；需要的話建立一把新的。

### 402

**你會看到：** `402`，帶著 `budget_exhausted` 或 `budget_unpriced`，以及標頭 `X-Forgeyard-Budget-Exhausted: key`（或 `person`、`provider:<name>`）。

**原因：** 這個請求只能由雲端供應商處理，而每月預算讓它無法送往任何一個能處理它的供應商：

- `budget_exhausted`：金鑰、金鑰擁有者或供應商的預算已經用完，或這個請求預留的費用會超過預算。錯誤裡的 `budget` 物件會列出預算、已花費和已預留的金額、這個請求最多可能花多少，以及 `resets_at`（下個月 1 日 00:00 UTC）。
- `budget_unpriced`：有預算適用，但供應商的模型沒有價格，所以無法預留它的費用。

本機副本絕不受預算限制：只要有本機副本能處理，就由它處理。

**做法：**

- `budget_exhausted`：等到 `resets_at`，或請人提高預算（金鑰的預算在 **API keys** → **Limits**，個人的預算在 **Usage** → **Cost** → **Budgets this month**，供應商的預算在它的頁面）。啟動這個模型的本機部署也有幫助。`max_tokens` 較低的請求預留得比較少，可能還放得進預算。OpenAI 的 SDK 不會重試 402，重試也沒有用。
- `budget_unpriced`：由管理員在供應商的頁面設定模型的價格（**Prices**，或 **Refresh prices from OpenRouter**）。

更多內容請見[預算](cloud-providers.md#budgets)。

### 403

`model_not_allowed`：這把金鑰被限制只能用某些模型，而這個模型不在其中。金鑰的擁有者（或管理員）可以在 **API keys** 修改清單。

### 429

`rate_limit_exceeded`：超過這把金鑰每分鐘的請求數或 token 數。請等待 `Retry-After` 標頭裡的秒數；限制是以最近 60 秒計算。管理員為你的金鑰設定的限制是鎖定的；如果要修改，請找管理員。

### 503

`no_ready_replica`：模型存在，但它沒有任何一個副本處於就緒狀態：還在下載或啟動、已停止，或所在的機器離線。錯誤訊息會列出每個副本的狀態。請查看這個部署的頁面；另見[模型有列出，卻回應 503](#a-model-is-listed-but-answers-503)。

其他錯誤：`404 model_not_found`（對這把金鑰沒有這個名稱的模型：請查看 `/v1/models` 和[模型沒有出現在 /v1/models](#a-model-is-missing-from-v1models)）、`502`（連不到某個副本，或副本拒絕了它的內部金鑰：通常是因為它的 agent 重新啟動了；幾秒內就會恢復）、`504`（在 `GATEWAY_READ_TIMEOUT` 內沒有回應，預設 600 秒）。每個錯誤碼都列在[參考資料](reference.md#errors)。

### 來自雲端供應商的錯誤

送往雲端供應商的模型會多出這些錯誤。回應的 `X-Forgeyard-Target` 會說明是不是由供應商回答的。

- 模型明明列在 **Models** 上卻回應 `404 model_not_found`：它是只有雲端的模型，而金鑰的雲端設定不是 **Allowed**。
- `400 cloud_only_model`：請求帶了 `X-Forgeyard-Local-Only: 1`（或路由勾選了 **Local only**），但這個模型只由雲端供應商提供。
- `502 provider_auth_failed`：供應商拒絕了 Forgeyard 給它的金鑰；請管理員到 **Providers** 修正。`502 provider_blocked`：對外連線規則拒絕了供應商的位址。`502 provider_redirect`：供應商回應了重新導向，而 Forgeyard 絕不跟隨；請檢查它的 base URL。
- `429 provider_rate_limited`：雲端層的每個目標都達到速率限制；請依 `Retry-After` 等待。
- `402 budget_exhausted`、`402 budget_unpriced`：每月預算；請見 [402](#402)。
- 其他 4xx 會保留供應商的狀態碼、代碼和訊息，並在訊息前加上供應商的名稱。

更多內容請見[雲端供應商](cloud-providers.md#troubleshooting)。

## 程式不信任憑證

**你會看到：** 呼叫 `/v1` 的程式失敗，訊息是 `certificate verify failed`、`self-signed certificate in certificate chain`、`unable to get local issuer certificate` 或 `SELF_SIGNED_CERT_IN_CHAIN`。同一個位址在已經信任 CA 的瀏覽器裡卻正常。

**原因：** 伺服器的憑證來自 Forgeyard 自己的 CA，而每個程式各自決定要信任哪些憑證授權單位。把 CA 裝進瀏覽器或系統，管不到自帶清單的程式：Python 的 `requests` 和 OpenAI SDK（透過 `httpx`）使用 `certifi` 套件的清單，而不是系統的清單；Node.js 也內建自己的清單。

**做法：** 從 **Trust this server** 取得 `forgeyard-ca.pem`，核對它的指紋，再用下列其中一種方式交給程式：

| 程式 | 方式 |
|---|---|
| curl | `curl --cacert forgeyard-ca.pem …`，或把 CA 裝進系統（見下方） |
| Python、OpenAI SDK、`httpx` | 在程式碼裡：`httpx.Client(verify=ssl.create_default_context(cafile="forgeyard-ca.pem"))`，如[快速入門](quick-start.md#python-with-the-openai-sdk)所示；或用 `SSL_CERT_FILE` |
| Python、`requests` | `REQUESTS_CA_BUNDLE` |
| Node.js | `NODE_EXTRA_CA_CERTS=forgeyard-ca.pem`：加進 Node 自己的清單，原本的清單仍保留 |
| 其他程式 | 它自己指定 CA 檔案的設定，如果有的話 |

`SSL_CERT_FILE` 和 `REQUESTS_CA_BUNDLE` 會**取代**程式原本的清單。如果程式只連 Forgeyard，只指向 `forgeyard-ca.pem` 沒有問題；但這樣它連其他 HTTPS 網站（雲端 API、套件庫）都會失敗。這種程式請把 CA 裝進系統，再把變數指向系統的清單，這份清單就會同時包含兩者：

```bash
sudo cp forgeyard-ca.pem /usr/local/share/ca-certificates/forgeyard-ca.crt
sudo update-ca-certificates
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

以上是 Debian 和 Ubuntu 的做法；其他系統的步驟請看 **Trust this server**。設定變數後請重新啟動程式。完全無法指定 CA 的程式，需要一張它本來就信任的憑證：請見[使用自己的憑證或反向代理](how-to.md#use-your-own-certificate-or-a-reverse-proxy)。

## 模型沒有出現在 /v1/models

**你會看到：** 你預期的模型不在 `GET /v1/models` 裡，呼叫它則回應 `404 model_not_found`。

**原因：** `/v1/models` 只列出**這把金鑰**現在能使用的模型。以下情況模型不會列出：

- **它的部署已停止**，而且沒有這把金鑰能使用的供應商提供它。啟動中的部署仍會列出；停止的部署會從清單消失，直到再次啟動；
- 金鑰被限制只能用**其他模型**（這時呼叫會回 `403 model_not_allowed`）；
- 只有**雲端供應商**提供它，而金鑰的雲端設定不是 **Allowed**：對這把金鑰來說，這個模型不存在；
- 部署或路由已被**刪除**，或名稱不是你以為的那個：程式使用的名稱是部署（或路由）的名稱，在它的頁面上標示為 *Model name*。

**做法：** 打開 **Models** 查看模型的狀態；停止的部署請按 **Start**。到 **API keys** 檢查金鑰的模型清單和它的 *Cloud providers* 設定。

## 模型有列出，卻回應 503

**你會看到：** 模型在 `/v1/models` 裡，但呼叫它回應 `503 no_ready_replica`。

**原因：** 部署應該在執行，但還沒有就緒的副本：模型還在下載、引擎還在啟動、某個副本失敗後正在重新啟動，或它所在的機器離線。訊息會列出每個副本的狀態，例如 `replica 0 on gpu-01: downloading`。如果你還是呼叫已停止的部署，也會得到同樣的回應（`The deployment is stopped.`）。

**做法：** 打開部署的頁面。下載或啟動中就等待（頁面會顯示進度）。如果副本一直失敗，頁面上的輸出會說明原因；機器離線請見[agent 顯示離線](#an-agent-shows-offline)。如果想讓這段期間的請求改送雲端供應商，請在模型的路由加上雲端層（[雲端供應商](cloud-providers.md#route-a-model-to-the-cloud)）。

## 請求沒有溢出到雲端

**你會看到：** 模型的路由有雲端層，但本機副本忙碌或停擺時，請求仍然失敗（503、429）或在等待。回應沒有 `X-Forgeyard-Spilled`。

**原因**，依序檢查：

1. **金鑰的雲端設定是 Never。** 除非有人改過，每把金鑰都是 **Never**，包括 1.2 之前建立的每把金鑰。這種金鑰絕不會送到雲端供應商，而是直接拿到本機的錯誤。
2. 路由勾選了 **Local only**，或請求帶了 `X-Forgeyard-Local-Only: 1`（Playground 的 *Local only (never the cloud)* 會送出它）。
3. 這個端點不會送往供應商：只有對話、補全和嵌入會；rerank、score 和語音轉文字只在本機。
4. 原因不是會溢出的那幾種。請求只在回應開始之前、本機層無法使用、忙碌，或回應 429 或 503 時才會往下一層；提示太長、放不進本機上下文的情況，只有開啟 **Spill a prompt that is too long** 才會溢出。400、請求送出後逾時，或已經開始的串流，都絕不溢出。
5. **預算**用完了：回應會帶 `X-Forgeyard-Budget-Exhausted`。
6. 供應商，或它在路由裡的目標，已被**停用**。

**做法：** 到 **API keys**，打開金鑰的 **Limits**，把 *Cloud providers* 設為 **Only to spill**（或 **Allowed**，連只有雲端的模型也能用），再按 **Save limits**。一般人最多只能選到管理員在 **Cloud for keys** 允許的程度。在 Playground 裡，管理員可以使用雲端，其他人則是那個上限。詳細內容請見[請求何時溢出](cloud-providers.md#when-a-request-spills)。

## 圖片被拒絕

**你看到：** 帶圖片（或音訊、影片）的對話請求回應 400 或 413；`code` 說明原因。請求的任何內容都沒有送到引擎。

| `code` | 原因 | 怎麼做 |
|---|---|---|
| `media_not_supported` | 模型不接受這種媒體：純文字模型、加入時沒有 projector 檔的 GGUF 模型，或部署的 *Videos per request* 為 0 時送影片 | 改用視覺模型；重新加入 GGUF 模型並附上 projector；在 **Advanced** 開啟影片 |
| `too_many_media_items` | 圖片多於部署的 *Images per request*（4） | 少送一些，或調高它 |
| `media_url_refused` | 網址是 `file://` 或其他 scheme、帶有帳密，或主機是（或轉址到）私有、loopback、link-local 或 metadata 位址 | 改送公開的 `https://` 網址或 `data:` URI。區網內的圖片伺服器，請在伺服器上設定 `MEDIA_ALLOW_PRIVATE_HOSTS=true` |
| `media_fetch_failed` | 閘道下載不到：對方回應的狀態碼不是 200、超過 20 秒、轉址超過 3 次，或傳回的不是圖片 | 自己打開網址看看；有些網站拒絕程式下載。改以 `data:` URI 送出檔案 |
| `media_too_large`（413） | 檔案大於 `MEDIA_FETCH_MAX_MB`（20） | 送小一點的圖片，或調高設定 |
| `request_too_large`（413） | 圖片以 base64 內嵌後（比檔案大三分之一），請求超過部署的 *Largest request*（64 MiB） | 送小一點的圖片，或在 **Advanced** 調高 |
| `file_input_not_supported` | `file` 區塊（PDF）：內建引擎都不接受檔案，閘道也不會轉換 | 改送頁面圖片或文字 |
| `media_local_only` | 模型只由雲端供應商提供；媒體只留在你自己的部署上 | 改用本機的視覺模型 |

vLLM 上的視覺模型**開啟影片後無法啟動**：vLLM 啟動時會為一段長影片預留記憶體，剩給 KV cache 的就不夠了。調低上下文長度，或再把影片關掉。

## 費用顯示有請求沒有價格

**你會看到：** **Usage** → **Cost** 把一些請求算在 *Without a price* 底下，並說明上面的費用沒有包含它們。

**原因：** 雲端請求在結束時，依它的供應商模型的價格計價。如果供應商沒有自己回報費用，而模型當時沒有輸入或輸出價格，或者供應商有回答卻沒有回報任何 token（例如用戶端在用量送達前就關閉了串流），這個請求就沒有價格。它的費用是未知，而不是 0。本機副本和你網路內的供應商一律是 0，絕不會沒有價格。

**做法：** 由管理員打開供應商的頁面，設定模型的 **Prices**，或使用 **Refresh prices from OpenRouter**。價格從那之後的請求開始適用：已經計算過的請求仍然沒有價格。在預算之下，沒有價格的模型會被拒絕（`402 budget_unpriced`），直到它有價格為止。

## 在沒有 systemd 的容器裡執行 agent

**你會看到：** 行程類工作負載無法啟動，agent 的紀錄裡提到 `systemd-run` 或 `Failed to connect to bus`。

**原因：** 在 `limits = "auto"` 的情況下，如果 agent 以 root 執行而且找到 systemd，就會使用 systemd scope。在和主機共用 `/run` 的容器裡，它可能找得到 systemd 的檔案，卻無法和 systemd 溝通。

**做法：** 改用 rlimit。在 `/etc/forgeyard/agent.toml` 裡設定：

```toml
[workloads]
limits = "rlimit"
```

或使用 `--limits rlimit`，或 `FORGEYARD_LIMITS=rlimit`（agent 的 Docker 映像檔已經設定好了）。這樣行程類工作負載會以每個行程為單位套用資源限制，CPU 用量只會被計算，不會被設上限。

## 伺服器拒絕某個設定，或對它發出警告

**伺服器無法啟動**，紀錄裡寫著 `Setting COOKIE_SECURE: …`（或其他名稱）：這個值無法使用。訊息會列出這個設定可以填哪些值。`TLS`、`REGISTRATION`、`GATEWAY_CLOUD_DEFAULT` 和 `GATEWAY_CLOUD_MAX` 也是一樣，`TLS_CERT_FILE` 和 `TLS_KEY_FILE` 只設了其中一個時也是（`HTTPS: Set both TLS_CERT_FILE and TLS_KEY_FILE, or neither`）。

**伺服器有啟動，但出現像 `Settings: GATEWAY_CLOUD_DEFUALT is not a setting` 這樣的紀錄**：伺服器會忽略那個名稱。每一行都會說明原因和該怎麼做：

- *is not a setting… Did you mean …?*：打錯字，或是其他版本的設定。請在 `.env` 或環境變數裡改正名稱。
- *is ignored*：另一個設定讓它沒有作用，例如沒有自己的憑證卻設了 `TLS_CA_FILE`，或管理員已經儲存過 **Model sources** 之後的 `MODEL_URL_HOSTS`（以儲存的值為準：請在網頁介面修改）。
- *ADMIN_PASSWORD is still set*：它只在第一次啟動時讀取。請把它從 `.env` 移除。

管理員也會在 **Get started → Setup health** 看到同樣的內容，並附上修正位置的連結（[檢查設定健康狀態](how-to.md#check-the-setup-health)）。

## 回報問題時要附上的資訊

```bash
forgeyard --version
sudo forgeyard agent config
sudo forgeyard agent inventory --raw --json
sudo forgeyard agent runtimes
journalctl -u forgeyard-agent -n 200 --no-pager
```

分享之前，請先移除任何私人資訊（主機名稱、位址）。
