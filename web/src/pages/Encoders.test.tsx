// @vitest-environment jsdom
// 1.5.4 (#141, #144), the console of the transformers engine: the Playground's Embeddings panel with images and its
// similarity table, Classify with zero-shot labels, the model page's facts and language warning, Serving's image
// mark, the deploy form's hints and the add form's "two jobs" note. FY-WEB-944 to 953.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { setLang } from "../i18n/lang";
import { twoJobs } from "../lib/addmodel";
import {
  addLabels, cosine, embedSummary, englishOnly, hasCjk, imageUrl, itemLabel, languagesLine, norm, requestInput, shade, similarityGrid,
  templateProblem, urlName, type EmbedItem,
} from "../lib/encoders";
import type { CatalogModel, Deployment, ModelPage, RouteInfo } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const GIB = 1024 ** 3;
const UP = [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:47600/" }];
const route = (served_name: string, api: string, id: number) => ({ local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local",
  has_local: true, has_cloud: false, targets: [], usable: true, upstream_model: null, endpoints: UP, id, served_name, api, deployment_id: id }) as unknown as RouteInfo;
const dep = (id: number, task: string, model: Record<string, unknown>, params: Record<string, unknown> = {}) =>
  ({ id, name: `d${id}`, recipe: "transformers", params, model: { task, modalities: [], ...model } }) as unknown as Deployment;
const CLIP = { family: "clip", modalities: ["text", "image"], dimensions: 4, text_max_tokens: 77 };
const DINO = { family: "image-only", modalities: ["image"], dimensions: 3 };
const NLI = { architecture: "XLMRobertaForSequenceClassification", labels: 2, label_names: ["entailment", "not_entailment"], zero_shot: true };
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

type Sent = { url: string; body: Record<string, unknown> };
function server(routes: RouteInfo[], deployments: Deployment[], answer: (url: string, body: Record<string, unknown>) => { status: number; body: unknown } | undefined,
  extra: (url: string) => unknown = () => undefined) {
  const sent: Sent[] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith("/api/playground/")) {
      const body = JSON.parse(String(init?.body));
      sent.push({ url, body });
      const a = answer(url, body) ?? { status: 404, body: {} };
      return new Response(JSON.stringify(a.body), { status: a.status });
    }
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user: OWNER }
      : url === "/api/routes" ? { cloud: "off", is_admin: true, routes }
        : url === "/api/deployments" ? { deployments, server_time: "", can_manage: true } : extra(url);
    return new Response(JSON.stringify(body ?? {}), { status: body === undefined ? 404 : 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return { fetch, sent };
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const unit = (v: number[]) => { const n = Math.hypot(...v); return v.map((x) => x / n); };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setLang("en");
});

describe("the transformers engine in the console (1.5.4)", () => {
  it("FY-WEB-944 the similarity table: texts down the side and images across when there are both, else every item against every other", () => {
    const items: EmbedItem[] = [{ kind: "text", text: "two cats on a sofa" }, { kind: "text", text: "a street" },
      { kind: "image", name: "cats.jpg", uri: `data:image/png;base64,${PNG}` }, { kind: "image", name: "street.jpg", uri: "https://example.com/street.jpg", url: true }];
    const v = [unit([1, 0.1, 0]), unit([0, 1, 0.1]), unit([0.9, 0.2, 0.1]), unit([0.1, 0.9, 0.3])];
    const g = similarityGrid(items, v)!;
    expect(g.rows).toEqual([0, 1]);
    expect(g.cols).toEqual([2, 3]);
    expect(g.best).toEqual([0, 1]);
    expect(g.cells[0][0]).toBeCloseTo(cosine(v[0], v[2]), 9);
    expect(g.max).toBeGreaterThan(g.min);
    // Images only (DINOv2): every image against every other, itself left out.
    const imgs = items.slice(2);
    const gi = similarityGrid(imgs, v.slice(2))!;
    expect(gi.rows).toEqual([0, 1]);
    expect(gi.cols).toEqual([0, 1]);
    expect(gi.cells[0][0]).toBeNull();
    expect(gi.best).toEqual([1, 0]);
    // Texts only (a text embedding model), and one item: no table.
    expect(similarityGrid(items.slice(0, 2), v.slice(0, 2))!.cols).toEqual([0, 1]);
    expect(similarityGrid(items.slice(0, 1), v.slice(0, 1))).toBeNull();
    expect(similarityGrid(items, v.slice(0, 3))).toBeNull();
    // Vectors and shading.
    expect(norm(v[0])).toBeCloseTo(1, 9);
    expect(cosine([1, 0], [0, 0])).toBe(0);
    expect(shade(g.max, g)).toBe(1);
    expect(shade(g.min, g)).toBe(0);
    expect(shade(null, g)).toBe(0);
    expect(shade(0.3, { min: 0.3, max: 0.3 })).toBe(1);
    // The request: texts stay plain strings (OpenAI's shape), images are typed items; labels for the table.
    expect(requestInput(items)).toEqual(["two cats on a sofa", "a street", { image: `data:image/png;base64,${PNG}` }, { image: "https://example.com/street.jpg" }]);
    expect(itemLabel(items, 0, 10)).toBe("T1 two cats…");
    expect(itemLabel(items, 3)).toBe("I2 street.jpg");
    expect(imageUrl(" https://example.com/a.jpg ")).toBe("https://example.com/a.jpg");
    expect(imageUrl("file:///etc/passwd")).toBeNull();
    expect(imageUrl("javascript:alert(1)")).toBeNull();
    expect(imageUrl("cats.jpg")).toBeNull();
    expect(urlName("https://example.com/photos/two%20cats.jpg?x=1")).toBe("two cats.jpg");
    expect(urlName("https://example.com/")).toBe("example.com");
  });

  it("FY-WEB-945 zero-shot labels, the template, languages: split, each once, capped; what an English-only model is", () => {
    expect(addLabels(["billing"], "Billing, praise，技術問題、  \n shipping").labels).toEqual(["billing", "praise", "技術問題", "shipping"]);
    expect(addLabels([], "x".repeat(65)).error).toBe("A label is at most 64 characters.");
    const full = Array.from({ length: 32 }, (_, i) => `l${i}`);
    const over = addLabels(full, "one more");
    expect(over.labels).toHaveLength(32);
    expect(over.error).toBe("At most 32 labels.");
    expect(addLabels(full, "L0").error).toBeNull();   // already there (case-insensitive): not a 33rd
    expect(templateProblem("")).toBeNull();
    expect(templateProblem("這段文字是關於{}。")).toBeNull();
    expect(templateProblem("no braces")).toMatch(/one \{\} where the label goes/);
    expect(templateProblem("{} and {}")).not.toBeNull();
    expect(templateProblem("two\nlines {}")).not.toBeNull();
    expect(templateProblem(`${"x".repeat(199)}{}`)).not.toBeNull();
    expect(englishOnly(["en"])).toBe(true);
    expect(englishOnly(["en", "zh"])).toBe(false);
    expect(englishOnly(["multilingual"])).toBe(false);
    expect(englishOnly(null)).toBe(false);
    expect(hasCjk("我的發票")).toBe(true);
    expect(hasCjk("an invoice")).toBe(false);
    expect(languagesLine(["en"]).text).toBe("English");
    expect(languagesLine(["multilingual"]).text).toBe("Multilingual");
    expect(languagesLine(["en", "fr", "de", "es", "zh", "ja"])).toEqual({ text: "English, French, German, Spanish and 2 more",
      full: "English, French, German, Spanish, Chinese, Japanese" });
    setLang("zh-TW");
    expect(languagesLine(["en", "zh"]).text).toBe("英文、中文");
    expect(languagesLine(["multilingual"]).text).toBe("多語言");
    setLang("en");
    expect(embedSummary({ modalities: ["text", "image"], dimensions: 512, text_max_tokens: 77 })).toBe("Texts and images · 512 dimensions · texts up to 77 tokens");
    expect(embedSummary({ modalities: ["image"], dimensions: 384 })).toBe("Images only · 384 dimensions");
    expect(embedSummary(null)).toBe("Texts");
    expect(twoJobs({ embed: { modalities: ["text", "image"] } })).toBe(true);
    expect(twoJobs({ vision: { task: "image-classify", zero_shot: true } })).toBe(true);
    expect(twoJobs({ embed: { modalities: ["image"] } })).toBe(false);
    expect(twoJobs({ vision: { task: "image-classify" } })).toBe(false);
  });

  it("FY-WEB-946 Embeddings with a CLIP-like model: texts and images (a file and an address) in one request, a text x image table with each row's best", async () => {
    const { sent } = server([route("clip", "openai-embeddings", 1)], [dep(1, "embed", { embed: CLIP }, { max_images: 8 })], (_, b) => ({ status: 200, body: {
      data: (b.input as unknown[]).map((_, i) => ({ index: i, embedding: unit([[1, 0, 0, 0.1], [0, 1, 0, 0.1], [0.9, 0.1, 0, 0.1], [0.1, 0.9, 0.1, 0]][i]) })),
      usage: { prompt_tokens: 12, images: 2 } } }));
    mount("/playground?model=clip");
    const panel = (await screen.findByRole("heading", { name: "Embeddings" })).closest("section") as HTMLElement;
    expect(panel.textContent).toContain("Texts and images · 4 dimensions · texts up to 77 tokens. Texts and images land in one space");
    expect(within(panel).getByText(/Images \(0 of up to 8\)/)).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Texts, one per line"), { target: { value: "two cats\n\n a street " } });
    const file = new File([Uint8Array.from(atob(PNG), (c) => c.charCodeAt(0))], "cats.png", { type: "image/png" });
    await act(async () => fireEvent.change(within(panel).getByLabelText("Images to embed", { selector: "input" }), { target: { files: [file] } }));
    await within(panel).findByRole("list", { name: "Images to embed" });
    await waitFor(() => expect(within(panel).getByText(/Images \(1 of up to 8\)/)).toBeTruthy());
    fireEvent.change(within(panel).getByLabelText("An image's address"), { target: { value: "ftp://example.com/x.jpg" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Add" }));
    expect(within(panel).getByRole("alert").textContent).toBe("Give an http:// or https:// address of an image.");
    fireEvent.change(within(panel).getByLabelText("An image's address"), { target: { value: "https://example.com/street.jpg" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Add" }));
    expect(within(panel).getByText(/Images \(2 of up to 8\)/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Embed" }));
    const table = await within(panel).findByRole("table", { name: "Cosine similarity" });
    expect(sent).toHaveLength(1);
    expect(sent[0].body).toEqual({ model: "clip", input: ["two cats", "a street", { image: expect.stringMatching(/^data:image\/png;base64,/) }, { image: "https://example.com/street.jpg" }] });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);   // the header, then one row per text
    expect(within(rows[1]).getByRole("rowheader").textContent).toBe("T1 two cats");
    expect(within(rows[1]).getAllByRole("cell")).toHaveLength(2);
    expect(within(rows[1]).getAllByRole("cell")[0].className).toContain("emb-best");
    expect(within(rows[2]).getAllByRole("cell")[1].className).toContain("emb-best");
    expect(within(rows[1]).getByLabelText(/the most similar in its row/)).toBeTruthy();
    expect(within(panel).getByRole("status").textContent).toMatch(/^4 vectors of 4 dimensions · \d+ ms · 12 tokens · 2 images · normalised \(length 1\)$/);
    expect(panel.textContent).toContain("Compare within a row");
    // Removing an image takes it out of the next request.
    fireEvent.click(within(panel).getByRole("button", { name: "Remove street.jpg" }));
    expect(within(panel).getByText(/Images \(1 of up to 8\)/)).toBeTruthy();
  });

  it("FY-WEB-947 Embeddings with DINOv2: images only (no text box), an image x image table; the deployment's image cap; a refusal in the reader's words", async () => {
    let fail = false;
    const { sent } = server([route("dino", "openai-embeddings", 2)], [dep(2, "embed", { embed: DINO }, { max_images: 2 })], (_, b) => (fail
      ? { status: 400, body: { error: { message: "This model embeds images only: send images.", type: "invalid_request_error", code: "text_not_supported" } } }
      : { status: 200, body: { data: (b.input as unknown[]).map((_, i) => ({ index: i, embedding: unit([[1, 0, 0], [0.9, 0.1, 0], [0, 0, 1]][i]) })) } }));
    mount("/playground?model=dino");
    const panel = (await screen.findByRole("heading", { name: "Embeddings" })).closest("section") as HTMLElement;
    expect(within(panel).queryByLabelText("Texts, one per line")).toBeNull();
    expect(within(panel).getByRole("note").textContent).toContain("texts are refused (text_not_supported)");
    act(() => setLang("zh-TW"));
    // Chinese sentences end with a full-width stop, also between the model's summary and what it does.
    expect(await within(panel).findByText("只收圖片 · 3 維。只收圖片：拿圖片互相比，找出相似或重複的圖。")).toBeTruthy();
    act(() => setLang("en"));
    expect((within(panel).getByRole("button", { name: "Embed" }) as HTMLButtonElement).disabled).toBe(true);
    const png = (name: string) => new File([Uint8Array.from(atob(PNG), (c) => c.charCodeAt(0))], name, { type: "image/png" });
    // Two picks before the first is read: still no more than the cap.
    const input = () => within(panel).getByLabelText("Images to embed", { selector: "input" });
    await act(async () => {
      fireEvent.change(input(), { target: { files: [png("a.png")] } });
      fireEvent.change(input(), { target: { files: [png("b.png"), png("x.png")] } });
    });
    const pics = () => within(panel).queryByRole("list", { name: "Images to embed" });
    await waitFor(() => expect(within(pics()!).getAllByRole("listitem")).toHaveLength(2));
    await act(async () => fireEvent.change(input(), { target: { files: [png("c.png")] } }));
    expect(await within(panel).findByText("This deployment takes at most 2 images in one request.")).toBeTruthy();
    await waitFor(() => expect(within(pics()!).getAllByRole("listitem")).toHaveLength(2));
    expect((within(panel).getByRole("button", { name: "Choose images" }) as HTMLButtonElement).disabled).toBe(true);
    // Not an image: said, nothing read.
    fireEvent.click(within(panel).getByRole("button", { name: "Remove b.png" }));
    await act(async () => fireEvent.change(within(panel).getByLabelText("Images to embed", { selector: "input" }), { target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] } }));
    expect(within(panel).getByRole("alert").textContent).toContain("notes.txt");
    await act(async () => fireEvent.change(within(panel).getByLabelText("Images to embed", { selector: "input" }), { target: { files: [png("c.png")] } }));
    await waitFor(() => expect(within(pics()!).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["I1 a.png", "I2 c.png"]));
    fireEvent.click(within(panel).getByRole("button", { name: "Embed" }));
    const table = await within(panel).findByRole("table", { name: "Cosine similarity" });
    expect((sent[0].body.input as unknown[]).every((x) => typeof x === "object")).toBe(true);
    const cells = within(table).getAllByRole("cell");
    expect(cells).toHaveLength(4);
    expect(cells[0].textContent).toBe("—");
    expect(cells[0].getAttribute("aria-label")).toBe("the same item");
    expect(panel.textContent).toContain("between every two items");
    fail = true;
    act(() => setLang("zh-TW"));
    fireEvent.click(await within(panel).findByRole("button", { name: "產生嵌入" }));
    await waitFor(() => expect(panel.textContent).toContain("這個模型只收圖片，文字會被拒絕"));
  });

  it("FY-WEB-948 Classify with a zero-shot model: label chips, the template (a Chinese one in one click), multi-label; bars in the labels' order", async () => {
    const { sent } = server([route("topics", "classify", 3)], [dep(3, "classify", { classify: NLI, languages: ["multilingual"] }, { max_pairs: 12 })], (_, b) => ({ status: 200, body: {
      labels: b.labels, multi_label: !!b.multi_label, usage: { prompt_tokens: 300 },
      data: (b.input as string[]).map((_, i) => ({ index: i, label: "帳務", probs: [0.1, 0.7, 0.2], num_classes: 3 })) } }));
    mount("/playground?model=topics");
    const panel = (await screen.findByRole("heading", { name: "Text classification" })).closest("section") as HTMLElement;
    expect(panel.textContent).toContain("Zero-shot: the model chooses among the labels you give");
    expect(panel.textContent).toContain("Languages: Multilingual.");
    const chips = () => within(within(panel).getByRole("list", { name: "Labels to choose among" })).getAllByRole("listitem").map((li) => li.textContent);
    expect(chips()).toEqual(["technology", "food", "complaint", "praise"]);
    for (const l of ["technology", "food", "complaint", "praise"]) fireEvent.click(within(panel).getByRole("button", { name: `Remove the label ${l}` }));
    const input = within(panel).getByLabelText(/^Labels \(0 of up to 32\)/);
    fireEvent.change(input, { target: { value: "科技," } });
    fireEvent.change(input, { target: { value: "帳務" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "稱讚" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "帳務" } });
    fireEvent.keyDown(input, { key: "Enter" });   // already there: not twice
    expect(chips()).toEqual(["科技", "帳務", "稱讚"]);
    fireEvent.change(within(panel).getByLabelText("Texts, one per line"), { target: { value: "我的發票金額開錯了\n新筆電的電池更耐用\n這家店很棒\n東西壞了\n第五段" } });
    // 5 texts x 3 labels = 15 pairs: over this deployment's 12.
    expect(within(panel).getByText(/5 texts × 3 labels = 15 pairs \(this deployment takes up to 12/).className).toContain("field-error");
    expect((within(panel).getByRole("button", { name: "Classify" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Texts, one per line"), { target: { value: "我的發票金額開錯了\n新筆電的電池更耐用" } });
    fireEvent.change(within(panel).getByLabelText("Hypothesis template"), { target: { value: "no braces" } });
    expect(within(panel).getByRole("alert").textContent).toMatch(/one \{\} where the label goes/);
    expect((within(panel).getByRole("button", { name: "Classify" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(panel).getByRole("button", { name: "Use 這段文字是關於{}。" }));
    expect((within(panel).getByLabelText("Hypothesis template") as HTMLInputElement).value).toBe("這段文字是關於{}。");
    fireEvent.click(within(panel).getByLabelText("Several labels may apply (multi-label)"));
    fireEvent.click(within(panel).getByRole("button", { name: "Classify" }));
    const bars = await within(panel).findByRole("list", { name: "Probabilities for text 1" });
    expect(sent[0].body).toEqual({ model: "topics", input: ["我的發票金額開錯了", "新筆電的電池更耐用"], labels: ["科技", "帳務", "稱讚"],
      multi_label: true, hypothesis_template: "這段文字是關於{}。" });
    // The labels' own order (not sorted by probability), every label shown, the most likely marked.
    const items = within(bars).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".cls-name")?.textContent)).toEqual(["科技", "帳務", "稱讚"]);
    expect(items[1].className).toBe("cls-best");
    expect(panel.textContent).toContain("each label scored on its own (multi-label)");
  });

  it("FY-WEB-949 an English-only classifier warns when the texts are Chinese; a fixed-label model gets no label field and sends none", async () => {
    const { sent } = server([route("en-topics", "classify", 4), route("stars", "classify", 5)],
      [dep(4, "classify", { classify: NLI, languages: ["en"] }), dep(5, "classify", { classify: { labels: 2, label_names: ["negative", "positive"] }, languages: ["en"] })],
      (_, b) => ({ status: 200, body: { data: (b.input as string[]).map((_, i) => ({ index: i, label: "positive", probs: [0.2, 0.8], num_classes: 2 })) } }));
    mount("/playground?model=en-topics");
    const panel = (await screen.findByRole("heading", { name: "Text classification" })).closest("section") as HTMLElement;
    // The sample has a Chinese line.
    expect(within(panel).getByRole("note").textContent).toContain("English only");
    fireEvent.change(within(panel).getByLabelText("Texts, one per line"), { target: { value: "The soup was cold." } });
    expect(within(panel).queryByRole("note")).toBeNull();
    // Without a template or multi-label, only the labels go with the texts (the model's defaults apply).
    fireEvent.click(within(panel).getByRole("button", { name: "Classify" }));
    await within(panel).findByRole("list", { name: "Probabilities for text 1" });
    expect(sent.at(-1)!.body).toEqual({ model: "en-topics", input: ["The soup was cold."], labels: ["technology", "food", "complaint", "praise"] });
    cleanup();
    mount("/playground?model=stars");
    const fixed = (await screen.findByRole("heading", { name: "Text classification" })).closest("section") as HTMLElement;
    expect(within(fixed).queryByLabelText(/^Labels/)).toBeNull();
    expect(within(fixed).queryByLabelText("Hypothesis template")).toBeNull();
    fireEvent.change(within(fixed).getByLabelText("Texts, one per line"), { target: { value: "Great!" } });
    fireEvent.click(within(fixed).getByRole("button", { name: "Classify" }));
    await within(fixed).findByRole("list", { name: "Probabilities for text 1" });
    expect(sent.at(-1)!.body).toEqual({ model: "stars", input: ["Great!"] });
  });

  it("FY-WEB-950 the zero-shot panel in Traditional Chinese: its words, the template hint and the pair count", async () => {
    setLang("zh-TW");
    server([route("topics", "classify", 3)], [dep(3, "classify", { classify: NLI, languages: ["multilingual"] })], () => undefined);
    mount("/playground?model=topics");
    const panel = (await screen.findByRole("heading", { name: "文字分類" })).closest("section") as HTMLElement;
    expect(panel.textContent).toContain("零樣本：模型從你給的標籤裡挑");
    expect(panel.textContent).toContain("語言：多語言。");
    expect(within(panel).getByLabelText("假設句範本")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "改用「這段文字是關於{}。」" })).toBeTruthy();
    expect(within(panel).getByLabelText("可以同時符合多個標籤（多標籤）")).toBeTruthy();
    expect(panel.textContent).toContain("3 段文字 × 4 個標籤 = 12 組（這個部署一次請求最多 256 組）。");
    expect(within(panel).getByRole("button", { name: "分類" })).toBeTruthy();
  });

  it("FY-WEB-951 the model page: what an embedding model takes, zero-shot, its languages, and the English-only warning", async () => {
    const base = { kind: "hf", ref: "main", revision: "1a25a446712ba5ee05982a381eed697ef9b435cf", file: null, format: "safetensors", size_bytes: 0.6 * GIB,
      engine_args: {}, recipes: ["transformers"], deployments: 0, created_by: "owner", created_at: "2026-11-24T00:00:00", summary: {}, gguf_files: [],
      license: "mit", modalities: [], title: null };
    const page = (m: Partial<CatalogModel>): ModelPage => ({ model: { ...base, ...m } as CatalogModel, recipes: [], can_manage: true, can_deploy: true,
      files: { recorded: false, downloaded_count: 0, downloaded_bytes: 0, files: [] }, machines: [], fits: {}, deployments: [] }) as unknown as ModelPage;
    const pages: Record<string, ModelPage> = {
      "/api/models/clip-emb": page({ id: 1, name: "clip-emb", source: "hf://laion/CLIP-ViT-B-32-laion2B-s34B-b79K", task: "embed", task_label: "Embeddings",
        embed: { family: "clip", modalities: ["text", "image"], dimensions: 512, text_max_tokens: 77 }, languages: ["en"] }),
      "/api/models/dino": page({ id: 2, name: "dino", source: "hf://facebook/dinov2-small", task: "embed", task_label: "Embeddings",
        embed: { family: "image-only", modalities: ["image"], dimensions: 384 }, languages: null }),
      "/api/models/roberta-zs": page({ id: 3, name: "roberta-zs", source: "hf://MoritzLaurer/roberta-base-zeroshot-v2.0-c", task: "classify",
        task_label: "Text classification", classify: NLI, languages: ["en"] }),
      "/api/models/bge-zs": page({ id: 4, name: "bge-zs", source: "hf://MoritzLaurer/bge-m3-zeroshot-v2.0-c", task: "classify",
        task_label: "Text classification", classify: NLI, languages: ["multilingual"] }),
    };
    server([], [], () => undefined, (url) => pages[url] ?? (url === "/api/machines" ? { machines: [] } : undefined));
    mount("/models/clip-emb");
    await screen.findByRole("heading", { level: 1, name: "clip-emb" });
    expect(screen.getAllByText("Texts and images · 512 dimensions · texts up to 77 tokens").length).toBe(2);   // the chip and the fact
    expect(screen.getByText(/one space: a text can find images/)).toBeTruthy();
    expect(screen.getByText("English only").closest(".chip")?.className).toContain("chip-tone-run");
    expect(screen.getByRole("note").textContent).toContain("searching images in Chinese finds the wrong pictures");
    cleanup();
    mount("/models/dino");
    await screen.findByRole("heading", { level: 1, name: "dino" });
    expect(screen.getAllByText("Images only · 384 dimensions").length).toBe(2);
    expect(screen.queryByRole("note")).toBeNull();   // no languages: nothing said
    cleanup();
    mount("/models/roberta-zs");
    await screen.findByRole("heading", { level: 1, name: "roberta-zs" });
    expect(screen.getByText("Zero-shot: labels in each request")).toBeTruthy();
    expect(screen.getByText("Chosen in each request (zero-shot)")).toBeTruthy();
    expect(screen.queryByText(/entailment/)).toBeNull();   // the NLI head's own labels are not the model's labels
    expect(screen.getByRole("note").textContent).toContain("For Chinese, use a multilingual model such as bge-m3-zeroshot-v2.0-c");
    cleanup();
    mount("/models/bge-zs");
    await screen.findByRole("heading", { level: 1, name: "bge-zs" });
    expect(screen.getAllByText("Multilingual")[0].closest(".chip")?.className).toContain("chip-tone-ok");
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("FY-WEB-952 Serving marks an image embedder with an image; the deploy form says the transformers server runs on any CPU and why float32", async () => {
    const { ModelDeployPage, EstimatePanel, transformersEngine } = await import("./ModelDeploy");
    expect(transformersEngine({ engine: "forgeyard-vision" }, "embed")).toBe(true);
    expect(transformersEngine({ engine: "forgeyard-vision" }, "classify")).toBe(true);
    expect(transformersEngine({ engine: "forgeyard-vision" }, "detect")).toBe(false);
    expect(transformersEngine({ engine: "vllm" }, "classify")).toBe(false);
    void ModelDeployPage;
    const est = (label: string) => ({ estimate: { method: "files", device: "cpu" as const, gpus: 0, per_gpu: 0, ram: 3 * GIB, amount: 3 * GIB, kv_tokens: 0, notes: [],
      parts: [{ label, bytes: 2 * GIB, detail: "" }, { label: "Engine overhead", bytes: GIB, detail: "recipe" }] },
      placement: { fits: true, placed: [], machines: [] } }) as unknown as Parameters<typeof EstimatePanel>[0]["est"];
    render(<EstimatePanel est={est("Model weights in float32")} error={null} pending={false} />);
    expect(screen.getByText(/On a CPU the weights are held in float32/)).toBeTruthy();
    cleanup();
    render(<EstimatePanel est={est("Model files")} error={null} pending={false} />);
    expect(screen.queryByText(/held in float32/)).toBeNull();
    cleanup();
    const full = (id: number, name: string, embed: unknown, task = "embed") => ({ ...dep(id, task, { id, name, embed, source: "hf://x/y", format: "safetensors", size_bytes: 1 }),
      name, served_name: name, estimate: { device: "cpu", ram: GIB, per_gpu: 0 }, endpoints: UP,
      workload: { status: "running", ready: 1, replicas: 1, replica_list: [] } }) as unknown as Deployment;
    server([], [full(1, "clip", CLIP), full(2, "dino", DINO), full(3, "bge-m3", { modalities: ["text"], dimensions: 1024 })], () => undefined,
      (url) => (url === "/api/machines" ? { machines: [] } : url === "/api/models" ? { models: [], recipes: [], can_deploy: true, can_manage: true } : undefined));
    mount("/serving");
    const sec = await screen.findByRole("region", { name: /^Embeddings/ });
    expect(within(within(sec).getByRole("group", { name: "Deployment clip" })).getByRole("img", { name: "Texts and images · 4 dimensions · texts up to 77 tokens" })).toBeTruthy();
    expect(within(within(sec).getByRole("group", { name: "Deployment dino" })).getByRole("img", { name: "Images only · 3 dimensions" })).toBeTruthy();
    expect(within(within(sec).getByRole("group", { name: "Deployment bge-m3" })).queryByRole("img")).toBeNull();
  });

  it("FY-WEB-953 the add form reads a CLIP-like repository and says it does two jobs, one entry each; an NLI model's labels and languages", async () => {
    let meta: Record<string, unknown> = {};
    server([], [], () => undefined, (url) => (url === "/api/models" ? { can_manage: true, can_deploy: true, models: [], recipes: [],
      sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } } : undefined));
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (url, init) => (String(url) === "/api/models/inspect"
      ? new Response(JSON.stringify({ recipes: ["transformers"], meta, suggestion: { recipe: "transformers", recipes: ["transformers"] } }), { status: 200 })
      : base(url as string, init)));
    meta = { format: "safetensors", revision: "1".repeat(40), task_hint: "embed", size_bytes: 0.6 * GIB, file_count: 6, license: "mit", modalities: [],
      embed: { family: "clip", modalities: ["text", "image"], dimensions: 512, text_max_tokens: 77 }, languages: ["en"] };
    mount("/models");
    fireEvent.click(await screen.findByRole("button", { name: "Enter a source" }));
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "laion/CLIP-ViT-B-32-laion2B-s34B-b79K" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Look it up" })));
    expect(await screen.findByText("Texts and images · 512 dimensions · texts up to 77 tokens")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("This repository does two jobs, one catalog entry each");
    expect(screen.getByText("Languages").nextElementSibling?.textContent).toBe("English");
    cleanup();
    meta = { format: "safetensors", revision: "2".repeat(40), task_hint: "classify", size_bytes: 1.1 * GIB, file_count: 6, license: "mit", modalities: [],
      classify: NLI, languages: ["multilingual"] };
    mount("/models");
    fireEvent.click(await screen.findByRole("button", { name: "Enter a source" }));
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "MoritzLaurer/bge-m3-zeroshot-v2.0-c" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Look it up" })));
    expect(await screen.findByText("Zero-shot (NLI): chosen in each request")).toBeTruthy();
    expect(screen.getByText("Languages").nextElementSibling?.textContent).toBe("Multilingual");
    expect(screen.queryByRole("note")).toBeNull();
  });
});
