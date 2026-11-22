# 驗證過的硬體

[English](verified-hardware.md)

每個版本在打上 tag 之前，都會在真實的機器上驗收，不只是跑 CI。這一頁依硬體型號列出這些機器，以及每個版本通過的項目數，從 1.4.3
開始。每種機器上跑過哪些東西，見 README 的[已驗證的硬體](../README.zh-TW.md#已驗證的硬體)。

## 一個版本怎麼驗收

- **[`scripts/accept_all.py`](../scripts/accept_all.py)**，完整的回歸測試：在一台執行中的伺服器上，搭配真實的 agent 與真實的引擎，透過
  伺服器的 API、用官方 OpenAI SDK 呼叫 `/v1`，以及操作網頁主控台，檢查每一項已發布的功能。每項檢查回報 PASS、FAIL 或 SKIP 並附上原因；
  在那裡無法執行的檢查（例如共用的顯示卡上可用記憶體不夠）會被跳過，絕不算成通過。執行完會移除它建立的東西。
- **[`scripts/accept_vision.py`](../scripts/accept_vision.py)**，視覺模型：每個模型都以內建的 `vision` 配方部署，用一張已知的照片呼叫
  `/v1/vision/*`，再檢查各種拒絕情況與它記錄的用量。
- 其他 `scripts/accept_*.py` 腳本更深入地檢查單一功能（對話中的圖片、音訊與影片；文字轉語音；圖片與影片生成）；
  [docs/development.zh-TW.md](development.zh-TW.md) 列出了它們。

你也可以對自己的伺服器和機器執行同樣的腳本（[做法](guide/zh-TW/how-to.md#從頭到尾檢查一台伺服器)）。這些執行的原始紀錄不公開：裡面有
機器與網路的名稱。公開的只有硬體型號和數字。

## 機器

| 機器 | 用途 |
|---|---|
| NVIDIA A100 80GB PCIe，與其他工作共用的 8 卡主機（以 GPU 允許清單限制 Forgeyard 使用的卡） | GPU 引擎：vLLM、vLLM-Omni、llama.cpp（CUDA）、stable-diffusion.cpp（CUDA）、speaches、視覺映像檔 |
| AMD Ryzen AI MAX+ 395 與 Radeon 8060S（Strix Halo APU） | llama.cpp 與 stable-diffusion.cpp 的 Vulkan 版本，以及 CPU；檔案與映像檔經由伺服器取得，代表沒有網際網路的機器 |
| 沒有 GPU 的 x86-64 機器 | 以行程與容器執行的 CPU 引擎 |
| Google Gemini（`gemini-2.5-flash`） | 雲端層：溢出、費用與預算 |

## 各版本的結果

| 版本 | 執行內容 | 機器 | 通過 | 失敗 |
|---|---|---|---|---|
| 1.4.5 | 1.4.5rc1 的完整回歸測試（`accept_all.py`，13 項略過），兩台機器都用 rc 版 agent，所以閘道以 TLS 連到每個副本 | A100 主機、x86-64 CPU 機器、Gemini | 432 | 0 |
| 1.4.5 | 開發期間：閘道與 agent supervisor 拆成較小的部分後的完整回歸測試（#173；29 項略過） | A100 主機、x86-64 CPU 機器、Gemini | 380 | 0 |
| 1.4.5 | 開發期間：vLLM 0.32 上 16k context 的 Gemma 4 12B，KV cache 估算對照引擎自己的數字（#201）：估算 2.091 GiB，vLLM 2.09 GiB | A100 | 1 | 0 |
| 1.4.4 | 完整回歸測試（`accept_all.py`，分兩次執行：233 + 163） | A100 主機、x86-64 CPU 機器、Ryzen AI MAX+ 395、Gemini | 396 | 0 |
| 1.4.4 | 零樣本物件偵測與影像分類（`accept_vision.py`） | A100 | 77 | 0 |
| 1.4.4 | 零樣本物件偵測與影像分類（`accept_vision.py`） | Ryzen AI MAX+ 395，在 CPU 上 | 71 | 0 |
| 1.4.4 | vLLM 0.32 上的文字分類：三個推薦模型、金鑰防護、Playground | A100 | 全部 | 0 |
| 1.4.3 | 完整回歸測試（`accept_all.py`） | A100 主機、x86-64 CPU 機器、Ryzen AI MAX+ 395、Gemini | 303 | 0 |
| 1.4.3 | 網頁主控台（`accept_all.py` 的 console 群組） | 同上 | 31 | 0 |

Ryzen AI MAX+ 395 那台機器沒有參加 1.4.5 的這些執行。

歡迎回報在其他機器上的結果：見 [CONTRIBUTING.md](../CONTRIBUTING.md)（英文）。
