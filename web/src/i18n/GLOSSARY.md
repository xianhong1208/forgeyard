# Console glossary (English → 繁體中文)

The console's Traditional Chinese follows the zh-TW guide (`docs/guide/zh-TW/`). Use these words in every
dictionary module so pages translated in different pull requests read as one product. Taiwan usage:
伺服器 (not 服务器), 程式 (not 程序 for a program), 資料, 檔案, 預設, 設定, 網路, 連線, 記憶體, 硬碟/磁碟,
影片, 介面, 用戶端 (not 客戶端), 本機 (not 本地), 重新啟動 (not 重啟). Full-width punctuation in Chinese sentences
(，。：；（）「」), half-width inside code and numbers and between two numbers (`3/4 就緒`, `{done}/{total}`). A space
between Chinese and Latin letters or digits (`3 個副本`, `API 金鑰`), none before full-width punctuation. Speak to the
reader plainly with 你, never 您.

## Product terms

| English | 繁體中文 | Notes |
|---|---|---|
| machine | 機器 | |
| agent | agent | Kept in English, as in the guide (the guide adds 代理程式 once in Concepts). |
| join token / join command | 加入權杖 / 加入指令 | |
| credential | 憑據 | A machine's own credential. |
| certificate authority (CA) | 憑證授權單位（CA） | |
| certificate | 憑證 | |
| fingerprint | 指紋 | |
| heartbeat | 心跳 | |
| online / offline / revoked | 線上 / 離線 / 已撤銷 | |
| revoke / remove | 撤銷 / 移除 | |
| pinned (to a machine) | 固定 | 釘選 is for an image or a model *pinned to a version or digest*. |
| placed / placement | 放置 / 放在哪裡 | Where the scheduler puts a replica (配置 only for memory: 記憶體配置圖). |
| digest | digest | Kept in English, as in `@sha256:…`. |
| registry (container images) | registry | Kept in English (映像檔 registry). |
| export / load an image (docker save / load) | 匯出 / 載入 | |
| deployment / deploy | 部署 | |
| replica | 副本 | Only a replica. A copy of something else is 複本 (a cloned provider, a copied engine). |
| workload | workload | Kept in English (the guide does). |
| app (deprecated) | 應用程式 | |
| process (runtime) | 行程 | 程式 for a program. |
| engine (recipe) | 推論引擎（配方） | The software that runs a model (vLLM, llama.cpp, faster-whisper…). The console says 推論引擎, never a bare 引擎 (1.5.2: a bare 引擎 read as unclear); the API and YAML say recipe = 配方. See *Engine, image, program* below. |
| container engine (Docker, Podman) | 容器引擎 | The one compound that keeps a bare 引擎: it is not the engine above. |
| model library / catalog / sources | 模型庫 / 目錄 / 來源 | Models page tabs: 目錄, 推論引擎, 來源. |
| model cache | 模型快取 | |
| estimate | 估算 | |
| gated (model) | 受限 | |
| Hugging Face token | Hugging Face 權杖 | |
| gateway | 閘道 | |
| key guard (the agent's, in front of a replica) | 金鑰守門員 | |
| client (an OpenAI client, an HTTP client) | 用戶端 | |
| loopback address | 迴路位址 | |
| private address | 私有位址 | |
| local (not cloud) | 本機 | |
| route / tier | 路由 / 層 | |
| cloud provider | 雲端供應商 | |
| cloud fallback / spill | 雲端備援 / 溢出 | |
| budget | 預算 | |
| API key | API 金鑰 | |
| usage | 用量 | |
| audit log | 稽核紀錄 | |
| setup health | 設定健康檢查 | The advisor's panel. |
| console | 主控台 | |
| Get started | 開始使用 | |
| guide | 使用指南 | |
| Playground | 試用區 | |
| Open *a page* (a button that goes to a page) | 前往「頁面」 | 開啟 for a dialog, a menu or one item (開啟部署). |
| Actions (a table column) | 操作 | |
| Serving (page) | 服務中 | What runs now. |
| Machines / Models / Cloud / Users (pages) | 機器 / 模型 / 雲端 / 使用者 | |
| sleep when idle / asleep / waking / wake | 閒置時休眠 / 休眠中 / 喚醒中 / 喚醒 | |
| automatic replicas | 自動調整副本數 | |
| drain | 排空 | |
| ready / healthy | 就緒 / 健康 | |
| memory map | 記憶體配置圖 | |
| Exclusive / Share VRAM | 獨占 / 共用 VRAM | |
| secret key (data/secret.key) | 加密金鑰 | It encrypts the stored secrets = 機密資料. |
| passphrase | 通關密語 | |
| session | 工作階段 | |
| owner / admin / operator / user | 擁有者 / 管理員 / 操作員 / 使用者 | |
| sign in / sign out | 登入 / 登出 | |
| image (container) | 映像檔 | pull = 拉取, layer = 層. |
| relay (through the server) | 經由伺服器 | |
| try out (an engine: a trial run) / trial | 試跑 / 試跑紀錄 | 1.5.2: a temporary deployment that is checked, then removed. Not 試用 (*Try this version* = 試用這個版本, which makes a copy) and not 試試看 (a deployment's *Try it*, one request). |

## Engine, image, program

- 推論引擎 = the software: 推論引擎 llama-cpp, 加入自訂推論引擎, 沒有推論引擎能提供它.
- 映像檔 = its Docker image: 推論引擎的映像檔 where the sentence must say whose, plain 映像檔 where the context already
  says it (a variant's row, an engine's page). Not 引擎映像檔.
- 程式 = a process variant's program (its release archive or installed command): 推論引擎的程式.
- 容器引擎 = Docker or Podman on a machine; it keeps its name.
- Where space is tight (a tab, a column header) 推論引擎 still fits: four characters, no shorter form.
- `web/src/i18n/engineWording.test.ts` fails on a bare 引擎 in a zh-TW dictionary.

## Model kinds (the Serving sections and tasks)

| English | 繁體中文 |
|---|---|
| Chat / Multimodal chat | 對話 / 多模態對話 |
| Embeddings | 嵌入 |
| Rerank | 重排序 |
| Text classification | 文字分類 |
| Speech to text | 語音轉文字 |
| Text to speech | 文字轉語音 |
| Images | 圖片 |
| Video | 影片 |
| frame (video) | 影格；每秒 N 格 |
| Vision: object detection, segmentation, depth estimation, image classification | 視覺：物件偵測、影像分割、深度估計、圖片分類 |

## Left in English

Product and vendor names (Forgeyard, NVIDIA, AMD, Hugging Face, Docker, vLLM, llama.cpp), units (GiB, MiB,
GPU, CPU, RAM), protocol words and paths (`/v1`, `PUBLIC_URL`, `TLS=off`), commands, and anything the server
sends that the console has no translation for. Keys whose Chinese text has no Chinese character on purpose are
listed in `sameInBoth.ts`.

## Conventions for a dictionary module

- One module per area: `en/<area>.ts` (`as const`) and `zh-TW/<area>.ts` (`satisfies Record<keyof typeof en, string>`),
  both registered in `dictionaries.ts`. Every key starts with `<area>.`.
- `{name}` is a value; `<tag>text</tag>` is rich text (a link, `<strong>`), filled with `t.rich()`.
- Plurals: `<key>.one` and `<key>.other`, used with `t.plural()`; Chinese repeats the same text in both.
- Codes the server sends (error codes, finding ids, event kinds) go in `server.ts` or the area that shows them,
  as `<area>.<kind>.<code>`, read with `t.maybe(key, whatTheServerSaid)`.
