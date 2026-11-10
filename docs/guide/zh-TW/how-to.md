# 操作指南

[快速入門](quick-start.md)之後會用到的各項工作，每一項都可以單獨進行。這裡的指令假設 agent（代理程式）是用單行安裝裝好的（服務名稱 `forgeyard-agent`，設定在 `/etc/forgeyard/agent.toml`）；Docker 和手動安裝的做法如果不同，會另外註明。

## 加入 AMD 機器

1. 在機器上安裝 ROCm（6.0 或更新版本）和 Docker。確認 ROCm 看得到顯示卡：

   ```bash
   amd-smi list
   ```

2. 用和其他機器一樣的方式加入：**Machines** → **Add a machine** → **One-line install**，然後在這台機器上執行那段指令。
3. 確認 agent 偵測到的內容：

   ```bash
   sudo forgeyard agent inventory
   ```

   它會每張卡印出一行，附上卡的 `gfx` 目標（例如 `gfx1100`；Strix Halo APU 則是 `gfx1151`，並標示為 *unified-memory*），接著每個收集器各印一行。

agent 讀取 AMD 顯示卡時，能匯入 `amdsmi` 函式庫就用它，否則用 `amd-smi` 指令，再不行就用 `rocm-smi`。如果 inventory 顯示 `amdsmi Python library not importable`，但 `amd-smi` 可以正常使用，那就什麼都不缺。如果也想用這個函式庫：

```bash
sudo uv pip install --python /opt/forgeyard/venv/bin/python /opt/rocm/share/amd_smi
sudo systemctl restart forgeyard-agent
```

在 AMD 上，模型可以用 `vllm` recipe 的 ROCm 映像檔執行，也可以用 `llama-cpp`。行程類工作負載只會在 `ROCR_VISIBLE_DEVICES` 裡拿到自己的卡，而且只有在分配到 GPU 時，才會加入擁有 `/dev/kfd` 和對應 `/dev/dri/renderD*` 的群組。

> AMD 的支援已經用從 AMD 機器錄下來的資料驗證過，但還沒在實際的 AMD 機器上檢查。如果看到任何看起來不對的地方，請附上 `forgeyard agent inventory --raw` 的輸出回報。

## 加入純 CPU 機器

沒有什麼特別的：在那台機器上執行單行安裝就好。Forgeyard 會顯示這台機器的 CPU 和 RAM，並把 CPU 模型放到它上面：

- `llama-cpp` 在 CPU 上執行 GGUF 模型，可以用容器的方式（有 Docker 時），或在沒有 Docker 的 x86_64 機器上，以一支由 agent 下載並用 SHA-256 檢查過的程式執行；
- `faster-whisper` 在 CPU 上執行語音轉文字（需要 Docker）。

部署時，選擇 **Runs on** → **CPU only**。把 **CPU threads** 設成你想讓它使用的核心數：執行緒數超過空閒的核心數，只會讓 llama.cpp 變慢，不會變快。

## 用 Docker 執行 agent

每台機器執行一個 `docker run`，取代安裝腳本。映像檔要從 Forgeyard 的 repository 建置，目前還沒有公開發布。

### 建置映像檔

在這台機器上（或在另一台機器上建置，再用 `docker save` 和 `docker load` 搬過去），在和伺服器同版本的 Forgeyard 原始碼目錄中執行：

```bash
docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .
```

### NVIDIA

需要 NVIDIA Container Toolkit（`docker run --gpus all` 能正常執行）。伺服器位址、指紋和加入權杖（join token）請從 **Machines** → **Add a machine** → **Docker** 取得，那裡會幫你填好：

```bash
docker run -d --name forgeyard-agent --restart always --network host \
  --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

接著：

```bash
docker logs -f forgeyard-agent
```

會印出（還有其他幾行）：

```text
INFO forgeyard.agent: Joined https://192.168.1.20:7860 as 'gpu-01'; credential saved to /var/lib/forgeyard-agent/credential.json (mode 0600). Trusting only the CA sha256:3f9a….
INFO forgeyard.agent: Runtime container ok: docker 28.5.2; NVIDIA GPUs via gpus
INFO forgeyard.agent: Connected to https://192.168.1.20:7860 as 'gpu-01' (9 accelerators; heartbeat every 10 s).
```

### AMD

```bash
docker run -d --name forgeyard-agent --restart always --network host \
  --device /dev/kfd --device /dev/dri --group-add video --group-add render \
  -v /opt/rocm:/opt/rocm:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

主機的 ROCm 以唯讀方式掛載，讓 agent 可以用 `amd-smi` 和 `rocm-smi` 讀取顯示卡資訊。

### 純 CPU

```bash
docker run -d --name forgeyard-agent --restart always --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  forgeyard-agent:1.1.0
```

### 為什麼要用這些選項

- **`--network host`**：模型副本在主機的 `127.0.0.1` 上監聽，前面擋著 agent 的金鑰守衛，守衛則監聽副本對外公開的連接埠。只有 agent 和主機共用網路，這兩者才能運作。這也讓 agent 可以在機器自己的位址上公開連接埠。
- **模型快取在容器內外使用相同路徑**（`-v /srv/forgeyard-models:/srv/forgeyard-models`）：agent 把模型下載到這裡，再請主機的 Docker 啟動引擎容器，並以*主機上的路徑*掛載模型。路徑不同的話，什麼都掛載不到。
- **`/var/run/docker.sock`**：模型和容器類工作負載是在主機的 Docker 上以容器執行，和 agent 並列，而不是在 agent 裡面。這個 socket 等於完全掌控主機的 Docker，所以 agent 的容器要像 agent 本身一樣被信任。
- **`forgeyard-agent` volume** 保存機器的憑據和釘選的 CA。請保留它：在重新啟動、升級或 `docker rm` 之後執行同一個 `docker run`，就會帶回同一台機器，而且不會再使用加入權杖（紀錄裡會寫明）。沒有它的話，機器就得重新加入。
- 使用 **`FORGEYARD_*` 變數**而不是旗標：它們和 [agent.toml](reference.md#agent-settings) 裡的設定相同。你也可以掛載一個檔案：`-v /etc/forgeyard/agent.toml:/etc/forgeyard/agent.toml:ro`。
- agent 在容器裡以 **root** 執行：它需要 Docker socket，並會在執行行程類工作負載時切換到無特權帳號 `forgeyard-run`。工作負載絕不會以 root 執行。
- 容器裡沒有 systemd，所以行程類工作負載用 rlimit 限制資源（映像檔已設定 `FORGEYARD_LIMITS=rlimit`）。行程類工作負載在 agent 的容器裡執行，會隨容器一起停止；模型和容器類工作負載則會繼續在主機上執行。

### 把已安裝的 agent 搬到 Docker

已經加入的機器，只要容器拿到它的狀態目錄，就能保留原本的身分：

1. 建置映像檔（見上方）。
2. 停止舊的 agent：`sudo systemctl disable --now forgeyard-agent`（安裝版），或停止你自己啟動的行程。它的容器類工作負載和模型會繼續執行。
3. 用舊的狀態目錄和同一個模型快取啟動容器，而且**不要**帶加入用的變數：

   ```bash
   docker run -d --name forgeyard-agent --restart always --network host --gpus all \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /var/lib/forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     forgeyard-agent:1.1.0
   ```

4. `docker logs -f forgeyard-agent` 會為每個正在執行的模型顯示 `Adopted replica …`：新的 agent 會接手舊 agent 啟動的容器（容器上帶有 agent 的 id，而這個 id 存在狀態目錄裡）。

舊 agent 在容器外啟動的行程類工作負載不會被接手：請先從 UI 停止它們。

## 在共用機器上限制 GPU

1. 開啟 **Machines**，找到這台機器，選擇 **Change GPU access**（變更 GPU 存取權）。
2. 只勾選 Forgeyard 可以使用的卡，例如第 4 張，然後選擇 **Save**。

從此之後：

- 不管工作負載或部署要求什麼，Forgeyard 執行的任何東西都不會分配到其他卡；
- 其他卡會顯示 *not for Forgeyard*；
- 記憶體以每張允許使用的卡回報的**可用**量計算，所以別人在同一張卡上的使用量會被考慮進去。

正在使用中的卡不能從清單中移除：請先停止在它上面執行的東西。每次變更都會記錄在稽核紀錄裡，包含變更前後的內容。

如果要讓模型和別人的工作共用同一張卡，部署時選擇 **GPU sharing** → **Share VRAM**：這樣它只會保留估算的記憶體量，vLLM 也會被設定成只使用這麼多（請看 [vLLM 在共用的卡上無法啟動](troubleshooting.md#vllm-does-not-start-on-a-shared-card)）。

## 使用自己的憑證或反向代理

**使用自己的憑證**（公司的 CA，或真實網域的公開憑證）。在伺服器的 `.env` 裡設定：

```bash
TLS_CERT_FILE=/etc/forgeyard/tls/fullchain.pem
TLS_KEY_FILE=/etc/forgeyard/tls/privkey.pem
TLS_CA_FILE=/etc/forgeyard/tls/company-ca.pem
```

`TLS_CA_FILE` 是簽發這張憑證的 CA；agent 會改為釘選這個 CA，**Trust this server** 也會提供它。如果是公開信任的憑證，就不要設定這一項：這樣加入指令不會帶指紋，agent 會使用系統的信任存放區。檔案有變更時（例如憑證更新後），伺服器會重新載入。

**放在已經處理 HTTPS 的反向代理後面**（nginx、Caddy）：

```bash
TLS=off
HOST=127.0.0.1
PUBLIC_URL=https://forge.example.com
TRUSTED_PROXIES=127.0.0.1
```

代理必須轉送 WebSocket（agent 會用到），不能緩衝串流回應，而且要允許長時間讀取（600 秒）和大型上傳（32 MB，給音訊檔用）。nginx：

```nginx
server {
    listen 443 ssl;
    server_name forge.example.com;
    ssl_certificate     /etc/letsencrypt/live/forge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/forge.example.com/privkey.pem;
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Caddy（它會自己取得並更新憑證）：

```caddyfile
forge.example.com {
    request_body {
        max_size 32MB
    }
    reverse_proxy 127.0.0.1:7860 {
        flush_interval -1
        transport http {
            read_timeout 600s
        }
    }
}
```

用 Forgeyard 自己的 CA 加入的 agent，需要切換到新憑證一次，憑據會保留：

```bash
sudo forgeyard agent trust --server https://forge.example.com
```

（如果是公司的 CA，請加上 `--ca-file company-ca.pem`），然後執行 `sudo systemctl restart forgeyard-agent`。

## 把伺服器當成服務執行

讓伺服器隨電腦開機啟動，停止時自動重新啟動。以 root 執行，假設原始碼在 `/opt/forgeyard-server`，設定在 `/opt/forgeyard-server/.env`：

```bash
sudo useradd --system --home-dir /opt/forgeyard-server --shell /usr/sbin/nologin forgeyard
sudo chown -R forgeyard: /opt/forgeyard-server
sudo tee /etc/systemd/system/forgeyard.service > /dev/null <<'EOF'
[Unit]
Description=Forgeyard server
Wants=network-online.target
After=network-online.target

[Service]
User=forgeyard
WorkingDirectory=/opt/forgeyard-server
ExecStart=/opt/forgeyard-server/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now forgeyard
```

伺服器會從它的工作目錄讀取 `.env`，資料存在該目錄下的 `data/`，紀錄寫到 journal：`journalctl -u forgeyard -f`。

## 升級伺服器和 agent

1. 先**備份**伺服器（[做法](#back-up-and-restore)）。
2. **伺服器**：在它的資料夾裡執行：

   ```bash
   git pull
   cd web && npm ci && npm run build && cd ..
   uv sync
   ```

   然後重新啟動它（`sudo systemctl restart forgeyard`）。資料庫的變更會在啟動時自動執行。請閱讀 CHANGELOG，看看有沒有需要額外處理的步驟。
3. **agent**：一台一台來。執行中的 agent 重新啟動時，會保留它的工作負載。
   - **用單行安裝裝的**：再執行一次，但不加 `--join`。它會安裝伺服器的新版本，並保留設定和憑據：

     ```bash
     curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
       sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
       echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
       curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
       sudo sh -s -- --ca-fingerprint sha256:3f9a…
     ```

   - **Docker**：建置新的映像檔，然後執行 `docker rm -f forgeyard-agent`，再用新的 tag 執行同一個 `docker run`。volume 會保留這台機器。
   - **手動安裝**：`git pull`、`uv sync`，然後重新啟動 agent。

**Machines** 頁面會顯示每個 agent 的版本。伺服器升級後，請盡快升級 agent：太舊的 agent 可能缺少新版伺服器需要的功能。

## 備份與還原

**伺服器上要備份的東西**，除非你搬過位置，否則都在它的 `data/` 資料夾裡：

| 項目 | 原因 |
|---|---|
| `data/forgeyard.db`（或你的 PostgreSQL 資料庫） | Forgeyard 知道的一切：使用者、機器、模型、部署、金鑰、用量 |
| `data/secret.key` | 它負責加密儲存的機密資料（Hugging Face 權杖、副本的金鑰、工作負載的環境變數值）。請和資料庫備份**分開**存放 |
| `data/tls/` | 每台裝置和每個 agent 信任的 CA。少了它，每台裝置和每個 agent 都得重新信任一個新的 CA |
| `.env` | 你的設定 |

在伺服器執行中取得 SQLite 資料庫的一致副本：

```bash
uv run --no-sync python -c "import sqlite3; sqlite3.connect('data/forgeyard.db').backup(sqlite3.connect('/backup/forgeyard.db'))"
cp -a data/tls data/secret.key .env /backup/
```

PostgreSQL：照常使用 `pg_dump`。

**每台機器上**（選擇性）：`/var/lib/forgeyard-agent`（憑據；少了它，機器就得用新的權杖重新加入）和 `/etc/forgeyard/agent.toml`。模型快取隨時可以重新下載。

**還原**：停止伺服器，把檔案放回 `data/`（以及 `.env`），再啟動它。如果 `secret.key` 錯了或不見了，伺服器會拒絕啟動並說明原因，而不是把機密資料弄丟。如果金鑰真的遺失了，請停止伺服器，然後用以下指令重新開始：

```bash
uv run --no-sync python -m forgeyard secrets reset --yes
```

它會產生一把新的金鑰，並捨棄舊金鑰加密的內容：gated 模型的 Hugging Face 權杖要重新輸入，模型副本會用新的內部金鑰重新啟動，環境變數值遺失的工作負載則必須重新建立（它們的事件裡會說明）。

## 搬移模型快取

例如從 `/srv/forgeyard-models` 搬到較大磁碟上的 `/data/forgeyard-models`：

1. 停止在這台機器上執行的部署（各部署的頁面 → **Stop**）。
2. 停止 agent，然後複製快取（快取使用相對連結，所以直接複製就可以）：

   ```bash
   sudo systemctl stop forgeyard-agent
   sudo rsync -a /srv/forgeyard-models/ /data/forgeyard-models/
   ```

3. 在 `/etc/forgeyard/agent.toml` 的 `[models]` 底下，設定 `cache = "/data/forgeyard-models"`，然後執行：

   ```bash
   sudo forgeyard agent config --get models.cache
   sudo systemctl start forgeyard-agent
   ```

4. 重新啟動這些部署。它們幾秒內就會啟動：不會重新下載任何東西。
5. 一切都正常運作後，刪除舊的資料夾。

如果用 Docker，請把快取 volume 的兩邊都改掉（`-v /data/forgeyard-models:/data/forgeyard-models`），也要改 `FORGEYARD_MODEL_CACHE`，然後重新建立容器。

## 輪替金鑰

- **API 金鑰**：在 **API keys** 建立一把新的金鑰，放進你的程式，然後把舊的 **Revoke**（撤銷）。被撤銷的金鑰從下一個請求開始就失效。
- **機器的憑據**：撤銷這台機器，建立一個加入指令，然後在機器上用 `--rejoin` 重新加入：

  ```bash
  sudo forgeyard agent join --rejoin --join-token fyj_…
  sudo systemctl restart forgeyard-agent
  ```

  它會以一台新機器出現在頁面上；請移除舊的那一筆。
- **CA**：只有在必須更換時才換（它的金鑰外洩，或你要改用自己的憑證）。每台裝置都要重新信任新的 CA，每個 agent 都要用新的指紋執行 `forgeyard agent trust`（管理員可以在 **Trust this server** 看到新指紋）。
- **`secret.key`** 目前還不能就地輪替。請像保護密碼一樣保護它（權限 0600，和資料庫備份分開存放）。

## 撤銷機器

1. 先停止或搬走在那台機器上執行的東西：只放在那台機器上的部署會無處可去。
2. **Machines** → 該機器 → **Revoke**。它的憑據會失效，agent 會被中斷連線並結束（安裝的服務不會再重新啟動它），它的副本也會從閘道上移除。
3. **Remove** 會把它從清單中移除。如果要把這台機器加回來，請用新的權杖重新加入（`forgeyard agent join --rejoin --join-token fyj_…`）。

伺服器已經連不到被撤銷機器上的 agent，所以不會停止那台機器上仍在執行的工作負載。請在機器上自行停止它們：`docker ps --filter label=forgeyard.agent` 會列出這些容器。

## 解除安裝 agent

在機器上執行和安裝時相同的指令，但把 `--join` 換成 `--uninstall`：

```bash
curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | sudo sh -s -- --uninstall
```

它會停止並移除服務和 `/opt/forgeyard`，但保留 `/etc/forgeyard/agent.toml`、憑據和模型快取，所以重新安裝時會帶回同一台機器。加上 `--purge` 會連設定和憑據一起刪除（之後請到 **Machines** 頁面移除這台機器）。模型快取和 `forgeyard-run` 帳號永遠不會被刪除：如果需要，請自行移除。

不透過伺服器，手動移除：

```bash
sudo systemctl disable --now forgeyard-agent
sudo rm -f /etc/systemd/system/forgeyard-agent.service /usr/local/bin/forgeyard
sudo rm -rf /opt/forgeyard
```
