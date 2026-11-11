# 雲端供應商

Forgeyard 可以把請求送到它以外的模型 API：OpenAI、Anthropic、Google Gemini、Azure OpenAI、OpenRouter，或任何相容 OpenAI 的伺服器（例如另一個網路上的 vLLM）。你的程式仍然只用一個位址、一組 API 金鑰、一套限制和一個用量頁面，而且本機模型優先。[English](../cloud-providers.md)

## 運作方式

- **供應商（provider）**是那些 API 之一的一個帳號：它的 base URL 和 API 金鑰。金鑰加密儲存，之後不會再顯示。管理員在 **Providers** 新增供應商。
- 供應商的**模型**是你可以送給它的名稱，例如 `gpt-4o-mini`；Azure 則是你在 Azure 上的部署名稱。
- **路由（route）**是 `/v1` 上的一個模型名稱，它有分成好幾**層（tier）**的**目標（target）**。每個部署都有自己的路由，唯一的目標就是這個部署。管理員可以替它加上第二層，例如本機副本忙碌或停擺時改用的雲端模型；也可以建立只有雲端模型的路由。
- Forgeyard 一律透過各供應商**自己的 OpenAI 相容端點**溝通。它沒有轉換成各家原生 API 的翻譯層，也不使用 LiteLLM。各端點做不到的事列在[下方](#what-each-providers-endpoint-cannot-do)。
- 每把 API 金鑰都有一個**雲端**設定，除非有人改過，否則一律是 **Never**。請見[誰可以使用雲端](#who-may-use-the-cloud)。

只有 chat、completions 和 embeddings 會送到供應商。rerank、score 和語音轉文字一律留在本機。

## 新增供應商

在 **Providers** 選 **Add a provider**，挑選種類並貼上金鑰。接著：

1. **Test connection** 會帶著金鑰向 base URL 查詢 `/models`，顯示供應商列出了幾個模型，或它拒絕的原因。
2. 打開這個供應商，加入它的模型：直接輸入名稱，或用 **Import from /models**（Azure 不適用，它的清單不會列出你的部署）。
3. 把模型放進路由（請見[把模型導向雲端](#route-a-model-to-the-cloud)）。

除了 *OpenAI-compatible server* 以外，所有種類都預設勾選 **Outside your network**。第三方服務請保持勾選；自己的伺服器請取消勾選：在你網路內的供應商不算雲端，所以每把金鑰都能使用。

### OpenAI

種類選 *OpenAI*，金鑰從 platform.openai.com 取得。base URL 已預先填好（`https://api.openai.com/v1`）。chat、completions 和 embeddings 都能用。`max_tokens` 會改以 `max_completion_tokens` 送出，因為較新的 OpenAI 模型不接受舊的名稱。

### Anthropic

種類選 *Anthropic*，金鑰從 console.anthropic.com 取得。base URL 已預先填好（`https://api.anthropic.com/v1`，也就是相容端點）。如果你的金鑰屬於組織而不是某個 workspace，請加上額外標頭 `anthropic-workspace-id: <id>`。Anthropic 沒有 embeddings。依賴它之前，請先讀[它做不到的事](#anthropic-1)。

### Google Gemini

種類選 *Google Gemini*，金鑰從 aistudio.google.com 取得。base URL 已預先填好（`https://generativelanguage.googleapis.com/v1beta/openai`）。Gemini 列出的模型名稱是 `models/gemini-…`；匯入時會去掉 `models/`，因為請求用的是去掉後的名稱。chat 和 embeddings（`gemini-embedding-001`）都能用。

### Azure OpenAI

種類選 *Azure OpenAI*，填入你 Azure OpenAI 資源的金鑰，以及你自己的 base URL：`https://<resource>.openai.azure.com/openai/v1`（v1 API，不需要 `api-version`）。模型名稱是你在 Azure 上的**部署名稱**，請手動加入。

### OpenRouter

種類選 *OpenRouter*，金鑰從 openrouter.ai 取得。base URL 已預先填好（`https://openrouter.ai/api/v1`）。模型名稱長得像 `openai/gpt-4o-mini` 或 `anthropic/claude-sonnet-4.5`，可以直接匯入。OpenRouter 會在它自己的供應商之間轉送，並回報每個請求的費用。

### 你自己的伺服器（其他地方的 vLLM）

種類選 *OpenAI-compatible server*，填入它的 base URL（例如 `https://llm.example.org/v1`），需要的話再加上金鑰。*Outside your network* 預設不勾選：這是你自己的伺服器，所以金鑰不需要雲端權限就能使用。如果透過 VPN 連到私有位址，可以勾選 **Allow plain HTTP**。Forgeyard 送給它的請求不會刪掉任何參數。

## 把模型導向雲端

讓某個部署在忙碌或停擺時溢出到雲端：

1. 打開這個部署，選 **Routing and cloud spill**。也可以到 **Models** 的 **Routes and cloud** 分頁。
2. 按 **Add a tier**，把供應商的模型加進去。
3. 選好規則（請見[請求何時溢出](#when-a-request-spills)），按 **Save routing**。

要用自己的名稱提供雲端模型，請到 **Models** 的 **Routes and cloud**，選 **New route**。填入用戶端會以 `model` 送出的名稱，再加入供應商的模型。只有雲端設定為 **Allowed** 的金鑰看得到這種路由。

每個回應都會在 `X-Forgeyard-Target` 標頭說明是誰回答的：`local:<deployment>#<replica>` 或 `provider:<provider>/<model>`。如果請求越過了第一層，`X-Forgeyard-Spilled` 會說明原因：`unavailable`、`busy`、`refused` 或 `too_long`。回應本文裡的 `model` 是上游寫的值。Playground 會在每個回答下方顯示目標。

## 請求何時溢出

只有在**還沒有任何東西回傳給用戶端之前**，而且這一層處於下列狀況時，請求才會移到下一層：

- **無法使用（unavailable）**：沒有任何副本就緒，或連不上任何副本；
- **忙碌（busy）**：每個就緒副本經由這台伺服器處理中的請求數，都已達到它所屬部署的 *Concurrent requests* 設定。如果設了 **Wait for a free local slot**，會先最多等待那麼多毫秒（上限 10,000；預設 0）；
- **拒絕（refusing）**：引擎回應 429 或 503；
- **太小（too small）**，只在路由勾選 **Spill a prompt that is too long** 時：提示的大小（以每個 token 約 3 bytes 偏高估算）加上 `max_tokens`，超過部署的 context。

下列情況**絕不**會往下走：

- 引擎回應其他錯誤，例如請求有誤的 400（換到哪裡都會失敗，還會在上游花錢）；
- 請求已送出，但沒有及時回應（504）：再試一次會讓工作加倍；
- 副本拒絕閘道的內部金鑰（502）；
- 用戶端已經離開；
- 回答已經開始串流。

如果本機這層是忙碌，而雲端也接不下這個請求，請求最後還是會等待本機副本。

在雲端層內，回應 429 或 5xx、或連不上的目標會被略過，並在 30 秒內（`PROVIDER_COOLDOWN_SECONDS`）排到最後才嘗試。同一層的多個目標會依權重分攤請求。當沒有其他目標可試、而供應商要求等待不超過 2 秒時（`Retry-After`，`PROVIDER_RETRY_AFTER_CAP`），請求會等一次再試。

單一請求可以用 `X-Forgeyard-Local-Only: 1` 標頭禁止自己使用雲端。它永遠不會放寬金鑰原本的權限：

```bash
curl https://192.168.1.20:7860/v1/chat/completions \
  -H "Authorization: Bearer fyk_…" -H "X-Forgeyard-Local-Only: 1" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3-8b", "messages": [{"role": "user", "content": "Hello"}]}'
```

勾選 **Local only** 的路由絕不會離開你的網路，不論金鑰允許什麼。敏感的模型可以把它當成開關。

## 誰可以使用雲端

每把 API 金鑰都有一個**雲端**設定：

| 設定 | 混合路由（先本機、再雲端） | 只有雲端的路由 |
|---|---|---|
| **Never**（`off`） | 只用本機：請求會拿到本機的錯誤，而不是溢出 | 不存在：404，也不會出現在 `/v1/models` |
| **Only to spill**（`fallback`） | 依上面的規則溢出 | 不存在 |
| **Allowed**（`on`） | 依上面的規則溢出 | 會列出，也可以使用 |

- 1.2 以前建立的金鑰一律是 **Never**。新金鑰會拿到伺服器的預設值，除非管理員改過，否則是 **Never**。
- 每個人能替自己的金鑰設定的值，最多到「the most people may give their own keys」，除非管理員調高，否則是 **Never**。兩者都在 **API keys** 的 **Cloud for keys**。
- 管理員可以設定任何金鑰的雲端設定。設在別人的金鑰上時，這個值會像限制一樣對擁有者**鎖定**。**Unlock cloud** 會把它還原成預設值。
- 在你網路內的供應商（沒有勾選 *Outside your network*）不算雲端：每把金鑰都能使用。
- 在 **Playground** 裡，管理員可以使用雲端；其他人則拿到他們能替金鑰設定的最大值。

## 各供應商端點做不到的事

Forgeyard 以 OpenAI 的格式，把請求送到各供應商的 OpenAI 相容端點，並把回應原封不動傳回。以下是這些端點本身的限制，依各供應商的文件整理（2026 年 11 月）。

### OpenAI

在 Chat Completions 和 embeddings 上沒有損失。較新的功能會先（或只會）出現在 Responses API，而 Forgeyard 不提供這個 API。

### Anthropic

Anthropic 表示它的 OpenAI 相容層並非大多數用途的長期或正式環境方案。透過它，你會失去：

- **提示快取（prompt caching）**，所以長提示會比直接用 Anthropic 自己的 API 貴；
- **thinking**：永遠不會回傳，而且 `thinking: {type: adaptive}` 會被拒絕；
- **PDF、檔案和音訊輸入**（400）；
- **大於 1 的 `n`**（拒絕）、工具上的 `strict`（忽略）、`reasoning_effort`（忽略）；
- **用量明細**：`prompt_tokens_details` 和 `completion_tokens_details` 永遠是空的，所以 Usage 不會顯示快取或推理 token；
- `json_schema` 加 `strict: true` 以外的 `response_format`（Forgeyard 會拿掉單純的 `{"type": "text"}`，反正它就是預設值）；
- **embeddings**：Anthropic 沒有提供。

在最新的 Claude 模型上，`temperature` 小於 1 又同時設定 `top_p`，或強制指定 `tool_choice`，也可能被拒絕。system 和 developer 訊息會被合併成一則。之後可能會加入保留快取和 thinking 的原生 `/v1/messages` 直通。

### Google Gemini

- 這個端點還在 **beta**，不認得的參數會被默默忽略。
- 不支援檔案上傳或下載。context 快取只能透過 `extra_body.google.cached_content` 使用。
- **Gemini 3 thought signature**（每個工具呼叫上的 `extra_content.google.thought_signature`）必須在下一輪送回。Forgeyard 會原封不動地傳遞。如果對話較早的回合是由本機模型回答，就不會有 signature，Gemini 可能以 400 拒絕。
- 有些模型無法關閉 thinking。thinking token 是否計入 `completion_tokens`，目前還沒確認。

### Azure OpenAI

- 模型名稱是你的**部署名稱**，不是模型本身的名稱。
- Microsoft 建議使用 Responses API。v1 API 的第一個版本只包含 Azure 部分的功能，而它的內容過濾錯誤是 Azure 自己的格式。

### OpenRouter 和其他伺服器

只有它們背後某個供應商特有的功能。OpenRouter 接受 `top_k`、`min_p` 和 `repetition_penalty` 並往下傳；對其他雲端種類，請求送出時 Forgeyard 會拿掉這些以及其他只有引擎才有的參數（vLLM 的 `chat_template_kwargs`、`guided_*`，llama.cpp 的取樣設定）。

## 對外連線：哪些東西會離開你的伺服器

- 送往供應商的請求使用自己的 HTTP client：驗證 TLS，**絕不跟隨重新導向**（金鑰不會被轉送到重新導向指向的地方），也絕不使用環境變數裡的 proxy。
- `EGRESS_PROXY` 會改經由你指定的 proxy。`EGRESS_ALLOWED_HOSTS` 會限制只能連到某些主機。`EGRESS_CA_FILE` 會額外信任公司的 CA。
- 只允許 **https**。雲端供應商必須解析到公開位址。私有位址只給你網路內的供應商使用，純 http 只給勾選 **Allow plain HTTP** 的供應商。
- **link-local 和雲端 metadata 位址**（169.254.169.254、fd00:ec2::254）一律拒絕。每次請求前都會檢查位址，而且連線會直接連到檢查過的那個位址。
- 供應商金鑰絕不會寫進記錄檔、稽核紀錄或任何回應，複製供應商時也絕不會一起複製（複本沒有金鑰，而且預設停用）。
- 提示和回答絕不會寫進記錄檔。

## 疑難排解

### Test connection 說金鑰被拒絕

供應商回應 HTTP 401 或 403。請到供應商的網站檢查金鑰。Anthropic 的組織層級金鑰需要 `anthropic-workspace-id` 標頭。Azure 的金鑰必須屬於 base URL 裡的那個資源。

### 502 provider_auth_failed

供應商拒絕了 Forgeyard 給它的金鑰。Forgeyard 絕不會把它當成 401 傳下去，否則用戶端會以為是自己的金鑰有問題。請管理員修正供應商的金鑰。

### 502 provider_blocked

請求在送出之前就被對外連線規則拒絕了：base URL 是 http、不在 `EGRESS_ALLOWED_HOSTS` 裡，或解析到私有、link-local 或 metadata 位址。錯誤訊息會說明是哪一個。

### 模型出現在 Models 的清單裡，卻回應找不到

那是只有雲端的路由，而金鑰的雲端設定不是 **Allowed**。對這種金鑰來說，這些模型就和不存在的名稱一樣「找不到」。

### 請求沒有溢出

依序檢查：金鑰的雲端設定（**Never** 永遠不會溢出）；路由的 **Local only**；請求的 `X-Forgeyard-Local-Only`；以及原因。400、送出後逾時、或回答已經開始，都不會溢出。「忙碌」只計算經由這台伺服器、相對於部署 *Concurrent requests* 的請求數。

### 請求送到雲端的次數比預期多

每個副本都達到了它的 *Concurrent requests*。可以調高這個值（如果引擎和記憶體允許）、增加副本，或把 **Wait for a free local slot** 設成幾百毫秒。`X-Forgeyard-Spilled` 和 Usage（**Local or cloud**）會顯示溢出的次數和原因。
