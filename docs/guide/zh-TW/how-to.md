# 操作指南

[快速入門](quick-start.md)之後會用到的各項工作，每一項都可以單獨進行。這裡的指令假設 agent（代理程式）是用單行安裝裝好的（服務名稱 `forgeyard-agent`，設定在 `/etc/forgeyard/agent.toml`）；Docker 和手動安裝的做法如果不同，會另外註明。

## 加入 AMD 機器

1. 在機器上安裝 ROCm（6.0 或更新版本）和 Docker。確認 ROCm 看得到顯示卡：

   ```bash
   amd-smi list
   ```

2. 用和其他機器一樣的方式加入：**機器** → **加入機器** → **一行安裝**，然後在這台機器上執行那段指令。
3. 確認 agent 偵測到的內容：

   ```bash
   sudo forgeyard agent inventory
   ```

   它會每張卡印出一行，附上卡的 `gfx` 目標（例如 `gfx1100`；Strix Halo APU 則是 `gfx1151`，並標示為 *unified-memory*）和 ROCm 版本，接著每個收集器各印一行。以 BIOS 切出 96 GiB 的 Strix Halo 為例：

   ```text
   amd     0  AMD Radeon 8060S  arch=gfx1151  32.6 GiB free of 96.0 GiB  rocm=7.0.2 unified-memory
   ```

agent 讀取 AMD 顯示卡時，能匯入 `amdsmi` 函式庫就用它（先找 agent 自己的 Python，找不到就用 ROCm 附在 `/opt/rocm/share/amd_smi` 的那一份），否則用 `amd-smi` 指令，再不行就用 `rocm-smi`。如果 inventory 顯示 `amdsmi … not importable` 或 `failed to load`，但 `amd-smi` 可以正常使用，那就什麼都不缺：同樣的數值會從指令讀到。

每張卡顯示的內容：

- **名稱：**卡的市售名稱。APU 的名稱常常只是「AMD Radeon Graphics」，這時改用 CPU 型號裡寫的 GPU（「… w/ Radeon 8060S」）。
- **ROCm 版本**（這裡是 `rocm=`，Machines 頁面上寫「ROCm」），放在 NVIDIA 卡顯示驅動程式版本的位置。核心驅動程式的版本保留在卡的詳細資料裡。
- **記憶體：**APU 取 VRAM 切割區（在 BIOS 設定）和 GTT 兩者中較大的那個，和 `amd-smi monitor` 顯示的一樣。GTT 就是這台機器本身的 RAM，所以卡回報的是 GTT 時，Forgeyard 把卡和 RAM 當成同一個記憶體池計算（1.4.5）：GPU 上的模型預留的記憶體不會再承諾給 CPU 上的模型，卡能提供的也不會超過 RAM 剩下的。切割區是 Linux 看不到的記憶體，所以用切割區時兩者分開計算。
- **識別鍵（key）：**卡有序號時用它的 UUID，否則用 PCI 位址（APU 沒有序號，所以每台機器上的 UUID 都一樣）。用 Forgeyard 1.1.0 或更舊版本加入的機器，識別鍵改變後 GPU 允許清單仍會保留。

在 AMD 上，模型可以用 `vllm` recipe 的 ROCm 映像檔執行，也可以用 `llama-cpp`。行程只會在 `ROCR_VISIBLE_DEVICES` 裡拿到自己的卡，而且只有在分配到 GPU 時，才會加入擁有 `/dev/kfd` 和對應 `/dev/dri/renderD*` 的群組。

`llama-cpp` 會依每張卡選兩個映像檔之一：

| 卡 | `llama-cpp` 用 | 原因 |
|---|---|---|
| Instinct（`gfx908`、`gfx90a`、`gfx942`，包含 MI300A），以及它的 ROCm 映像檔有編譯的獨立 Radeon 卡（`gfx1030`、`gfx1100`、`gfx1101`、`gfx1102`、`gfx1200`、`gfx1201`） | **ROCm**（variant `rocm`，7.7 GB 的映像檔） | Instinct 卡沒有 Vulkan |
| 其他所有 AMD GPU：統一記憶體的 APU（Strix Halo `gfx1151`、Strix Point `gfx1150`、Phoenix `gfx1103`…），以及 ROCm 映像檔沒有編譯的獨立卡（`gfx1031`、`gfx1032`…） | **Vulkan**（variant `vulkan`，0.3 GB、使用 Mesa RADV 驅動的映像檔） | 映像檔裡不需要 ROCm；在實際的 Strix Halo 上用的就是這條路 |

Vulkan 容器只會拿到自己那張卡的 render node（`/dev/dri/renderD*`）和擁有它的群組，不會拿到 `/dev/kfd`。它的記憶體和 ROCm 一樣，用同一個記憶體池檢查：APU 取 carve-out 和 GTT 中較大的那個。部署的事件會寫出它用哪個 variant（`… (vulkan; reserves …)`）。

部署本身不能選 variant。要讓某張卡改用另一個，就加一個自訂推論引擎（**模型 → 推論引擎 → 加入自訂推論引擎**，*從內建推論引擎開始* 選 `llama-cpp`）：複本會換一個名字；`variants` 底下只留你要的那個，再儲存。自訂推論引擎的卡片上有 **編輯** 和 **刪除**（有部署在用時，刪除會被拒絕）。

**從自訂的 `llama-cpp-vulkan` 配方換過來。** 1.3 之前，Vulkan 要用自訂配方；內建的現在取代了它。部署會一直用建立時的配方，所以請用 `llama-cpp` 重新部署這個模型，再刪掉舊的部署；沒有部署在用之後，再刪掉自訂配方。

**連不到 `ghcr.io` 的機器**用檔案拿到映像檔。在連得到的機器上，用 tag 拉取，並確認它就是釘選的那一個（這個 tag 的 `DIGEST` 必須是配方裡的 `sha256:784bb87c…`）：

```bash
sudo docker pull ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d
sudo docker images --digests ghcr.io/ggml-org/llama.cpp
sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d | gzip > llama-vulkan.tar.gz
```

把檔案複製過去，在那裡用 `gunzip -c llama-vulkan.tar.gz | sudo docker load` 載入。配方寫的映像檔是 `tag@sha256:digest`，Docker 會用 digest 找它。Docker 用傳統的映像檔儲存方式時（`docker info` 顯示的 storage driver 是 `overlay2` 之類），載入的映像檔保留 tag，但不保留拉取時的 digest（`docker images --digests` 顯示 `<none>`）：`docker run` 找不到它，會試著從 `ghcr.io` 拉取，副本就帶著這個錯誤失敗。用 containerd 映像檔儲存時（`docker info` 裡有 `driver-type: io.containerd.snapshotter.v1`），載入後用 `docker images --digests` 檢查。沒有 digest 的話，就用一個自訂配方，映像檔寫載入的 tag、不加 digest：Docker 會在本機找到它，不會去拉取。

> AMD 的支援已經在實際的 Strix Halo（Radeon 8060S，gfx1151）、ROCm 7.0.2 上檢查過：硬體偵測（透過 `amdsmi` 函式庫、`amd-smi` 和 `rocm-smi`）、加入伺服器、GPU 允許清單，以及用 llama.cpp 的 Vulkan 版本透過 `/v1` 提供模型：先是自訂配方（build b11977），後來是內建的 Vulkan variant（build b12242），映像檔從檔案載入。還沒在實際硬體上檢查的：llama.cpp 1.0.0 上的內建 Vulkan variant、`vllm` 和 `llama-cpp` 配方的 ROCm 映像檔，以及獨立 AMD 顯示卡（目前只用錄下來的資料驗證過）。如果看到任何看起來不對的地方，請附上 `forgeyard agent inventory --json --raw` 的輸出回報。

## 加入純 CPU 機器

沒有什麼特別的：在那台機器上執行單行安裝就好。Forgeyard 會顯示這台機器的 CPU 和 RAM，並把 CPU 模型放到它上面：

- `llama-cpp` 在 CPU 上執行 GGUF 模型，可以用容器的方式（有 Docker 時），或在沒有 Docker 的 x86_64 機器上，以一支由 agent 下載並用 SHA-256 檢查過的程式執行；
- `faster-whisper` 在 CPU 上執行語音轉文字（需要 Docker）。

部署時，選擇 **執行於** → **僅 CPU**。把 **CPU threads** 設成你想讓它使用的核心數：執行緒數超過空閒的核心數，只會讓 llama.cpp 變慢，不會變快。

## 用 Docker 執行 agent

每台機器執行一個 `docker run`，取代安裝腳本。每個版本發布時，都會把 agent 映像檔發布到 Docker Hub 的 `docker.io/tonyhuang1101/forgeyard-agent`，支援 linux/amd64 和 linux/arm64（AMD 顯示卡只能用 amd64：ROCm 只支援 x86_64），tag 就是該版本的版本號。指令會拉取和你的伺服器相符的那一版。

**機器** → **加入機器** → **Docker** 會幫你產生這個指令：選好機器的顯示卡（NVIDIA、AMD 或 僅 CPU），再填好指令上方的表單。指令會隨著你輸入即時更新，**複製** 複製的就是畫面上顯示的內容，填過的值也會記在這個瀏覽器裡，下一台機器直接沿用：

- **那台機器上的模型目錄**（預設 `/srv/forgeyard-models`）：放模型的目錄，要選空間夠大的磁碟。它會同時填進 `-v` 冒號的兩邊和 `FORGEYARD_MODEL_CACHE`。必須是絕對路徑，不能有空白、引號或冒號。
- **機器名稱**（選填）：這台機器在 **機器** 上顯示的名稱；留空就用它的主機名稱。可以用英文字母、數字、`.`、`_` 和 `-`，最多 64 個字元。填了會加上 `FORGEYARD_NAME`。
- **伺服器位址（那台機器連過來用的）**（預設是這台伺服器的 `PUBLIC_URL`；`PUBLIC_URL` 空白時，則是你開啟這個頁面時使用的位址）：機器要用另一個位址連到伺服器時，就改這一欄：透過 VPN、轉送的連接埠（`https://127.0.0.1:7860`）或另一個區網位址。如果這個主機名稱不在伺服器的憑證裡，頁面會提醒你：把它加進 `TLS_EXTRA_NAMES` 再重新啟動伺服器，或改用憑證裡有的名稱。
- **進階 → 連接埠範圍**（`FORGEYARD_PORT_RANGE`，預設 `30000-32767`）：服務和模型副本使用的主機連接埠。只有部分連接埠連得到這台機器時才需要設定，例如防火牆開放的 `40000-40099`。伺服器必須連得到這些連接埠。應用程式每公開一個連接埠就佔用範圍內的一個連接埠；模型副本（或公開在 `/x/` 的應用程式）佔用兩個：一個給 agent 的金鑰守門員，一個給守門員後面的推論引擎。Forgeyard 會計算這些連接埠，所以 3 個連接埠的範圍只放得下一個模型：範圍用完的機器會被略過，沒有其他地方可去的副本會等待（[沒有空的主機連接埠](troubleshooting.md#a-replica-waits-no-free-host-port)）。

欄位的值如果 agent 不接受，頁面會顯示錯誤訊息，而不是產生指令。映像檔的 tag 就是伺服器自己的版本，所以 agent 和伺服器的版本永遠一致。管理員可以用 [`AGENT_IMAGE`](reference.md#machines) 讓指令改用鏡像站、私有 registry 或指定的 digest。和頁面一樣，這裡的指令開頭都有 `sudo`；如果是 root，或你的帳號已經在 `docker` 群組裡，就把它拿掉。

連不到 Docker Hub 的機器，可以用檔案的方式取得映像檔，或自己建置：見[離線機器，或自己建置映像檔](#offline-machines-or-build-it-yourself)。

### NVIDIA

需要 NVIDIA Container Toolkit（`docker run --gpus all` 能正常執行）。表單維持預設值時，頁面產生的指令長這樣（伺服器位址、指紋和加入權杖（join token）會換成你的）：

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  --gpus all \
  $(for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]*; do [ -e "$d" ] && printf -- '--device %s ' "$d"; done) \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  docker.io/tonyhuang1101/forgeyard-agent:1.5.1
```

`$(for d in …)` 那一行會再用 `--device` 傳入主機上每個 NVIDIA 裝置節點（`/dev/nvidiactl`、`/dev/nvidia-uvm`、`/dev/nvidia-uvm-tools`、`/dev/nvidia-modeset` 和每個 `/dev/nvidiaN`），主機沒有的會跳過。Docker 可以同時接受它們和 `--gpus all`，而且這很重要：`--gpus` 給的裝置是在 Docker 和 systemd 不知道的情況下設定的，主機下一次執行 `systemctl daemon-reload` 時，容器裡的新行程就會失去它們；用 `--device` 給的則會留著（[機器上的新行程打不開它的 GPU](troubleshooting.md#new-processes-on-a-machine-cannot-open-its-gpus)）。如果還沒有 `/dev/nvidia-uvm`（主機開機後還沒跑過 CUDA 程式），先執行 `sudo nvidia-modprobe -u -c=0`。設定好 CDI（`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`）的話，可以改用 `--device nvidia.com/gpu=all` 取代這兩者：CDI 會宣告這些裝置，所以它們也會留著。

接著：

```bash
sudo docker logs -f forgeyard-agent
```

會印出（還有其他幾行）：

```text
INFO forgeyard.agent: Joined https://192.168.1.20:7860 as 'gpu-01'; credential saved to /var/lib/forgeyard-agent/credential.json (mode 0600). Trusting only the CA sha256:3f9a….
INFO forgeyard.agent: Runtime container ok: docker 28.5.2; NVIDIA GPUs via gpus
INFO forgeyard.agent: Connected to https://192.168.1.20:7860 as 'gpu-01' (9 accelerators; heartbeat every 10 s).
```

### AMD

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  --device /dev/kfd --device /dev/dri \
  --group-add "$(getent group video | cut -d: -f3)" \
  --group-add "$(getent group render | cut -d: -f3)" \
  -v /opt/rocm:/opt/rocm:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  docker.io/tonyhuang1101/forgeyard-agent:1.5.1
```

主機的 ROCm 以唯讀方式掛載，讓 agent 可以用 `amdsmi`、`amd-smi` 和 `rocm-smi` 讀取顯示卡資訊。群組要用主機上的**編號**指定：Docker 會到映像檔裡查群組*名稱*，而映像檔裡沒有 `render` 群組（`Unable to find group render`）。如果是沒有 `render` 群組的舊系統（`getent group render` 什麼都沒印），就把那個 `--group-add` 拿掉。

加入之前想先確認 agent 看到什麼，可以用同樣的裝置執行它的 `inventory` 指令（映像檔的指令本身就是 `forgeyard agent`，所以只要加上 `inventory`）：

```bash
sudo docker run --rm --device /dev/kfd --device /dev/dri \
  --group-add "$(getent group video | cut -d: -f3)" --group-add "$(getent group render | cut -d: -f3)" \
  -v /opt/rocm:/opt/rocm:ro \
  docker.io/tonyhuang1101/forgeyard-agent:1.5.1 inventory
```

### 純 CPU

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  docker.io/tonyhuang1101/forgeyard-agent:1.5.1
```

### 為什麼要用這些選項

- **`--network host`**：模型副本在主機的 `127.0.0.1` 上監聽，前面擋著 agent 的金鑰守門員，守門員則監聽副本對外公開的連接埠。只有 agent 和主機共用網路，這兩者才能運作。這也讓 agent 可以在機器自己的位址上公開連接埠。
- **模型快取在容器內外使用相同路徑**（`-v /srv/forgeyard-models:/srv/forgeyard-models`）：agent 把模型下載到這裡，再請主機的 Docker 啟動推論引擎容器，並以*主機上的路徑*掛載模型。路徑不同的話，什麼都掛載不到。
- **`/var/run/docker.sock`**：模型和容器類應用程式是在主機的 Docker 上以容器執行，和 agent 並列，而不是在 agent 裡面。這個 socket 等於完全掌控主機的 Docker，所以 agent 的容器要像 agent 本身一樣被信任。
- **`forgeyard-agent` volume** 保存機器的憑據和釘選的 CA。請保留它：在重新啟動、升級或 `docker rm` 之後執行同一個 `docker run`，就會帶回同一台機器，而且不會再使用加入權杖（紀錄裡會寫明）。沒有它的話，機器就得重新加入。
- 使用 **`FORGEYARD_*` 變數**而不是旗標：它們和 [agent.toml](reference.md#agent-settings) 裡的設定相同。你也可以掛載一個檔案：`-v /etc/forgeyard/agent.toml:/etc/forgeyard/agent.toml:ro`。
- agent 在容器裡以 **root** 執行：它需要 Docker socket，並會在執行行程時切換到無特權帳號 `forgeyard-run`。應用程式和推論引擎絕不會以 root 執行。
- 容器裡沒有 systemd，所以行程用 rlimit 限制資源（映像檔已設定 `FORGEYARD_LIMITS=rlimit`）。行程在 agent 的容器裡執行，會隨容器一起停止；模型和容器類應用程式則會繼續在主機上執行。

### 離線機器，或自己建置映像檔

頁面把這些放在 **進階／離線** 底下。連不到 Docker Hub 的機器（沒有網路，或被 DNS 過濾），可以用檔案取得映像檔。在連得到的機器上拉取並存成檔案：

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.1 && \
  sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.5.1 | gzip > forgeyard-agent.tar.gz
```

把 `forgeyard-agent.tar.gz` 複製到新機器（`scp` 或 USB 隨身碟），在那裡載入：

```bash
gunzip -c forgeyard-agent.tar.gz | sudo docker load
```

如果兩台機器的架構不同（x86_64 和 arm64），在 `docker pull` 加上 `--platform linux/arm64` 或 `--platform linux/amd64`。之後照原樣執行頁面上的指令即可：Docker 會在本機找到映像檔，不會再去拉取。如果 `AGENT_IMAGE` 指定了 digest，指令最後一行要改用 tag 而不是 digest：載入的映像檔只保留 tag，不保留拉取時用的 digest（頁面上也會提醒）。

或者在那台機器上，用和伺服器同版本的 Forgeyard 原始碼，以相同名稱建置映像檔：

```bash
sudo docker build -f Dockerfile.agent -t docker.io/tonyhuang1101/forgeyard-agent:1.5.1 .
```

沒有指定已發布映像檔的伺服器（見[容器映像檔](reference.md#container-images)）會在指令裡使用本機建置的 `forgeyard-agent:1.5.1`，頁面的第一步就是這個 `docker build`。

### 把已安裝的 agent 搬到 Docker

已經加入的機器，只要容器拿到它的狀態目錄，就能保留原本的身分：

1. 拉取映像檔：`sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.1`（或像上面那樣用檔案帶過去）。
2. 停止舊的 agent：`sudo systemctl disable --now forgeyard-agent`（安裝版），或停止你自己啟動的行程。它的容器類應用程式和模型會繼續執行。
3. 用舊的狀態目錄和同一個模型快取啟動容器，而且**不要**帶加入用的變數：

   ```bash
   sudo docker run -d --name forgeyard-agent --restart always --network host --gpus all \
     $(for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]*; do [ -e "$d" ] && printf -- '--device %s ' "$d"; done) \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /var/lib/forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     docker.io/tonyhuang1101/forgeyard-agent:1.5.1
   ```

4. `sudo docker logs -f forgeyard-agent` 會為每個正在執行的模型顯示 `Adopted replica …`：新的 agent 會接手舊 agent 啟動的容器（容器上帶有 agent 的 id，而這個 id 存在狀態目錄裡）。

舊 agent 在容器外啟動的行程不會被接手：請先從 UI 停止它們。

## 讓沒有網路的機器執行模型

自己沒有網路的機器（在防火牆後面、DNS 被過濾，或只能透過 SSH tunnel 連到），執行模型的方式和其他機器一樣：它只需要連得到伺服器。模型、推論引擎的壓縮檔和推論引擎的映像檔都經由伺服器送過去，或來自已經有它們的另一台機器，agent 也會像直接下載一樣檢查每個檔案。本節從加入機器到部署好模型，依序走過一遍，細節請看各段連結的章節。範例中，`amd-01` 沒有網路，`gpu-01` 有。

請讓它跑和伺服器同版本的 agent。經由伺服器下載需要 1.4；把檔案送給其他機器需要 1.4.1；更新指令、計算空的連接埠和移除推論引擎的映像檔需要 1.4.2。agent 太舊而做不到某件事時，機器的頁面會告訴你。

### 加入機器

1. **agent 只需要連到伺服器。** 它由機器往外連，走 HTTPS 連到伺服器的位址（沒改過的話是 port 7860），之後下載的一切也都走這條連線。照常加入機器：**機器** → **加入機器**。連不到 Docker Hub 的機器，請用檔案把 agent 映像檔帶過去（[離線機器，或自己建置映像檔](#offline-machines-or-build-it-yourself)）。
2. **伺服器要連得到這台機器的連接埠範圍。** 模型副本在 agent 連接埠範圍（`FORGEYARD_PORT_RANGE`，預設 `30000-32767`）內的主機連接埠上監聽，閘道會連到這些連接埠。請在防火牆開放這個範圍，或選一個小範圍並轉送它。
3. **透過 SSH tunnel** 時，一條連線就能承載兩個方向。從伺服器所在的主機執行：`-R` 把伺服器的連接埠帶到機器上，每個 `-L` 把範圍內的一個連接埠帶回來。範圍是 `33000-33002` 時：

   ```bash
   ssh -N -R 7860:127.0.0.1:7860 \
     -L 33000:127.0.0.1:33000 -L 33001:127.0.0.1:33001 -L 33002:127.0.0.1:33002 \
     USER@amd-01
   ```

   接著加入機器時，把 **伺服器位址（那台機器連過來用的）** 設成 `https://127.0.0.1:7860`，**進階 → 連接埠範圍** 設成 `33000-33002`（[表單說明](#run-the-agent-in-docker)）。

排程會計算範圍內空的連接埠。一個模型副本佔用兩個（agent 的金鑰守門員和守門員後面的推論引擎），所以三個連接埠放得下一個模型；範圍用完的機器會被略過，沒有其他地方可去的副本會等待（[沒有空的主機連接埠](troubleshooting.md#a-replica-waits-no-free-host-port)）。

### 選擇它的網路存取方式

在機器的頁面（admin）的 **快取中的模型** 底下，設定 **下載時的網路連線**（[細節](#give-an-offline-machine-models-through-the-server)）：

- **一律經由伺服器**：給完全沒有網路的機器，例如 `amd-01`。每個模型檔、推論引擎的壓縮檔和有釘選的推論引擎映像檔都經由伺服器取得。agent 不會先試來源，所以不用先等一次失敗，這台機器也完全不會收到 Hugging Face 權杖。
- **自動**（預設）：給平常連得到網路的機器。它先直接下載；一旦這台機器連不到來源（名稱解析失敗、連線被拒、被重設或逾時、網路無法連線），這次下載剩下的部分，以及之後一小時內從那個主機或 registry 的下載，都改經由伺服器。HTTP 錯誤（檔案不存在、權杖被拒）或 checksum 不符不會切換：伺服器也會得到同樣的結果。
- **只直接下載**：永遠不經由伺服器。這種機器連不到來源時，一小時內排程會把它排在最後（[機器連不到 Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face)）。

經由伺服器的每個位元組都會經過伺服器的網路兩次，所以連得到網路的機器請維持 **自動** 或 **只直接下載**。

### 把模型送到機器上

照常部署，或用 **下載到…** 預先把模型放進機器的模型快取，預設也會一起抓推論引擎（[部署之前先下載模型](#download-a-model-before-deploying-it)）。不論哪一種，每個檔案都經由伺服器，來自以下兩個地方之一：

- **另一台機器的快取**：agent 是 1.4.1 以後的機器（`gpu-01`）快取裡有這個模型時，它把檔案送給伺服器，伺服器再轉過去。那台機器同樣只往外連，不開任何 port。只有在沒有機器能送這個檔案時，才會用到網際網路（[從另一台機器](#give-an-offline-machine-models-through-the-server)）。
- **網際網路**：伺服器從 Hugging Face 或模型的 URL 下載檔案，再轉過去。受限模型需要伺服器的 Hugging Face 權杖（**模型 → 來源**）或部署自己的權杖。

是哪一種，可以在這些地方看到：

- 進度，在部署的頁面、目錄裡模型的下方和機器的頁面上：從網際網路是 *經由伺服器下載模型：…/…*，從那台機器的快取是 *經由伺服器從 gpu-01 下載模型：…/…*，完成時是 *…下載完成，經由伺服器* 或 *…下載完成，經由伺服器，來自 gpu-01*；
- **下載到…**，開始之前在機器那一行顯示 *經由伺服器* 或 *經由伺服器，來自 gpu-01*；
- 下載的 API：`via` 是 `direct`、`server` 或 `machine`，送出的機器在 `source` 裡（[參考資料](reference.md#download-a-model-ahead-through-the-api)）；
- 伺服器的 log，每個檔案一行：*Relayed … to amd-01: …*，或 *… from gpu-01 to amd-01: …*。

推論引擎的壓縮檔（例如 CPU 機器上 llama.cpp 的 process 變體）一律經由伺服器從網際網路取得：agent 解開壓縮檔後就會刪掉它，所以沒有機器有壓縮檔可以送。

### 把推論引擎的映像檔送到機器上

- **有釘選的映像檔會自動經由伺服器送過去。** 每個內建 recipe 都用 digest 釘選它的映像檔，Forgeyard 自己的 vision 映像檔也是（1.4.1 起）。伺服器從 registry 讀取適合那台機器平台的映像檔，把各層轉過去；agent 逐一檢查、載入映像檔，只有在符合記錄的 id 時才執行。進度會顯示 *經由伺服器拉取映像檔：…/…，…/… 層*（[細節](#give-an-offline-machine-models-through-the-server)）。所以 vision 模型（[部署物件偵測模型](#deploy-an-object-detection-model)）在這種機器上的部署方式和在其他地方一樣。
- **只用 tag 指定的映像檔**（自訂 recipe 的，或你自己建置的）沒有記錄的 digest 可以比對，所以不能經由伺服器。請從機器的頁面 → **推論引擎的映像檔** 放上去：
  - **上傳映像檔**：從你的電腦上傳 `docker save` 存成的檔案（[把推論引擎的映像檔上傳到機器](#upload-an-engine-image-to-a-machine)）；
  - **從其他機器複製**：複製 `gpu-01` 的容器引擎裡已經有的映像檔，不需要檔案（[從另一台機器複製推論引擎的映像檔](#copy-an-engine-image-from-another-machine)）。
- recipe 換成較新的映像檔後，在同一頁**移除舊的映像檔**：那台機器上沒有部署在用的映像檔才能 **移除**（[推論引擎的映像檔](#engine-images)）。
- 1.4 之前的 agent 都做不到這些：請用檔案把映像檔帶過去，再用 `docker load` 載入（[離線機器](#offline-machines)）。

### 更新它的 agent

伺服器升級後，機器的頁面會在 **更新 agent** 顯示完整的指令（[更新 agent](#update-an-agent)）。設成 **一律經由伺服器** 的機器，指令會改為搬運 agent 映像檔，而不是拉取。在連得到 Docker Hub 的機器上執行第一步（把 `USER` 換掉；如果你用別的名稱連到那台機器，主機名稱也要換）：

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.1 && \
sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.5.1 | gzip | ssh USER@amd-01 'gunzip -c | sudo docker load'
```

再到 `amd-01` 上執行第二步：`docker rm -f`，然後用載入的映像檔執行同樣的 `docker run`。沒有 `ssh` 連得到那台機器？把映像檔存成檔案帶過去（[離線機器，或自己建置映像檔](#offline-machines-or-build-it-yourself)）。1.4.2 之前的 agent 不會回報它的執行方式：請先用這種方式手動更新一次，之後它的頁面就會顯示指令。

### 出問題時

- 經由伺服器的下載或映像檔失敗：每種訊息和處理方式，請看[機器連不到 Hugging Face](troubleshooting.md#a-machine-cannot-reach-hugging-face)裡的「經由伺服器仍然失敗」。
- *Cannot be placed: amd-01: no free port: 3 of 3 in use (host ports 33000-33002)*：連接埠範圍用完了（[沒有空的主機連接埠](troubleshooting.md#a-replica-waits-no-free-host-port)）。
- 伺服器的 log 裡有 *Relay of … to amd-01 cut short: the source broke off (…)*：來源（網際網路上的主機、registry，或 `machine gpu-01`）在檔案傳到一半時中斷。agent 會自己要求剩下的部分，下載會從磁碟上已有的部分繼續；只有同一個來源一再發生時才需要深入查看（那段網路，或那台機器的 agent log）。
- 副本說它的映像檔不在這台機器上：[replica 失敗：映像檔不在這台機器上](troubleshooting.md#a-replica-fails-the-image-is-not-on-this-machine)。
- 機器顯示離線：檢查它和伺服器之間的 tunnel 或防火牆（[agent 顯示離線](troubleshooting.md#an-agent-shows-offline)）。

## 離線機器

從加入這種機器到部署好模型的完整流程，請看[讓沒有網路的機器執行模型](#run-a-machine-without-internet-access)。

連不到映像檔 registry 的機器（沒有網路，或 DNS 過濾擋掉 `ghcr.io` 或 Docker Hub），也能用內建 recipe，
推論引擎的映像檔用檔案帶過去就好。recipe 和部署都不用改。

1.4 起，用 1.4 的 agent 有兩個更簡單的做法：內建 recipe 的映像檔（全部都有釘選）會自動**經由伺服器**一層一層送過去（[經由伺服器把模型送到離線機器](#give-an-offline-machine-models-through-the-server)），任何映像檔也都可以從機器的頁面**上傳**（[把推論引擎的映像檔上傳到機器](#upload-an-engine-image-to-a-machine)）。以下步驟仍然可用，適合較舊的 agent 或不經過伺服器的情況。

1. **找出映像檔。** 在 **模型 → 推論引擎**，打開推論引擎上的 **無法連到映像檔 registry 的機器：載入映像檔**：它列出每個容器 variant 的映像檔，以及填好的下列指令。機器會用第一個 `when` 符合它顯示卡的
   variant（[內建 recipe](reference.md#built-in-recipes)）：例如 llama.cpp 在 AMD APU（gfx1151、gfx1150……）
   上用 `vulkan`。部署頁面上的 replica 也會顯示它執行的映像檔。
2. **在連得到 registry 的機器上**，用 digest 拉映像檔、加上它的 tag，再存成檔案。以 llama.cpp 的 Vulkan variant
   為例（頁面上的指令已填好它的 digest）：

   ```bash
   sudo docker pull ghcr.io/ggml-org/llama.cpp@sha256:<digest> && \
     sudo docker tag ghcr.io/ggml-org/llama.cpp@sha256:<digest> ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d && \
     sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d | gzip > llama.cpp-server-vulkan-v1.0.0-b12359-b1b770d.tar.gz
   ```

   用 digest 拉，檔案裡就正好是 recipe 釘住的映像檔；`docker load` 會保留 tag，agent 就是靠 tag 找到它。
   兩台機器架構不同（x86_64 和 arm64）時，在 `docker pull` 加上 `--platform linux/amd64` 或
   `--platform linux/arm64`。
3. **把檔案複製到**離線機器（`scp`、USB 隨身碟），在那裡載入（Podman 用 `podman load`）：

   ```bash
   gunzip -c llama.cpp-server-vulkan-v1.0.0-b12359-b1b770d.tar.gz | sudo docker load
   ```

4. 照平常一樣**部署**。

**Forgeyard 怎麼檢查載入的映像檔。** recipe 用 `tag@sha256:…` 釘住映像檔，而 Docker 只用 digest 找這種參照。
載入的副本常常沒有這個 digest：Docker 傳統的映像檔儲存會保留 tag、丟掉 registry 的 digest，containerd
儲存則可能給副本一個它自己的 id。所以 digest 不在、但 tag 在時，agent 會拿那個映像檔和釘住時記錄下來的 id
（`forgeyard/recipes/images.lock.json`）比對：registry 的 digest（index，以及每個平台的 manifest 和
config），或者當 id 是儲存自己產生的時候，比對它的內容（各層、平台、entrypoint、command、環境變數、使用者、
工作目錄和停止訊號）。相符就用它在本機的映像檔 id 執行，不會去拉。tag 底下若是別的映像檔，一律不執行：agent
改向 registry 要釘住的那個。所以內建 recipe 原封不動就能離線用，不需要另寫只用 tag 指定映像檔的自訂 recipe。

內建 recipe 的每個映像檔都有記錄 id；自訂 recipe 只要寫的是完全相同的映像檔參照，也一樣。其他用 digest
釘住的映像檔無法檢查：在離線機器上，請在你的自訂 recipe 裡只用 tag 指定它。這項檢查需要 1.3 或更新的
agent；舊版 agent 會試著去拉。

## 部署物件偵測模型

物件偵測、影像分割和深度估計是各自的模型種類（1.4）：像其他模型一樣加入目錄（**模型**），再從 **服務中** 部署。它們在 Forgeyard 自己的視覺伺服器上執行（配方 `vision`）：NVIDIA GPU 上用 CUDA 版（以記憶體共用一張卡），任何機器（包括 AMD 機器）都能用 CPU 版。模型來自機器的模型快取，所以只下載一次，快取裡有了之後離線機器也能用。

1. **模型 → 加入模型 → 推薦**：*物件偵測、影像分割與深度* 列出 RT-DETRv2 R18（偵測 COCO 的 80 個類別）、SAM 2.1 Hiera tiny（分割）和 Depth Anything V2 Small（深度），都是 Apache-2.0。也可以用 **物件偵測**、**影像分割** 或 **深度估計** 任務搜尋 Hugging Face：任何 transformers 偵測模型（RT-DETR、D-FINE、不用 timm backbone 的 DETR…）、SAM 2 或 2.1，或深度模型（Depth Anything、DPT…），授權要寬鬆（Apache-2.0、MIT、BSD）。其他授權或沒有授權的不能加入。1.4.4 起清單裡還有三個**找你說的東西**的偵測模型（Grounding DINO tiny、MM-Grounding-DINO tiny、OWLv2 base）：沒有固定類別，每個請求說要找什麼（見下文）。它們需要 1.4.4 起的視覺映像檔。
2. **部署**：建議是 CPU（約 1.3 GiB 記憶體，四個執行緒上每張圖片零點幾秒到一秒半）或 NVIDIA GPU（檔案大小再加約 1.5 GiB，共用一張卡）。
3. 部署會出現在 **服務中 → 視覺**，標出它的路徑。部署頁的 **試試看** 會送一張測試圖片；**試用區**（選單 *視覺* 底下的模型）可以用你自己的照片。

用 API 金鑰呼叫 `/v1/vision/detect`（或 `/segment`、`/depth`），`model` 填模型名稱，照片放在檔案欄位 `image`。找你說的東西的偵測模型另外接受 `labels`，也就是要找的名稱（以逗號分隔的欄位，或 JSON 清單），沒給時會拒絕（400 `labels_required`）：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=grounding-dino-tiny -F image=@cats.jpg -F 'labels=cat, remote control' -F threshold=0.3
```

其他種類：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=rtdetr-v2-r18 -F image=@street.jpg -F threshold=0.5
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/segment \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=sam2.1-hiera-tiny -F image=@street.jpg -F 'box=[40, 40, 320, 240]'
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/depth \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=depth-anything-v2-small -F image=@street.jpg
```

用 Python（視覺是 Forgeyard 自己的 API：OpenAI SDK 沒有，任何 HTTP 用戶端都可以）：

```python
import base64
import os
import ssl

import httpx

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
key = os.environ["FORGEYARD_API_KEY"]
with httpx.Client(base_url="https://192.168.1.20:7860/v1", verify=ca, timeout=120,
                  headers={"Authorization": f"Bearer {key}"}) as fy:
    with open("street.jpg", "rb") as f:
        r = fy.post("/vision/detect", data={"model": "rtdetr-v2-r18", "threshold": "0.5"}, files={"image": f})
    for d in r.json()["detections"]:
        print(d["label"], round(d["score"], 2), d["box"])  # box: [x0, y0, x1, y1] in pixels

    with open("street.jpg", "rb") as f:
        r = fy.post("/vision/depth", data={"model": "depth-anything-v2-small"}, files={"image": f})
    with open("depth.png", "wb") as out:
        out.write(base64.b64decode(r.json()["depth_png"]))  # 16-bit grey, nearer is brighter
```

回應：`detections`（標籤、分數、方框）、`masks`（每個有分數、面積、方框和一張 1 位元 PNG，最好的在前）或 `depth_png`（附 `min` 和 `max`）；都附圖片的 `width` 和 `height`。它們接受的所有選項見[參考資料](reference.md#vision)。用量每個請求計一張圖片和它的千像素數；金鑰的模型清單和每分鐘請求數照常適用。

連不到 Docker Hub 的機器，會和其他固定的推論引擎映像檔一樣經由伺服器取得視覺映像檔（[離線機器](#offline-machines)）；也可以在那台機器上載入（**模型 → 推論引擎** → `vision` → *無法連到映像檔 registry 的機器：載入映像檔* 會給出指令）。

## 圖片分類

圖片分類（1.4.4）是一種獨立的模型，*圖片分類*（任務 `image-classify`），由同一個視覺伺服器在 `/v1/vision/classify` 回應；它不是文字分類（`/v1/classify`）。模型有兩類：

- **固定類別：** ViT、ConvNeXt V2、DINOv2 分類器…（`…ForImageClassification` 模型）有自己的類別清單，常見的是 ImageNet 的 1,000 類，回應其中最符合的幾個。
- **零樣本：** CLIP 和 SigLIP 2 把圖片分到每個請求送來的標籤（`labels`）之中，每個標籤讀成「This is a photo of …」（`template` 可以改）。SigLIP 每個標籤各自評分（`multi_label: true`：可能好幾個都符合，也可能都不符合）；CLIP 的分數加起來是 1。

1. **模型 → 加入模型 → 推薦 → 圖片分類** 列出 SigLIP 2 base 和 LAION 的 CLIP ViT-B/32（零樣本）、ViT base、ConvNeXt V2 tiny 和 DINOv2 small（ImageNet 類別），都是 Apache-2.0 或 MIT。也可以用 **圖片分類** 任務搜尋 Hugging Face。
2. 像其他視覺模型一樣**部署**：CPU 就夠了（四個執行緒每張圖片 0.1 到 0.4 秒）。需要 1.4.4 起的視覺映像檔。
3. 呼叫它：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=vit-base-patch16-224 -F image=@cats.jpg -F top_k=3
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/vision/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F model=siglip2-base -F image=@cats.jpg -F 'labels=cats on a sofa, a dog, an invoice'
```

回應：`labels`，最好的在前，每個是 `{"label", "score"}`，以及 `multi_label`。零樣本分類器沒給 `labels` 時回 400 `labels_required`；固定類別的分類器收到 `labels` 回 400 `labels_not_supported`。

## 在試用區試用視覺

試用區的視覺面板以你自己的身分、不需要 API 金鑰，把一張圖片送給視覺模型（[像其他模型一樣部署的](#deploy-an-object-detection-model)）。任何已登入的人都可以使用；請求在 **用量** 裡以 *主控台試用區*（`console`）記在那個模型底下。1.4 起沒有 Vision 分頁：視覺模型在試用區唯一的模型選單裡，1.3 的網址（`/playground?tab=vision`）會開出第一個視覺模型。從 1.3 範本做的應用程式不會列在這裡（1.4 起應用程式離開主控台，[1.5 移除](reference.md#apps-deprecated)）。

1. 打開 **模型** 選單，在 **視覺** 底下選一個模型：每一個都標出任務。只會列出已就緒的模型；完全沒有時，試用區會這樣說明，管理員和操作員會看到前往目錄的連結。
2. 把 PNG、JPEG 或 WebP 圖片拖到框裡，或按 **選擇圖片**（最多 20 MiB）。
3. 接著依任務而定：
   - **物件偵測：** 按 **偵測物件**，模型找到的每樣東西都會畫出框、標籤和分數；**分數至少** 會隱藏分數較低的，不必重新送出。找你說的東西的偵測模型（1.4.4）會先問 **要找的東西**：輸入名稱、以逗號分隔（*cat, remote control*），再按 **尋找**；滑桿從 0.3 開始，因為這類模型的分數較低。
   - **影像分割：** 在東西上點一下，或拖出一個框把它圈起來。最好的遮罩會疊在圖片上；上方的按鈕可以切換模型給的其他遮罩。再點一下或再拖一次就換一個。
   - **深度估計：** 按 **估計深度**，深度圖會顯示在照片旁邊，越近越亮或越暖，可選 **彩色** 或 **灰階**。深度是相對的，不是公尺。
   - **圖片分類（1.4.4）：** 按 **分類**，列出最符合的類別，每個分數有一條長條。零樣本分類器會先要你輸入 **標籤**，以逗號分隔（*a cat, a dog, a screenshot*）。

結果下方會顯示是哪個副本回答、圖片大小，以及模型和整個請求花的時間。模型停止了，或讀不了這張圖片時，會用白話說明。主控台把它送到 `/api/playground/vision/<task>`，對已登入的人來說和 `/v1/vision/<task>` 一樣（[參考資料](reference.md#vision)）。

## 把圖片送給模型

1. **加入能讀圖片的模型。** 在 **模型 → 加入模型 → 推薦** 的 **視覺（對話中讀圖片）** 群組裡，有給 vLLM 用的 Qwen3-VL 2B、4B 和 8B，以及給 llama.cpp（CPU 或 AMD APU）用、附 projector 檔的 Qwen3-VL GGUF。只要目錄項目標示 *圖片* 的模型都可以：目錄會從模型的 `config.json` 讀出來。GGUF 儲存庫還要選它的 **投影器** 檔（有 F16 就預設選它；選 *無：僅文字* 就只能處理文字）。
2. 照平常的方式**部署**。*Images per request*（4）、*Videos per request*（0，關閉）和 *Largest request*（64 MiB）都在 **進階** 底下，預設值就能用。估算會把視覺編碼器（vLLM）或 projector（llama.cpp）列成獨立的一項。
3. **呼叫它**，圖片用 `data:` URI 或 `https://` 網址：

   ```python
   import base64

   with open("photo.jpg", "rb") as f:
       photo = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
   client.chat.completions.create(model="qwen3-vl-8b", messages=[{"role": "user", "content": [
       {"type": "text", "text": "What is in this picture?"},
       {"type": "image_url", "image_url": {"url": photo}},
   ]}])
   ```

   `https://` 網址由閘道下載（只連公開位址，每張圖片最多 `MEDIA_FETCH_MAX_MB`），再以資料形式轉送。區網內的圖片伺服器需要在伺服器上設定 `MEDIA_ALLOW_PRIVATE_HOSTS=true`。
4. **在試用區試用：** 訊息框旁的圖片按鈕可以附上圖片。

**影片：** 在 **進階** 把 *Videos per request* 設成 1 以上，再送 `video_url` 區塊。在 vLLM 上，這會讓推論引擎啟動時為一段長影片預留記憶體，KV cache 能用的就變少：啟動不了的話就調低上下文長度。在 llama.cpp 上，請把部署的上下文長度設成 8,192 以上：Qwen3-VL 用它自己的每秒 2 格讀影片（*Video frames per second* 只對其他模型有效），一段 6 秒的影片用了 5,345 個 token。

**自訂的 llama.cpp 配方**（例如另一個 build）只要傳入 projector 並宣告它接受什麼，就能讀圖片：

```yaml
optional_args:
  - if_model: mmproj
    args: [--mmproj, "{mmproj_file}"]
media: {inputs: [image, audio, video], files: false}
```

## 把文字轉成語音

1. **加入語音模型。** 在 **模型 → 加入模型 → 推薦** 的 **文字轉語音** 群組裡有：
   - 給 NVIDIA GPU 用的 Qwen3-TTS 0.6B 或 1.7B CustomVoice（vLLM-Omni）：中文與英文，九種聲音。大約需要 10 與 12 GiB 的 GPU 記憶體。
   - 給 llama.cpp 用的 Qwen3-TTS 1.7B GGUF：在 AMD APU 或 CPU 上說中文與英文；它只有自己的一種聲音。
   - Kokoro 82M（speaches）與 Chatterbox Turbo（llama.cpp）：CPU 就跑得動的小型英文聲音。

   從 **搜尋 Hugging Face** 找到的模型，只要標了 *text-to-speech*、且有配方支援它的格式就能用：safetensors 的 Qwen3-TTS 用 vLLM-Omni，附 projector（`mmproj-*.gguf`）的 TTS GGUF 用 llama.cpp，ONNX 格式的 Kokoro 或 Piper 用 speaches。
2. **照常部署。** *Longest input* 在 **進階** 裡，預設值就能用。
3. **用 OpenAI SDK 呼叫：**

   ```python
   with client.audio.speech.with_streaming_response.create(
       model="qwen3-tts-1-7b", voice="vivian", input="今天天氣很好。", response_format="wav",
   ) as response:
       response.stream_to_file("speech.wav")
   ```

   `GET /v1/audio/voices?model=qwen3-tts-1-7b` 會列出聲音與模型能產生的格式。要一邊產生一邊收音訊，加上 `extra_body={"stream_format": "audio"}`，並使用 `pcm` 或 `wav`。
4. **在試用區試試看：** 語音模型會有一個 **朗讀** 區塊，可以選聲音、格式，並附播放器。

**要用哪種格式：** vLLM-Omni 產生 wav、pcm、flac、mp3 與 opus；llama.cpp 產生 wav 與 pcm；speaches 產生 mp3、flac、wav 與 pcm。沒指定 `response_format` 時得到的是推論引擎的預設（wav，speaches 為 mp3），不是 OpenAI 的 mp3。`pcm` 是 16 位元單聲道，取樣率見回應（`x-audio-sample-rate`，或 `Content-Type` 裡的 `rate=`）。

**在 AMD APU（Vulkan）上：** 開啟 llama.cpp 的 cooperative matrices 時，Qwen3-TTS 對同一個請求可能產生略有不同的音訊（ggml-org/llama.cpp#32050）。如果在那裡聽起來不對，新增一個自訂配方，在它的 vulkan variant 的 `env` 設定 `GGML_VK_DISABLE_COOPMAT: "1"`。

## 生成圖片

1. **加入圖片模型。** 在 **模型 → 加入模型 → 推薦** 的 **圖片（文字生成圖片）** 群組裡有：
   - 給 NVIDIA GPU 用的 Z-Image-Turbo、Stable Diffusion XL 與 FLUX.1-schnell（vLLM-Omni）。在預設上限下，大約各需要 30、17 與 42 GiB 的 GPU 記憶體；FLUX.1-schnell 是 gated（要在 Hugging Face 接受它的條款並提供 token）。
   - 給 stable-diffusion.cpp 用的 GGUF 版 Z-Image-Turbo 與 FLUX.1-schnell：AMD GPU 或 APU（Vulkan）或 NVIDIA GPU（CUDA），1024x1024 時大約 18 與 19 GiB，或是 CPU（很慢）。
   - 給 CPU 用的 GGUF 版 Stable Diffusion 1.5：又小又舊（512x512），用來確認圖片生成能用。

   從 **搜尋 Hugging Face** 找到的模型，只要標了 *text-to-image*，而且是 diffusers 儲存庫（vLLM-Omni）或 GGUF / safetensors 檔案（stable-diffusion.cpp；選模型檔，旁邊的文字編碼器與 VAE 檔會依名稱找到）就能用。目錄會顯示它的授權；使用產生的圖片之前請先確認。
2. **照常部署。** *Images per request* 和 *Default size* 在表單上；最長邊、步數、佇列、逾時和卸載在 **進階** 底下。預設值就能用。
3. **用 OpenAI SDK 呼叫：**

   ```python
   import base64

   out = client.images.generate(model="z-image-turbo", prompt="A lighthouse on a rocky coast at sunset",
                                size="1024x1024", n=1)
   with open("lighthouse.png", "wb") as f:
       f.write(base64.b64decode(out.data[0].b64_json))
   ```

   步數、負面提示和 seed 放在 `extra_body`：
   `extra_body={"num_inference_steps": 8, "negative_prompt": "blurry", "seed": 7}`。
4. **在試用區試試看：** 圖片模型（選單的 *圖片* 底下）會有一個 **圖片** 面板，可以輸入提示、選部署接受的尺寸和張數，每張圖片下方都有下載連結。

**要多久：** 在 A100 上每張幾秒；在 CPU 上要幾分鐘。在一台忙碌的 x86-64 機器上用 16 到 32 個核心量測：Stable Diffusion 1.5、512x512、20 步大約 100 秒；Z-Image-Turbo（Q4_0）8 步在 512x512 大約 2 分鐘，1024x1024 大約 8.5 分鐘。機器較慢或尺寸較大時，在 **進階** 調高 *Timeout*。

**模型自己的設定**（Z-Image-Turbo 需要 CFG 1 與 8 步）對 stable-diffusion.cpp 來說放在目錄項目的推論引擎參數裡（`--cfg-scale 1 --steps 8`）；推薦的項目都已經帶上了。vLLM-Omni 則從模型本身讀取。

**在離線的 AMD 機器上：** 用 1.4 的 agent 時，`sd-cpp` 配方的 Vulkan 映像檔（有釘選）和模型都會經由伺服器送過去，有機器的快取裡已經有這個模型時就從那台機器取得（[讓沒有網路的機器執行模型](#run-a-machine-without-internet-access)）。用較舊的 agent 時，用 `docker load` 載入映像檔（見[離線機器](#offline-machines)）；模型則先在一台連得到 Hugging Face 的機器上部署一次，再把它在那台機器模型快取裡的目錄（`hub/models--<org>--<name>`，用 `tar` 或 `rsync -a`：裡面用的是相對連結）複製到離線機器快取裡的相同位置。那裡的部署會發現已經快取好，什麼都不用下載。

## 生成影片

1. **加入影片模型。** 在 **模型 → 加入模型 → 推薦** 的 **影片（文字生成影片）** 群組裡有 Wan2.1-T2V-1.3B（832x480、16 fps，在預設上限下大約需要 22 GiB 的 GPU 記憶體）和 Wan2.2-TI2V-5B（1280x704、24 fps，大約 33 GiB；它的估算還沒實測），兩個都是 Apache-2.0，給 NVIDIA GPU 用（vLLM-Omni）。從 **搜尋 Hugging Face** 找到的模型，只要是 diffusers 儲存庫，而且 `model_index.json` 寫的是文字轉影片 pipeline（`WanPipeline`）就能用。
2. **照常部署。** *Default size*、*Default length* 和 *Longest video* 在表單上；最長邊、每秒影格數、步數、同時的工作數、每把金鑰的工作數、結果保留多久、最長的工作和卸載在 **進階** 底下。預設值就能用。
3. **用 OpenAI SDK 呼叫：**要求影片、輪詢它，然後下載。

   ```python
   import time

   video = client.videos.create(model="wan2.1-t2v-1.3b", prompt="A red kite over a green hill, slow camera pan",
                                seconds="5", size="832x480")
   while video.status in ("queued", "in_progress"):
       time.sleep(5)
       video = client.videos.retrieve(video.id)
   if video.status == "completed":
       client.videos.download_content(video.id).write_to_file("kite.mp4")
   else:
       print(video.error.code, video.error.message)
   ```

   `seconds` 是字串，和 OpenAI 的 API 一樣。`size` 請用模型接受的尺寸：OpenAI 自己的尺寸（`1280x720`、`720x1280`）比 Wan2.1-1.3B 允許的大。步數、負面提示和 seed 放在 `extra_body`：`extra_body={"num_inference_steps": 30, "negative_prompt": "blurry", "seed": 7}`。`client.videos.list()` 列出你這把金鑰的影片，`client.videos.delete(video.id)` 會取消製作中的影片或刪除完成的影片。

   用 curl 的話，以表單送出：

   ```bash
   curl https://192.168.1.20:7860/v1/videos -H "Authorization: Bearer $FORGEYARD_KEY" \
     -F model=wan2.1-t2v-1.3b -F prompt="A red kite over a green hill" -F seconds=5 -F size=832x480
   curl https://192.168.1.20:7860/v1/videos/<id> -H "Authorization: Bearer $FORGEYARD_KEY"
   curl https://192.168.1.20:7860/v1/videos/<id>/content -H "Authorization: Bearer $FORGEYARD_KEY" -o kite.mp4
   ```

4. **在試用區試試看：** 影片模型會有一個 **影片** 面板，可以輸入提示、選部署接受的尺寸和長度、看工作的進度，完成的影片會直接在頁面上播放並附下載連結。你之前用這個模型做的影片列在下方。

**要多久：** 每段影片要幾分鐘，每個副本一次做一段；其他的在它上面排隊。影片長或尺寸大時，在 **進階** 調高 *Longest job*。

## 文字分類

文字分類（1.4.4）用小而快的模型替文字加上標籤：情緒、語言、有害內容。它在 GPU 上用 vLLM 的 pooling runner（配方 `vllm-pooling`）執行，以記憶體共用一張卡。

1. **模型 → 加入模型 → 推薦**：*文字分類* 列出多語言情緒模型（1 到 5 顆星）、語言偵測模型（20 種語言，包含中文）和有害內容模型（6 個標籤，各自評分），授權都是 MIT 或 Apache-2.0。也可以用任務 **文字分類** 搜尋 Hugging Face：有兩個以上標籤的 BERT、RoBERTa、XLM-RoBERTa 或 ModernBERT 分類模型都能加入。目錄會顯示它的標籤；reranker（只有一個輸出）、零樣本（NLI）模型和 DeBERTa 或 DistilBERT 分類模型會被拒絕，並說明原因。
2. **部署**。模型本身的最大長度比推論引擎預設短時，context 會從模型的最大值開始（這些 BERT 大小的模型是 512 個 token）。在共用的卡上大約 2 GiB。
3. 它會出現在 **服務中 → 文字分類**。部署頁面的 **試試看** 會送一句話；**試用區** 接受你自己的文字，一行一段，並顯示每個標籤的機率。

用 API 金鑰呼叫 `/v1/classify`（Forgeyard 自己的 API，格式和 vLLM 一樣；OpenAI SDK 沒有）：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/classify \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "bert-multilingual-sentiment", "input": ["Great phone, fast delivery.", "Broke after a day."]}'
```

每段文字傳回 `index`、`label`（最可能的標籤）、`probs`（每個標籤的機率，順序和模型的 `config.json` 相同）和 `num_classes`，`usage` 裡有 `prompt_tokens`：

```json
{"id": "classify-…", "object": "list", "created": 1795000000, "model": "bert-multilingual-sentiment",
 "data": [{"index": 0, "label": "5 stars", "probs": [0.01, 0.01, 0.04, 0.26, 0.68], "num_classes": 5},
          {"index": 1, "label": "1 star", "probs": [0.81, 0.14, 0.03, 0.01, 0.01], "num_classes": 5}],
 "usage": {"prompt_tokens": 19, "total_tokens": 19, "completion_tokens": 0}}
```

在 Python 裡，任何 HTTP 用戶端都可以：

```python
import os
import ssl

import httpx

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
r = httpx.post("https://192.168.1.20:7860/v1/classify", verify=ca,
               headers={"Authorization": f"Bearer {os.environ['FORGEYARD_API_KEY']}"},
               json={"model": "xlm-roberta-language-detection", "input": ["今天天氣很好", "Bonjour à tous"]})
for d in r.json()["data"]:
    print(d["label"], round(max(d["probs"]), 3))
```

`truncate_prompt_tokens`（token 數，或 -1 代表模型的最大值）會把太長的文字截短而不是拒絕；一個請求最多 2,048 段文字。

### 用對話模型分到你自己選的標籤

對話模型可以用結構化輸出分類到任何標籤：它只能回答其中一個。在 vLLM 上最簡短的寫法是 `structured_outputs.choice`：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "qwen3-4b", "temperature": 0,
       "chat_template_kwargs": {"enable_thinking": false},
       "messages": [{"role": "user", "content": "Which department should handle this? 我的發票金額開錯了"}],
       "structured_outputs": {"choice": ["billing", "shipping", "technical", "other"]}}'
```

回答的 `content` 會是這四個之一。用含 `enum` 的 JSON schema（`response_format: {"type": "json_schema", …}`）在 vLLM、llama.cpp 和雲端供應商上也能做到同樣的事。它比分類模型慢、成本也高，但不需要新模型，而且每個請求的標籤都可以不同。零樣本分類模型（請求裡帶你的標籤的 NLI 模型，也能在 CPU 上執行）會在 1.5 和 transformers 推論引擎一起推出（#144）。

## 使用受限（gated）的 Hugging Face 模型

有些模型（Llama、Gemma 3、FLUX.1-schnell……）是 *gated*：Hugging Face 只提供給接受了它授權條款的帳號。Forgeyard 需要這種帳號的權杖才能檢查和下載它們，而整台伺服器有一個權杖就夠了：

1. 在 huggingface.co 登入，打開模型的頁面並接受它的授權條款。然後在 **Settings → Access Tokens** 建立一個有 *Read* 權限的權杖。
2. 管理員：打開 **模型 → 來源**，把它貼進 **Hugging Face 權杖**，再選 **儲存權杖**。它用伺服器的機密金鑰加密儲存，之後不會再顯示，只會顯示最後四個字元。
3. 照常加入並部署模型。檢查模型時會用伺服器的權杖，下載它的 agent 會隨下載工作拿到權杖（agent 不會儲存它）。

部署仍然可以帶自己的權杖：**部署模型 → 進階 → Hugging Face 權杖**。它只對那個部署優先於伺服器的權杖，1.3.1 之前建立的部署也保留各自的權杖。**加入模型** 裡的權杖欄位也一樣，只用於那一次檢查。**移除權杖** 會移除伺服器的權杖；有自己權杖的部署照常運作。

權杖只會送到 Hugging Face（`HF_ENDPOINT`），絕不會送到 `https://` 的 GGUF 主機。受限模型的部署沒有可用的權杖時，設定健康檢查會提醒（`gated-model-no-token`）。

## 加入雲端供應商

管理員：打開 **雲端 → 供應商**，選 **新增供應商**，挑選 OpenAI、Anthropic、Google Gemini、Azure OpenAI、OpenRouter 或 *OpenAI-compatible server*，貼上金鑰，再按 **測試連線**。加入你要的模型，然後使用其中一個：打開一個部署，在 **雲端備援** 新增雲端層並放入雲端模型，再儲存；或用 **雲端 → 雲端模型 → 新增雲端模型** 以自己的名稱提供它。在 **API 金鑰** 把可以使用它的金鑰的雲端設定改成 **永不** 以外的值，或選一個[雲端政策](#choose-the-cloud-policy-for-keys)。各供應商的步驟，以及各自端點做不到的事，請見[雲端供應商](cloud-providers.md#add-a-provider)。

**Outside your network (cloud)** 會自動幫你勾好：OpenAI、Anthropic、Gemini、Azure 和 OpenRouter 一律勾選；*OpenAI-compatible server* 則在它的 base URL 是公開位址（或解析成公開位址的名稱）時勾選。私有位址（`10.x`、`172.16–31.x`、`192.168.x`、`100.64–127.x`、loopback、link-local、IPv6 `fc00::/7`），或像 `gpu-box`、`*.local`、`*.lan`、`*.internal` 這樣的名稱則不勾選。表單會說明原因；判斷錯了就自己改。名稱在伺服器上解析不出來時，維持不勾選，並說明無法判斷。

## 選擇金鑰的雲端政策

管理員：**API 金鑰 → 雲端政策** 決定新金鑰拿到什麼，以及大家最多能把自己的金鑰開放到什麼程度。選一個，再按 **儲存雲端政策**：

| 政策 | 新金鑰拿到 | 大家能把自己的金鑰設成 |
|---|---|---|
| **不使用雲端**（預設） | 永不 | 永不 |
| **個人可自行選擇溢出** | 永不 | 僅溢出時 |
| **預設溢出** | 僅溢出時 | 僅溢出時 |
| **允許雲端** | 僅溢出時 | 允許 |
| **自訂** | 自己選 | 自己選 |

*僅溢出時* 只在本機部署無法使用、忙碌，或回應 429 或 503 時，才把請求送到雲端層；*允許* 也會使用排在本機之前的雲端層。改變政策不會改動既有金鑰自己的設定，但調低上限會立刻套用到大家設得比較高的金鑰。管理員可以把任何金鑰設成任何值，並對擁有者鎖定。預算仍然有效：[追蹤並限制雲端花費](#track-and-limit-cloud-spend)。

設了 `GATEWAY_CLOUD_DEFAULT` 或 `GATEWAY_CLOUD_MAX`、而還沒有人儲存過政策時，面板會把這些值標示為 *from .env*。儲存之後就永久取代它們。

## 追蹤並限制雲端花費

管理員在開始使用供應商之後：

1. **設定價格。** 在 **雲端 → 供應商** 選 **從 OpenRouter 更新價格**。檢查建議的對應，勾選要採用的列，再選 **套用勾選的**。OpenRouter 沒有列出的模型，請打開它的供應商，在模型上選 **價格**，依供應商的價格頁面輸入價格（每百萬 token 的美元），再按 **儲存價格**。沒有價格的雲端模型會算在 *沒有價格*，而且在任何預算之下都會被拒絕。
2. **查看費用。** **用量** → **費用** 依日期、金鑰、個人、模型或供應商顯示費用，以及有多少由本機處理。
3. **設定預算**，以日曆月（UTC）計，單位是美元：
   - 金鑰：**API 金鑰** → 金鑰的 **限制** → **每月預算（美元）** → **儲存限制**。管理員在別人金鑰上設定的值會對擁有者鎖定；**解除預算鎖定** 會移除它；
   - 個人：**用量** → **費用** → **本月預算**，選擇 **所有人** → **設定預算**；
   - 供應商：它的頁面 → **每月預算** → **儲存預算**。
4. **留意警示。** 預算用到 80% 和 100% 時，**稽核紀錄** 會各記錄一筆事件（`budget.warning`、`budget.exhausted`），頁面也會顯示警告。不會寄電子郵件。

費用怎麼算、預算如何預留金額、用完之後會怎樣：請見[費用與價格](cloud-providers.md#cost-and-prices)和[預算](cloud-providers.md#budgets)。

## 修改部署

在部署的頁面選擇 **編輯**（管理員和操作員）。可以修改：

- **GPU 共用**：*獨占* 讓每張卡只給一個副本；*共用 VRAM* 只保留估算的記憶體量，卡上其餘的空間可以放其他模型，彼此共用算力（[GPU 共用](concepts.md#gpu-sharing)）；
- **每個副本的 GPU 數**、**Context length**、**Concurrent requests** 和 recipe 的其他參數、記憶體覆寫值，以及額外的推論引擎參數；
- **副本**：增加的副本會直接加上，不影響正在執行的；減少時移除編號最大的；
- **機器**：*自動*（由 Forgeyard 選擇），或 *固定在* 某一台機器。固定到另一台機器時，副本會搬過去；改回 *自動* 則什麼都不會搬；
- （1.5.2）**推論引擎版本**：內建推論引擎，或用 **試用這個版本** 建立的複本（[把部署換到推論引擎的其他版本](#move-a-deployment-to-another-engine-version)）。

模型和名稱不能改：要改就部署一個新的。儲存之前，頁面會顯示新的估算、每個副本會放在哪裡，以及儲存後會發生什麼事。放不下的修改會像部署一樣被拒絕（[部署放不下](troubleshooting.md#a-deployment-does-not-fit)）。已停止的部署只會儲存設定：下次啟動時才套用。

儲存後，副本會以新設定重新啟動：

- **多個副本**一次重新啟動一個：前一個以新設定就緒之後，才換下一個，所以模型會持續回應。全部完成之前，頁面會顯示 *updating: 1 of 2 on the new settings*；
- **只有一個副本**時，它會停止再啟動：新的就緒之前，模型不會回應。儲存之前，頁面會說明大約多久（1.5.2；依它上次啟動的時間，沒有的話依同一個推論引擎其他部署的啟動時間，再沒有就用一般的啟動時間），以及怎麼避免。要避免這樣，可以在機器放得下兩個副本時，在同一次修改裡把 **副本** 調成 2：新副本會先啟動，舊的再重新啟動，之後再調回來即可；
- 只改副本數量，或改回 *自動*，不會重新啟動任何東西。

重新啟動的副本（修改之後，或先 **停止** 再 **啟動**）只要原本那台機器還放得下，就會回到原本的機器，而不是改放到別處最剛好的地方：那台機器已經有模型和推論引擎了。如果那台機器沒有空間了，或已離線，副本就放到放得下的地方。伺服器把這件事記在記憶體裡：伺服器重新啟動時還在等空間的副本，會照一般規則放置。

用程式修改：`PATCH /api/deployments/{id}`（[參考](reference.md#change-a-deployment-through-the-api)）。

## 查看模型啟動花了多久

部署的副本每次就緒時，部署的事件都會寫出這次啟動花了多久、時間花在哪裡，例如 *Replica 0 on gpu-01: ready in 74.2 s (model 0.3 s, image 0.1 s, engine 73.8 s)*：

- `model`：在機器的模型快取裡檢查模型檔案，或下載它們（以及行程推論引擎的封存檔）；
- `waiting`：等共用同一張 GPU 的另一個副本載入完成；
- `image`：檢查推論引擎的映像檔，或拉取它；
- `engine`：從啟動推論引擎到它第一次健康檢查通過。

各部分加起來就是總時間。同一次啟動也是 `GET /api/deployments/{id}` 和列表裡的 `last_start`（[參考](reference.md#change-a-deployment-through-the-api)），以及 `/metrics` 裡的 `forgeyard_replica_start_seconds`（[指標](reference.md#metrics)）。agent 比 1.5.0 舊時，伺服器只能量到推論引擎的部分，事件會寫 *measured by the server*：[更新 agent](#update-an-agent) 就能看到其他部分。

副本啟動期間，1.5.0 的 agent 每秒檢查一次它的健康狀態，而不是照配方的 *interval*（vLLM 是 10 秒），所以模型一就緒，一秒內就會開始使用，而不是最多晚 10 秒。只在配方的 start period 內這樣做（這段時間裡檢查失敗不算數），而且只到第一次通過為止；之後照配方的 interval 檢查，和以前一樣，所以副本絕不會比以前更早被判定為 unhealthy。

### 在兩次啟動之間保留 vLLM 的編譯快取

vLLM 每次啟動都會編譯模型（torch.compile，以及 Inductor 和 Triton 編譯它的 kernel），寫到容器裡的一個目錄，容器沒了它就會跟著沒了。從 1.5.0 起，`vllm` 和 `vllm-pooling` 配方會把這個目錄保留在機器上（`engine_cache: {env: VLLM_CACHE_ROOT, enabled: true}`），所以只有副本的第一次啟動需要編譯；之後的啟動（包括[休眠](#let-a-deployment-sleep-when-nobody-uses-it)後被喚醒）都會載入編譯好的結果。在 A100 上用 Qwen3-0.6B 量過：第一次啟動兩者都是 118 秒；重新啟動沒有快取要 114 秒，有快取只要 57 秒，兩者的回答相同；目錄大小 44 MiB。在部署的事件裡可以看到：重新啟動後 *ready in …* 的 `engine` 部分。

使用它不需要做任何事。如果要不用它啟動（例如追查問題時想先排除它），在 **模型 → 推論引擎** 選 **加入自訂推論引擎**，從內建的 `vllm` 開始，取個名字（例如 `vllm-nocache`），把它的 `engine_cache` 那一行改成 `enabled: false`，再用它部署。開啟或關閉都不會重新啟動正在執行的副本：從副本的下一次啟動開始生效。

每個副本在它的機器上都有自己的目錄 `<model cache>/engine-cache/<key>`，以可寫入的方式掛載在 `/forgeyard/engine-cache`，`VLLM_CACHE_ROOT` 指向它；其他部署或副本都不會用到。vLLM 依模型、會改變計算圖的設定、它自己的程式碼和編譯器的雜湊值保存編譯結果，所以改了參數就會重新編譯，不會載入錯的計算圖。刪除部署（或它的配方不再保留快取）時目錄會被移除；副本以新的映像檔（新的配方版本）啟動時，它較舊的目錄也會被移除。需要有模型快取和容器引擎的 1.5.0 agent；行程 variant，以及較舊 agent 上的副本，啟動方式和以前一樣。不以 root 執行的映像檔無法寫入這個目錄。

## 讓沒人使用的部署休眠

偶爾才用的模型（晚上才試一下的、每週跑一次工作的大模型），可以在沒人呼叫時把 GPU 記憶體還回來（[閒置時休眠](concepts.md#sleep-when-idle)）。除非你開啟它，否則它是關閉的，每個部署分開設定。

1. 在表單裡開啟它：**服務中 → 部署模型**，或部署頁面上的 **編輯**。在 *沒有使用時* 底下勾選 **閒置時休眠**，保留或修改 **閒置分鐘數**（預設 30，1 到 1,440）；**進階** 裡的 **喚醒等待** 是請求等它醒來的最長時間（預設 300 秒，10 到 1,800）。設定旁邊會寫上次啟動花了多久（在部署表單上是同一個模型和推論引擎的另一個部署上次啟動的時間），休眠後的第一個請求大約就要等這麼久。只改這些設定不會重新啟動任何東西。用腳本：對 `PATCH /api/deployments/{id}` 送 `{"params": {"sleep_when_idle": true, "idle_minutes": 30}}`（管理員和操作員；不送 `idle_minutes` 就是 30 分鐘；加上 `"wake_wait": 120`，請求最多等 2 分鐘而不是 5 分鐘）；部署時也接受這些參數。
2. 部署頁面上有 *沒有使用時* 卡片：設定、它現在的狀況（*清醒中；如果沒有請求，18 分鐘後休眠*，或讓它保持清醒的原因：有請求正在處理、有副本正在啟動……）、最後一個請求、上次啟動的時間，以及過去 24 小時被喚醒了幾次。
3. 在閒置時間內沒有請求，它的副本就會停止。**服務中** 會顯示 **休眠中**（月亮圖示），寫著 *不佔用任何資源* 和它會在哪台機器上醒來；它的頁面會寫從什麼時候開始休眠，事件裡會有 *Asleep: no requests for 30 min; frees …*。**機器** 會在它會執行的那台機器底下列出它（*qwen3-4b asleep here (14.4 GiB on GPU #4 when awake)*）：這段期間那些記憶體可以給別人用。模型仍然列在 `/v1/models` 裡；機器保留模型檔案和推論引擎的映像檔。
4. 下一個請求會喚醒它：請求最多等到 *喚醒等待* 的時間，模型就緒後就得到回應（可以使用路由雲端分層的金鑰，等待期間由雲端服務）。事件裡會有 *Woken by a request (key …)*。也可以在它的頁面或 **試用區** 按 **立即喚醒** 自己喚醒它（`POST /api/deployments/{id}/start`）；休眠中的模型仍會帶著月亮圖示留在試用區的選單裡，它啟動期間到達的請求會等它。在就緒之前 Serving 會顯示 **喚醒中** 和喚醒後經過的時間；試用區直接送出你的訊息，等待時會顯示 *正在喚醒模型（約 74 秒）…*。如果無法喚醒（沒有空間、機器離線、推論引擎失敗），請求會得到 503 `cannot_wake` 和原因。
5. 對休眠中的部署按 **停止** 會真正停止它：在有人啟動它之前，它不會出現在 `/v1/models`。

設定健康檢查兩個方向都會提醒：佔著沒人用的 GPU 記憶體的部署會出現 *had no requests for 3 days: let it sleep when idle?*；在收到的請求之間一直休眠、而且啟動要超過一分鐘的部署會出現 *woke 11 times in a day: raise its idle time*（[Setup health](reference.md#setup-health-checks)）。

閒置時間要依模型啟動需要多久來選：llama.cpp 的模型幾秒就就緒，大的 vLLM 模型可能要好幾分鐘，所以要給它一個遠比啟動時間長的閒置時間（預設的 30 分鐘就是）。休眠時它的記憶體可以給別人用：在共用的卡上可能被其他程式用掉，這時模型會在其他有空間的機器上啟動，或等待。

要在你自己的機器上檢查休眠和喚醒，執行 `scripts/accept_scale.py`（[檢查休眠、喚醒和自動副本數](#check-sleep-waking-and-automatic-replicas)）。

## 機器一直離線時

超過 30 秒（`AGENT_OFFLINE_SECONDS`）沒有收到機器的任何訊息，它就是 *offline*。從這時起：

1. **立刻：**閘道不再把請求送到它的副本。在其他機器上也有副本的部署，繼續由那些副本回應；沒有其他就緒副本的部署回應 503 `no_ready_replica`，並說明 *the machine is offline*（或轉到它的雲端備援）。只有在伺服器自己剛啟動時，每台機器在 agent 重新連線（幾秒內）之前都顯示離線，還沒連回來的機器會在那 30 秒內保留它的副本：它們的推論引擎在這段時間仍然會回應。
2. **10 分鐘之後**（`AGENT_OFFLINE_MOVE_SECONDS`，從最後一次心跳起算）：沒有固定在這台機器上的部署，它的每個副本會依部署自己的規則（分散到不同機器、記憶體足夠、依目前的估算）重新放到放得下的機器上。部署頁面會顯示 *Replica 0: moved: gpu-01 offline for 10 minutes; placing it on another machine (gpu-02 fits)*，接著是它被放到哪裡。這是同一個副本，絕不會多出一份：部署的副本數量不變。
   - **固定（pinned）**在這台機器上的部署，副本留在原地等它回來（*waiting for gpu-01 to come online (the deployment is pinned to it)*）。
   - **沒有其他機器放得下**時，副本也留下來等；之後某次檢查有機器放得下了，才會搬過去。
3. **機器回來時**，它的 agent 收到的工作清單裡已經沒有搬走的副本，就會停掉它們的推論引擎。留下來等的副本會重新開始服務。搬走的副本不會自己搬回來：要放回去，請修改部署的機器。

在 10 分鐘內回來的機器（重新開機、更新 agent、短暫斷線）什麼都不會被搬走；時間會從它下一次心跳重新起算。伺服器自己重新啟動後，每台機器在 agent 重新連線之前都會顯示離線，所以這 10 分鐘從伺服器啟動時才開始算。設定 `AGENT_OFFLINE_MOVE_SECONDS=0` 就永遠不搬移副本。應用程式（已淘汰）遵守同樣的規則，只有工作（task）會留在原地：它可能還在那裡執行，在別處再跑一次可能會把它的工作做兩次。

## 讓副本數跟著負載變化

有些時段很忙、有些時段很閒的部署，可以只在需要時多跑幾個副本（[自動副本數](concepts.md#automatic-replicas)）。除非你開啟它，否則它是關閉的，每個部署分開設定。

1. 在 **服務中** 打開部署，選 **編輯**（或部署一個新的），把 **副本** 設為 **依負載自動調整**。設定最少和最多（沒改時是 1 和 2；最多 16）。按 **儲存**：不會重新啟動任何東西。目前的副本數如果超出新的範圍，會立刻調整進範圍內（多一個或少幾個）。
   - 用腳本：`PATCH /api/deployments/{id}`，內容 `{"params": {"autoscale": true, "min_replicas": 1, "max_replicas": 4}}`（[參考](reference.md#automatic-replicas)）。
2. 把 *Concurrent requests* 設成一個副本同時應該處理的數量（recipe 的預設值是不錯的起點）：負載是拿它來比，沒改 **進階** 裡的 *每個副本的目標負載（%）* 時是 80%。
3. 看部署頁面：**副本** 底下會寫 *自動，1～4 個 · 目前 2 個*、負載和目標的比較、正在排空的副本，以及為什麼放不下多一個；它的事件會列出每個決定。

開始依賴它之前：

- **空間：**只會在現在放得下的地方加副本。在共用的卡上要留足夠的記憶體，或把部署固定在有空間的機器上。同一個部署的兩個副本能分開時會放在不同機器。
- **啟動需要時間：**新副本要就緒後才幫得上忙（llama.cpp 幾秒，vLLM 一分鐘以上）。這段時間請求在隊伍裡等，有雲端分層的金鑰也可能和以前一樣轉到雲端。
- **移除會等串流做完：**被移除的副本不再接新請求，正在處理的請求不管多長都會做完，最多 30 分鐘；之後還是會移除。
- 要回到固定數量，把 **副本** 設為 **固定數量**（用腳本：`{"params": {"autoscale": null}}` 加上 `replicas` 數字）；在你改數字之前，部署保留它現有的副本。

要在你自己的機器上看到負載增加副本、再排空移除，執行 `scripts/accept_scale.py`（[檢查休眠、喚醒和自動副本數](#check-sleep-waking-and-automatic-replicas)）。

## 在共用機器上限制 GPU

1. 開啟 **機器**，點進這台機器，選擇 **變更 GPU 存取**。
2. 只勾選 Forgeyard 可以使用的卡，例如第 2 和第 3 張，然後選擇 **儲存**。

從此之後：

- 不管應用程式或部署要求什麼，Forgeyard 執行的任何東西都不會分配到其他卡；
- 其他卡會顯示 *not for Forgeyard*；
- 記憶體以每張允許使用的卡回報的**可用**量計算，所以其他程式在同一張卡上的使用量會被考慮進去。

正在使用中的卡不能從清單中移除：請先停止在它上面執行的東西。每次變更都會記錄在稽核紀錄裡，包含變更前後的內容。

如果要讓模型和其他程式共用同一張卡，部署時選擇 **GPU 共用** → **共用 VRAM**：這樣它只會保留估算的記憶體量，vLLM 也會被設定成只使用這麼多（請看 [vLLM 在共用的卡上無法啟動](troubleshooting.md#vllm-does-not-start-on-a-shared-card)）。

## 加入其他人

管理員在 **使用者**：

1. **新增使用者**：一個 **使用者名稱** 和 **角色**（`user`、`operator` 或 `admin`；請見[角色](concepts.md#roles)）。選 **建立並取得連結**。
2. 把一次性連結傳給對方。它用來設定對方的密碼，只能用一次，72 小時後失效。你永遠看不到對方的密碼。

之後在同一份清單上：**密碼連結** 會給忘記密碼的人一個新連結（24 小時）；**登出** 會結束對方所有的工作階段；**停用** 讓對方無法登入，他的 API 金鑰也會失效；**刪除** 會刪除對方。擁有者不能被停用、降級或刪除。

如果要讓別人自己申請帳號，請以 `REGISTRATION=approval` 啟動伺服器。登入頁就會提供申請帳號的連結，申請會在 **使用者** 等待，直到管理員選 **核准** 或 **拒絕**。

## 使用繁體中文的主控台

主控台提供英文和繁體中文（1.5.1）。瀏覽器第一次開啟它時，會跟著瀏覽器的語言：任何中文設定都會顯示繁體中文，其他語言則顯示英文。要切換時，打開帳號選單（右上角你的名稱），在**語言**底下選 **English** 或 **繁體中文**；在手機上，同樣的切換在選單的最後面。頁面會立刻切換，不需要重新載入。

- 這個選擇存在**這個瀏覽器**裡，而不是你的帳號：換一個瀏覽器或電腦，會再從那邊的語言開始。伺服器上什麼都不會儲存，API 用戶端和腳本也不受影響。
- 它**也是使用指南的語言**：指南頁面上方的語言連結和主控台的切換，設定的是同一個選擇，主控台裡的說明連結也會用這個語言開啟指南。
- 數字、大小、時間和日期會跟著語言顯示（*41 秒前*、*2026年11月23日*）。
- 伺服器的訊息仍然以英文撰寫。主控台會依代碼翻譯常見的訊息（例如 `model_waking`、`cannot_wake`、`budget_exhausted`），並以中文說明每一則**設定健康檢查**項目的種類，底下附上伺服器的原文（裡面有機器和模型的名稱）。沒有翻譯的訊息會照伺服器送來的內容顯示。
- 每個頁面都有翻譯：外框（選單、登入、開始使用和設定健康檢查）、**機器**、**服務中**、**模型**、**雲端**、**試用區**、**API 金鑰**、**用量**、**使用者**、**稽核紀錄**、你的帳號，以及**信任這台伺服器**。你自己取的名稱（機器、模型、金鑰）、推論引擎和 agent 印出的內容，以及 API 本身，都維持原樣。

## 使用自己的憑證或反向代理

**使用自己的憑證**（公司的 CA，或真實網域的公開憑證）。在伺服器的 `.env` 裡設定：

```bash
TLS_CERT_FILE=/etc/forgeyard/tls/fullchain.pem
TLS_KEY_FILE=/etc/forgeyard/tls/privkey.pem
TLS_CA_FILE=/etc/forgeyard/tls/company-ca.pem
```

`TLS_CA_FILE` 是簽發這張憑證的 CA；agent 會改為釘選這個 CA，**信任這台伺服器** 也會提供它。如果是公開信任的憑證，就不要設定這一項：這樣加入指令不會帶指紋，agent 會使用系統的信任存放區。伺服器每 12 小時檢查一次這些檔案，憑證更新後不需重新啟動就會換用新的；想立刻換用就重新啟動伺服器。

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

## 用 Docker 執行伺服器

每個版本也會發布伺服器映像檔：`docker.io/tonyhuang1101/forgeyard`，支援 linux/amd64 和 linux/arm64。只需要 Docker，不需要原始碼、uv 或 Node。伺服器以無特權帳號（uid 10001）執行，所有要保存的資料都放在掛載於 `/data` 的 volume 裡：

```bash
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.5.1
sudo docker logs forgeyard
```

紀錄的內容和[快速入門](quick-start.md#1-install-the-server)裡的一樣，包含一次性的設定權杖（setup token）；接著照[建立擁有者並信任 CA](quick-start.md#2-create-the-owner-and-trust-the-ca)繼續。之後要再看一次權杖：`sudo docker logs forgeyard 2>&1 | grep -A 2 "setup token"`。

- **`PUBLIC_URL`**：大家和 agent 使用的位址，連接埠要是你對外公開的那個。伺服器在容器裡只看得到容器自己的位址，所以主機的位址要靠這個設定才會寫進伺服器憑證和 **機器** 頁面上的指令。要加更多名稱或位址：`-e TLS_EXTRA_NAMES=forge.example.com,192.168.1.21`。
- **`-p 7860:7860`**：映像檔在容器所有位址的 7860 連接埠上監聽（`HOST=0.0.0.0`）。想對外公開別的連接埠也可以，例如 `-p 443:7860` 搭配 `PUBLIC_URL=https://192.168.1.20`。
- **`--hostname forgeyard`**：固定主機名稱。沒有它的話，每個新容器的主機名稱都不同，伺服器憑證會在啟動時重新產生。這沒有害處（瀏覽器和 agent 信任的是 CA，不是憑證），只是紀錄比較多。
- **volume** 裡有資料庫、`secret.key`、`secrets.json` 和 `tls/`：升級時要保留。也可以改用主機上的資料夾，擁有者要是 uid 10001：`sudo install -d -o 10001 -g 10001 -m 700 /srv/forgeyard-data`，再用 `-v /srv/forgeyard-data:/data`。
- **其他設定**都用 `-e` 指定（[伺服器設定](reference.md#server-settings)），或用 `--env-file` 搭配 `.env` 檔。`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 會在第一次啟動時直接建立擁有者，不必用設定權杖。要用 PostgreSQL 就設定 `DATABASE_URL`：映像檔裡已經有驅動程式。
- **健康檢查**：映像檔內建健康檢查，`/api/health` 有回應後，`docker ps` 就會顯示 `healthy`。它透過容器自己的 loopback 詢問，而且不檢查憑證：這個請求不會離開容器，也不帶任何機密。

**備份** volume。先停止伺服器，資料庫的副本才會一致：

```bash
sudo docker stop forgeyard
sudo docker run --rm --user 0 -v forgeyard-data:/data:ro -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.5.1 tar czf /backup/forgeyard-data.tar.gz -C /data .
sudo docker start forgeyard
```

用同一個映像檔還原到新的空 volume（tar 以 root 執行，所以檔案會保留原本的擁有者，也就是伺服器的帳號）：

```bash
sudo docker run --rm --user 0 -v forgeyard-data:/data -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.5.1 tar xzf /backup/forgeyard-data.tar.gz -C /data
```

和所有備份一樣，`secret.key` 要和資料庫分開保存（[原因](#back-up-and-restore)）。

**升級**靠 tag。先備份，再拉取新版本，用它執行同一個指令；volume 會保留所有資料，資料庫的變更會在啟動時執行：

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard:1.6.0
sudo docker rm -f forgeyard
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.6.0
```

`1.6.0` 就是那個版本；`1.6` 會跟著它最新的修補版本；`latest` 則是最新的正式版本。建議指定完整版本：agent 的指令使用伺服器的版本，所以升級伺服器後要接著升級 agent（[做法](#upgrade-the-server-and-the-agents)）。

**釘選 digest**：當某個 tag 絕對不能在你不知情時改變。先查出 digest：

```bash
sudo docker buildx imagetools inspect docker.io/tonyhuang1101/forgeyard:1.5.1
```

輸出裡有一行 `Digest: sha256:…`：改用 `docker.io/tonyhuang1101/forgeyard@sha256:…` 取代 tag。agent 的部分，在伺服器上設定 `AGENT_IMAGE=docker.io/tonyhuang1101/forgeyard-agent@sha256:…`：**機器** 就會在每台 agent 的指令裡使用這個 digest。每個映像檔也附有建置來源證明（provenance）和 SBOM（`docker buildx imagetools inspect --format '{{ json .SBOM }}' …`）。

**自己建置**，從原始碼：`sudo docker build -f Dockerfile.server -t forgeyard:1.5.1 .`

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
   uv sync --extra server
   ```

   然後重新啟動它（`sudo systemctl restart forgeyard`）。資料庫的變更會在啟動時自動執行。請閱讀 CHANGELOG，看看有沒有需要額外處理的步驟。用 Docker 的話：拉取新的 tag，再用它執行同一個 `docker run`（[做法](#run-the-server-in-docker)）。

   **升級到 1.4：** 1.3.1 標為淘汰的七個設定已經不再讀取（[清單](reference.md#removed-settings)）。重新啟動之前，如果你設了 `SECRET_KEY_FILE`，請把它改名為 `FORGEYARD_SECRET_KEY_FILE`（否則伺服器會拒絕啟動），其他的則移除；還留著的會列在設定健康檢查裡。
3. **agent**：一台一台來。執行中的 agent 重新啟動時，會保留它的應用程式。
   - **用單行安裝裝的**：再執行一次，但不加 `--join`。它會安裝伺服器的新版本，並保留設定和憑據：

     ```bash
     curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
       sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
       echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
       curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
       sudo sh -s -- --ca-fingerprint sha256:3f9a…
     ```

   - **Docker**：用 `docker pull` 拉取伺服器新版本的映像檔（**機器** 頁面上有名稱），然後執行 `docker rm -f forgeyard-agent`，再用新的 tag 執行同一個 `docker run`。volume 會保留這台機器。每台機器的頁面都會顯示完整的指令（[做法](#update-an-agent)）。
   - **手動安裝**：`git pull`、`uv sync`，然後重新啟動 agent。

**機器** 頁面會顯示每個 agent 的版本。伺服器升級後，請盡快升級 agent：太舊的 agent 可能缺少新版伺服器需要的功能。

1.4.5 起，升級期間版本不同的伺服器和 agent 仍能一起運作，先升級哪一邊都可以：任一邊收到不認得的訊息都會略過（並在自己的紀錄留下警告）；伺服器遇到還不認得的卡片種類時，會略過那張卡，而不是拒絕這個 agent。機器頁面的 **偵測備註** 會把它列為 *unknown vendor*；升級伺服器後才能使用那張卡。

## 更新 agent

每台機器的頁面都有**更新 agent** 區塊。管理員會在這裡看到把這台機器的 agent 更新到伺服器版本的完整指令，它是根據 agent 回報的執行方式產生的（1.4.2 起的 agent 每次連線都會回報）：

- **在 Docker 裡執行**：agent 透過它本來就在用的容器引擎讀取自己的容器：名稱、映像檔、網路、重新啟動政策、GPU（`--gpus`）、裝置與群組 ID（AMD 的 `/dev/kfd`、`/dev/dri` 與數字形式的 `--group-add`）、掛載，以及用 `-e` 給它的變數。指令會拉取伺服器版本的 agent 映像檔，移除舊容器，再用新映像檔啟動同樣的容器：

   ```bash
   sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.5.1 && \
   sudo docker rm -f forgeyard-agent && \
   sudo docker run -d \
     --name forgeyard-agent \
     --restart always \
     --network host \
     --gpus all \
     --device /dev/nvidiactl \
     --device /dev/nvidia-uvm \
     --device /dev/nvidia-uvm-tools \
     --device /dev/nvidia0 \
     --device /dev/nvidia1 \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
     -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     -e HF_TOKEN="$HF_TOKEN" \
     docker.io/tonyhuang1101/forgeyard-agent:1.5.1
   ```

   在 NVIDIA 機器上，指令還會在 `--gpus` 旁邊用 `--device` 傳入 GPU 的裝置節點（1.4.5 起是 agent 回報的那些；較舊的 agent 則用一小段 shell 迴圈列出主機上的），所以主機執行 `systemctl daemon-reload` 時，agent 不會失去它的 GPU。

   經由伺服器取得檔案的機器（**一律經由伺服器**）則改為搬運映像檔：在能連到 Docker Hub 的機器上 `docker save`，透過 `ssh` 直接送進那台機器的 `docker load`（把 `USER` 換掉），再執行同樣的 `docker rm -f` 與 `docker run`。其他機器也會在 *cannot reach the registry?* 底下列出這個做法。
- **用 install.sh 安裝的**：再執行一次單行安裝，但不加 `--join`（[原因](#upgrade-the-server-and-the-agents)）。
- **從原始碼執行的**：`git checkout` 伺服器的版本並執行 `uv sync`，然後用你原本的方式重新啟動 agent。

複製指令，在那台機器上執行。替換 agent 時，它的模型與應用程式會繼續執行；volume（或 install.sh 的狀態目錄）會保留這台機器，幾秒內它就會以原本的身分重新上線。

agent 從不回報任何秘密：不回報 join token（已加入的機器不再需要它）、不回報它的憑據，也不回報名稱或值看起來像秘密的變數（`…TOKEN`、`…KEY`、`…PASSWORD`、Hugging Face 或 GitHub 的 token、含密碼的 URL）。這類變數會顯示為 `-e NAME="$NAME"`：請先在 shell 裡設定它（`export HF_TOKEN=…`），頁面上也會提醒。無法安全重現的值會被略過，並列出名稱。

**比伺服器舊的 agent** 會在 **機器** 的機器方塊與它的頁面上標示：*agent 1.4.0，伺服器 1.4.2：請更新*；點這個提示就會打開指令。與伺服器同版本、但缺少這個版本 agent 應有功能的 agent（用較舊的程式碼建置）也會標示，**設定健康檢查** 會列出這兩種情況。1.4.2 之前的 agent 不會回報執行方式：請先手動更新一次（[做法](#upgrade-the-server-and-the-agents)）；之後它的頁面就會顯示指令。

## 檢查設定健康狀態

Forgeyard 會檢查自己的設定，找出壞掉、有風險或沒有作用的設定和組合：拼錯的設定、可以花錢用雲端供應商卻沒有預算的金鑰、沒有人選擇 GPU 存取的共用機器、比伺服器舊的 agent、先試雲端再試本機層的路由。管理員會在三個地方看到結果：

- **開始使用 → 設定健康檢查** 列出每一項檢查結果，附上該怎麼做，以及到修正位置的連結。
- **頂端列的數字**顯示錯誤和警告的數量。沒有的時候就不會出現。
- **相關頁面上的橫幅**（機器、服務中、API 金鑰、雲端、雲端模型、部署、使用者）顯示那個頁面的檢查結果。

一般使用者和 operator 都看不到這些。

- **錯誤**代表現在就壞了或不安全。它會一直顯示到修好為止。
- **警告**代表可能會花錢、外洩或壞掉。**提示**代表沒有作用或出乎意料。這兩種都可以 **隱藏 30 天**：只對你自己隱藏，30 天後如果情況還在就會再出現。**顯示已隱藏的項目** 會列出它們，並提供 **再次顯示**。

檢查結果在頁面要求時，根據設定、資料庫和各 agent 最後一次的回報算出來，所以修好之後一分鐘內就會反映（下一個頁面則會立刻反映）。設定的問題也會在伺服器啟動時寫進它的紀錄，每一行都以 `Settings:` 開頭。完整清單在參考資料裡：[設定健康檢查的項目](reference.md#setup-health-checks)。

## 從頭到尾檢查一台伺服器

升級之後，或要開始使用新版本之前，`scripts/accept_all.py` 會在你自己的伺服器和機器上檢查每一項功能：只要一個指令，每項檢查印出一行 PASS、FAIL 或 SKIP（SKIP 會說明原因），最後再印出總結。它用管理員帳號透過 API 操作，用官方 OpenAI SDK 呼叫 `/v1`，並用瀏覽器檢查網頁主控台。

在 Forgeyard 的 checkout 裡執行它，所在的電腦要連得到伺服器（金鑰守門員的檢查還需要連得到機器上模型副本的連接埠），並準備好從 **信任這台伺服器** 下載的伺服器 CA：

```bash
FORGEYARD_PASSWORD='…' python3 scripts/accept_all.py --server https://192.168.1.20:7860 \
  --ca forgeyard-ca.pem --user admin \
  --gpu-machine gpu-01 --cpu-machine cpu-01 --second-cpu-machine cpu-02 \
  --cloud-provider openai --out accept-all.json --shots accept-shots --install-playwright
```

每台機器和雲端供應商都可以不給：少了哪一個，需要它的檢查就會跳過，並說明原因。各組檢查依執行順序如下：

| 組別 | 檢查什麼 |
|---|---|
| `server` | 用 CA 驗證的 HTTPS、安全性標頭、陌生的主機名稱（421）、CSRF 規則、用一次性帳號測試登入失敗後的等待、`/guide`、`/trust`、`/install.sh` |
| `machines` | 每台機器都在線上、硬體正確；GPU 允許清單確實生效；加入指令會釘選 CA |
| `workloads` | 行程類服務、任務（成功、重試、`max_duration`）、機器有 Docker 或 Podman 時的容器任務；在 GPU 機器上，應用程式只看得到分配給它的卡 |
| `models` | 部署小模型並等它們就緒：每台 CPU 機器上的 Qwen3-1.7B GGUF；GPU 機器上的對話、embedding、rerank 和語音模型（估算放不下的會跳過）；部署頁面上的估算；**試試看** |
| `gateway` | `/v1`：對話、串流、embedding、rerank、語音轉文字；金鑰的模型允許清單、限制和撤銷；每個副本連接埠上的金鑰守門員；兩台 CPU 機器之間的負載平衡；試用區；用量 |
| `cloud` | 給了 `--cloud-provider` 才會執行：溢出到雲端、設為 **永不** 的金鑰、只用本機、雲端上限、費用，以及預算和它的警示 |
| `passthrough` | 一個公開在 `/x/` 的小型 HTTP 程式：在 CPU 機器上以行程執行，在 GPU 機器上以容器執行。金鑰（none、listed）、照原樣轉送的路徑、被拒絕的路徑、被替換的憑證與轉發標頭、強制的沙箱、轉址、串流、本文上限、rpm、同時請求數、讀取逾時、副本連接埠前的金鑰守門員、用量 |
| `tts`、`img`、`video` | 在 GPU 機器上（有 CPU 推論引擎的也在 CPU 機器上）的語音、圖片和影片生成：SDK 的呼叫、格式、上限、佇列、用量和金鑰守門員 |
| `vision` | 用 vision recipe 部署的偵測、分割和深度模型：結果、拒絕的請求、金鑰和用量 |
| `relay` | 經由伺服器取得模型：把 CPU 機器的 **下載時的網路連線** 設為 **一律經由伺服器**，用 **下載到…** 下載一個小的 GGUF（26 MB），確認它經由伺服器送達；接著第二台機器（`--relay-second-machine`，預設是第二台 CPU 機器，否則是 GPU 機器）從第一台機器的快取取得它。兩台機器的設定都會改回原值，模型也會從它們的快取移除 |
| `images` | 在有 Docker 或 Podman 的機器上（`--engine-machine`，預設是 GPU 機器）：它的推論引擎映像檔清單；腳本自己做出一個只有幾 KB 的映像檔，上傳、複製到第二台可以接收映像的機器（`--second-engine-machine`），再用 **移除映像檔** 移除（因為只有 recipe 指名的映像才能移除，會有一個 recipe 暫時指名它幾秒鐘） |
| `scale` | 閒置時休眠、喚醒（由請求、**立即喚醒**、試用區）、喚醒期間轉到雲端、`model_waking`、`cannot_wake`、**停止**、自動副本數與排空、啟動時間，以及它們的指標（[見下方](#check-sleep-waking-and-automatic-replicas)） |
| `console` | 以管理員和一般使用者身分，在 1440 和 390 px 寬度下打開每個主要頁面：能載入、分頁標題正確、沒有錯誤、不會左右捲動 |

用 `--only gateway,cloud` 或 `--skip console` 選擇要跑的組別；即使沒選 `models`，`gateway` 和 `cloud` 也會部署它們需要的模型。全部沒有失敗時結束代碼是 0，有檢查失敗時是 1，無法開始執行時是 2。

執行前要知道的事：

- **它會自己清理。**它建立的所有東西都命名為 `accept-<run>-…`：部署、應用程式、路由、API 金鑰、一個一次性使用者，以及它必須新增的模型目錄項目。結束時會全部移除，失敗或按下 **Ctrl-C** 也一樣；relay 這組放進機器快取的模型、機器的取得設定，以及 images 這組的映像和 recipe 也會一併復原。這次執行留下的用量紀錄和稽核事件會保留。只有在 1.4.2 以前、無法移除映像的 agent 上，那個很小的映像 `accept-<run>-image:1` 會留下來，報告裡會說明。
- **和別人共用的卡**（1.4.5）：伺服器因為沒有任何一張卡有足夠的可用記憶體而放不下的部署或應用程式，等了 `--place-timeout` 秒（5 分鐘）之後就會跳過，並附上伺服器給的原因（`Cannot be placed: GPU #4 has 1.8 GiB available, needs 3 GiB`），不會一直等到 `--timeout`（40 分鐘；下載和啟動仍然以它為上限）。每個會在 GPU 機器上部署的組別（`tts`、`img`、`video`、`passthrough`、`vision`、`scale`）開始之前，這次執行會先停止前面組別留在 GPU 上、後面組別也用不到的東西，再把它移除（`models` 組的部署只會停止，因為 console 組要顯示其中一個；`tts` 還可能用到的語音轉文字模型會保留），讓每一組都能用到這次執行在那張卡上的空間。報告的備註會列出釋放了哪些東西。
- **只有一個請求會送到雲端供應商**，內容是固定的一小段提示，費用不到一美分。供應商的每月預算會被改動幾秒鐘再改回原值；其他預算檢查只用這次執行自己的金鑰和使用者。
- **一次性使用者會有五次登入失敗**：伺服器允許每個位址在 15 分鐘內失敗 30 次，所以連續執行很多次時，中間要等一下。
- **時間：**機器上已經有模型時大約 15 到 20 分鐘（沒有 GPU 機器時約 3 分鐘）；需要下載的話另外加上下載時間。
- 加上 `--kill-local` 還會檢查容錯移轉：它會強制結束在執行這支腳本的電腦（兩台 CPU 機器之一）上那個 CPU 副本的推論引擎。
- 主控台的檢查需要 Playwright for Python 和 Chrome（`--chrome`，預設 `/opt/google/chrome/chrome`）；`--install-playwright` 會把 Playwright 和 OpenAI SDK 一起裝進腳本自己的虛擬環境（`--venv`）。沒有的話，console 這組會跳過。
- **副本連接埠的呼叫方式和閘道一樣。**agent 以 TLS 提供服務的機器（1.4.5）上，金鑰守門員的檢查（這裡和 `scripts/accept_*.py` 都一樣）會用 TLS 連到副本的位址，並要求憑證上帶有該機器的綁定名稱（`m<id>-<hash>.replica.forgeyard.invalid`，在 **機器** 可以看到），而且只信任 Forgeyard 的 CA。每個這樣的連接埠還會多三項檢查：純 HTTP 會被拒絕、憑證能以這台機器的名稱驗證通過，以及另一台機器的名稱（有兩台機器用 TLS 時）和不屬於任何機器的名稱都會被拒絕。agent 較舊的機器照舊用純 HTTP 呼叫，並多一行說明它的流量沒有加密。CA 就是 `--ca` 給的那一個；伺服器使用自己的憑證或 `TLS=off` 時，把 `FORGEYARD_REPLICA_CA` 指向伺服器的 `data/tls/ca.crt`。完全沒有 CA 時呼叫照樣進行，只有憑證檢查會跳過。只有完全連不到的連接埠會當成在別的網路而跳過；連上後被關閉連線或憑證不對，都算失敗。

### 檢查休眠、喚醒和自動副本數

`scripts/accept_scale.py` 會在你自己的伺服器和機器上檢查 1.5 的[閒置時休眠](concepts.md#sleep-when-idle)和[自動副本數](concepts.md#automatic-replicas)。它也是 `accept_all.py` 的 `scale` 組，選項和報告都一樣：

```bash
FORGEYARD_PASSWORD='…' python3 scripts/accept_scale.py --server https://192.168.1.20:7860 \
  --ca forgeyard-ca.pem --user admin \
  --cpu-machine cpu-01 --second-cpu-machine cpu-02 --gpu-machine gpu-01 \
  --cloud-provider openai --out accept-scale.json
```

它在 CPU 機器上用 llama.cpp 部署 Qwen3-1.7B GGUF，開啟 **閒置時休眠**，閒置時間 1 分鐘（`--scale-idle-minutes`），然後依報告裡的編號檢查：

1. **它會休眠：**副本停止、它們的連接埠不再回應，它在機器上的保留量被釋放（`/metrics`，扣掉機器上其他副本占用的部分來比對），`/v1/models` 仍然列出它而且不會因此喚醒它，模型也留在機器的快取裡。
2. **請求會喚醒它，而且只喚醒一次：**同時送出 20 個請求（`--scale-parallel`）只會喚醒一次（一個 *Woken by a request* 事件，`forgeyard_deployment_wakes_total{by="request"}` 多 1），而且全部都得到回應。
3. **立即喚醒**（Start）和試用區的請求也會喚醒它，事件會寫出是誰喚醒的。
4. **喚醒期間轉到雲端**，只有給了 `--cloud-provider` 才會執行：供應商成為模型路由的第 1 層；可以溢出的金鑰立刻由雲端回應（`X-Forgeyard-Spilled: waking`，這是唯一會送到供應商的請求），同一把金鑰的結構化輸出請求會等待並由本機回應，模型就緒後這把金鑰又回到本機服務。
5. **很短的等待：***喚醒等待* 設為 10 秒（`--scale-wake-wait`）時，請求會得到 503 `model_waking`，`Retry-After` 至少 5 秒、最多是上次啟動的時間，而喚醒會繼續進行。只有啟動比這更慢的模型才看得出來：CPU 上的 llama.cpp 模型幾秒就就緒，所以這一步在那裡會跳過並說明原因。給了 `--gpu-machine` 時，這次執行也會在那台機器上用 vLLM 部署 Qwen3-0.6B，並對它檢查第 1、2、5 步。
6. **無法喚醒**，只有在你指定一台允許這次執行停止 agent 的機器時才會執行：`--stoppable-agent cpu-01` 加上 `--agent-stop` 和 `--agent-start`，這兩個 shell 指令在執行腳本的電腦上執行（啟動指令必須立刻結束，讓 agent 繼續執行）。模型休眠時，把它固定在那台機器上，再停止 agent；請求會立刻得到 503 `cannot_wake`，並指出那台機器；agent 回來後，模型會啟動並回應。沒有這些選項時這一步會跳過：腳本絕不會停止沒有被告知可以停止的機器。
7. **Stop 不會被喚醒：**對休眠中的部署按 **停止**；請求會得到 503 `no_ready_replica`，沒有任何東西會喚醒它，`/v1/models` 也不再列出它。
8. **自動副本數**，在兩台 CPU 機器上：副本設為 *依負載自動調整*、從 1 到 2、*Concurrent requests* 為 2、縮減時間窗為 2 分鐘（`--scale-down-minutes`）的部署。過了它的 2 分鐘暫停後，4 個用戶端（`--scale-clients`）會在大約一分鐘內增加第二個副本，放在另一台機器上；每個請求都得到回應，沒有一個被執行兩次（每個副本的服務次數等於送給它的請求數）。接著在新副本上跑一個很長的串流：串流進行中縮減會排空它，這段時間的短請求都送到另一個副本，串流完整結束，副本要等串流最後一個位元組送出之後才會被移除。之後一分鐘內不會再加回副本。沒有第二台 CPU 機器，或放不下兩個副本時，這一步會跳過。
9. **自動副本數加上休眠：**同一個部署開啟 *閒置時休眠*、最少為 0，會以 1 個副本休眠，請求會以 1 個副本喚醒它。
10. **啟動時間和指標：**每個部署上次啟動的時間（`last_start`），以及 `/metrics` 上的啟動時間、喚醒次數、等待喚醒的請求、部署的副本數和負載、自動調整的決定，以及喚醒期間轉到雲端的請求。

執行前要知道的事：

- **時間：**休眠的模型、自動副本數和 vLLM 模型會同時進行，機器上已經有模型時大約 15 分鐘；第 6 步和第 7 步在它們之後，每一步都要再等一次休眠。
- **指標：**伺服器沒有設定 `METRICS_TOKEN` 時，`/metrics` 只回應來自伺服器本機的直接請求。請在那台主機上執行腳本，或給 `--metrics-token`（或 `FORGEYARD_METRICS_TOKEN`）；否則指標的檢查會跳過並說明原因。
- **它會自己清理，**和 `accept_all.py` 一樣：所有東西都命名為 `accept-<run>-scale-…`（部署、它們的路由、API 金鑰、它必須新增的模型目錄項目），結束時全部移除，失敗或按下 **Ctrl-C** 也一樣；它停止過的 agent 會再啟動。伺服器放不下的部署，等了 `--place-timeout` 秒之後會跳過，並附上伺服器給的原因。

## 用 Prometheus 收集 Forgeyard 的指標

伺服器以 Prometheus 的文字格式回應 `GET /metrics`：閘道每個模型的請求數、延遲和 token、轉到雲端的請求、排隊情況、各狀態的副本、機器、GPU 記憶體和雲端花費（[所有指標](reference.md#metrics)）。標籤只用模型、機器和供應商的名稱，絕不會有金鑰、使用者或提示內容。

**誰可以讀取。**沒有設定 `METRICS_TOKEN` 時，只有來自伺服器本機、且沒有經過任何 proxy 的請求可以讀（沒有 `X-Forwarded-For`、`Forwarded` 或 `X-Real-IP`）；其他人會得到 403。設定了 `METRICS_TOKEN` 之後，每一次收集都需要 `Authorization: Bearer <token>`，不論從哪裡來，伺服器本機也一樣；沒帶或帶錯會得到 401。

**Prometheus 在伺服器本機上**，不需要權杖。從 **信任這台伺服器** 把伺服器的 CA 存成 `/etc/prometheus/forgeyard-ca.pem`；伺服器的憑證一定包含 `127.0.0.1`：

```yaml
scrape_configs:
  - job_name: forgeyard
    scheme: https
    tls_config:
      ca_file: /etc/prometheus/forgeyard-ca.pem
    static_configs:
      - targets: ["127.0.0.1:7860"]
```

**Prometheus 在其他地方，或在 Docker 裡：**在伺服器上設定一個權杖（一段很長的隨機值，例如 `openssl rand -hex 32`），寫進它的 `.env` 或用 `-e METRICS_TOKEN=…`，重新啟動伺服器，再把同一個值放在檔案裡給 Prometheus：

```yaml
scrape_configs:
  - job_name: forgeyard
    scheme: https
    authorization:
      credentials_file: /etc/prometheus/forgeyard-metrics-token
    tls_config:
      ca_file: /etc/prometheus/forgeyard-ca.pem
    static_configs:
      - targets: ["192.168.1.20:7860"]
```

- **Docker 需要權杖。**經由對外公開的連接埠進到容器的請求，不是從容器自己的 loopback 來的；Prometheus 容器則是另一台主機：兩者都不算「本機」。
- **`targets` 裡的名稱**必須是伺服器會回應、而且憑證上有的名稱：IP 位址一定可以。如果用名稱，例如 Docker 網路上伺服器的容器名稱（`forgeyard:7860`），請把它加到 `ALLOWED_HOSTS` 和 `TLS_EXTRA_NAMES`（否則會是 421，或憑證錯誤）。
- **在你自己的反向 proxy 後面**時，帶著權杖經由它收集：它轉送的請求絕不會被當成本機的請求，即使來自 `127.0.0.1`。

可以從這些查詢開始：

| 查詢 | 顯示什麼 |
|---|---|
| `sum by (model) (rate(forgeyard_gateway_requests_total[5m]))` | 每個模型每秒的請求數 |
| `sum by (model) (rate(forgeyard_gateway_requests_total{status=~"5.."}[5m]))` | 每秒的伺服器錯誤 |
| `histogram_quantile(0.95, sum by (le, model) (rate(forgeyard_gateway_time_to_first_byte_seconds_bucket[5m])))` | 第一個位元組時間的第 95 百分位數 |
| `sum by (model, reason) (rate(forgeyard_gateway_spills_total[1h]))` | 請求為什麼離開本機層 |
| `forgeyard_machine_up == 0` | 離線的機器（適合做告警） |
| `forgeyard_gpu_memory_free_bytes / forgeyard_gpu_memory_total_bytes` | 每張卡可用記憶體的比例 |

伺服器重新啟動時計數器會從 0 開始（Prometheus 的 `rate()` 會處理）。狀態類的指標（副本、機器、記憶體、花費）在每次收集時從資料庫讀取：每 15 到 60 秒收集一次就好，不要更快。

## 備份與還原

**伺服器上要備份的東西**，除非你搬過位置，否則都在它的 `data/` 資料夾裡（用 Docker 的話是 `/data` volume，[這樣備份](#run-the-server-in-docker)）：

| 項目 | 原因 |
|---|---|
| `data/forgeyard.db`（或你的 PostgreSQL 資料庫） | Forgeyard 知道的一切：使用者、機器、模型、部署、金鑰、用量 |
| `data/secret.key` | 它負責加密儲存的機密資料（Hugging Face 權杖、副本的金鑰、應用程式的環境變數值）。請和資料庫備份**分開**存放 |
| `data/tls/` | 每台裝置和每個 agent 信任的 CA。少了它，每台裝置和每個 agent 都得重新信任一個新的 CA |
| `.env` | 你的設定 |

在伺服器執行中取得 SQLite 資料庫的一致副本：

```bash
uv run --no-sync python -c "import sqlite3; sqlite3.connect('data/forgeyard.db').backup(sqlite3.connect('/backup/forgeyard.db'))"
cp -a data/tls data/secret.key .env /backup/
```

PostgreSQL：照常使用 `pg_dump`。

備份好 `secret.key` 之後，到 **開始使用 → 設定健康檢查** 勾選 **我已經備份了識別碼 … 的金鑰**。在有管理員勾選之前，設定健康檢查會提醒每一位管理員；金鑰更換後也會再次提醒。

**或從主控台下載金鑰**（1.4.2）。管理員可以把機密金鑰和伺服器的 CA（`ca.key` 和 `ca.crt`）下載成一個檔案，用自己選的通關密語加密：

1. 在 **開始使用 → 設定健康檢查** 的提醒上按 **下載備份…**，或在 **信任這台伺服器** 頁面的 **給管理員** 底下按 **下載金鑰備份…**。
2. 再輸入一次你的密碼（輸錯的次數和登入失敗一起計算：錯幾次之後就要等一下），以及兩次備份通關密語：至少 12 個字元，而且不能是你的密碼。請把通關密語記下來：沒有它，任何東西都打不開這個檔案。
3. 按 **下載備份**。瀏覽器會存下 `forgeyard-key-backup-<金鑰 id>-<日期>.fykey`。

下載完成就等於勾選了這把金鑰 id 的提醒，並記在稽核紀錄裡（`secret_key.backup_downloaded`）。伺服器不會保留檔案，也不會保留通關密語。請把檔案和資料庫備份分開存放，通關密語也要和檔案分開：兩者合在一起就能讀出所有儲存的機密，並簽出你的裝置信任的憑證。檔案裡沒有伺服器憑證：伺服器啟動時會用 CA 重新產生一張。使用自己憑證（`TLS_CERT_FILE`）或 `TLS=off` 的伺服器沒有 Forgeyard 自己的 CA，檔案裡就只有金鑰；你的憑證請自己備份。檔案格式見[參考](reference.md#the-key-backup-file)。

**每台機器上**（選擇性）：`/var/lib/forgeyard-agent`（憑據；少了它，機器就得用新的權杖重新加入）和 `/etc/forgeyard/agent.toml`。模型快取隨時可以重新下載。

**還原**：停止伺服器，把檔案放回 `data/`（以及 `.env`），再啟動它。如果 `secret.key` 錯了或不見了，伺服器會拒絕啟動並說明原因，而不是把機密資料弄丟。

如果用的是從主控台下載的金鑰備份，先把資料庫放回去，然後在伺服器停止的狀態下執行：

```bash
uv run --no-sync python -m forgeyard secrets restore forgeyard-key-backup-ab12cd34-20261117.fykey
```

它會詢問通關密語，然後寫入 `data/secret.key`（權限 600）以及 `data/tls/ca.key`（600）和 `ca.crt`（資料夾 700）；加上 `--data-dir DIR` 就寫到另一個資料夾。通關密語錯誤、檔案被改過或不完整時，什麼都不會寫入。如果那裡已經有一把不同的金鑰或 CA，它絕不會直接取代：它會停下來並說明原因；加上 `--force` 才會取代，舊檔案保留為 `<名稱>.replaced-<時間>`。那裡已經是同一把金鑰則沒有問題。`--passphrase-file PATH` 會從檔案的第一行讀取通關密語，給腳本使用。伺服器啟動時一樣會檢查這把金鑰是不是屬於這個資料庫。

如果金鑰真的遺失了，請停止伺服器，然後用以下指令重新開始：

```bash
uv run --no-sync python -m forgeyard secrets reset --yes
```

它會產生一把新的金鑰，並捨棄舊金鑰加密的內容：伺服器的 Hugging Face 權杖（以及各部署自己的）要重新設定，模型副本會用新的內部金鑰重新啟動，環境變數值遺失的應用程式則必須重新建立（它們的事件裡會說明）。

## 查看模型或推論引擎

打開 **模型**，在 **目錄** 點模型的名稱，或在 **推論引擎** 分頁點推論引擎的名稱（其他地方的名稱也都連到這些頁面：**服務中**、部署的頁面、機器的模型快取、**下載到…**）。所有登入的人都可以查看。

**模型頁面**（`/models/<name>`）最上面一行是重點（任務、大小、授權、用第一個推論引擎的預設值一份副本需要多少、有幾個部署），下面是跳到各區段的連結：

- **概覽**：來源與版本（附 Hugging Face 連結）、授權、任務和它在 `/v1` 底下回應的路徑、架構。
- **機器**：每台機器的模型快取裡有沒有這個模型（以它的 agent 最後一次回報為準，附在那裡的大小和誰在用）、一份副本現在**放不放得下**（先看 GPU，再看 CPU；和部署頁面同一個估算，用推論引擎的預設值），以及它會用的推論引擎變體和那個映像檔或程式是否已經在那裡。在表格上方選另一個推論引擎，就會看到那個推論引擎的結果。**部署到這裡** 和 **下載到這裡** 會從那台機器開始。
- 這個模型的 **部署**，和 **可以執行它的推論引擎**（各自連到推論引擎頁面）。
- **檔案**，收成一行（機器會下載幾個檔案、總大小、模型檔）；**顯示檔案** 會列出每個檔案和大小，以及儲存庫裡不會下載的其他檔案（其他量化版本和投影器檔）。

**部署**、**下載到…** 和 **移除** 在最上面，和目錄裡一樣。管理員可以在這裡設定**顯示名稱**，例如把 `accept-qwen3-4b` 顯示成 *Qwen3 4B*；留空就顯示名稱。名稱本身不會改變。

**推論引擎頁面**（`/models/engines/<name>`）有 **概覽**（它服務什麼、在哪個路徑、讀哪些格式、怎麼共用 GPU）、**變體**（每種硬體的映像檔或程式、映像檔是否以 digest 釘選、機器需要什麼，以及把映像檔帶到連不到 registry 的機器的指令）、**機器**（每台機器會用的變體，以及映像檔是否已在那台機器的容器引擎裡（以它最後一次列出的映像檔為準），或程式是否在模型快取裡）、**參數**（收成一行：預設值和範圍），以及使用它的**部署**和**目錄模型**。打開頁面時，連線中的機器如果上次列出映像檔已超過十分鐘，會重新列一次；**重新列出** 會立刻再問一次。離線的機器顯示它最後一次的清單。管理員可以在這裡**編輯**或**刪除**自訂推論引擎，或把內建推論引擎**複製**成自訂推論引擎（例如試用較新的映像檔）。

## 查看推論引擎有沒有新版本

Forgeyard 把每個內建推論引擎釘選在一個版本（vLLM 0.32.0、llama.cpp 1.0.0…），只在自己的版本裡、測試過後才更新。想知道上游這段期間有沒有發布更新的版本，管理員可以打開 **模型 → 推論引擎**，按 **檢查推論引擎有沒有新版本**。這個檢查不會定時執行，也不會改變任何東西：它只讀取資料。

伺服器會透過自己的[網路規則](reference.md#models)（只連 https 和公開位址；你網路裡的鏡像站用 `MODEL_ALLOW_PRIVATE_HOSTS`），向每個內建變體**自己的**映像檔儲存庫（Docker Hub 或 GHCR）查詢它的標籤，再向 GitHub 讀取找到的版本的版本說明。大約要半分鐘（最多一分半鐘）。結果會保留到下次檢查，並記下檢查的時間和執行的人，所有登入的人都看得到；每次檢查都會記在稽核紀錄裡。

- **卡片** 會加上標記：*有新版 0.34.0*、*有較新的建置*、*已是最新* 或 *無法檢查*（registry 或 GitHub 連不上時；滑鼠移到標記上會說明原因，其他推論引擎照樣會檢查）。點標記會打開那個推論引擎的頁面。
- **推論引擎頁面** 有一個 **新版本** 區段，每個較新的版本一塊，最新的在最前面：
  - 哪些變體有那個版本的映像檔，附上它的標籤和現在的 digest，以及其他變體為什麼沒有（例如 *docker.io/vllm/vllm-openai 沒有 v0.34.0-cu129 映像檔；這個變體的映像檔只到 0.32.0*：vLLM 已經不再發布 CUDA 12.9 的映像檔）。行程變體（`cpu-process`）只有在發布版本提供同一種壓縮檔和它的 SHA-256 時才會包含；
  - 版本說明的連結，以及說明裡提到的、這個推論引擎會傳入的參數和設定（vLLM 0.34.0 提到 `--gpu-memory-utilization`，它的預設值和意義都變了）。請先讀這些：新版本可能改變預設值和記憶體用量，而 Forgeyard 的估算是用目前釘選的版本量測的。

怎樣算是較新的版本：以映像檔發布的**穩定**版本，而且標籤形式和內建的一樣（`v0.32.0-cu129` 變體對應 `v0.34.0-cu129`）。候選版本（rc）、每日建置和特定模型專用的映像檔一律不列；GitHub 上已發布、但映像檔還沒出來的版本只會提一下。每個較新的版本都會列出，你可以選跨度比較小的那個。只用會移動的標籤發布的推論引擎（stable-diffusion.cpp 的 `master-cuda`），在標籤指向較新的映像檔時會顯示**有較新的建置**。Forgeyard 自己的視覺映像檔不檢查：它跟著 Forgeyard 的版本一起更新。

## 試用推論引擎的新版本

檢查找到較新的版本之後，管理員可以在不動到內建推論引擎的情況下試用它。在推論引擎的頁面上，**新版本** 裡的每個版本都有 **試用這個版本** 按鈕。它會建立一個**自訂推論引擎**：內建推論引擎的複本，改用那個版本的映像檔，名稱是 `<推論引擎>-x-y-z`（例如 `vllm-0-34-0`，名稱可以修改）。其他都不會變：內建推論引擎維持原樣，在你自己選擇之前，不會有任何部署改用這個複本。

建立之前，對話框會先說明：

- **複本包含哪些變體**：有新版本映像檔的變體。沒有映像檔的變體不會包含，並附上原因（vLLM 0.34.0 沒有 CUDA 12.9 的映像檔）；需要那個變體的機器會繼續使用內建推論引擎。行程變體（`cpu-process`）只有在發布版本提供同一種壓縮檔和它的 SHA-256 時才會包含。
- **簽章**：llama.cpp 會為它的映像檔和發布壓縮檔簽章（GitHub 建置證明，artifact attestation）。Forgeyard 建立複本時會逐一檢查簽章；只要有一個驗證不過或找不到，就不會建立複本。如果完全無法檢查簽章（伺服器連不到 GitHub 或 Sigstore），對話框會說明原因，並提供 **不驗證，照樣建立**：這樣建立的複本，在所有顯示簽章的地方都會標示為 *未驗證*，稽核紀錄也會記下是誰做的選擇。vLLM、vLLM-Omni 和 speaches 沒有提供簽章，stable-diffusion.cpp 只附上沒有簽章的建置紀錄：它們的複本只以 digest 釘選，對話框和頁面都會清楚說明。
- **要先讀什麼**：新版本可能改變參數、預設值和記憶體用量。複本會沿用內建推論引擎原本的參數，而 Forgeyard 的估算是以內建的版本量測的。對話框會再列一次版本說明提到的參數，並附上版本說明的連結。

按 **建立複本** 時，Forgeyard 會重新查詢每個映像檔標籤指向的內容。只要有標籤指向的映像檔和你檢查時不同，就什麼都不會建立：請再檢查一次，然後再試用。每個映像檔都以 digest 釘選，並記下 Forgeyard 辨認它所需要的 ID（和內建映像檔記錄的 ID 相同）。所以複本的映像檔可以**經由伺服器**送到連不到 registry 的機器、用 `docker load` 載入後可以**離線執行**，也會出現在機器頁面上、可以用 **移除** 刪掉，和內建的映像檔一樣。建立複本會以 `recipe.created` 記進稽核紀錄，並寫明它的來源。

複本的頁面會說明它的來源、建立時間和建立者，每個變體釘選的 digest 和驗證了什麼（簽章的 workflow 和建置紀錄，或是 *上游未簽章*），以及哪些變體沒有包含。目錄中為內建推論引擎設定的推論引擎參數（例如 Qwen3-Reranker 給 `vllm-pooling` 的 `--hf-overrides`），也同樣適用於它的複本。

要使用複本，建議先[試跑](#try-out-a-new-engine-version-before-switching)，再到部署的 **編輯** 頁面把部署換過去（[把部署換到推論引擎的其他版本](#move-a-deployment-to-another-engine-version)），或用它部署一個模型（在部署頁面選它當推論引擎），先和內建推論引擎比較過，再把其他部署換過去。複本頁面上的 **下載到…** 可以先把它的映像檔下載到機器上（[預先下載推論引擎](#download-an-engine-ahead-of-time)）。用完之後請刪除複本：還有部署在使用時無法刪除。刪除之前，請先從各台機器移除它的映像檔（機器頁面的 **推論引擎的映像檔**）：複本刪除之後，Forgeyard 就不再把那個映像檔當成自己的。

## 換推論引擎版本前先試跑

把部署換到推論引擎的新版本之前，可以先**試跑**：Forgeyard 會在一台機器上用它暫時部署一個模型，大約幾分鐘，檢查完再移除。通常是試跑用[試用這個版本](#try-a-newer-version-of-an-engine)建立的複本，不過任何推論引擎都能試跑；試跑內建推論引擎，可以得到它的複本拿來比較的基準數字。管理員和操作員可以開始和取消試跑；所有登入的人都能看到結果。

1. 打開推論引擎的頁面（**模型**，**推論引擎** 分頁，再點推論引擎），按 **試跑**。
2. 選一個這個推論引擎能提供的**模型**，以及一台**機器**（能執行這個推論引擎某個變體的機器；離線的機器不能選）。**參數**會沿用這個模型在內建推論引擎上的部署（如果有的話），讓試跑的方式和目前提供服務的方式一樣；沒有的話就用推論引擎的預設值。你也可以在對話框裡修改（context、並行數，GPU 上可以選 **獨占** 或 **共用 VRAM**），進階參數則維持原本的值。
3. 對話框會顯示估算，以及目前放不放得進那台機器；放不下時會像一般部署一樣說明原因。如果機器上還沒有這個推論引擎的映像檔，對話框也會提醒：試跑會先拉取映像檔，可能要花上好幾分鐘（vLLM 的映像檔大約 9 GiB）；同一個頁面上的 **下載到…** 可以預先下載。
4. **啟動時限**限制啟動的時間，下載也算在內（預設 30 分鐘，可以設 5 到 180 分鐘）。按 **開始試跑**。

試跑的部署是**隔離**的：它沒有路由，所以不會出現在 `/v1/models`，任何 API 金鑰或試用區都無法呼叫它，閘道也不會替它記錄用量或費用。它不會休眠，也不會被自動調整副本數；服務中頁面只會把它列為進行中的試跑，機器頁面則把它佔用的卡標示為推論引擎的*試跑*。試跑進行期間，推論引擎和模型都不能刪除。一台伺服器同時最多四個試跑，每個推論引擎一個。

接著 Forgeyard 會直接對副本送出檢查（帶著副本的內部金鑰，和 **試試看** 一樣）：

| 檢查 | 內容 | 未通過的情況 |
|---|---|---|
| **啟動** | 多久才就緒，分段列出（模型、映像檔、推論引擎），並和內建推論引擎上次啟動同一個模型的時間比較 | 無法放置、啟動失敗（會保留它最後的輸出），或在時限內沒有就緒 |
| **記憶體** | 它在 GPU 上（或機器 RAM 中）用了多少：就緒後的機器讀數減去推論引擎啟動前的讀數，各取幾次，並和副本預留的量比較 | 用量超過預留的量（考量忙碌的卡上讀數會有誤差，容許 5%） |
| **回應** | 先送一個請求暖機，再依模型種類送五個：對話（固定的問題，temperature 0，最多 128 個 token）、嵌入、重排序（關於巴黎的句子必須排第一）、文字分類、語音轉文字（測試音檔裡的話）、文字轉語音、圖片（一張）、視覺 | 有回應不是該種類的格式，或是錯誤 |
| **延遲** | 那五個請求的 p50 和 p95，以及第一個請求的時間 | —（只顯示，不評判） |
| **回應比較** | 如果內建推論引擎上同一個模型的部署已經就緒，會把同樣的請求送給它一次，比較兩邊的回應是否相同 | —（新版本的回應可能不一樣：請兩邊都看過） |

影片不會送請求（產生一段影片要好幾分鐘），只檢查它的啟動和記憶體。如果這張卡也有其他工作在使用，記憶體量測只能當作參考，檢查結果也會註明。沒有任何檢查未通過，試跑就算**通過**。通過之後，或有檢查未通過、你按了 **取消試跑**、超過時限，或試跑期間伺服器重新啟動時，它的部署都會被移除。試跑會記進稽核紀錄：先是 `engine_trial.started`，之後是 `engine_trial.finished` 或 `engine_trial.cancelled`。

推論引擎頁面的 **試跑紀錄** 會保留每次試跑，最新的在最上面：模型、機器、執行者和結果。按 **檢查項目** 會展開那次試跑的表格（量測值、比較基準、說明），以及它和內建推論引擎的比較：內建推論引擎上同一個模型的部署（上次啟動的時間和預留的量），以及內建推論引擎最近一次通過的試跑（啟動時間、記憶體和延遲）。**推論引擎** 分頁的卡片會顯示最近一次的結果；部署的 **編輯** 頁面也會說明所選版本上次試跑的結果（*上次在 gpu-01 上用 qwen3-0.6b 試跑：通過*），或還沒有試跑過。這些都只是參考資訊：沒有試跑過的版本，也一樣可以換過去。

用程式操作：`POST /api/recipes/{name}/trials`（[參考資料](reference.md#trial-runs-through-the-api)）。

## 把部署換到推論引擎的其他版本

部署可以在內建推論引擎和用[試用這個版本](#try-a-newer-version-of-an-engine)建立的複本之間切換，例如從 `vllm`（vLLM 0.32.0）換到 `vllm-0-34-0`，之後再換回來。部署的名稱、提供服務的模型名稱、API 金鑰和路由都不變，用戶端只會遇到一次重新啟動。

1. 在部署的頁面選擇 **編輯**（管理員和操作員）。
2. 在 **推論引擎** 底下的 **推論引擎版本** 選擇版本：內建的版本，以及每個用同樣方式提供這個模型的複本。沒有複本的推論引擎只會列出自己，並附上它頁面的連結，可以在那裡檢查有沒有新版本。
3. 表單會改成新版本的參數。**儲存後會發生什麼** 會列出推論引擎的變更、新版本不接受的參數（會被拿掉）、副本怎麼重新啟動，右側則是新的估算（複本計算記憶體的方式可能不同）。現在放不下的版本，會和其他修改一樣被拒絕。
4. 按 **儲存並重新啟動**。

副本會像其他修改一樣重新啟動：有多個副本時一次一個，模型會持續回應。只有一個執行中的副本時，新的就緒之前模型不會回應，表單會在你儲存之前先說明：

- 大約多久：依它上次啟動的時間（來自它的事件）；沒有的話，依這台伺服器上同一個推論引擎其他部署最近幾次啟動時間的中間值；再沒有，就用這個推論引擎一般的啟動時間（vLLM 在 A100 上大約兩分鐘，llama.cpp 只要幾秒）；
- 新版本的推論引擎第一次啟動可能比較久（它可能要先編譯）；如果機器上還沒有它的映像檔，會先下載（[先下載好](#download-an-engine-ahead-of-time)）；
- 怎麼避免這段空檔：在機器放得下兩個副本時，在同一次修改裡把 **副本** 調成 2（新副本會先啟動），或開啟自動調整副本數；如果部署的路由有雲端層，這段期間可以使用雲端的 API 金鑰會改由雲端層回應。

它只是說明：儲存後的行為和以前一樣。要在舊版本旁邊先啟動新版本，卡上必須放得下兩份，共用的 GPU 常常沒有這個空間，所以 Forgeyard 不會替你這樣做。

要換回來，再選一次內建推論引擎就好。這次變更會以 `deployment.changed` 記入稽核紀錄，包含 `recipe`（換之前和換之後）、兩個版本和被拿掉的參數；部署的事件會寫 *engine vllm → vllm-0-34-0*。部署的頁面會註明它執行的是複本（*執行的是複本：vLLM 0.34.0（以 vllm 為基礎）*）。之後的 Forgeyard 版本把內建推論引擎升到複本的版本或更新時，複本的頁面會說明：先把部署換回來，再刪除複本。不會有任何東西自動切換。

用程式修改：`PATCH /api/deployments/{id}` 加上 `"recipe"`（[參考](reference.md#change-a-deployment-through-the-api)）。

## 預先下載推論引擎

推論引擎頁面（內建推論引擎或複本）上的 **下載到…** 只會把推論引擎下載到你選的機器：每台機器會執行的變體的映像檔（依它釘選的 digest），或它的行程程式（用 SHA-256 核對）。不含模型，也不會啟動任何東西。管理員和操作員可以使用。例如在把部署換過去之前，先把 9 GiB 的 vLLM 映像檔放到機器上。

1. 打開推論引擎的頁面（**模型** 的 **推論引擎** 分頁，再點推論引擎），按 **下載到…**。
2. 勾選機器。每一行會寫出那台機器會拿到的變體，以及能不能下載：**放得下**、**已經在這裡**、**下載中**、**離線**、**請更新 agent**，或是 **沒有適用的變體**（並說明原因：這個推論引擎沒有能在那台機器上執行的變體，例如機器上沒有 vLLM 支援的卡）。
3. 按 **下載到 N 台機器**。

進度會顯示在推論引擎的頁面上（機器表格裡，以及 **這個推論引擎的下載** 底下），也會顯示在每台機器的頁面上，並可以 **取消**。設成經由伺服器下載的機器，會經由伺服器拿到映像檔（[經由伺服器把模型送到離線機器](#give-an-offline-machine-models-through-the-server)）：複本的映像檔和內建的一樣，會依建立時記下的 ID 經由伺服器傳送。每次要求和取消都會記入稽核紀錄（`engine.download_requested`、`engine.download_cancelled`），刪除推論引擎時，它正在進行的下載也會結束。API 見[參考資料](reference.md#download-a-model-ahead-through-the-api)。

## 部署之前先下載模型

**下載到…**會現在就把模型，以及預設也把它的推論引擎，放進你選的機器的模型快取，這樣在那台機器上部署時就不用等下載。不會啟動任何東西。admin 和 operator 可以使用。

1. 打開 **模型**（**目錄** 分頁），在模型那一列按 **下載到…**。或打開機器的頁面，在 **快取中的模型** 上方按 **下載模型到這裡…**，再選模型。
2. 勾選機器。每一行會說明那台機器能不能下載：
   - **放得下**：要下載多少、可用的磁碟空間，以及會一起抓的推論引擎；
   - **已經在這裡**：模型（和推論引擎）已經在那台機器的快取裡；
   - **下載中**：那台機器上已經在下載這個模型；
   - **離線**、**沒有模型快取**、**磁碟空間不足**（模型加上 1 GiB 的保留空間）；
   - **請更新 agent**：1.4 以前的 agent 不能先下載。
3. **連同推論引擎** 預設是勾選的。依你選的配方（預設是第一個能跑這個模型的配方，和部署頁面一樣），抓那台機器會用到的東西：process 變體的推論引擎壓縮檔，或容器變體的映像檔（和部署時一樣 pull）。取消勾選就只下載模型。
4. 按 **下載到 N 台機器**。

進度會顯示在目錄裡模型那一列的下方，以及每台機器的頁面上，文字和進度條與部署的下載相同。**取消** 會立刻停止那台機器上的下載；已經在磁碟上的部分會保留，再按一次 **下載到…** 就從那裡繼續。如果機器下線，下載會等 agent 回來後繼續。下載完成後，機器的 **快取中的模型** 會列出這個模型，部署這個模型時排程也會優先選這台機器。

每次要求和取消都會記入稽核紀錄（`model.download_requested`、`model.download_cancelled`）。從 URL 下載的單一 GGUF 檔沒有公布的 checksum：第一次完整下載後會記下它的 SHA-256，之後每次下載都必須相符（`model.checksum_recorded`）。API 見[參考資料](reference.md#download-a-model-ahead-through-the-api)。

自己連不到 Hugging Face 或 GitHub 的機器，會經由伺服器取得檔案：請看下一節，以及[讓沒有網路的機器執行模型](#run-a-machine-without-internet-access)。

## 經由伺服器把模型送到離線機器

自己沒有網路的機器（在防火牆後面、DNS 被過濾，或只能透過 SSH tunnel 連到），一樣可以下載模型、推論引擎的壓縮檔和有釘選的推論引擎映像檔：**由伺服器去抓，再把資料轉給它**，伺服器不存任何檔案。agent 用它原本連伺服器的方式提出要求：由機器往外連，走 HTTPS、使用它釘選的 CA 和機器憑證，所以只要原本能承載 agent 連線的路（例如 `ssh -R 7860:…` tunnel）都能用。機器和 tunnel 都不用改。從加入這種機器到部署好模型的完整流程，請看[讓沒有網路的機器執行模型](#run-a-machine-without-internet-access)。

1. 打開機器的頁面（admin）。在 **快取中的模型** 底下設定 **下載時的網路連線**：
   - **自動**（預設）：先直接下載；一旦那台機器連不到來源（名稱解析失敗、連線被拒、被重設或逾時、網路無法連線），這次下載剩下的部分，以及之後一小時內從那個主機的下載，都改經由伺服器。HTTP 錯誤（檔案不存在、權杖被拒）或 checksum 不符不會切換：伺服器也會得到同樣的結果。
   - **一律經由伺服器**：給完全沒有網路的機器。它不會先直接試，所以不用先等一次失敗。
   - **只直接下載**：永遠不經由伺服器。
2. 照常部署，或使用 **下載到…**。進度會在部署的頁面、目錄裡模型的下方和機器的頁面上顯示 *經由伺服器下載模型：…/…*，完成的下載會顯示 *…下載完成，經由伺服器*。對設成一律經由伺服器的機器，**下載到…** 對話框會標示 *經由伺服器*。

不變的部分：中斷後 agent 會從磁碟上已有的部分繼續下載（伺服器會把 `Range` 轉給來源）、會重試、按 **取消** 會立刻停止，而且每個檔案都像直接下載一樣檢查（大檔的 SHA-256、小檔的 git blob id、推論引擎的壓縮檔釘選的 SHA-256、URL 模型記下的 SHA-256）。在路上被改過的檔案會被拒絕並刪除，所以伺服器並沒有因此多了任何它原本做不到的權限。

不同的部分：

- 受限模型由伺服器用它自己的 Hugging Face 權杖（**模型 → 來源**），或部署自己的權杖下載。設成 **一律經由伺服器** 的機器完全不會收到權杖。
- 套用伺服器自己的模型來源規則：只限 https 和公開位址（除非設了 `MODEL_ALLOW_PRIVATE_HOSTS`），URL 模型只限允許的主機。
- 伺服器只轉送那台機器現在應該下載的東西（它上面部署的副本，以及它的 **下載到…** 要求）；取消下載或刪除部署後，幾秒內就會關閉。同時經由伺服器的下載每台機器最多兩個、全部最多四個；其他的會排隊等。
- 每個位元組都會經過伺服器的網路兩次（從來源進來、送到機器出去），所以連得到網路的機器請維持 **自動** 或 **只直接下載**。
- 排程不再因為某台機器自己下載不到模型而避開它：部署的頁面會顯示 *amd-01 could not reach huggingface.co just now; the model comes through the server*，或 *amd-01 downloads the model and the engine through the server (its internet access setting)*。

**從另一台機器**（1.4.1）。伺服器替這種機器從網際網路抓模型檔之前，會先問快取裡已經有這個模型的機器。其中一台把檔案送給伺服器（和它的 agent 做的其他事一樣，由機器往外連：沒有機器要開 port，tunnel 也不用改），伺服器再轉給要的機器，不存任何檔案。在區域網路上這比網際網路快，也省下伺服器的對外頻寬。不需要任何設定：

- **哪些機器會送**：任何一台 agent 是 1.4.1 以後、而且快取裡列出這個模型的其他機器，不論它自己能不能上網（完全沒有網路的機器也能送）。只會送它下載並檢查過的檔案，而且只依伺服器持有的 digest 指定。URL 模型要等它的 SHA-256 記錄下來後，才會從機器取得。
- **選哪一台**：先選有那個 GGUF 檔本身的機器，再選正在送的檔案比較少的。沒有那個檔案、已經在送兩個，或 10 秒內沒開始送的機器會被跳過，改問下一台，最後才用網際網路；這些都發生在提出要求的機器收到第一個位元組之前。送到一半中斷的機器會讓那次回應提早結束；下載會從磁碟上已有的部分繼續，伺服器會重新選來源。
- **檢查**：提出要求的機器照常檢查每個檔案。伺服器也會檢查經過的完整檔案，並保留最後一段直到相符才送出：副本不符的機器在一小時內不會再被要求送那個檔案，稽核紀錄會寫出是哪台（`node.relay_copy_refused`），檔案會從別處重新開始。請從那台送出檔案的機器的快取移除這個模型，再重新下載。
- **你會看到**：*經由伺服器從 gpu-host 下載模型：…/…*、*…下載完成，經由伺服器，來自 gpu-host*，有機器能送出時，**下載到…** 會顯示 *經由伺服器，來自 gpu-host*。
- 只有經由伺服器取得檔案的機器會用到（**一律經由伺服器**，或 **自動** 且連不到來源之後）。連得到網際網路的機器仍然直接下載。
- 受限模型可能這樣送到一台機器而不用到任何 Hugging Face 權杖，因為另一台機器已經有它。和以前一樣，每台機器拿到什麼由伺服器決定。
- 推論引擎的壓縮檔仍然經由伺服器從網際網路取得：agent 解開壓縮檔後就會刪掉它，所以沒有機器有壓縮檔可以送。

**推論引擎的映像檔**（container 變體）在 recipe 以 digest 釘選時也會經由伺服器，所有內建 recipe 都是如此：

- 映像檔不在機器上時，agent 照常 pull（**自動**）；pull 連不到 registry（`lookup ghcr.io: no such host`、逾時）時改經由伺服器，之後一小時內同一個 registry 的映像檔也一樣。設成 **一律經由伺服器** 時完全不會先試 registry。
- 伺服器依 Forgeyard 為這個映像檔記下的 digest（`forgeyard/recipes/images.lock.json`），用 registry 發的匿名 pull token 讀取**那台機器平台**（amd64、arm64）的 manifest，再依 digest 提供那份 manifest、它的 config 和每一層。那份 manifest 沒列的 digest 一律不給，完整的一層經過時也會檢查。
- agent 把每一份都和它的 digest 比對，先放在模型快取的 `.partial/image-…` 底下（中斷後從那一層接續；**移除未完成的下載** 會清掉），組成 `docker load` 和 `podman load` 都接受的封存檔、載入、加上 recipe 的 tag，並且只有在載入的映像檔符合記錄的 id 時才執行，和手動載入的映像檔一樣。之後刪除這些檔案。
- 進度會顯示 *經由伺服器拉取映像檔：120 MiB/300 MiB，3/7 層*，接著是 *載入中*。每一層都會經過伺服器的網路兩次，和模型檔一樣。
- 只用 tag 指定的映像檔（自訂 recipe 的，或你自己建置的）不能經由伺服器：沒有記錄的 digest 可以比對。請改用上傳，或從另一台機器複製（接下來兩節）。Forgeyard 自己的 vision 映像檔從 1.4.1 起有釘選，會經由伺服器送過去。

機器的 agent 必須是 1.4 以後的版本；較舊的 agent 不管設定都會直接下載，設定欄位也會提示你更新它（agent 能轉送模型、但還不能轉送映像檔時也會提示）。要送檔案給其他機器，agent 必須是 1.4.1 以後的版本；提出要求的機器只要 1.4。

## 把推論引擎的映像檔上傳到機器

管理員可以從 console 把映像檔放到機器上：不能經由伺服器的映像檔（自訂 recipe 只用 tag 指定的映像檔、自己建置的映像檔），或那台機器要 pull 很久的映像檔。

1. 在已經有這個映像檔（或能 pull 它）的機器上存成檔案：

   ```bash
   sudo docker save ghcr.io/example/engine:1.2 | gzip > engine.tar.gz
   ```

   `podman save` 也可以。有釘選的映像檔請先依 digest pull 並加上 tag，做法和[離線機器](#offline-machines)一樣，這樣檔案裡就是那個映像檔、而且是 recipe 的 tag。
2. 打開目標機器的頁面 → **推論引擎的映像檔** → **上傳映像檔**，選擇檔案，按 **上傳**。對話框會顯示已送出多少，接著顯示 *正在載入 … 的容器引擎…*。
3. 載入完成後，對話框會列出檔案裡的每個映像檔：它的 tag、id，以及它是哪個 recipe 的映像檔。*llama-cpp (vulkan) 釘選的映像檔，已用記錄的 ID 核對*：那台機器上的部署不用 pull 就會使用它。*名稱和 … 的映像檔相同*：只用 tag 指定它的 recipe 會照樣使用。*不是任何配方的映像檔*：自訂 recipe 可以用它的 tag 指定它。

檔案從你的瀏覽器經過伺服器直接送到 agent，agent 一邊收一邊把它串流進 `docker load`（或 `podman load`）：伺服器只保留很小的緩衝，機器也不需要其他通往你電腦的路。容器引擎載入時會檢查每一層；中斷的檔案絕不會被載入。每台機器同時只能有一個上傳，全部最多兩個；檔案最大為 `IMAGE_UPLOAD_MAX_GB`（沒設定時是 64 GiB）。機器的 agent 必須是 1.4 以後的版本、有 容器引擎，而且在線上。每次上傳和失敗的原因都會記入稽核紀錄（`node.image_uploaded`）。

要複製另一台機器已經有的映像檔，不需要檔案：請看下一節。

## 從另一台機器複製推論引擎的映像檔

管理員可以把一台機器的 容器引擎裡的映像檔，經由伺服器複製到另一台機器（1.4.1）：只用 tag 指定的自訂 recipe 映像檔、在某台機器上自己建置的映像檔，或連不到 registry 的機器需要的映像檔。不會自動複製任何東西。

1. 打開目標機器的頁面 → **推論引擎的映像檔** → **從其他機器複製**。
2. 在 **來源** 選要複製的來源機器。它的 agent 會列出容器引擎裡的映像檔：每個映像檔的 tag、id、大小、平台，以及它是哪個 recipe 的映像檔。對話框只顯示 recipe 指定的映像檔（1.4.2）；勾選 **顯示所有映像檔** 才會顯示其他的，例如共用機器上別人的映像檔。
3. 選一個，按 **複製**。來源機器會用它自己的 tag 對它執行 `docker save`（或 `podman save`），送給伺服器，伺服器直接轉進目標機器的 `docker load`，只保留很小的緩衝。載入完成後，對話框會列出載入了什麼、它是哪個 recipe 的映像檔，以及 *The same image as on gpu-host (same content id)*：載入的就是來源機器上的那個映像檔。

注意事項：

- **信任**：只用 tag 指定的映像檔沒有記錄的 id，所以目標機器會執行來源機器在那個 tag 底下的任何東西。只從你信任的機器複製。有釘選的映像檔在執行前仍會以記錄的 id 檢查。
- `docker save` 的資料流事先不知道大小，也不能續傳：中斷的複製會從頭重來，目標機器不會從中斷的資料流載入任何東西。對話框會說明中斷，以及來源機器的說明，例如 *The copy broke off after 131072 bytes: gpu-host stopped sending, so nothing was loaded on gpu-01. gpu-host says: docker save failed: …*：先在那台機器上解決，再複製一次。
- 限制和上傳相同：每台目標機器同時只能有一個複製或上傳，全部最多兩個，最大 `IMAGE_UPLOAD_MAX_GB`；每台來源機器同時只能送一個。
- 來源機器的 agent 必須是 1.4.1 以後、有 容器引擎；目標機器是 1.4 以後、有 容器引擎。兩台都必須在線上。每次複製和失敗的原因都會記入稽核紀錄（`node.image_copied`，包含兩台機器和載入的 id）。

## 釋出機器上的磁碟空間

機器的頁面上有 **快取中的模型**：列出它的 agent 下載過的東西和大小。每個 Hugging Face revision 一列；有好幾個 GGUF 檔的 revision，底下每個檔案各一列。從 URL 下載的單一 GGUF 檔，以及 process 變體的推論引擎壓縮檔也會列出。清單上方是快取在那顆磁碟上占了多少，以及磁碟還剩多少可用空間。

- **移除** 會從那台機器刪除一個項目，管理員和操作員可以使用。那台機器上有部署正在使用這個模型時會被拒絕，該列也會指出是哪個部署：請先停止它。同一個 repository 的其他 revision 共用的檔案會保留，和 `hf cache rm` 的做法相同。之後需要這個模型的部署會在那裡重新下載。
- **移除未完成的下載** 會刪除未完成的下載留下的檔案。下載會從這些檔案接續，所以只有在不想再繼續那次下載時才刪除。那台機器上有下載正在進行時會被拒絕。
- 早於 1.4 的 agent 會列出模型但沒有大小，也不能刪除。請更新 agent。

每次刪除都會以 `model.cache_removed` 記入稽核紀錄，包含刪了什麼、釋出了多少空間。API 請見[參考文件](reference.md#a-machines-model-cache-through-the-api)。

### 推論引擎的映像檔

推論引擎的映像檔也占磁碟空間，recipe 換成較新的映像檔後，舊的會越積越多。機器的頁面在 **推論引擎的映像檔** 底下列出它們（1.4.2）：agent 目前列出的、由 recipe 指定的每個映像檔（有釘選的映像檔以記錄的 id 辨識，只用 tag 指定的映像檔以那個 tag 辨識），包含大小、指定它的 recipe，以及那台機器上使用它的部署。清單上方是容器引擎存放映像檔的位置還剩多少空間，前提是 agent 看得到那個目錄（在 container 裡執行的 agent 通常看不到）。容器引擎裡的其他映像檔只會計數：沒有 recipe 指定它們，所以它們不屬於 Forgeyard。

- **移除**（管理員）會從那台機器刪除一個映像檔。該列會先詢問，然後 agent 對它執行 `docker rmi`（或 `podman rmi`），絕不加 `-f`。完成後主控台會說明釋出了多少空間：最多是映像檔的大小，因為其他映像檔共用的 layer 會保留；agent 看得到容器引擎的磁碟時，也會顯示之後的可用空間。之後需要這個映像檔的部署會重新 pull（或經由伺服器取得，或由你再載入一次）。
- 以下情況會被拒絕，該列也會說明原因：那台機器上有部署正在使用這個映像檔（請先停止它）、agent 本身就是從這個映像檔執行的，以及容器引擎裡有任何 container 正在使用它，即使不是 Forgeyard 啟動的（容器引擎會保留這種映像檔；訊息會指出是哪個 container）。
- 沒有 recipe 指定的映像檔，絕不會提供刪除。
- agent 只會依 id 刪除它自己清單裡的映像檔：伺服器絕不會指定名稱要它刪除。有好幾個 tag 的映像檔會以那些 tag 刪除。
- 早於 1.4.2 的 agent 會列出映像檔，但不能刪除。請更新 agent。

每次要求 agent 刪除，都會以 `node.image_removed` 記入稽核紀錄，包含映像檔、它的 tag、指定它的 recipe，以及釋出了多少空間或沒有刪除的原因。API 請見[參考文件](reference.md#a-machines-engine-images-through-the-api)。

## 搬移模型快取

例如從 `/srv/forgeyard-models` 搬到較大磁碟上的 `/data/forgeyard-models`：

1. 停止在這台機器上執行的部署（各部署的頁面 → **停止**）。
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

- **API 金鑰**：在 **API 金鑰** 建立一把新的金鑰，放進你的程式，然後把舊的 **撤銷**。被撤銷的金鑰從下一個請求開始就失效。
- **機器的憑據**：撤銷這台機器，建立一個加入指令，然後在機器上用 `--rejoin` 重新加入：

  ```bash
  sudo forgeyard agent join --rejoin --join-token fyj_…
  sudo systemctl restart forgeyard-agent
  ```

  它會以一台新機器出現在頁面上；請移除舊的那一筆。
- **CA**：只有在必須更換時才換（它的金鑰外洩，或你要改用自己的憑證）。每台裝置都要重新信任新的 CA，每個 agent 都要用新的指紋執行 `forgeyard agent trust`（管理員可以在 **信任這台伺服器** 看到新指紋）。
- **`secret.key`** 目前還不能就地輪替。請像保護密碼一樣保護它（權限 0600，和資料庫備份分開存放）。

## 撤銷機器

1. 先停止或搬走在那台機器上執行的東西：只放在那台機器上的部署會無處可去。
2. **機器** → 該機器 → **撤銷**。它的憑據會失效，agent 會被中斷連線並結束（安裝的服務不會再重新啟動它），它的副本也會從閘道上移除。
3. **移除** 會把它從清單中移除。如果要把這台機器加回來，請用新的權杖重新加入（`forgeyard agent join --rejoin --join-token fyj_…`）。

伺服器已經連不到被撤銷機器上的 agent，所以不會停止那台機器上仍在執行的應用程式。請在機器上自行停止它們：`docker ps --filter label=forgeyard.agent` 會列出這些容器。

## 解除安裝 agent

在機器上執行和安裝時相同的指令，但把 `--join` 換成 `--uninstall`：

```bash
curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | sudo sh -s -- --uninstall
```

它會停止並移除服務和 `/opt/forgeyard`，但保留 `/etc/forgeyard/agent.toml`、憑據和模型快取，所以重新安裝時會帶回同一台機器。加上 `--purge` 會連設定和憑據一起刪除（之後請到 **機器** 頁面移除這台機器）。模型快取和 `forgeyard-run` 帳號永遠不會被刪除：如果需要，請自行移除。

不透過伺服器，手動移除：

```bash
sudo systemctl disable --now forgeyard-agent
sudo rm -f /etc/systemd/system/forgeyard-agent.service /usr/local/bin/forgeyard
sudo rm -rf /opt/forgeyard
```
