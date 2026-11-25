// @vitest-environment jsdom
// The model library in Traditional Chinese (#189, group "Model library"): Models (Catalog, Engines, Sources), Add a
// model, a model's page and an engine's page, and the words of lib/models, lib/addmodel, lib/licences and lib/pages.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { lookup } from "../i18n/core";
import { en } from "../i18n/dictionaries";
import { setLang } from "../i18n/lang";
import { compact, fitsLine, searchLicenceProblem, visionProblem, type SearchResult, type Suggestion } from "../lib/addmodel";
import { engineDescription } from "../lib/engines";
import { licenceTerms } from "../lib/licences";
import { billions, mediaText, params, sentText, taskLabel } from "../lib/models";
import { presenceText, whereText } from "../lib/pages";
import fixture from "../test/machines.json";
import type { CatalogModel, CatalogResponse, EnginePage, ModelPage, Recipe } from "../lib/types";
import { languagesText } from "./AddModel";

const GIB = 1024 ** 3;
const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

const LLAMA_DESC = lookup("en", "library.engineDesc.llama-cpp");
const variant = (name: string, vendor: "nvidia" | "amd" | "cpu", image: string | null, extra: Record<string, unknown> = {}) => ({
  name, vendor, runtime: image ? "container" as const : "process" as const, image, arch: null, compute_capability: null, driver: null,
  engine: image ? null : "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-bin.zip", ...extra,
});
const LLAMA: Recipe = {
  name: "llama-cpp", title: "llama.cpp", description: LLAMA_DESC, engine: "llama.cpp", version: "1.0.0",
  tasks: ["chat", "tts"], formats: ["gguf"], api: { chat: "openai-chat", tts: "openai-speech" }, share: "exclusive", builtin: true, estimate: "gguf",
  devices: ["gpu", "cpu"],
  params: { context: { type: "int", default: 4096, min: 512, label: "Context length", help: "Tokens per request" },
    kv_cache: { type: "choice", default: "f16", choices: ["f16", "q8_0"], advanced: true } },
  variants: [variant("cuda", "nvidia", "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:a8cf841eef4e9d6ea28bb4d88eea1004032a2bf0d1302bff7b8d2100e801ed16", { image_check: true, compute_capability: "7.0" }),
    variant("cpu-process", "cpu", null)],
} as Recipe;
const CUSTOM: Recipe = { ...LLAMA, name: "llama-cpp-mine", description: "My own build of llama.cpp.", builtin: false, params: {},
  variants: [variant("vulkan", "amd", "forgeyard-local/llama.cpp:server-vulkan-b11977")] } as Recipe;

const MODEL: CatalogModel = {
  id: 7, name: "qwen3-1.7b-gguf", title: null, source: "hf://ggml-org/Qwen3-1.7B-GGUF", kind: "hf", ref: "main", revision: "daeb8e2d528a760970442092",
  file: "Qwen3-1.7B-Q4_K_M.gguf", format: "gguf", task: "chat", task_label: "Chat", size_bytes: 1282439264, engine_args: {}, recipes: ["llama-cpp", "llama-cpp-mine"],
  deployments: 0, created_by: "owner", created_at: "2026-11-10T00:00:00", summary: { params: 2031739904, architecture: "qwen3", layers: 28, max_context: 40960 },
  gguf_files: [], license: "apache-2.0", modalities: ["image"], gated: true,
} as CatalogModel;

const EST = (device: "gpu" | "cpu") => ({ method: "gguf", device, gpus: device === "gpu" ? 1 : 0, per_gpu: device === "gpu" ? 3 * GIB : 0,
  ram: device === "cpu" ? 2.5 * GIB : 0, amount: 1, parts: [], notes: [], kv_tokens: 4096 });

const MODEL_PAGE: ModelPage = {
  model: MODEL, recipes: [LLAMA, CUSTOM], can_manage: true, can_deploy: true,
  files: { recorded: true, downloaded_count: 1, downloaded_bytes: 1282439264, files: [
    { path: "Qwen3-1.7B-Q4_K_M.gguf", size: 1282439264, role: "model file", downloaded: true },
    { path: "Qwen3-1.7B-Q8_0.gguf", size: 2165000000, role: "other quantisation", downloaded: false }] },
  machines: [
    { machine_id: 1, name: "gpu-01", status: "online", connected: true, download: null,
      cache: { reported: true, cached: true, bytes: 1282439264, files: 1, used_by: [{ kind: "deployment", id: 3, name: "chat-small" }] },
      engines: { "llama-cpp": { variant: "cuda", runtime: "container", kind: "image", engine_name: LLAMA.variants[0].image, present: true, listed_at: null, note: null },
        "llama-cpp-mine": { variant: null, runtime: null, kind: null, engine_name: null, present: null, listed_at: null, note: null } } },
    { machine_id: 2, name: "cpu-01", status: "offline", connected: false, download: null,
      cache: { reported: false, cached: false, bytes: null, files: null, used_by: [] },
      engines: { "llama-cpp": { variant: "cpu-process", runtime: "process", kind: "program", engine_name: "llama.cpp-1.0.0", present: false, listed_at: null, note: null },
        "llama-cpp-mine": { variant: null, runtime: null, kind: null, engine_name: null, present: null, listed_at: null, note: null } } },
  ],
  fits: {
    "llama-cpp": {
      gpu: { estimate: EST("gpu"), fits: true, machines: [
        { machine_id: 1, name: "gpu-01", status: "online", fits: true, reason: "", short_by: null, cards: [], leftover: 40 * GIB, gpus: [4] },
        { machine_id: 2, name: "cpu-01", status: "offline", fits: false, reason: "no GPU it may use", short_by: null, cards: [] }] },
    },
  },
  deployments: [],
} as unknown as ModelPage;

const ENGINE_PAGE: EnginePage = {
  recipe: { ...LLAMA, yaml: "name: llama-cpp\n" }, can_manage: true,
  machines: [
    { machine_id: 1, name: "gpu-01", status: "online", connected: true, can_list: true, variant: "cuda", runtime: "container", kind: "image",
      engine_name: LLAMA.variants[0].image, present: true, listed_at: new Date().toISOString(), note: null },
    { machine_id: 2, name: "cpu-01", status: "offline", connected: false, can_list: false, variant: "cpu-process", runtime: "process", kind: "program",
      engine_name: "llama.cpp-1.0.0", present: false, listed_at: null, note: null },
  ],
  deployments: [], models: [{ id: 7, name: MODEL.name, title: null }],
} as unknown as EnginePage;

const CATALOG: CatalogResponse = { models: [MODEL], recipes: [LLAMA, CUSTOM], can_manage: true, can_deploy: true,
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: true, env_seeds: ["MODEL_URL_HOSTS"],
    hf_token: { set: true, last4: "abcd", updated_by: "owner", updated_at: "2026-11-20T00:00:00Z" } } } as CatalogResponse;

const RECOMMENDED = {
  groups: [
    // As the server sends them: one group's English unchanged (translated), one changed since (shown as sent).
    { id: "qwen3", title: "Qwen3", description: lookup("en", "addmodel.group.qwen3.description") },
    { id: "llm", title: "Other open chat models", description: "A newer description the console has no translation for." },
  ],
  models: [
    { name: "qwen3-reranker-0.6b", title: "Qwen3 Reranker 0.6B", source: "hf://Qwen/Qwen3-Reranker-0.6B", revision: null, file: null, group: "qwen3",
      task: "rerank", format: "safetensors", recipe: "vllm-pooling", device: "gpu", download_gib: 1.1, memory_gib: 3.4, gated: false,
      engine_args: {}, note: "Re-orders results.", in_catalog: null, license: "apache-2.0", inputs: [], mmproj: null },
    { name: "gemma-3-4b-it", title: "Gemma 3 4B", source: "hf://google/gemma-3-4b-it", revision: null, file: null, group: "llm", task: "chat",
      format: "safetensors", recipe: "vllm", device: "gpu", download_gib: 8, memory_gib: 14.7, gated: true, engine_args: {}, note: "Google.",
      in_catalog: "gemma", license: "gemma", inputs: ["image", "video"], mmproj: null, languages: ["zh", "en", "ja"] },
  ],
};

const SUGGESTION: Suggestion = {
  recipe: "vllm-pooling", recipes: ["vllm-pooling", "vllm"], device: "gpu", devices: ["gpu", "cpu"], params: { gpus: 1 }, fits: true,
  estimate: { method: "safetensors-kv", device: "gpu", gpus: 1, per_gpu: 3.38 * GIB, ram: 0, amount: 3.38 * GIB, kv_tokens: 8192, notes: [],
    parts: [{ label: "Weights", bytes: 1.11 * GIB, detail: "" }] },
  machines: [{ name: "gpu-01", status: "online", fits: true, reason: "", gpus: [4, 5], short_by: null },
    { name: "cpu-01", status: "online", fits: true, reason: "", gpus: [], short_by: null }],
};
const INSPECTED = { recipes: ["vllm-pooling"], suggestion: SUGGESTION, meta: { format: "safetensors", revision: "e61197ed45024b0ed8a2d74b80b4d909f1255473", ref: "main",
  task_hint: "rerank", size_bytes: 1.12 * GIB, file_count: 9, license: "apache-2.0", modalities: [], config: { layers: 28, kv_heads: 8, max_context: 40960, dtype: "bfloat16" },
  weights: { params: { BF16: 595776512 } } } };

function server(route: (url: string, init?: RequestInit) => Reply | undefined = () => undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? (url === "/api/models" ? { status: 200, body: CATALOG }
        : url === "/api/machines" ? { status: 200, body: fixture }
          : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: true } }
            : url === "/api/downloads" ? { status: 200, body: { downloads: [], can_download: true } }
              : url === "/api/models/recommended" ? { status: 200, body: RECOMMENDED }
                : url === `/api/models/${MODEL.name}` ? { status: 200, body: MODEL_PAGE }
                  : url.startsWith("/api/recipes/llama-cpp") ? { status: 200, body: ENGINE_PAGE }
                    : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

// Headings and table headers in Chinese: none of them may be left in English words (names and codes aside).
function englishIn(els: Element[], allowed: RegExp): string[] {
  return els.map((e) => (e.textContent ?? "").replace(allowed, "").trim()).filter((s) => /[A-Za-z]{3,}/.test(s));
}
const page = () => document.querySelector("main") ?? document.body;
const headings = () => screen.getAllByRole("heading").map((h) => h.textContent);

beforeEach(() => setLang("zh-TW"));
afterEach(() => {
  cleanup();
  setLang("en");
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the model library in Traditional Chinese", () => {
  it("FY-WEB-740 the Catalog: title, tabs, table, chips and actions in Chinese; names and codes as they are", async () => {
    server();
    mount("/models");
    expect(await screen.findByRole("heading", { level: 1, name: "模型" })).toBeTruthy();
    const table = await screen.findByRole("table");
    const tabs = screen.getByRole("tablist", { name: "模型" });
    expect(within(tabs).getAllByRole("tab").map((x) => x.textContent)).toEqual(["目錄（1）", "推論引擎", "來源"]);
    expect(within(table).getAllByRole("columnheader").map((x) => x.textContent).filter(Boolean))
      .toEqual(["模型", "用途", "格式", "大小", "架構", "授權", "推論引擎"]);
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getByText("對話")).toBeTruthy();       // the task by its code, not the server's "Chat"
    expect(within(row).getByText("圖片")).toBeTruthy();        // what it takes besides text
    expect(within(row).getByText("受限")).toBeTruthy();
    expect(row.textContent).toContain("qwen3 · 20.3 億參數");
    expect(row.textContent).toContain("28 層");
    expect(within(row).getByRole("button", { name: "下載到…" })).toBeTruthy();
    expect(within(row).getByRole("link", { name: "部署" })).toBeTruthy();
    expect(screen.getByText("1 個模型")).toBeTruthy();
    expect(screen.getByRole("button", { name: /加入模型/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: "怎麼部署第一個模型？" })).toBeTruthy();
    expect(englishIn([...page().querySelectorAll("h1, h2, h3, th, button, [role=tab]")], /qwen3[-.\w]*|GGUF|Hugging Face/gi)).toEqual([]);
    expect(document.title).toBe("模型 · Forgeyard");
  });

  it("FY-WEB-741 Engines: a built-in engine's description is translated while the server sends the English it was made from", async () => {
    server();
    mount("/models?tab=engines");
    const card = await screen.findByRole("article", { name: "推論引擎 llama-cpp" });
    expect(within(card).getByText(lookup("zh-TW", "library.engineDesc.llama-cpp"))).toBeTruthy();
    expect(within(card).getByText("提供對話、文字轉語音")).toBeTruthy();
    expect(within(card).getByText("詳細資訊")).toBeTruthy();
    expect(within(card).getByText("運算能力 7.0")).toBeTruthy();
    expect(within(card).getByText("無法連到映像檔 registry 的機器：載入映像檔")).toBeTruthy();
    // A custom engine's description is its author's, shown as written.
    const mine = screen.getByRole("article", { name: "推論引擎 llama-cpp-mine" });
    expect(within(mine).getByText("My own build of llama.cpp.")).toBeTruthy();
    expect(within(mine).getByText("自訂")).toBeTruthy();
    expect(within(mine).getByRole("button", { name: "編輯" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /加入自訂推論引擎/ })).toBeTruthy();
    // The rule itself: English as sent; a text the server changed is shown as sent, not covered by the old translation.
    expect(engineDescription({ name: "llama-cpp", builtin: true, description: "A newer description." })).toBe("A newer description.");
    expect(engineDescription({ name: "llama-cpp", builtin: false, description: LLAMA_DESC })).toBe(LLAMA_DESC);
    expect(sentText("library.engineDesc.nope", "As sent.")).toBe("As sent.");
    setLang("en");
    expect(engineDescription(LLAMA)).toBe(LLAMA_DESC);
  });

  it("FY-WEB-742 Sources and the server's Hugging Face token in Chinese, with the server's values as they are", async () => {
    server();
    mount("/models?tab=sources");
    expect(await screen.findByRole("heading", { level: 2, name: "來源" })).toBeTruthy();
    const where = screen.getByText(/的模型從/);
    expect(where.textContent).toBe("Hugging Face 的模型從 https://huggingface.co 取得。其他來源只能是這裡列出的。私有位址和中繼資料位址一律拒絕（例外：這台伺服器允許私有主機，供區域網路鏡像使用）。");
    expect(screen.getByText("來自 .env")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("這些清單來自 MODEL_URL_HOSTS：還沒有人在這裡儲存過。");
    expect(screen.getByLabelText("GGUF 網址允許的主機（每行一個，*.example.org 代表其子網域）")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 3, name: "Hugging Face 權杖" })).toBeTruthy();
    expect(screen.getByText("已設定")).toBeTruthy();
    expect(document.querySelector(".hf-token-state")!.textContent).toMatch(/^已設定 結尾是 …abcd · 由 owner 設定 · /);
    expect(screen.getByLabelText("更換權杖")).toBeTruthy();
    expect(screen.getByRole("button", { name: "移除權杖" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "儲存來源" })).toBeTruthy();
  });

  it("FY-WEB-743 Add a model: modes, the recommended list's groups by id (a changed group as sent), sizes, licences and languages", async () => {
    server((url) => (url === "/api/models" ? { status: 200, body: { ...CATALOG, models: [] } } : undefined));
    mount("/models");
    expect(await screen.findByRole("heading", { level: 2, name: "加入模型" })).toBeTruthy();
    const modes = screen.getByRole("group", { name: "怎麼找模型" });
    expect(within(modes).getAllByRole("button").map((b) => b.textContent)).toEqual(["推薦", "搜尋 Hugging Face", "輸入來源"]);
    expect(await screen.findByRole("heading", { level: 3, name: "Qwen3 系列" })).toBeTruthy();
    expect(screen.getByText(lookup("zh-TW", "addmodel.group.qwen3.description"))).toBeTruthy();
    // Its title is the English the translation was made from; its description has changed since, so it is shown as sent.
    expect(screen.getByRole("heading", { level: 3, name: "其他開放對話模型" })).toBeTruthy();
    expect(screen.getByText("A newer description the console has no translation for.")).toBeTruthy();
    const rr = screen.getByText("Qwen3 Reranker 0.6B").closest("li")!;
    expect(within(rr).getByText("重排序")).toBeTruthy();
    expect(within(rr).getByText("大約要下載 1.1 GiB；一個副本大約需要 3.4 GiB 的 GPU 記憶體。")).toBeTruthy();
    expect(within(rr).getByText("Re-orders results.")).toBeTruthy(); // a model's note is the server's, as sent
    expect(within(rr).getByRole("button", { name: "選擇" })).toBeTruthy();
    const gemma = screen.getByText("Gemma 3 4B").closest("li")!;
    expect(within(gemma).getByText("圖片、影片")).toBeTruthy();
    expect(within(gemma).getByText("中文、英文 +1")).toBeTruthy();
    expect(within(gemma).getByText(/^Gemma 條款：允許商業使用，但須遵守 Google 的禁止用途政策/)).toBeTruthy();
    expect(within(gemma).getByRole("link", { name: "授權條款" })).toBeTruthy();
    expect(within(gemma).getByText("需要 Hugging Face 權杖，並在 Hugging Face 上接受授權。")).toBeTruthy();
    expect(within(gemma).getByText("已在目錄中，名稱 gemma")).toBeTruthy();
    expect(screen.getByText("目錄列出可以部署的模型。目前是空的：從上面的推薦模型挑一個，或搜尋 Hugging Face。")).toBeTruthy();
  });

  it("FY-WEB-744 the review in Chinese: what it reads, the facts, what it needs and where it fits", async () => {
    server((url) => (url === "/api/models" ? { status: 200, body: { ...CATALOG, models: [] } }
      : url === "/api/models/inspect" ? { status: 200, body: INSPECTED } : undefined));
    mount("/models");
    const rr = (await screen.findByText("Qwen3 Reranker 0.6B")).closest("li")!;
    await act(async () => fireEvent.click(within(rr).getByRole("button", { name: "選擇" })));
    const form = await screen.findByRole("form", { name: "加入 Qwen3 Reranker 0.6B" });
    await within(form).findByText("結構");
    expect(within(form).getByText("結構").nextElementSibling!.textContent).toBe("28 層、8 個 KV 頭、最多 40,960 個 token、bfloat16");
    expect(within(form).getByText("參數量").nextElementSibling!.textContent).toBe("5.96 億");
    expect(within(form).getByText("下載").nextElementSibling!.textContent).toBe("1.1 GiB，共 9 個檔案");
    expect(within(form).getByText("輸入").nextElementSibling!.textContent).toBe("僅文字");
    expect(document.querySelector(".suggest-line")!.textContent).toBe("用 vllm-pooling 在 GPU 上執行；一個副本大約保留 3.38 GiB 的 GPU 記憶體。");
    expect(within(form).getByText("現在放得進：gpu-01 的 GPU #4、#5、cpu-01 的 CPU。")).toBeTruthy();
    expect(within(form).getByText("以上是推論引擎的預設值（也可以用：vllm；上下文長度和並行數可在部署時變更）。")).toBeTruthy();
    expect(within(form).getByRole("group", { name: "估算對象" })).toBeTruthy();
    expect(within(form).getByLabelText("目錄中的名稱")).toBeTruthy();
    expect(within(form).getByRole("button", { name: "加入目錄" })).toBeTruthy();
    expect(within(form).getByRole("button", { name: "改選其他" })).toBeTruthy();
    expect(englishIn([...form.querySelectorAll("dt, label, button")], /GPU|CPU/g)).toEqual([]);
  });

  it("FY-WEB-745 Hugging Face search in Chinese: columns, chips, counts in 萬 and the vision licence rule", async () => {
    const results = { cached: false, endpoint: "https://huggingface.co", results: [
      { id: "Qwen/Qwen3-4B", source: "hf://Qwen/Qwen3-4B", downloads: 1234567, likes: 15300, last_modified: null, gated: true, gated_mode: null, private: false,
        pipeline_tag: "text-generation", library: "transformers", task: "chat", format: "safetensors", params: 4022468096, weights_bytes: null, in_catalog: null, recipes: ["vllm"] },
      { id: "some/detector", source: "hf://some/detector", downloads: 900, likes: 3, last_modified: null, gated: false, gated_mode: null, private: false,
        pipeline_tag: "object-detection", library: "transformers", task: "detect", format: "other", params: null, weights_bytes: null, in_catalog: null, recipes: [],
        license: "cc-by-nc-4.0" },
    ] };
    server((url) => (url === "/api/models" ? { status: 200, body: { ...CATALOG, models: [] } }
      : url.startsWith("/api/models/search") ? { status: 200, body: results } : undefined));
    mount("/models");
    fireEvent.click(await screen.findByRole("button", { name: "搜尋 Hugging Face" }));
    expect(screen.getByRole("search", { name: "在 Hugging Face 上搜尋" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("搜尋 Hugging Face"), { target: { value: "qwen3" } });
    const table = await screen.findByRole("table", {}, { timeout: 3000 });
    expect(within(table).getAllByRole("columnheader").map((x) => x.textContent)).toEqual(["模型", "內容", "大小", "下載次數", "更新時間", "操作"]);
    const [, qwen, det] = within(table).getAllByRole("row");
    expect(within(qwen).getByText("需要權杖")).toBeTruthy();
    expect(qwen.textContent).toContain("40.2 億參數");
    expect(qwen.textContent).toContain("123.5 萬 · 1.5 萬個讚");
    expect(within(det).getByText("授權 cc-by-nc-4.0：視覺模型需要寬鬆授權")).toBeTruthy();
    expect(det.textContent).toContain("物件偵測 · 其他");
    expect(screen.getByRole("option", { name: "任何用途" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "GGUF（CPU 或 GPU）" })).toBeTruthy();
  });

  it("FY-WEB-746 a model's page in Chinese: header, facts, jump links, sections, machines and files", async () => {
    server();
    mount(`/models/${MODEL.name}`);
    expect(await screen.findByRole("heading", { level: 1, name: MODEL.name })).toBeTruthy();
    expect(screen.getByText("模型 · 對話")).toBeTruthy();
    expect(screen.getByRole("link", { name: /目錄/ }).getAttribute("href")).toBe("/models");
    const facts = document.querySelector(".mk-facts")!;
    expect(facts.textContent).toContain("20.3 億參數");
    expect(facts.textContent).toContain("用 llama-cpp 的預設值需要每張 GPU 3.00 GiB");
    expect(facts.textContent).toContain("尚未部署");
    const jump = screen.getByRole("navigation", { name: "本頁內容" });
    expect(within(jump).getAllByRole("link").map((a) => a.textContent)).toEqual(["概覽", "機器", "部署", "推論引擎", "檔案"]);
    expect(headings()).toEqual([MODEL.name, "概覽", "機器 · 1 台的快取中有它", "部署 · 0", "可以執行它的推論引擎 · 2", "檔案"]);
    expect(screen.getByText("加入時間").nextElementSibling!.textContent).toBe("由 owner 加入 · 2026年11月10日");
    expect(screen.getByText("結構").nextElementSibling!.textContent).toBe("28 層 · 上下文 40,960 個 token");
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("columnheader").map((x) => x.textContent)).toEqual(["機器", "快取", "放得下？", "那裡的推論引擎", "操作"]);
    const [, gpu, cpu] = within(table).getAllByRole("row");
    expect(within(gpu).getByText("放得下")).toBeTruthy();
    expect(gpu.textContent).toContain("GPU #4 上剩 40.00 GiB");
    expect(within(gpu).getByText("映像檔在這裡")).toBeTruthy();
    expect(within(gpu).getByText("chat-small 正在使用")).toBeTruthy();
    expect(within(gpu).getByText("線上")).toBeTruthy();
    expect(within(cpu).getByText("沒有回報")).toBeTruthy();
    expect(within(cpu).getByText("離線")).toBeTruthy();
    expect(document.querySelector(".lp-fold-line")!.textContent).toContain("已下載 1 個檔案");
    fireEvent.click(screen.getByRole("button", { name: "顯示檔案" }));
    expect(screen.getByRole("button", { name: "隱藏檔案" })).toBeTruthy();
    expect(screen.getByText("other quantisation，沒有下載")).toBeTruthy();
    expect(englishIn([...page().querySelectorAll("h1, h2, th, dt, nav a, button")], /qwen3[-.\w]*|llama-cpp[-\w]*|GPU|Hugging Face/gi)).toEqual([]);
  });

  it("FY-WEB-747 an engine's page in Chinese: header, facts, variants, machines and parameters", async () => {
    server();
    mount("/models/engines/llama-cpp");
    expect(await screen.findByRole("heading", { level: 1, name: "llama-cpp" })).toBeTruthy();
    expect(screen.getByText("推論引擎 · 內建")).toBeTruthy();
    expect(screen.getByRole("button", { name: /複製成自訂推論引擎/ })).toBeTruthy();
    expect(document.querySelector(".mk-facts")!.textContent).toContain("2 個變體");
    expect(screen.getByText(lookup("zh-TW", "library.engineDesc.llama-cpp"))).toBeTruthy();
    expect(screen.getByText("GPU 用法").nextElementSibling!.textContent).toBe("獨占一張卡，除非部署設定為共用 · 也能在 CPU 上執行");
    const [variants, machines] = screen.getAllByRole("table");
    expect(within(variants).getAllByRole("columnheader").map((x) => x.textContent)).toEqual(["變體", "執行", "映像檔或程式", "釘選", "需求"]);
    expect(within(variants).getByText("以 digest 釘選")).toBeTruthy();
    expect(within(variants).getByText("可離線使用")).toBeTruthy();
    expect(within(variants).getByText("發行封存檔（SHA-256）")).toBeTruthy();
    expect(within(variants).getByText("容器")).toBeTruthy();
    expect(within(variants).getByText("運算能力 7.0")).toBeTruthy();
    const [, gpu, cpu] = within(machines).getAllByRole("row");
    expect(within(gpu).getByText("在這裡")).toBeTruthy();
    expect(gpu.textContent).toMatch(/在它的容器引擎中 · .*列出/);
    expect(gpu.textContent).toContain("· 容器");
    expect(within(cpu).getByText("不在這裡")).toBeTruthy();
    expect(within(cpu).getByText("部署在那裡啟動時下載")).toBeTruthy();
    expect(screen.getByText("2 個參數")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "顯示參數" }));
    const params = screen.getAllByRole("table")[2];
    expect(within(params).getAllByRole("columnheader").map((x) => x.textContent)).toEqual(["參數", "預設值", "範圍", "作用"]);
    expect(params.textContent).toContain("kv_cache · 進階");
    expect(headings()).toEqual(["llama-cpp", "概覽", "試跑紀錄", "變體 · 2", "新版本", "機器 · 1 台已有", "參數", "部署 · 0", "它能執行的目錄模型 · 1"]);
    expect(englishIn([...page().querySelectorAll("h1, h2, th, dt, nav a")], /llama-cpp|GPU/g)).toEqual([]);
  });

  it("FY-WEB-872 the image an engine runs beside its name: on each card (the first by its tag, +N, all in the tooltip) and the engine's page", async () => {
    server();
    mount("/models?tab=engines");
    const card = await screen.findByRole("article", { name: "推論引擎 llama-cpp" });
    const img = within(card).getByTestId("engine-image");
    expect(img.textContent).toBe("ghcr.io/ggml-org/llama.cpp:server-cuda+1");
    expect(img.getAttribute("title")).toBe(`NVIDIA: ${LLAMA.variants[0].image}\nCPU: ${LLAMA.variants[1].engine}`);
    // Before Details: the card shows it closed.
    expect(img.closest("details")).toBeNull();
    const mine = within(screen.getByRole("article", { name: "推論引擎 llama-cpp-mine" })).getByTestId("engine-image");
    expect(mine.textContent).toBe("forgeyard-local/llama.cpp:server-vulkan-b11977");
    cleanup();
    mount("/models/engines/llama-cpp");
    await screen.findByRole("heading", { level: 1, name: "llama-cpp" });
    const head = document.querySelector(".lp-header")!;
    expect(within(head as HTMLElement).getByTestId("engine-image").textContent).toBe("ghcr.io/ggml-org/llama.cpp:server-cuda+1");
  });

  it("FY-WEB-748 the library's words in lib code follow the language; English stays as it was", () => {
    expect(mediaText(["image", "audio", "video"])).toBe("圖片、音訊、影片");
    expect(mediaText([])).toBe("僅文字");
    expect(params(4022468096)).toBe("40.2 億");
    expect(params(595776512)).toBe("5.96 億");
    expect(billions(2031739904)).toBe("20.3 億");
    expect(compact(1234567)).toBe("123.5 萬");
    expect(compact(900)).toBe("900");
    expect(languagesText(["zh", "en", "ja", "ko"])).toBe("中文、英文 +2");
    expect(licenceTerms("OpenRAIL")).toMatch(/^OpenRAIL：允許商業使用/);
    expect(licenceTerms("mit")).toBeNull();
    expect(visionProblem({ license: "apache-2.0" }, "segment")).toBe("這個儲存庫不是視覺伺服器能執行的影像分割模型。");
    expect(visionProblem({ vision: { task: "detect", architecture: "x" } as CatalogModel["vision"], license: null }, "detect")).toBe(
      "視覺模型只能以寬鬆授權加入（Apache-2.0、MIT、BSD）；這個模型的授權是 未註明。");
    expect(searchLicenceProblem({ task: "depth", license: "cc-by-nc-4.0" } as SearchResult)).toBe("授權 cc-by-nc-4.0：視覺模型需要寬鬆授權");
    expect(fitsLine(SUGGESTION)).toBe("gpu-01 的 GPU #4、#5、cpu-01 的 CPU");
    expect(presenceText("program", false)).toBe("程式不在這裡：部署時下載");
    expect(whereText("image", null)).toBe("還沒有列出");
    expect(taskLabel({ task: "rerank", task_label: "Rerank" })).toBe("重排序");
    setLang("en");
    expect(mediaText(["image", "audio", "video"])).toBe("Images, audio, video");
    expect(params(4022468096)).toBe("4.02 B");
    expect(params(595776512)).toBe("596 M");
    expect(billions(595776512)).toBe("0.60 B");
    expect(compact(1234567)).toBe("1.2 M");
    expect(languagesText(["zh", "en", "ja", "ko"])).toBe("Chinese, English +2");
    expect(fitsLine(SUGGESTION)).toBe("gpu-01 (GPU #4, #5), cpu-01 (CPU)");
    expect(presenceText("program", false)).toBe("program not here: downloaded on deploy");
    // English shows the server's own task label, even one the console would word differently.
    expect(taskLabel({ task: "rerank", task_label: "Reranking" })).toBe("Reranking");
  });

  it("FY-WEB-749 switching the language re-renders the library at once, and English is unchanged", async () => {
    setLang("en");
    server();
    mount("/models");
    expect(await screen.findByRole("heading", { level: 1, name: "Models" })).toBeTruthy();
    await screen.findByRole("table");
    expect(screen.getAllByRole("tab").map((x) => x.textContent)).toEqual(["Catalog (1)", "Engines", "Sources"]);
    const row = (await screen.findAllByRole("row"))[1];
    expect(row.textContent).toContain("qwen3 · 2.03 B params");
    expect(within(row).getByText("Chat")).toBeTruthy();
    act(() => setLang("zh-TW"));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1, name: "模型" })).toBeTruthy());
    expect(screen.getAllByRole("tab").map((x) => x.textContent)).toEqual(["目錄（1）", "推論引擎", "來源"]);
    expect(within(screen.getAllByRole("row")[1]).getByText("對話")).toBeTruthy();
  });

  it("FY-WEB-750 the console's copies of the server's English match the recipe files and the recommended list, so none is left untranslated", async () => {
    // Read from the repository (node's fs, loaded by a name the bundler leaves alone: the web build has no node types).
    const fs = (await import(/* @vite-ignore */ ["node", "fs"].join(":"))) as {
      readdirSync: (p: string) => string[]; readFileSync: (p: string, enc: "utf8") => string };
    // vitest runs in web/ (npm test, the CI job): the server's files are one level up.
    const root = `${(globalThis as unknown as { process: { cwd(): string } }).process.cwd()}/../forgeyard/`;
    // The built-in engines' descriptions: `description: >-` and its indented lines, folded with spaces as YAML does.
    const dir = `${root}recipes/`;
    const yamls = fs.readdirSync(dir).filter((f) => f.endsWith(".yaml")).map((f) => fs.readFileSync(dir + f, "utf8"));
    const desc = (yaml: string) => /^description: >-\n((?: {2}.*\n)+)/m.exec(yaml)?.[1].split("\n").map((l) => l.trim()).filter(Boolean).join(" ");
    const byName = Object.fromEntries(yamls.map((y) => [/^name: (\S+)/m.exec(y)?.[1], desc(y)]));
    const engineKeys = Object.keys(en).filter((k) => k.startsWith("library.engineDesc."));
    expect(engineKeys.map((k) => k.slice("library.engineDesc.".length)).sort()).toEqual(Object.keys(byName).sort());
    for (const k of engineKeys) expect(lookup("en", k as "library.engineDesc.vllm"), k).toBe(byName[k.slice("library.engineDesc.".length)]);
    // The recommended list's groups: id, title and description, one line each.
    const yaml = fs.readFileSync(`${root}models/recommended.yaml`, "utf8");
    const groups = [...yaml.matchAll(/^ {2}- id: (\S+)\n {4}title: (.*)\n {4}description: (.*)$/gm)].map((m) => ({ id: m[1], title: m[2], description: m[3] }));
    expect(groups.length).toBeGreaterThan(5);
    expect(Object.keys(en).filter((k) => /^addmodel\.group\..*\.title$/.test(k)).length).toBe(groups.length);
    for (const g of groups) {
      expect(lookup("en", `addmodel.group.${g.id}.title` as "addmodel.title"), g.id).toBe(g.title);
      expect(lookup("en", `addmodel.group.${g.id}.description` as "addmodel.title"), g.id).toBe(g.description);
    }
  });

  it("FY-WEB-751 an engine with one variant says so in each language's plural", async () => {
    const one = { ...ENGINE_PAGE, recipe: { ...ENGINE_PAGE.recipe, variants: [LLAMA.variants[0]] } };
    server((url) => (url.startsWith("/api/recipes/llama-cpp") ? { status: 200, body: one } : undefined));
    setLang("en");
    mount("/models/engines/llama-cpp");
    await screen.findByRole("heading", { level: 1, name: "llama-cpp" });
    expect(document.querySelector(".mk-facts")!.textContent).toContain("1 variant");
    expect(document.querySelector(".mk-facts")!.textContent).not.toContain("1 variants");
    act(() => setLang("zh-TW"));
    await waitFor(() => expect(document.querySelector(".mk-facts")!.textContent).toContain("1 個變體"));
  });
});
