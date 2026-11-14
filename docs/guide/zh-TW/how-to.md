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

   它會每張卡印出一行，附上卡的 `gfx` 目標（例如 `gfx1100`；Strix Halo APU 則是 `gfx1151`，並標示為 *unified-memory*）和 ROCm 版本，接著每個收集器各印一行。以 BIOS 切出 96 GiB 的 Strix Halo 為例：

   ```text
   amd     0  AMD Radeon 8060S  arch=gfx1151  32.6 GiB free of 96.0 GiB  rocm=7.0.2 unified-memory
   ```

agent 讀取 AMD 顯示卡時，能匯入 `amdsmi` 函式庫就用它（先找 agent 自己的 Python，找不到就用 ROCm 附在 `/opt/rocm/share/amd_smi` 的那一份），否則用 `amd-smi` 指令，再不行就用 `rocm-smi`。如果 inventory 顯示 `amdsmi … not importable` 或 `failed to load`，但 `amd-smi` 可以正常使用，那就什麼都不缺：同樣的數值會從指令讀到。

每張卡顯示的內容：

- **名稱：**卡的市售名稱。APU 的名稱常常只是「AMD Radeon Graphics」，這時改用 CPU 型號裡寫的 GPU（「… w/ Radeon 8060S」）。
- **ROCm 版本**（這裡是 `rocm=`，Machines 頁面上寫「ROCm」），放在 NVIDIA 卡顯示驅動程式版本的位置。核心驅動程式的版本保留在卡的詳細資料裡。
- **記憶體：**APU 取 VRAM 切割區（在 BIOS 設定）和 GTT 兩者中較大的那個，和 `amd-smi monitor` 顯示的一樣。
- **識別鍵（key）：**卡有序號時用它的 UUID，否則用 PCI 位址（APU 沒有序號，所以每台機器上的 UUID 都一樣）。用 Forgeyard 1.1.0 或更舊版本加入的機器，識別鍵改變後 GPU 允許清單仍會保留。

在 AMD 上，模型可以用 `vllm` recipe 的 ROCm 映像檔執行，也可以用 `llama-cpp`。行程類工作負載只會在 `ROCR_VISIBLE_DEVICES` 裡拿到自己的卡，而且只有在分配到 GPU 時，才會加入擁有 `/dev/kfd` 和對應 `/dev/dri/renderD*` 的群組。

`llama-cpp` 會依每張卡選兩個映像檔之一：

| 卡 | `llama-cpp` 用 | 原因 |
|---|---|---|
| Instinct（`gfx908`、`gfx90a`、`gfx942`，包含 MI300A），以及它的 ROCm 映像檔有編譯的獨立 Radeon 卡（`gfx1030`、`gfx1100`、`gfx1101`、`gfx1102`、`gfx1200`、`gfx1201`） | **ROCm**（variant `rocm`，7.7 GB 的映像檔） | Instinct 卡沒有 Vulkan |
| 其他所有 AMD GPU：統一記憶體的 APU（Strix Halo `gfx1151`、Strix Point `gfx1150`、Phoenix `gfx1103`…），以及 ROCm 映像檔沒有編譯的獨立卡（`gfx1031`、`gfx1032`…） | **Vulkan**（variant `vulkan`，0.3 GB、使用 Mesa RADV 驅動的映像檔） | 映像檔裡不需要 ROCm；在實際的 Strix Halo 上用的就是這條路 |

Vulkan 容器只會拿到自己那張卡的 render node（`/dev/dri/renderD*`）和擁有它的群組，不會拿到 `/dev/kfd`。它的記憶體和 ROCm 一樣，用同一個記憶體池檢查：APU 取 carve-out 和 GTT 中較大的那個。部署的事件會寫出它用哪個 variant（`… (vulkan; reserves …)`）。

部署本身不能選 variant。要讓某張卡改用另一個，就加一個自訂配方（**Models**、*Recipes*、**Add a custom recipe**）：複製 `llama-cpp`、換個名字，`variants` 底下只留你要的那個。

**從自訂的 `llama-cpp-vulkan` 配方換過來。** 1.3 之前，Vulkan 要用自訂配方；內建的現在取代了它。部署會一直用建立時的配方，所以請用 `llama-cpp` 重新部署這個模型，再刪掉舊的部署；沒有部署在用之後，再刪掉自訂配方。

**連不到 `ghcr.io` 的機器**用檔案拿到映像檔。在連得到的機器上，用 tag 拉取，並確認它就是釘選的那一個（這個 tag 的 `DIGEST` 必須是配方裡的 `sha256:28a56d03…`）：

```bash
sudo docker pull ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242
sudo docker images --digests ghcr.io/ggml-org/llama.cpp
sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 | gzip > llama-vulkan.tar.gz
```

把檔案複製過去，在那裡用 `gunzip -c llama-vulkan.tar.gz | sudo docker load` 載入。配方寫的映像檔是 `tag@sha256:digest`，Docker 會用 digest 找它。Docker 用傳統的映像檔儲存方式時（`docker info` 顯示的 storage driver 是 `overlay2` 之類），載入的映像檔保留 tag，但不保留拉取時的 digest（`docker images --digests` 顯示 `<none>`）：`docker run` 找不到它，會試著從 `ghcr.io` 拉取，副本就帶著這個錯誤失敗。用 containerd 映像檔儲存時（`docker info` 裡有 `driver-type: io.containerd.snapshotter.v1`），載入後用 `docker images --digests` 檢查。沒有 digest 的話，就用一個自訂配方，映像檔寫載入的 tag、不加 digest：Docker 會在本機找到它，不會去拉取。

> AMD 的支援已經在實際的 Strix Halo（Radeon 8060S，gfx1151）、ROCm 7.0.2 上檢查過：硬體偵測（透過 `amdsmi` 函式庫、`amd-smi` 和 `rocm-smi`）、加入伺服器、GPU 允許清單，以及用 llama.cpp 的 Vulkan 版本（build b11977）作為自訂配方部署模型、透過 `/v1` 呼叫。還沒在實際硬體上檢查的：內建的 Vulkan variant（b12242）、`vllm` 和 `llama-cpp` 配方的 ROCm 映像檔，以及獨立 AMD 顯示卡（目前只用錄下來的資料驗證過）。如果看到任何看起來不對的地方，請附上 `forgeyard agent inventory --json --raw` 的輸出回報。

## 加入純 CPU 機器

沒有什麼特別的：在那台機器上執行單行安裝就好。Forgeyard 會顯示這台機器的 CPU 和 RAM，並把 CPU 模型放到它上面：

- `llama-cpp` 在 CPU 上執行 GGUF 模型，可以用容器的方式（有 Docker 時），或在沒有 Docker 的 x86_64 機器上，以一支由 agent 下載並用 SHA-256 檢查過的程式執行；
- `faster-whisper` 在 CPU 上執行語音轉文字（需要 Docker）。

部署時，選擇 **Runs on** → **CPU only**。把 **CPU threads** 設成你想讓它使用的核心數：執行緒數超過空閒的核心數，只會讓 llama.cpp 變慢，不會變快。

## 用 Docker 執行 agent

每台機器執行一個 `docker run`，取代安裝腳本。每個版本發布時，都會把 agent 映像檔發布到 Docker Hub 的 `docker.io/tonyhuang1101/forgeyard-agent`，支援 linux/amd64 和 linux/arm64（AMD 顯示卡只能用 amd64：ROCm 只支援 x86_64），tag 就是該版本的版本號。指令會拉取和你的伺服器相符的那一版。

**Machines** → **Add a machine** → **Docker** 會幫你產生這個指令：選好機器的顯示卡（NVIDIA、AMD 或 CPU only），再填好指令上方的表單。指令會隨著你輸入即時更新，**Copy** 複製的就是畫面上顯示的內容，填過的值也會記在這個瀏覽器裡，下一台機器直接沿用：

- **Model directory on that machine**（預設 `/srv/forgeyard-models`）：放模型的目錄，要選空間夠大的磁碟。它會同時填進 `-v` 冒號的兩邊和 `FORGEYARD_MODEL_CACHE`。必須是絕對路徑，不能有空白、引號或冒號。
- **Machine name**（選填）：這台機器在 **Machines** 上顯示的名稱；留空就用它的主機名稱。可以用英文字母、數字、`.`、`_` 和 `-`，最多 64 個字元。填了會加上 `FORGEYARD_NAME`。
- **Server address, as that machine reaches it**（預設是這台伺服器的 `PUBLIC_URL`；`PUBLIC_URL` 空白時，則是你開啟這個頁面時使用的位址）：機器要用另一個位址連到伺服器時，就改這一欄：透過 VPN、轉送的連接埠（`https://127.0.0.1:7860`）或另一個區網位址。如果這個主機名稱不在伺服器的憑證裡，頁面會提醒你：把它加進 `TLS_EXTRA_NAMES` 再重新啟動伺服器，或改用憑證裡有的名稱。
- **Advanced → Port range**（`FORGEYARD_PORT_RANGE`，預設 `30000-32767`）：服務和模型副本使用的主機連接埠。只有部分連接埠連得到這台機器時才需要設定，例如防火牆開放的 `40000-40099`。伺服器必須連得到這些連接埠。

欄位的值如果 agent 不接受，頁面會顯示錯誤訊息，而不是產生指令。映像檔的 tag 就是伺服器自己的版本，所以 agent 和伺服器的版本永遠一致。管理員可以用 [`AGENT_IMAGE`](reference.md#machines) 讓指令改用鏡像站、私有 registry 或指定的 digest。和頁面一樣，這裡的指令開頭都有 `sudo`；如果是 root，或你的帳號已經在 `docker` 群組裡，就把它拿掉。

連不到 Docker Hub 的機器，可以用檔案的方式取得映像檔，或自己建置：見[離線機器，或自己建置映像檔](#offline-machines-or-build-it-yourself)。

### NVIDIA

需要 NVIDIA Container Toolkit（`docker run --gpus all` 能正常執行）。表單維持預設值時，頁面產生的指令長這樣（伺服器位址、指紋和加入權杖（join token）會換成你的）：

```bash
sudo docker run -d --name forgeyard-agent --restart always --network host \
  --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v forgeyard-agent:/var/lib/forgeyard-agent \
  -v /srv/forgeyard-models:/srv/forgeyard-models \
  -e FORGEYARD_SERVER=https://192.168.1.20:7860 \
  -e FORGEYARD_CA_FINGERPRINT=sha256:3f9a… \
  -e FORGEYARD_JOIN_TOKEN=fyj_… \
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
```

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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
```

主機的 ROCm 以唯讀方式掛載，讓 agent 可以用 `amdsmi`、`amd-smi` 和 `rocm-smi` 讀取顯示卡資訊。群組要用主機上的**編號**指定：Docker 會到映像檔裡查群組*名稱*，而映像檔裡沒有 `render` 群組（`Unable to find group render`）。如果是沒有 `render` 群組的舊系統（`getent group render` 什麼都沒印），就把那個 `--group-add` 拿掉。

加入之前想先確認 agent 看到什麼，可以用同樣的裝置執行它的 `inventory` 指令（映像檔的指令本身就是 `forgeyard agent`，所以只要加上 `inventory`）：

```bash
sudo docker run --rm --device /dev/kfd --device /dev/dri \
  --group-add "$(getent group video | cut -d: -f3)" --group-add "$(getent group render | cut -d: -f3)" \
  -v /opt/rocm:/opt/rocm:ro \
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0 inventory
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
  docker.io/tonyhuang1101/forgeyard-agent:1.2.0
```

### 為什麼要用這些選項

- **`--network host`**：模型副本在主機的 `127.0.0.1` 上監聽，前面擋著 agent 的金鑰守衛，守衛則監聽副本對外公開的連接埠。只有 agent 和主機共用網路，這兩者才能運作。這也讓 agent 可以在機器自己的位址上公開連接埠。
- **模型快取在容器內外使用相同路徑**（`-v /srv/forgeyard-models:/srv/forgeyard-models`）：agent 把模型下載到這裡，再請主機的 Docker 啟動引擎容器，並以*主機上的路徑*掛載模型。路徑不同的話，什麼都掛載不到。
- **`/var/run/docker.sock`**：模型和容器類工作負載是在主機的 Docker 上以容器執行，和 agent 並列，而不是在 agent 裡面。這個 socket 等於完全掌控主機的 Docker，所以 agent 的容器要像 agent 本身一樣被信任。
- **`forgeyard-agent` volume** 保存機器的憑據和釘選的 CA。請保留它：在重新啟動、升級或 `docker rm` 之後執行同一個 `docker run`，就會帶回同一台機器，而且不會再使用加入權杖（紀錄裡會寫明）。沒有它的話，機器就得重新加入。
- 使用 **`FORGEYARD_*` 變數**而不是旗標：它們和 [agent.toml](reference.md#agent-settings) 裡的設定相同。你也可以掛載一個檔案：`-v /etc/forgeyard/agent.toml:/etc/forgeyard/agent.toml:ro`。
- agent 在容器裡以 **root** 執行：它需要 Docker socket，並會在執行行程類工作負載時切換到無特權帳號 `forgeyard-run`。工作負載絕不會以 root 執行。
- 容器裡沒有 systemd，所以行程類工作負載用 rlimit 限制資源（映像檔已設定 `FORGEYARD_LIMITS=rlimit`）。行程類工作負載在 agent 的容器裡執行，會隨容器一起停止；模型和容器類工作負載則會繼續在主機上執行。

### 離線機器，或自己建置映像檔

頁面把這些放在 **Advanced / offline** 底下。連不到 Docker Hub 的機器（沒有網路，或被 DNS 過濾），可以用檔案取得映像檔。在連得到的機器上拉取並存成檔案：

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.2.0 && \
  sudo docker save docker.io/tonyhuang1101/forgeyard-agent:1.2.0 | gzip > forgeyard-agent.tar.gz
```

把 `forgeyard-agent.tar.gz` 複製到新機器（`scp` 或 USB 隨身碟），在那裡載入：

```bash
gunzip -c forgeyard-agent.tar.gz | sudo docker load
```

如果兩台機器的架構不同（x86_64 和 arm64），在 `docker pull` 加上 `--platform linux/arm64` 或 `--platform linux/amd64`。之後照原樣執行頁面上的指令即可：Docker 會在本機找到映像檔，不會再去拉取。如果 `AGENT_IMAGE` 指定了 digest，指令最後一行要改用 tag 而不是 digest：載入的映像檔只保留 tag，不保留拉取時用的 digest（頁面上也會提醒）。

或者在那台機器上，用和伺服器同版本的 Forgeyard 原始碼，以相同名稱建置映像檔：

```bash
sudo docker build -f Dockerfile.agent -t docker.io/tonyhuang1101/forgeyard-agent:1.2.0 .
```

沒有指定已發布映像檔的伺服器（見[容器映像檔](reference.md#container-images)）會在指令裡使用本機建置的 `forgeyard-agent:1.2.0`，頁面的第一步就是這個 `docker build`。

### 把已安裝的 agent 搬到 Docker

已經加入的機器，只要容器拿到它的狀態目錄，就能保留原本的身分：

1. 拉取映像檔：`sudo docker pull docker.io/tonyhuang1101/forgeyard-agent:1.2.0`（或像上面那樣用檔案帶過去）。
2. 停止舊的 agent：`sudo systemctl disable --now forgeyard-agent`（安裝版），或停止你自己啟動的行程。它的容器類工作負載和模型會繼續執行。
3. 用舊的狀態目錄和同一個模型快取啟動容器，而且**不要**帶加入用的變數：

   ```bash
   sudo docker run -d --name forgeyard-agent --restart always --network host --gpus all \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v /var/lib/forgeyard-agent:/var/lib/forgeyard-agent \
     -v /srv/forgeyard-models:/srv/forgeyard-models \
     -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \
     docker.io/tonyhuang1101/forgeyard-agent:1.2.0
   ```

4. `sudo docker logs -f forgeyard-agent` 會為每個正在執行的模型顯示 `Adopted replica …`：新的 agent 會接手舊 agent 啟動的容器（容器上帶有 agent 的 id，而這個 id 存在狀態目錄裡）。

舊 agent 在容器外啟動的行程類工作負載不會被接手：請先從 UI 停止它們。

## 離線機器

連不到映像檔 registry 的機器（沒有網路，或 DNS 過濾擋掉 `ghcr.io` 或 Docker Hub），也能用內建 recipe，
引擎映像檔用檔案帶過去就好。recipe 和部署都不用改。

1. **找出映像檔。** 在 **Models → Catalog and recipes**，打開 recipe 上的 **Machine without registry access:
   load the image**：它列出每個容器 variant 的映像檔，以及填好的下列指令。機器會用第一個 `when` 符合它顯示卡的
   variant（[內建 recipe](reference.md#built-in-recipes)）：例如 llama.cpp 在 AMD APU（gfx1151、gfx1150……）
   上用 `vulkan`。部署頁面上的 replica 也會顯示它執行的映像檔。
2. **在連得到 registry 的機器上**，用 digest 拉映像檔、加上它的 tag，再存成檔案。以 llama.cpp 的 Vulkan variant
   為例（頁面上的指令已填好它的 digest）：

   ```bash
   sudo docker pull ghcr.io/ggml-org/llama.cpp@sha256:<digest> && \
     sudo docker tag ghcr.io/ggml-org/llama.cpp@sha256:<digest> ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 && \
     sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 | gzip > llama.cpp-server-vulkan-b12242.tar.gz
   ```

   用 digest 拉，檔案裡就正好是 recipe 釘住的映像檔；`docker load` 會保留 tag，agent 就是靠 tag 找到它。
   兩台機器架構不同（x86_64 和 arm64）時，在 `docker pull` 加上 `--platform linux/amd64` 或
   `--platform linux/arm64`。
3. **把檔案複製到**離線機器（`scp`、USB 隨身碟），在那裡載入（Podman 用 `podman load`）：

   ```bash
   gunzip -c llama.cpp-server-vulkan-b12242.tar.gz | sudo docker load
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
釘住的映像檔無法檢查：在離線機器上，請在你的自訂 recipe 裡只用 tag 指定它。這項檢查需要這一版或更新的
agent；舊版 agent 會試著去拉。

## 執行服務或任務

管理員和操作員可以把自己的程式當成**工作負載**在機器上執行：打開 **Workloads** → **New workload**，填好表單，選 **Create and start**。

1. **What：** **Service**（一直執行到你停止它；結束時會重新啟動）或 **Task**（執行到結束）。**Name**：`a-z`、`0-9` 和 `-`，不能重複。
2. **Run：** **Container**（一個 **Image**，例如 `python:3.13-slim`，可選填 **Entrypoint**）或 **Process**（基本 `PATH` 上的 **Command** 或絕對路徑，由 agent 以工作負載帳號執行）。**Arguments** 一行一個，不需要 shell 引號。
3. **Where：** 一台 **Machine**，或 *Any online machine with room*；**GPUs**：*None*、*How many*（再加上 **Vendor**），或在選定的機器上指定 *These cards*；**CPU cores**、**Memory**（例如 `4Gi`）和 **Replicas**（1 到 16 個，每個在一台機器上）。
4. **Environment：** 它需要的變數。它從空的環境開始；Forgeyard 會加入的變數列在[參考資料](reference.md#workload-environment)。值會加密儲存。
5. **Ports and health**（服務）：它監聽的每個連接埠都按一次 **Add port**。agent 會從它的範圍挑一個主機連接埠，並把位址顯示在工作負載的頁面上。行程監聽 `$PORT`（參數裡用 `${PORT_<NAME>}`）；容器則填它自己的連接埠，主機連接埠會對應過去。**Health check**：*None*、*HTTP*（一個 **Path**）或 *TCP*。**Restart**：*Always*、*On failure* 或 *Never*。
6. **Limits and retries**（任務）：**Max duration (s)** 超過時會停止並標為失敗；非零結束時依 **Retries** 重試，**Retry backoff (s)** 每次加倍，最多 5 分鐘。

*Spec that will be sent* 會顯示表單產生的 JSON。工作負載的頁面會顯示每個副本、它的機器、GPU 和位址、事件，以及最後幾行輸出。**Stop** 會停止每個副本（先 SIGTERM，10 秒後 SIGKILL）並釋出它的 GPU；**Start** 會重新配置它們；**Delete** 還會刪除它的歷史和輸出。

隔離方式簡述：行程以無特權的工作負載帳號執行，只看得到它自己的卡；容器只拿到它自己的卡，絕不使用 `--privileged`。請見[工作負載](concepts.md#workloads)。

## 在 /x/ 公開工作負載

透過閘道、帶著 API 金鑰呼叫服務自己的 HTTP API。由管理員和操作員設定。

1. **服務監聽 loopback。** 在 agent 的金鑰守門員後面，行程必須監聽 `127.0.0.1`：在它的參數裡用 `${BIND_ADDRESS}` 和 `${PORT}`，它們會變成 `127.0.0.1` 和它的連接埠。容器照常監聽它自己的連接埠（agent 會把它發布在 loopback 上）。監聽其他位址的行程會被停止，所以它永遠不會繞過守門員被連到。
2. **公開它。** 在 **New workload** 的 **Gateway** 底下勾選 **Expose at** `/x/<name>`；或在執行中工作負載的頁面上，**Gateway** → **Expose** → **Save**：它的副本會在守門員後面重新啟動。**Limits and timeouts**（預設收合）裡有 **Port**（預設：第一個）、**Read timeout (s)**（600）、**Largest request (MiB)**（32）、**Requests at once per replica** 和 **Requests at once per key**（預設不限）。
3. **讓金鑰可以呼叫。** 在 **API keys** 頁面，金鑰的 **Workloads at /x/**：*Listed workloads*（勾選它）或 *Every exposed workload*。新金鑰是 *None*。
4. **呼叫它。** 工作負載的頁面會顯示它的位址和一個 `curl` 呼叫。前綴會被拿掉：`/x/detector/v1/detect` 到服務那邊是 `/v1/detect`。

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/detector/v1/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" \
  -F image=@street.jpg
```

會產生連結或轉址的服務，可以讀 `X-Forwarded-Prefix`（`/x/<name>`），或設定它的 root path。例如以行程方式用 `python3 -m uvicorn` 執行的 FastAPI 服務，**Environment** 加上變數 `ROOT_PATH=/x/echo`：

```python
# app.py: a service behind /x/<name>. The gateway strips the prefix, so routes start at /;
# root_path makes the docs and redirects use the public prefix.
import os
from fastapi import FastAPI, Request

app = FastAPI(root_path=os.environ.get("ROOT_PATH", ""))

@app.post("/v1/echo")
async def echo(request: Request):
    return {"bytes": len(await request.body())}
```

**Arguments** 填：

```text
-m
uvicorn
app:app
--host
${BIND_ADDRESS}
--port
${PORT}
```

關掉 **Expose** 會立刻關閉 `/x/<name>`；守門員會一直留在副本前面，直到工作負載重新啟動。哪些東西會通過、哪些會被拿掉，請見[參考資料](reference.md#the-passthrough-x)。

## 從範本開始一個工作負載

**Workloads** → **Start from a template** 提供現成的視覺 API。每個範本都執行 Forgeyard 自己的 vision server 映像檔，並且直接公開在 `/x/<name>`：

| 範本 | 模型（授權） | 呼叫 |
|---|---|---|
| **Object detection** | RT-DETRv2 ResNet-18，`PekingU/rtdetr_v2_r18vd`（Apache-2.0） | `POST /x/detector/v1/detect`：框、標籤與分數 |
| **Segmentation** | SAM 2.1 Hiera tiny，`facebook/sam2.1-hiera-tiny`（Apache-2.0） | `POST /x/segmenter/v1/segment`，給一個點或一個框：遮罩 |
| **Depth estimation** | Depth Anything V2 Small，`depth-anything/Depth-Anything-V2-Small-hf`（Apache-2.0） | `POST /x/depth/v1/depth`：16 位元的深度 PNG |

1. 選 **On an NVIDIA GPU**（映像檔的 CUDA 版本，以記憶體共用一張卡，3 GiB）或 **On the CPU**（CPU 版本，不用 GPU）。表單會開好並填好：映像檔、任務、連接埠、健康檢查，以及 **Expose at** `/x/<name>`。可以改任何東西，例如名稱或機器，再按 **Create and start**。
2. 讓 API 金鑰可以呼叫它（**API keys** → 金鑰的 **Workloads at /x/**）。
3. 以 multipart 檔案欄位 `image` 送出圖片（或 JSON `{"image": "<base64>"}`）：

```bash
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/detector/v1/detect \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg -F threshold=0.5
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/segmenter/v1/segment \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg -F 'points=[[320, 240]]'
curl --cacert forgeyard-ca.pem https://192.168.1.20:7860/x/depth/v1/depth \
  -H "Authorization: Bearer $FORGEYARD_API_KEY" -F image=@street.jpg
```

只提供寬鬆授權的模型：不用 Ultralytics YOLOv8 或 YOLO11（AGPL-3.0），Depth Anything V2 也只用 Small（Base 和 Large 是 CC-BY-NC-4.0）。模型以固定的 revision 包在映像檔裡，所以映像檔到了機器上之後，範本就不需要網路。連不到 Docker Hub 的機器，請把映像檔複製過去（CUDA 版本需要 NVIDIA 驅動 580 或更新；沒有可用的 GPU 時會改用 CPU）：

```bash
docker pull docker.io/tonyhuang1101/forgeyard-vision:1.3.0-cuda
docker save docker.io/tonyhuang1101/forgeyard-vision:1.3.0-cuda | gzip > forgeyard-vision.tar.gz
# on the machine without the network:
gunzip -c forgeyard-vision.tar.gz | sudo docker load
```

AMD 機器請用 **On the CPU**：映像檔沒有 ROCm 版本。每個端點接受什麼、回傳什麼，請見[參考資料](reference.md#workload-templates)。不用 API 金鑰就想試試看，請見[在 Playground 試用視覺工作負載](#try-a-vision-workload-in-the-playground)。

## 在 Playground 試用視覺工作負載

**Playground → Vision** 以你自己的身分、不需要 API 金鑰，把一張圖片送給公開的視覺工作負載（從[範本](#start-a-workload-from-a-template)啟動的，或任何以 `--task detect`、`--task segment` 或 `--task depth` 啟動的公開服務）。任何已登入的人都可以使用；請求依照工作負載的限制，並在 **Usage** 裡以 `console` 記在 `x:<name>` 底下。

1. 選 **Workload**：每一個都標出它的任務和模型。還沒有公開的工作負載時，分頁會這樣說明；管理員和操作員會看到 **Start from a template** 的連結。
2. 把 PNG、JPEG 或 WebP 圖片拖到框裡，或按 **Choose an image**（最多 20 MiB；工作負載的 **Largest request** 比較小時以它為準）。
3. 接著依任務而定：
   - **Object detection：** 按 **Detect objects**，模型找到的每樣東西都會畫出框、標籤和分數；**Score at least** 會隱藏分數較低的，不必重新送出。
   - **Segmentation：** 在東西上點一下，或拖出一個框把它圈起來。最好的遮罩會疊在圖片上；上方的按鈕可以切換模型給的其他遮罩。再點一下或再拖一次就換一個。
   - **Depth estimation：** 按 **Estimate depth**，深度圖會顯示在照片旁邊，越近越亮或越暖，可選 **Colour** 或 **Grey**。深度是相對的，不是公尺。

結果下方會顯示是哪個副本回答、圖片大小，以及模型和整個請求花的時間。工作負載忙碌、停止或限制太小時，會用白話說明；你的 console 登入不會送到 `/x/`，那裡只接受 API 金鑰。路由請見[參考資料](reference.md#from-the-playground)。

## 把圖片送給模型

1. **加入能讀圖片的模型。** 在 **Models → Add a model → Recommended** 的 **Vision** 群組裡，有給 vLLM 用的 Qwen3-VL 2B、4B 和 8B，以及給 llama.cpp（CPU 或 AMD APU）用、附 projector 檔的 Qwen3-VL GGUF。只要目錄項目標示 *Images* 的模型都可以：目錄會從模型的 `config.json` 讀出來。GGUF 儲存庫還要選它的 **Projector** 檔（有 F16 就預設選它；選 *None* 就只能處理文字）。
2. 照平常的方式**部署**。*Images per request*（4）、*Videos per request*（0，關閉）和 *Largest request*（64 MiB）都在 **Advanced** 底下，預設值就能用。估算會把視覺編碼器（vLLM）或 projector（llama.cpp）列成獨立的一項。
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
4. **在 Playground 試用：** 訊息框旁的圖片按鈕可以附上圖片。

**影片：** 在 **Advanced** 把 *Videos per request* 設成 1 以上，再送 `video_url` 區塊。在 vLLM 上，這會讓引擎啟動時為一段長影片預留記憶體，KV cache 能用的就變少：啟動不了的話就調低上下文長度。在 llama.cpp 上，請把部署的上下文長度設成 8,192 以上：Qwen3-VL 用它自己的每秒 2 幀讀影片（*Video frames per second* 只對其他模型有效），一段 6 秒的影片用了 5,345 個 token。

**自訂的 llama.cpp 配方**（例如另一個 build）只要傳入 projector 並宣告它接受什麼，就能讀圖片：

```yaml
optional_args:
  - if_model: mmproj
    args: [--mmproj, "{mmproj_file}"]
media: {inputs: [image, audio, video], files: false}
```

## 把文字轉成語音

1. **加入語音模型。** 在 **Models → Add a model → Recommended** 的 **Text to speech** 群組裡有：
   - 給 NVIDIA GPU 用的 Qwen3-TTS 0.6B 或 1.7B CustomVoice（vLLM-Omni）：中文與英文，九種聲音。大約需要 10 與 12 GiB 的 GPU 記憶體。
   - 給 llama.cpp 用的 Qwen3-TTS 1.7B GGUF：在 AMD APU 或 CPU 上說中文與英文；它只有自己的一種聲音。
   - Kokoro 82M（speaches）與 Chatterbox Turbo（llama.cpp）：CPU 就跑得動的小型英文聲音。

   從 **Search Hugging Face** 找到的模型，只要標了 *text-to-speech*、且有配方支援它的格式就能用：safetensors 的 Qwen3-TTS 用 vLLM-Omni，附 projector（`mmproj-*.gguf`）的 TTS GGUF 用 llama.cpp，ONNX 格式的 Kokoro 或 Piper 用 speaches。
2. **照常部署。** *Longest input* 在 **Advanced** 裡，預設值就能用。
3. **用 OpenAI SDK 呼叫：**

   ```python
   with client.audio.speech.with_streaming_response.create(
       model="qwen3-tts-1-7b", voice="vivian", input="今天天氣很好。", response_format="wav",
   ) as response:
       response.stream_to_file("speech.wav")
   ```

   `GET /v1/audio/voices?model=qwen3-tts-1-7b` 會列出聲音與模型能產生的格式。要一邊產生一邊收音訊，加上 `extra_body={"stream_format": "audio"}`，並使用 `pcm` 或 `wav`。
4. **在 Playground 試試看：** 語音模型會有一個 **Speak** 區塊，可以選聲音、格式，並附播放器。

**要用哪種格式：** vLLM-Omni 產生 wav、pcm、flac、mp3 與 opus；llama.cpp 產生 wav 與 pcm；speaches 產生 mp3、flac、wav 與 pcm。沒指定 `response_format` 時得到的是引擎的預設（wav，speaches 為 mp3），不是 OpenAI 的 mp3。`pcm` 是 16 位元單聲道，取樣率見回應（`x-audio-sample-rate`，或 `Content-Type` 裡的 `rate=`）。

**在 AMD APU（Vulkan）上：** 開啟 llama.cpp 的 cooperative matrices 時，Qwen3-TTS 對同一個請求可能產生略有不同的音訊（ggml-org/llama.cpp#32050）。如果在那裡聽起來不對，新增一個自訂配方，在它的 vulkan variant 的 `env` 設定 `GGML_VK_DISABLE_COOPMAT: "1"`。

## 生成圖片

1. **加入圖片模型。** 在 **Models → Add a model → Recommended** 的 **Images** 群組裡有：
   - 給 NVIDIA GPU 用的 Z-Image-Turbo、Stable Diffusion XL 與 FLUX.1-schnell（vLLM-Omni）。在預設上限下，大約各需要 30、17 與 42 GiB 的 GPU 記憶體；FLUX.1-schnell 是 gated（要在 Hugging Face 接受它的條款並提供 token）。
   - 給 stable-diffusion.cpp 用的 GGUF 版 Z-Image-Turbo 與 FLUX.1-schnell：AMD GPU 或 APU（Vulkan）或 NVIDIA GPU（CUDA），1024x1024 時大約 18 與 19 GiB，或是 CPU（很慢）。
   - 給 CPU 用的 GGUF 版 Stable Diffusion 1.5：又小又舊（512x512），用來確認圖片生成能用。

   從 **Search Hugging Face** 找到的模型，只要標了 *text-to-image*，而且是 diffusers 儲存庫（vLLM-Omni）或 GGUF / safetensors 檔案（stable-diffusion.cpp；選模型檔，旁邊的文字編碼器與 VAE 檔會依名稱找到）就能用。目錄會顯示它的授權；使用產生的圖片之前請先確認。
2. **照常部署。** *Images per request* 和 *Default size* 在表單上；最長邊、步數、佇列、逾時和卸載在 **Advanced** 底下。預設值就能用。
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
4. **在 Playground 試試看：** 圖片模型會有一個 **Image** 分頁，可以輸入提示、選部署接受的尺寸和張數，每張圖片下方都有下載連結。

**要多久：** 在 A100 上每張幾秒；在 CPU 上要幾分鐘。在一台忙碌的 x86-64 機器上用 16 到 32 個核心量測：Stable Diffusion 1.5、512x512、20 步大約 100 秒；Z-Image-Turbo（Q4_0）8 步在 512x512 大約 2 分鐘，1024x1024 大約 8.5 分鐘。機器較慢或尺寸較大時，在 **Advanced** 調高 *Timeout*。

**模型自己的設定**（Z-Image-Turbo 需要 CFG 1 與 8 步）對 stable-diffusion.cpp 來說放在目錄項目的引擎參數裡（`--cfg-scale 1 --steps 8`）；推薦的項目都已經帶上了。vLLM-Omni 則從模型本身讀取。

**在離線的 AMD 機器上：** 用 `docker load` 載入 `sd-cpp` 配方的 Vulkan 映像檔（見[離線機器](#offline-machines)）。模型則先在一台連得到 Hugging Face 的機器上部署一次，再把它在那台機器模型快取裡的目錄（`hub/models--<org>--<name>`，用 `tar` 或 `rsync -a`：裡面用的是相對連結）複製到離線機器快取裡的相同位置。那裡的部署會發現已經快取好，什麼都不用下載。

## 生成影片

1. **加入影片模型。** 在 **Models → Add a model → Recommended** 的 **Video** 群組裡有 Wan2.1-T2V-1.3B（832x480、16 fps，在預設上限下大約需要 22 GiB 的 GPU 記憶體）和 Wan2.2-TI2V-5B（1280x704、24 fps，大約 33 GiB；它的估算還沒實測），兩個都是 Apache-2.0，給 NVIDIA GPU 用（vLLM-Omni）。從 **Search Hugging Face** 找到的模型，只要是 diffusers 儲存庫，而且 `model_index.json` 寫的是文字轉影片 pipeline（`WanPipeline`）就能用。
2. **照常部署。** *Default size*、*Default length* 和 *Longest video* 在表單上；最長邊、每秒影格數、步數、同時的工作數、每把金鑰的工作數、結果保留多久、最長的工作和卸載在 **Advanced** 底下。預設值就能用。
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

4. **在 Playground 試試看：** 影片模型會有一個 **Video** 面板，可以輸入提示、選部署接受的尺寸和長度、看工作的進度，完成的影片會直接在頁面上播放並附下載連結。你之前用這個模型做的影片列在下方。

**要多久：** 每段影片要幾分鐘，每個副本一次做一段；其他的在它上面排隊。影片長或尺寸大時，在 **Advanced** 調高 *Longest job*。

## 加入雲端供應商

管理員：打開 **Providers**，選 **Add a provider**，挑選 OpenAI、Anthropic、Google Gemini、Azure OpenAI、OpenRouter 或 *OpenAI-compatible server*，貼上金鑰，再按 **Test connection**。加入你要的模型，然後把其中一個放進路由：打開一個部署，選 **Routing and cloud spill**，新增一層並放入雲端模型，再儲存。在 **API keys** 把可以使用它的金鑰的雲端設定改成 **Never** 以外的值。各供應商的步驟，以及各自端點做不到的事，請見[雲端供應商](cloud-providers.md#add-a-provider)。

## 追蹤並限制雲端花費

管理員在開始使用供應商之後：

1. **設定價格。** 在 **Providers** 選 **Refresh prices from OpenRouter**。檢查建議的對應，勾選要採用的列，再選 **Apply ticked**。OpenRouter 沒有列出的模型，請打開它的供應商，在模型上選 **Prices**，依供應商的價格頁面輸入價格（每百萬 token 的美元），再按 **Save prices**。沒有價格的雲端模型會算在 *without a price*，而且在任何預算之下都會被拒絕。
2. **查看費用。** **Usage** → **Cost** 依日期、金鑰、個人、模型或供應商顯示費用，以及有多少由本機處理。
3. **設定預算**，以日曆月（UTC）計，單位是美元：
   - 金鑰：**API keys** → 金鑰的 **Limits** → **Monthly budget (USD)** → **Save limits**。管理員在別人金鑰上設定的值會對擁有者鎖定；**Unlock budget** 會移除它；
   - 個人：**Usage** → **Cost** → **Budgets this month**，選擇 **Everyone** → **Set budget**；
   - 供應商：它的頁面 → **Monthly budget** → **Save budget**。
4. **留意警示。** 預算用到 80% 和 100% 時，**Audit log** 會各記錄一筆事件（`budget.warning`、`budget.exhausted`），頁面也會顯示警告。不會寄電子郵件。

費用怎麼算、預算如何預留金額、用完之後會怎樣：請見[費用與價格](cloud-providers.md#cost-and-prices)和[預算](cloud-providers.md#budgets)。

## 在共用機器上限制 GPU

1. 開啟 **Machines**，找到這台機器，選擇 **Change GPU access**（變更 GPU 存取權）。
2. 只勾選 Forgeyard 可以使用的卡，例如第 2 和第 3 張，然後選擇 **Save**。

從此之後：

- 不管工作負載或部署要求什麼，Forgeyard 執行的任何東西都不會分配到其他卡；
- 其他卡會顯示 *not for Forgeyard*；
- 記憶體以每張允許使用的卡回報的**可用**量計算，所以其他程式在同一張卡上的使用量會被考慮進去。

正在使用中的卡不能從清單中移除：請先停止在它上面執行的東西。每次變更都會記錄在稽核紀錄裡，包含變更前後的內容。

如果要讓模型和其他程式共用同一張卡，部署時選擇 **GPU sharing** → **Share VRAM**：這樣它只會保留估算的記憶體量，vLLM 也會被設定成只使用這麼多（請看 [vLLM 在共用的卡上無法啟動](troubleshooting.md#vllm-does-not-start-on-a-shared-card)）。

## 加入其他人

管理員在 **Users**：

1. **Add user**：一個 **Username**、選填的 **Email (optional)**，以及 **Role**（`user`、`operator` 或 `admin`；請見[角色](concepts.md#roles)）。選 **Create and get link**。
2. 把一次性連結傳給對方。它用來設定對方的密碼，只能用一次，72 小時後失效。你永遠看不到對方的密碼。

之後在同一份清單上：**Password link** 會給忘記密碼的人一個新連結（24 小時）；**Sign out** 會結束對方所有的工作階段；**Disable** 讓對方無法登入，他的 API 金鑰也會失效；**Delete** 會刪除對方。擁有者不能被停用、降級或刪除。

如果要讓別人自己申請帳號，請以 `REGISTRATION=approval` 啟動伺服器。登入頁就會提供申請帳號的連結，申請會在 **Users** 等待，直到管理員選 **Approve** 或 **Reject**。

## 使用自己的憑證或反向代理

**使用自己的憑證**（公司的 CA，或真實網域的公開憑證）。在伺服器的 `.env` 裡設定：

```bash
TLS_CERT_FILE=/etc/forgeyard/tls/fullchain.pem
TLS_KEY_FILE=/etc/forgeyard/tls/privkey.pem
TLS_CA_FILE=/etc/forgeyard/tls/company-ca.pem
```

`TLS_CA_FILE` 是簽發這張憑證的 CA；agent 會改為釘選這個 CA，**Trust this server** 也會提供它。如果是公開信任的憑證，就不要設定這一項：這樣加入指令不會帶指紋，agent 會使用系統的信任存放區。伺服器每 12 小時檢查一次這些檔案，憑證更新後不需重新啟動就會換用新的；想立刻換用就重新啟動伺服器。

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
  docker.io/tonyhuang1101/forgeyard:1.2.0
sudo docker logs forgeyard
```

紀錄的內容和[快速入門](quick-start.md#1-install-the-server)裡的一樣，包含一次性的設定權杖（setup token）；接著照[建立擁有者並信任 CA](quick-start.md#2-create-the-owner-and-trust-the-ca)繼續。之後要再看一次權杖：`sudo docker logs forgeyard 2>&1 | grep -A 2 "setup token"`。

- **`PUBLIC_URL`**：大家和 agent 使用的位址，連接埠要是你對外公開的那個。伺服器在容器裡只看得到容器自己的位址，所以主機的位址要靠這個設定才會寫進伺服器憑證和 **Machines** 上的指令。要加更多名稱或位址：`-e TLS_EXTRA_NAMES=forge.example.com,192.168.1.21`。
- **`-p 7860:7860`**：映像檔在容器所有位址的 7860 連接埠上監聽（`HOST=0.0.0.0`）。想對外公開別的連接埠也可以，例如 `-p 443:7860` 搭配 `PUBLIC_URL=https://192.168.1.20`。
- **`--hostname forgeyard`**：固定主機名稱。沒有它的話，每個新容器的主機名稱都不同，伺服器憑證會在啟動時重新產生。這沒有害處（瀏覽器和 agent 信任的是 CA，不是憑證），只是紀錄比較多。
- **volume** 裡有資料庫、`secret.key`、`secrets.json` 和 `tls/`：升級時要保留。也可以改用主機上的資料夾，擁有者要是 uid 10001：`sudo install -d -o 10001 -g 10001 -m 700 /srv/forgeyard-data`，再用 `-v /srv/forgeyard-data:/data`。
- **其他設定**都用 `-e` 指定（[伺服器設定](reference.md#server-settings)），或用 `--env-file` 搭配 `.env` 檔。`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 會在第一次啟動時直接建立擁有者，不必用設定權杖。要用 PostgreSQL 就設定 `DATABASE_URL`：映像檔裡已經有驅動程式。
- **健康檢查**：映像檔內建健康檢查，`/api/health` 有回應後，`docker ps` 就會顯示 `healthy`。它透過容器自己的 loopback 詢問，而且不檢查憑證：這個請求不會離開容器，也不帶任何機密。

**備份** volume。先停止伺服器，資料庫的副本才會一致：

```bash
sudo docker stop forgeyard
sudo docker run --rm --user 0 -v forgeyard-data:/data:ro -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.2.0 tar czf /backup/forgeyard-data.tar.gz -C /data .
sudo docker start forgeyard
```

用同一個映像檔還原到新的空 volume（tar 以 root 執行，所以檔案會保留原本的擁有者，也就是伺服器的帳號）：

```bash
sudo docker run --rm --user 0 -v forgeyard-data:/data -v "$PWD":/backup \
  docker.io/tonyhuang1101/forgeyard:1.2.0 tar xzf /backup/forgeyard-data.tar.gz -C /data
```

和所有備份一樣，`secret.key` 要和資料庫分開保存（[原因](#back-up-and-restore)）。

**升級**靠 tag。先備份，再拉取新版本，用它執行同一個指令；volume 會保留所有資料，資料庫的變更會在啟動時執行：

```bash
sudo docker pull docker.io/tonyhuang1101/forgeyard:1.2.1
sudo docker rm -f forgeyard
sudo docker run -d --name forgeyard --restart always --hostname forgeyard \
  -p 7860:7860 \
  -v forgeyard-data:/data \
  -e PUBLIC_URL=https://192.168.1.20:7860 \
  docker.io/tonyhuang1101/forgeyard:1.2.1
```

`1.2.1` 就是那個版本；`1.2` 會跟著它最新的修補版本；`latest` 則是最新的正式版本。建議指定完整版本：agent 的指令使用伺服器的版本，所以升級伺服器後要接著升級 agent（[做法](#upgrade-the-server-and-the-agents)）。

**釘選 digest**：當某個 tag 絕對不能在你不知情時改變。先查出 digest：

```bash
sudo docker buildx imagetools inspect docker.io/tonyhuang1101/forgeyard:1.2.0
```

輸出裡有一行 `Digest: sha256:…`：改用 `docker.io/tonyhuang1101/forgeyard@sha256:…` 取代 tag。agent 的部分，在伺服器上設定 `AGENT_IMAGE=docker.io/tonyhuang1101/forgeyard-agent@sha256:…`：**Machines** 就會在每台 agent 的指令裡使用這個 digest。每個映像檔也附有建置來源證明（provenance）和 SBOM（`docker buildx imagetools inspect --format '{{ json .SBOM }}' …`）。

**自己建置**，從原始碼：`sudo docker build -f Dockerfile.server -t forgeyard:1.2.0 .`

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

   然後重新啟動它（`sudo systemctl restart forgeyard`）。資料庫的變更會在啟動時自動執行。請閱讀 CHANGELOG，看看有沒有需要額外處理的步驟。用 Docker 的話：拉取新的 tag，再用它執行同一個 `docker run`（[做法](#run-the-server-in-docker)）。
3. **agent**：一台一台來。執行中的 agent 重新啟動時，會保留它的工作負載。
   - **用單行安裝裝的**：再執行一次，但不加 `--join`。它會安裝伺服器的新版本，並保留設定和憑據：

     ```bash
     curl -fsSk https://192.168.1.20:7860/api/tls/ca.pem -o forgeyard-ca.pem && \
       sed '/-----/d' forgeyard-ca.pem | base64 -d > forgeyard-ca.der && \
       echo "3f9a…  forgeyard-ca.der" | sha256sum -c && \
       curl -fsS --cacert forgeyard-ca.pem https://192.168.1.20:7860/install.sh | \
       sudo sh -s -- --ca-fingerprint sha256:3f9a…
     ```

   - **Docker**：用 `docker pull` 拉取伺服器新版本的映像檔（**Machines** 頁面上有名稱），然後執行 `docker rm -f forgeyard-agent`，再用新的 tag 執行同一個 `docker run`。volume 會保留這台機器。
   - **手動安裝**：`git pull`、`uv sync`，然後重新啟動 agent。

**Machines** 頁面會顯示每個 agent 的版本。伺服器升級後，請盡快升級 agent：太舊的 agent 可能缺少新版伺服器需要的功能。

## 檢查設定健康狀態

Forgeyard 會檢查自己的設定，找出壞掉、有風險或沒有作用的設定和組合：拼錯的設定、可以花錢用雲端供應商卻沒有預算的金鑰、沒有人選擇 GPU 存取的共用機器、比伺服器舊的 agent、先試雲端再試本機層的路由。管理員會在三個地方看到結果：

- **Get started → Setup health** 列出每一項檢查結果，附上該怎麼做，以及到修正位置的連結。
- **頂端列的數字**顯示錯誤和警告的數量。沒有的時候就不會出現。
- **相關頁面上的橫幅**（Machines、API keys、Providers、Models、路由、部署、Users）顯示那個頁面的檢查結果。

一般使用者和 operator 都看不到這些。

- **錯誤**代表現在就壞了或不安全。它會一直顯示到修好為止。
- **警告**代表可能會花錢、外洩或壞掉。**提示**代表沒有作用或出乎意料。這兩種都可以 **Snooze 30 days**：只對你自己隱藏，30 天後如果情況還在就會再出現。**Show snoozed** 會列出它們，並提供 **Show again**。

檢查結果在頁面要求時，根據設定、資料庫和各 agent 最後一次的回報算出來，所以修好之後一分鐘內就會反映（下一個頁面則會立刻反映）。設定的問題也會在伺服器啟動時寫進它的紀錄，每一行都以 `Settings:` 開頭。完整清單在參考資料裡：[Setup health 檢查](reference.md#setup-health-checks)。

## 從頭到尾檢查一台伺服器

升級之後，或要開始使用新版本之前，`scripts/accept_all.py` 會在你自己的伺服器和機器上檢查每一項功能：只要一個指令，每項檢查印出一行 PASS、FAIL 或 SKIP（SKIP 會說明原因），最後再印出總結。它用管理員帳號透過 API 操作，用官方 OpenAI SDK 呼叫 `/v1`，並用瀏覽器檢查網頁主控台。

在 Forgeyard 的 checkout 裡執行它，所在的電腦要連得到伺服器（金鑰守門員的檢查還需要連得到機器上模型副本的連接埠），並準備好從 **Trust this server** 下載的伺服器 CA：

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
| `workloads` | 行程類服務、任務（成功、重試、`max_duration`）、機器有 Docker 或 Podman 時的容器任務；在 GPU 機器上，工作負載只看得到分配給它的卡 |
| `models` | 部署小模型並等它們就緒：每台 CPU 機器上的 Qwen3-1.7B GGUF；GPU 機器上的對話、embedding、rerank 和語音模型（估算放不下的會跳過）；部署頁面上的估算；**Try it** |
| `gateway` | `/v1`：對話、串流、embedding、rerank、語音轉文字；金鑰的模型允許清單、限制和撤銷；每個副本連接埠上的金鑰守門員；兩台 CPU 機器之間的負載平衡；Playground；用量 |
| `cloud` | 給了 `--cloud-provider` 才會執行：外溢到雲端、設為 **Never** 的金鑰、只用本地、雲端上限、費用，以及預算和它的警示 |
| `passthrough` | 一個公開在 `/x/` 的小型 HTTP 程式：在 CPU 機器上以行程執行，在 GPU 機器上以容器執行。金鑰（none、listed）、照原樣轉送的路徑、被拒絕的路徑、被替換的憑證與轉發標頭、強制的沙箱、轉址、串流、本文上限、rpm、同時請求數、讀取逾時、副本連接埠前的金鑰守門員、用量 |
| `console` | 以管理員和一般使用者身分，在 1440 和 390 px 寬度下打開每個主要頁面：能載入、分頁標題正確、沒有錯誤、不會左右捲動 |

用 `--only gateway,cloud` 或 `--skip console` 選擇要跑的組別；即使沒選 `models`，`gateway` 和 `cloud` 也會部署它們需要的模型。全部沒有失敗時結束代碼是 0，有檢查失敗時是 1，無法開始執行時是 2。

執行前要知道的事：

- **它會自己清理。**它建立的所有東西都命名為 `accept-<run>-…`：部署、工作負載、路由、API 金鑰、一個一次性使用者，以及它必須新增的模型目錄項目。結束時會全部移除，失敗或按下 **Ctrl-C** 也一樣。這次執行留下的用量紀錄和稽核事件會保留。
- **只有一個請求會送到雲端供應商**，內容是固定的一小段提示，費用不到一美分。供應商的每月預算會被改動幾秒鐘再改回原值；其他預算檢查只用這次執行自己的金鑰和使用者。
- **一次性使用者會有五次登入失敗**：伺服器允許每個位址在 15 分鐘內失敗 30 次，所以連續執行很多次時，中間要等一下。
- **時間：**機器上已經有模型時大約 15 到 20 分鐘（沒有 GPU 機器時約 3 分鐘）；需要下載的話另外加上下載時間。
- 加上 `--kill-local` 還會檢查容錯移轉：它會強制結束在執行這支腳本的電腦（兩台 CPU 機器之一）上那個 CPU 副本的引擎。
- 主控台的檢查需要 Playwright for Python 和 Chrome（`--chrome`，預設 `/opt/google/chrome/chrome`）；`--install-playwright` 會把 Playwright 和 OpenAI SDK 一起裝進腳本自己的虛擬環境（`--venv`）。沒有的話，console 這組會跳過。

## 備份與還原

**伺服器上要備份的東西**，除非你搬過位置，否則都在它的 `data/` 資料夾裡（用 Docker 的話是 `/data` volume，[這樣備份](#run-the-server-in-docker)）：

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

備份好 `secret.key` 之後，到 **Get started → Setup health** 勾選 **I have a backup of key id …**。在有管理員勾選之前，Setup health 會提醒每一位管理員；金鑰更換後也會再次提醒。金鑰本身絕不會離開伺服器：頁面只會顯示它的 id。

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
