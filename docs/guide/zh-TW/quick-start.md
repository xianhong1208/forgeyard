# 快速入門

從零開始到第一次呼叫 API，大約十分鐘，另加模型下載的時間。你需要：

- **一台伺服器**：任何一台其他機器連得到的 Linux、macOS 或 Windows（WSL）電腦，裝好 [uv](https://docs.astral.sh/uv/)（需要時它會自己取得 Python 3.13），以及用來建置網頁介面的 Node 22.12 或更新版本；或者只要有 Docker。伺服器不需要 GPU。
- **一台用來跑模型的機器**：x86_64 或 aarch64 的 Linux，搭配 NVIDIA GPU（驅動程式 575 或更新版本，以及裝好 NVIDIA Container Toolkit 的 Docker）、AMD GPU（ROCm 和 Docker），或只有 CPU。它也可以和伺服器是同一台電腦。
- 這台機器要能連上網路，用來下載幾個 Python 套件和模型。

範例中伺服器的位址是 `192.168.1.20`，請換成你自己的。

## 1. 安裝伺服器

在伺服器上執行：

```bash
git clone https://github.com/xianhong1208/forgeyard.git
cd forgeyard
cd web && npm ci && npm run build && cd ..
uv sync
```

啟動它，對網路開放，並指定其他機器要用的位址：

```bash
HOST=0.0.0.0 PUBLIC_URL=https://192.168.1.20:7860 uv run python main.py
```

會印出（還有其他幾行）：

```text
INFO forgeyard: Made Forgeyard's certificate authority in data/tls (fingerprint sha256:3f9a…).
INFO forgeyard: HTTPS with Forgeyard's own CA. CA fingerprint sha256:3f9a…
WARNING forgeyard:
================================================================
Forgeyard has no owner yet. Open https://192.168.1.20:7860/setup and enter this setup token:

    3xQ…

(also stored in data/secrets.json until setup is done)
================================================================
INFO:     Uvicorn running on https://0.0.0.0:7860 (Press CTRL+C to quit)
```

**或者用 Docker**，取代上面所有步驟（不用建置任何東西；linux/amd64 或 linux/arm64）：

```bash
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.2.0
sudo docker logs forgeyard
```

紀錄的內容相同，只是 `data/` 變成 `/data`；所有資料都存在 `forgeyard-data` volume 裡。細節、備份和升級：見[用 Docker 執行伺服器](how-to.md#run-the-server-in-docker)。

讓它保持執行。Forgeyard 存的所有東西都放在旁邊的 `data/` 裡：資料庫、憑證授權單位（`data/tls/`），以及加密機密資料的金鑰（`data/secret.key`）。請備份這個資料夾（[做法](how-to.md#back-up-and-restore)）。如果要在登出後讓伺服器繼續執行，請看[把伺服器當成服務執行](how-to.md#run-the-server-as-a-service)。

## 2. 建立擁有者並信任 CA

1. 在瀏覽器開啟 `https://192.168.1.20:7860/setup`。
2. 瀏覽器會對憑證發出警告。第一次出現是正常的：Forgeyard 自己建立了一個憑證授權單位（CA），而你的瀏覽器還不認得它。先略過警告一次（Chrome：*Advanced* → *Proceed*；Firefox：*Advanced* → *Accept the risk*）。
3. 貼上紀錄裡的設定權杖（setup token），選一個使用者名稱和密碼（至少 12 個字元），建立**擁有者**。完成後你就登入了。
4. 開啟 **Trust this server**（信任此伺服器，在右上角的帳號選單裡），把上面顯示的指紋和紀錄裡的指紋比對。下載 CA，並依照你裝置對應的步驟安裝。之後就不會再出現警告。每一台你要用來操作 Forgeyard 的裝置都要做一次。

Forgeyard 接著會顯示 **Get started**：一份接下來步驟的檢查清單，完成的項目會自動打勾。

## 3. 加入一台機器

1. 開啟 **Machines**（機器）→ **Add a machine** → **Create join command**。
2. 保持選取 **One-line install**（單行安裝），然後複製指令。它長得像這樣：

   ```bash
   curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
     sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
     echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
     curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
     sudo sh -s -- --join fyj_… --ca-fingerprint sha256:3f9a…
   ```

   裡面的權杖只能用**一次**，而且要在 **15 分鐘**內使用。
3. 把它貼到那台機器的終端機裡執行。如果你是 root，請拿掉 `sudo`。

會印出：

```text
forgeyard-ca.der: OK

==> Checking this machine
Linux x86_64, server https://192.168.1.20:7860, agent 1.2.0.

==> Checking the server's certificate authority
The CA matches sha256:3f9a….

==> Downloading the agent 1.2.0
forgeyard-1.2.0-py3-none-any.whl (335662 bytes, SHA-256 checked).
NVIDIA driver found: installing NVML support too.

==> Installing it in /opt/forgeyard/venv
Installed with uv (uv 0.9.24).
forgeyard 1.2.0, as /usr/local/bin/forgeyard (try: forgeyard agent config).

==> Account for process workloads
Created the system account 'forgeyard-run' (no password, no shell, no home).

==> Settings (/etc/forgeyard/agent.toml)
# Forgeyard agent settings, written by `forgeyard agent init` (1.2.0).
…
# GPUs: 2 x NVIDIA GeForce RTX 4090 (driver 580.95.05). Choose which ones Forgeyard may use on the Machines page.
…

==> Joining https://192.168.1.20:7860
INFO forgeyard.agent: Joined https://192.168.1.20:7860 as 'gpu-01'; credential saved to /var/lib/forgeyard-agent/credential.json (mode 0600). Trusting only the CA sha256:3f9a….

==> Service forgeyard-agent.service
● forgeyard-agent.service - Forgeyard agent
     Active: active (running)
…
Next, on https://192.168.1.20:7860:
  1. Machines: this machine shows as online within a few seconds.
  2. Change GPU access: choose which cards Forgeyard may use here.
  3. Models: deploy a model on it. The guide: https://192.168.1.20:7860/guide
```

依序發生了這些事：

1. 前三行先**不經檢查**下載伺服器的 CA（過程中不會送出任何機密），把它轉回二進位格式，再把它的 SHA-256 和指令裡的指紋比對。如果拿到的是另一個 CA（例如中間有別的機器攔截），一切會在這裡停下，並顯示 `forgeyard-ca.der: FAILED`。
2. 比對通過後，才透過以這個 CA 驗證的連線下載安裝程式。
3. 安裝程式從你的伺服器下載 agent（代理程式，版本一律和伺服器相同），檢查後連同它自己的 Python 一起裝到 `/opt/forgeyard`。
4. 它建立了 `forgeyard-run` 帳號，行程類工作負載都以這個帳號執行。工作負載絕不會以 root 執行。
5. 它根據偵測到的 GPU、Docker、systemd 寫出 `/etc/forgeyard/agent.toml`。檔案裡每個設定都有說明。
6. 它用加入權杖（join token）換到這台機器自己的憑據，存放在 `/var/lib/forgeyard-agent`（權限 0600）。
7. 它安裝並啟動了 `forgeyard-agent` 服務，這個服務會隨開機啟動，停止時也會自動重新啟動。

回到 Machines 頁面，幾秒內就會看到這台機器和它的 GPU。

其他方式：**Docker** 分頁會把 agent 當成容器執行，一個 `docker run` 就會拉取和你的伺服器同版本的 agent 映像檔（[用 Docker 執行 agent](how-to.md#run-the-agent-in-docker)）；**Manual** 分頁則使用 Forgeyard 的原始碼和一個設定檔。AMD 和純 CPU 機器的做法相同（[AMD](how-to.md#add-an-amd-machine)、[純 CPU](how-to.md#add-a-cpu-only-machine)）。

## 4. 選擇 Forgeyard 可以使用的 GPU

預設情況下，Forgeyard 可以使用機器上的每一張 GPU。如果是和別人共用的機器：

1. 在 **Machines** 找到這台機器，選擇 **Change GPU access**（變更 GPU 存取權）。
2. 勾選 Forgeyard 可以使用的卡，然後儲存。

不在清單上的卡，絕不會分配給 Forgeyard 執行的任何東西；頁面上會把它們標成 *not for Forgeyard*。就算允許所有的卡，只要儲存一次選擇，這個步驟就會打勾。

## 5. 加入一個推薦的模型

1. 開啟 **Models** → **Catalog and recipes**（目錄與 recipe）→ **Add a model**。
2. **Recommended** 列出已知可以正常運作的模型。挑一個適合你機器的：
   - GPU 至少有 8 GB 可用記憶體：**Qwen3 0.6B**（下載 1.4 GB）；
   - 只有 CPU：**Qwen3 1.7B (GGUF, for the CPU)**（下載 1.2 GB，約需 3 GB RAM）。
3. Forgeyard 會從 Hugging Face 讀取模型的資訊，但不會下載它，然後顯示一份副本需要的記憶體，以及它**目前放得下**的機器。
4. 選擇 **Add to the catalog**（加入目錄）。

## 6. 部署它

1. 選擇 **Deploy it**（部署它），或從 **Models** → **Deploy a model** 再挑選模型。
2. 為這個部署取個名字，例如 `qwen`。這個名字就是程式呼叫時要用的**模型名稱**。
3. 確認 **Runs on**（執行於）：Qwen3 0.6B 選 **GPU**，GGUF 模型選 **CPU only**（用 *Deploy it* 會自動幫你選好）。
4. 頁面會顯示估算結果和它會在哪裡執行。選擇 **Deploy**。

部署頁面會先顯示下載進度，再顯示引擎啟動。當它的副本變成 **healthy**，就代表可以用了。第一次需要的時間和下載時間差不多；之後啟動只要幾秒。你可以直接在頁面上用 **Try it** 試試看，或到 **Playground** 試。

## 7. 建立 API 金鑰

1. 開啟 **API keys** → **New key**。
2. 取個名字，例如 `laptop`，然後選擇 **Create key**。
3. 現在就複製金鑰（`fyk_…`）：它只會顯示這一次。Forgeyard 只保留它的雜湊值。

在要呼叫模型的電腦上，把金鑰存進環境變數，並取得 CA 檔案：

```bash
export FORGEYARD_API_KEY=fyk_…
curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem
```

確認它是正確的 CA：指紋必須和步驟 2 的一樣。

```bash
sed '/-----/d' forgeyard-ca.pem | base64 -d | sha256sum
```

## 8. 呼叫它

### curl

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/models \
  -H "Authorization: Bearer $FORGEYARD_API_KEY"
```

會印出：

```json
{"object":"list","data":[{"id":"qwen","object":"model","created":1794314938,"owned_by":"forgeyard"}]}
```

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen", "messages": [{"role": "user", "content": "Say hello in five words. /no_think"}], "max_tokens": 100}'
```

會印出（節錄）：

```json
{"choices":[{"finish_reason":"stop","index":0,"message":{"role":"assistant","content":"Hello, world!"}}],"model":"qwen","object":"chat.completion","usage":{"completion_tokens":7,"prompt_tokens":18,"total_tokens":25}}
```

`/no_think` 是要求 Qwen3 直接回答，不要先把思考過程寫出來。

### Python，使用 OpenAI SDK

```bash
pip install openai
```

存成 `hello.py`，和 `forgeyard-ca.pem` 放在同一個資料夾：

```python
import os
import ssl

import httpx
from openai import OpenAI

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
client = OpenAI(base_url="https://192.168.1.20:7860/v1", api_key=os.environ["FORGEYARD_API_KEY"],
                http_client=httpx.Client(verify=ca))
reply = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "Say hello in five words. /no_think"}],
    max_tokens=100,
)
print(reply.choices[0].message.content)
```

```bash
python hello.py
```

會印出：

```text
Hello, how are you?
```

任何支援 OpenAI API 的程式都是同樣的用法：把它指向 `https://192.168.1.20:7860/v1`，給它金鑰，並讓它信任 `forgeyard-ca.pem`（很多程式只要設定 `SSL_CERT_FILE=forgeyard-ca.pem` 就夠了；這個變數會取代什麼、Node.js 怎麼設定，請見[程式不信任憑證](troubleshooting.md#a-program-does-not-trust-the-certificate)）。

## 接下來

- **Usage** 會依金鑰、模型和使用者顯示請求數、token 數和延遲。
- 在 **Users** 加入其他人：他們會收到一個一次性連結，用來設定自己的密碼。角色的說明在[概念](concepts.md#roles)。
- 用同樣的方式加入更多機器；同一個部署的副本會分散到這些機器上，閘道會在它們之間做負載平衡。
- 有東西不能用？請看[疑難排解](troubleshooting.md)。
