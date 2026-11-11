# Forgeyard 使用指南

Forgeyard 在你自己的 GPU（和 CPU）上執行工作與模型，並把模型統一放在一個相容 OpenAI 的位址後面提供服務。這份指南帶你從零開始，一路做到第一次呼叫 API，說明各個部分怎麼搭在一起，也回答之後會遇到的問題。

這份指南也內建在 Forgeyard 裡：在帳號選單開啟 **Guide**，或在你的伺服器上開 `/guide`。不需要連上網路也能看。[English](../README.md)

## 從哪裡開始

| 如果你想… | 請看 |
|---|---|
| 第一次架設 Forgeyard | [快速入門](quick-start.md)：大約 10 分鐘，另加下載模型的時間 |
| 了解機器、部署、金鑰和角色 | [概念](concepts.md) |
| 加入 AMD 或純 CPU 的機器、用 Docker 跑 agent（代理程式）、升級、備份 | [操作指南](how-to.md) |
| 加入 OpenAI、Anthropic、Gemini 或 Azure，讓模型溢出到雲端 | [雲端供應商](cloud-providers.md) |
| 排除無法運作的問題 | [疑難排解](troubleshooting.md) |
| 查某個設定或指令 | [參考資料](reference.md) |

## 各頁內容

1. [快速入門](quick-start.md)：安裝伺服器、信任它的憑證、加入一台機器、選擇它的 GPU、加入一個推薦的模型、部署它、建立 API 金鑰，再用 curl 和 OpenAI SDK 呼叫它。
2. [概念](concepts.md)：機器與 agent、GPU 存取權、工作負載、模型、recipe（配方）與估算、部署、閘道與 Playground、API 金鑰與限制與用量、雲端供應商與分層、費用與預算、角色、登入與工作階段。
3. [操作指南](how-to.md)：加入 AMD 機器或純 CPU 機器、用 Docker 跑 agent、執行服務或任務、加入雲端供應商、追蹤並限制雲端花費、在共用機器上限制 GPU、加入其他人、使用自己的憑證或反向代理、把伺服器當成服務執行、升級、備份與還原、搬移模型快取、輪替金鑰、撤銷機器、解除安裝 agent。
4. [疑難排解](troubleshooting.md)：在實際機器上遇過的問題，附上症狀、原因和解法：憑證、離線的 agent、下載、記憶體、`/v1` 的每種錯誤、不信任 CA 的程式、找不到的模型、溢出和費用。
5. [雲端供應商](cloud-providers.md)：加入 OpenAI、Anthropic、Gemini、Azure OpenAI、OpenRouter 或你自己的伺服器；把模型導向雲端；請求何時溢出；哪些金鑰可以使用雲端；各供應商端點做不到的事；哪些東西會離開你的伺服器。
6. [參考資料](reference.md)：伺服器的每個設定、agent 的每個設定、閘道的端點、標頭和錯誤碼、工作負載的環境變數、命令列、檔案和連接埠。

## 慣例

- 指令可以直接照原樣複製。只需要替換寫成 `192.168.1.20`（你的伺服器位址）、`fyj_…`（加入權杖，join token）或 `fyk_…`（API 金鑰）這類的部分。
- 指令區塊裡只放指令，所以它的 **Copy** 按鈕複製到的內容可以直接貼上執行。指令印出的內容會放在後面另一個區塊，並標示「會印出」。
- 範例中伺服器使用 `https://192.168.1.20:7860`，模型快取使用 `/srv/forgeyard-models`。請換成你自己的。
