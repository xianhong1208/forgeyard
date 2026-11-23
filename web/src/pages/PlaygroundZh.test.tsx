// @vitest-environment jsdom
// The Playground in Traditional Chinese (1.5.1, #189): the page's own words translated, the model's answers, the
// prompts and the server's specifics as they are (FY-WEB-790 to 797).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { setLang } from "../i18n/lang";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
const UP = [{ replica: 0, machine: "gpu-01", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }];
const route = (id: number, served_name: string, api: string, over: Record<string, unknown> = {}) => ({
  id, served_name, api, deployment_id: id, kind: "local", usable: true, endpoints: UP, local_only: false, spill_on_long_prompt: false, wait_ms: 0,
  has_local: true, has_cloud: false, targets: [], upstream_model: null, ...over,
});
const dep = (id: number, task: string, over: Record<string, unknown> = {}) => ({ id, name: `dep-${id}`, model: { task, modalities: null, classify: null }, ...over });

type Reply = { status: number; body?: unknown; raw?: string; headers?: Record<string, string> };

function server(routes: unknown[], deployments: unknown[], extra: (url: string, init?: RequestInit) => Reply | undefined = () => undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r: Reply = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url === "/api/routes" ? { status: 200, body: { cloud: "off", is_admin: user.role === "admin", routes } }
        : url === "/api/deployments" ? { status: 200, body: { deployments, server_time: "", can_manage: true } }
          : extra(url, init) ?? { status: 404, body: {} };
    return new Response(r.raw ?? JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path = "/playground") {
  setLang("zh-TW");
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  setLang("en");
  localStorage.clear();
});

const LATIN_WORD = /\b[A-Za-z]{3,}\b/;
// Headings and buttons of the page itself carry no English word (model names, units and product names aside).
function noEnglish(scope: HTMLElement, allowed: RegExp = /$^/) {
  for (const el of within(scope).queryAllByRole("heading")) expect(el.textContent!.replace(allowed, ""), el.textContent!).not.toMatch(LATIN_WORD);
  for (const el of within(scope).queryAllByRole("button")) expect(el.textContent!.replace(allowed, ""), el.textContent!).not.toMatch(LATIN_WORD);
}

describe("the Playground in Traditional Chinese", () => {
  it("FY-WEB-790 nothing to try: the empty page in Chinese, with the deployments not ready counted and Serving linked", async () => {
    server([route(1, "starting", "openai-chat", { endpoints: [] }), route(2, "later", "openai-chat", { endpoints: [] })], [dep(1, "chat"), dep(2, "chat")]);
    mount();
    expect(await screen.findByRole("heading", { level: 1, name: "試用區" })).toBeTruthy();
    const empty = (await screen.findByRole("heading", { name: "還沒有可以試用的模型" })).closest("section") as HTMLElement;
    expect(empty.textContent).toContain("有 2 個部署尚未就緒：服務中會顯示每個部署是已停止、啟動中，還是在等待機器。");
    expect(empty.textContent).toContain("從模型 → 目錄部署一個");
    expect(within(empty).getByRole("link", { name: "服務中" }).getAttribute("href")).toBe("/serving");
    expect(within(empty).getByRole("link", { name: /從目錄部署/ }).getAttribute("href")).toBe("/models");
    noEnglish(empty);
    cleanup();
    server([], [], undefined, USER);
    mount();
    expect((await screen.findByRole("heading", { name: "還沒有可以試用的模型" })).closest("section")!.textContent)
      .toContain("還沒有部署任何模型。 管理員和操作員才能部署模型；請請他們部署你需要的模型。");
  });

  it("FY-WEB-791 chat: the panel in Chinese; the answer streams in as the model wrote it, the meta line in Chinese words", async () => {
    const answer = "data: " + JSON.stringify({ choices: [{ delta: { content: "Hello there" } }] }) + "\n\n"
      + "data: " + JSON.stringify({ choices: [], usage: { prompt_tokens: 3, completion_tokens: 2 } }) + "\n\ndata: [DONE]\n\n";
    server([route(1, "qwen3-4b", "openai-chat", { kind: "mixed" })], [dep(1, "chat")], (url) =>
      url === "/api/playground/chat/completions" ? { status: 200, raw: answer, headers: { "Content-Type": "text/event-stream", "X-Forgeyard-Replica": "chat#0" } } : undefined);
    mount();
    const panel = (await screen.findByRole("heading", { name: "對話" })).closest("section") as HTMLElement;
    expect(screen.getByText(/1 個就緒的副本/).textContent).toContain("本機忙碌或無法使用時，會溢出到雲端。");
    expect(screen.getByLabelText("模型").textContent).toContain("先本機，再雲端");
    expect(within(panel).getByText("問點什麼吧。模型一邊寫，回答就一邊串流出來。")).toBeTruthy();
    expect(within(panel).getByLabelText("最大 token 數")).toBeTruthy();
    expect(within(panel).getByLabelText("僅本機（絕不使用雲端）")).toBeTruthy();
    fireEvent.change(within(panel).getByPlaceholderText("輸入訊息…"), { target: { value: "hi" } });
    fireEvent.click(within(panel).getByRole("button", { name: "送出" }));
    const meta = await within(panel).findByText(/副本 0/);
    expect(meta.textContent).toMatch(/^副本 0 · 第一個 token \d+ ms · \d+ ms · 輸入 3 · 輸出 2/);
    expect(within(panel).getByText("Hello there")).toBeTruthy();         // the model's own words, untouched
    expect(within(panel).getByText("你")).toBeTruthy();
    noEnglish(panel);
  });

  it("FY-WEB-792 an asleep model: the wake notice, the picker's moon and the bar in Chinese", async () => {
    const asleep = dep(3, "chat", { name: "chat", workload: { desired_state: "asleep", ready: 0 },
      sleep: { on: true, idle_minutes: 30, asleep: true, asleep_since: null, wake_wait: 300, waking: null }, last_start: { seconds: 74 } });
    server([route(3, "qwen3-4b", "openai-chat", { endpoints: [] })], [asleep]);
    mount();
    const bar = (await screen.findByText(/正在休眠/, { selector: ".play-wake strong" })).closest(".play-wake") as HTMLElement;
    // (the start time's own words come from lib/sleep, translated with the deployment pages)
    expect(bar.textContent).toMatch(/^chat 正在休眠：已有 30 分鐘沒人使用，所以它不占用任何記憶體。 送出請求就會喚醒它：請求會等到它就緒（約 74 (s|秒)），最多 300 秒/);
    expect(within(bar).getByRole("button", { name: "立即喚醒" })).toBeTruthy();
    expect(screen.getByText("休眠中：送出請求就會喚醒它。")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("模型"));
    expect(within(screen.getByRole("listbox")).getByRole("option", { name: /qwen3-4b/ }).textContent).toContain("休眠中");
  });

  it("FY-WEB-793 a refused request: a known code in Chinese with the server's specifics, an unknown one as sent", async () => {
    let reply: Reply = { status: 503, body: { error: { code: "no_ready_replica", message: "chat has no ready replica" } } };
    server([route(1, "qwen3-4b", "openai-chat")], [dep(1, "chat")], (url) => (url === "/api/playground/chat/completions" ? reply : undefined));
    mount();
    const panel = (await screen.findByRole("heading", { name: "對話" })).closest("section") as HTMLElement;
    const ask = () => {
      fireEvent.change(within(panel).getByPlaceholderText("輸入訊息…"), { target: { value: "hi" } });
      fireEvent.click(within(panel).getByRole("button", { name: "送出" }));
    };
    ask();
    expect(await within(panel).findByText("503：這個模型沒有就緒的副本：chat has no ready replica")).toBeTruthy();
    reply = { status: 400, body: { error: { code: "brand_new", message: "only the server says this" } } };
    ask();
    expect(await within(panel).findByText("400：only the server says this")).toBeTruthy();
    reply = { status: 500, body: null };
    ask();
    expect(await within(panel).findByText("500：伺服器發生問題。")).toBeTruthy();
  });

  it("FY-WEB-794 embeddings, rerank and text classification panels in Chinese; the label names joined the Chinese way", async () => {
    const classify = { problem: "single_label_classification", labels: 3, label_names: ["positive", "negative", "neutral"] };
    server([route(1, "bge-m3", "openai-embeddings"), route(2, "reranker", "rerank"), route(3, "sentiment", "classify")],
      [dep(1, "embed"), dep(2, "rerank"), dep(3, "classify", { model: { task: "classify", modalities: null, classify } })], (url) =>
        url === "/api/playground/embeddings" ? { status: 200, body: { data: [{ embedding: [0.1, 0.2, 0.3] }], usage: { prompt_tokens: 9 } } } : undefined);
    mount("/playground?model=bge-m3");
    const emb = (await screen.findByRole("heading", { name: "嵌入" })).closest("section") as HTMLElement;
    fireEvent.click(within(emb).getByRole("button", { name: "產生嵌入" }));
    expect((await within(emb).findByLabelText("回答")).textContent).toMatch(/^3 維，耗時 \d+ ms · 9 個 token\n\[0\.1000, 0\.2000, 0\.3000, …\]$/);
    noEnglish(emb);
    cleanup();
    server([route(2, "reranker", "rerank")], [dep(2, "rerank")]);
    mount("/playground?model=reranker");
    const rr = (await screen.findByRole("heading", { name: "重排序" })).closest("section") as HTMLElement;
    expect(within(rr).getByLabelText("文件，每行一份")).toBeTruthy();
    expect(within(rr).getByRole("button", { name: "排序" })).toBeTruthy();
    cleanup();
    server([route(3, "sentiment", "classify")], [dep(3, "classify", { model: { task: "classify", modalities: null, classify } })]);
    mount("/playground?model=sentiment");
    const cls = (await screen.findByRole("heading", { name: "文字分類" })).closest("section") as HTMLElement;
    expect(cls.textContent).toContain("3 個標籤：positive、negative、neutral。 所有標籤的機率加起來等於 1。");
    expect(within(cls).getByLabelText("文字，每行一段")).toBeTruthy();
    noEnglish(cls);
  });

  it("FY-WEB-795 speak and image panels: counters, options and buttons in Chinese", async () => {
    server([route(1, "kokoro", "openai-speech"), route(2, "sd", "openai-images")], [dep(1, "tts"), dep(2, "image")], (url) =>
      url.startsWith("/api/playground/audio/voices") ? { status: 200, body: { voices: [], default: null, formats: ["wav", "mp3"], default_format: "wav", max_input_chars: 4000 } }
        : url.startsWith("/api/playground/images/options")
          ? { status: 200, body: { model: "sd", max_n: 2, max_side: 1024, default_size: "512x512", multiple: 64, formats: ["png"], max_steps: 50, max_prompt_chars: 1000 } }
          : undefined);
    mount("/playground?model=kokoro");
    const speak = (await screen.findByRole("heading", { name: "朗讀" })).closest("section") as HTMLElement;
    await waitFor(() => expect(within(speak).getByRole("option", { name: "模型本身的聲音" })).toBeTruthy());
    expect(within(speak).getByText(/個字元，最多 4,000 個$/)).toBeTruthy();
    expect(within(speak).getByLabelText("格式")).toBeTruthy();
    expect(within(speak).getByText("串流").closest("label")!.getAttribute("title")).toBe("這個模型或格式不支援串流");
    noEnglish(speak);
    cleanup();
    server([route(2, "sd", "openai-images")], [dep(2, "image")], (url) => url.startsWith("/api/playground/images/options")
      ? { status: 200, body: { model: "sd", max_n: 2, max_side: 1024, default_size: "512x512", multiple: 64, formats: ["png"], max_steps: 50, max_prompt_chars: 1000 } } : undefined);
    mount("/playground?model=sd");
    const img = (await screen.findByRole("heading", { name: "圖片" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(img).getByLabelText("尺寸") as HTMLSelectElement).value).toBe("512x512"));
    expect(within(img).getByRole("option", { name: "512x512（預設）" })).toBeTruthy();
    expect(within(img).getByLabelText("張數")).toBeTruthy();
    expect(within(img).getByLabelText("提示詞")).toBeTruthy();
    noEnglish(img);
  });

  it("FY-WEB-796 video: the lead, the lengths in seconds, the frames line and the job's status in Chinese", async () => {
    const OPTIONS = { model: "wan", default_size: "832x480", max_side: 832, max_pixels: 832 * 480, multiple: 16, seconds: 5, max_seconds: 5, fps: 16,
      max_steps: 50, max_prompt_chars: 400, keep_minutes: 1440 };
    const now = Math.floor(Date.now() / 1000);
    const job = { id: "video_0123456789abcdef", object: "video", model: "wan", status: "completed", progress: 100, created_at: now - 90, completed_at: now - 5,
      expires_at: now + 86400, size: "832x480", seconds: "5", error: null };
    server([route(7, "wan", "openai-videos")], [dep(7, "video")], (url) =>
      url === "/api/playground/videos?limit=20" ? { status: 200, body: { data: [job] } }
        : url.startsWith("/api/playground/videos/options") ? { status: 200, body: OPTIONS } : undefined);
    mount("/playground?model=wan");
    const vid = (await screen.findByRole("heading", { name: "影片" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(vid).getByLabelText("尺寸") as HTMLSelectElement).value).toBe("832x480"));
    expect(within(vid).getByText(/^影片是一個工作/)).toBeTruthy();
    expect([...(within(vid).getByLabelText("長度") as HTMLSelectElement).options].map((o) => o.textContent)).toEqual(["1 秒", "2 秒", "3 秒", "4 秒", "5 秒"]);
    expect(within(vid).getByText("81 幀，每秒 16 幀")).toBeTruthy();
    expect(within(vid).getByRole("heading", { name: "你用這個模型做的影片" })).toBeTruthy();
    const item = within(vid).getByText("已完成").closest("li") as HTMLElement;
    expect(within(item).getByText(/ · 832x480 · 5 秒$/).textContent).toMatch(/^\d+月\d+日 /);   // the date in Chinese
    expect(within(item).getByRole("button", { name: "顯示" })).toBeTruthy();
    expect(within(item).getByRole("button", { name: /^刪除 .+ 的影片$/ })).toBeTruthy();
    fireEvent.click(within(item).getByRole("button", { name: "顯示" }));
    expect(within(vid).getByText(/^832x480 · 5 秒 · 耗時 1 分 25 秒 · 保留到 /)).toBeTruthy();
    noEnglish(vid);
  });

  it("FY-WEB-797 vision: our title for the task, the drop zone, the call line and a refused file in Chinese; English keeps the server's title", async () => {
    const VISION = { name: "det", task: "detect", title: "Object detection (server)", endpoint: "/v1/vision/detect", deployment_id: 4, model: "PekingU/rtdetr_v2_r18vd",
      licence: "apache-2.0", ready: 1, running: true, reason: null };
    const routes = [route(4, "det", "vision-detect")];
    const extra = (url: string) => (url === "/api/playground/vision/options" ? { status: 200, body: { models: [VISION], tasks: [] } } : undefined);
    server(routes, [dep(4, "detect")], extra);
    mount("/playground?model=det");
    const vis = (await screen.findByRole("heading", { name: /物件偵測/ })).closest("section") as HTMLElement;
    expect(vis.textContent).toContain("找出物件，並為每一個畫出方框、標籤和分數。");
    expect(vis.textContent).toContain("程式以 API 金鑰呼叫 POST /v1/vision/detect（參數 model=det）。");
    expect(within(vis).getByRole("link", { name: "運作方式" })).toBeTruthy();
    expect(within(vis).getByText("PNG、JPEG 或 WebP，最多 20 MiB。")).toBeTruthy();
    const input = vis.querySelector("#vis-file") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["x"], "cat.gif", { type: "image/gif" })] } });
    expect(await within(vis).findByText("cat.gif 不是 PNG、JPEG 或 WebP 圖片。")).toBeTruthy();
    noEnglish(vis, /apache-2\.0|PekingU\/rtdetr_v2_r18vd/g);
    cleanup();
    localStorage.clear();
    server(routes, [dep(4, "detect")], extra);
    render(<MemoryRouter initialEntries={["/playground?model=det"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
    setLang("en");
    expect(await screen.findByRole("heading", { name: "Object detection (server)" })).toBeTruthy();
  });
});
