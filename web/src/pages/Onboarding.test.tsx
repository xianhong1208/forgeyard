// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { RecommendedResponse, SearchResponse, Suggestion } from "../lib/addmodel";
import { exampleCall, type Checklist, type Step } from "../lib/onboarding";
import type { CatalogResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };
const GIB = 1024 ** 3;

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: Record<string, unknown> = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const where = () => screen.getByTestId("where").textContent;

const step = (id: Step["id"], done: boolean, over: Partial<Step> = {}): Step => ({
  id, title: { machine: "Add a machine", gpus: "Choose which GPUs Forgeyard may use", model: "Add a model", deploy: "Deploy it",
    key: "Create an API key", call: "Call /v1", usage: "See your usage" }[id],
  why: `why ${id}`, done, detail: `detail ${id}`, action: { to: `/${id}`, label: `Go ${id}` }, blocked_by: null, who: null, ...over,
});

function checklist(done: number, over: Partial<Checklist> = {}): Checklist {
  const ids: Step["id"][] = ["machine", "gpus", "model", "deploy", "key", "call"];
  const steps = ids.map((id, i) => step(id, i < done));
  return { kind: "setup", steps, done, total: 6, complete: done === 6, hidden: false, gateway_url: "http://fy.lan:7860/v1",
    example: { model: "qwen3-4b", api: "openai-chat", ready: true }, ...over };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("get started", () => {
  it("FY-WEB-101 home shows the checklist in order, the next step highlighted, and a ready-to-copy call", async () => {
    server((url) => (url === "/api/onboarding" ? { status: 200, body: checklist(2) } : undefined));
    mount("/");
    expect(await screen.findByRole("heading", { name: "Set up Forgeyard" })).toBeTruthy();
    expect(screen.getByText("2 of 6 done.")).toBeTruthy();
    const items = screen.getAllByRole("listitem").filter((li) => li.className.includes("onb-step"));
    expect(items.map((li) => li.getAttribute("aria-label"))).toEqual([
      "Step 1: Add a machine (done)", "Step 2: Choose which GPUs Forgeyard may use (done)", "Step 3: Add a model",
      "Step 4: Deploy it", "Step 5: Create an API key", "Step 6: Call /v1"]);
    expect(items[2].className).toContain("onb-current");
    expect(within(items[2]).getByRole("link", { name: "Go model" }).className).toContain("btn-primary");
    expect(within(items[3]).getByRole("link", { name: "Go deploy" }).className).not.toContain("btn-primary");
    expect(within(items[0]).getByRole("link", { name: "Open" }).className).toContain("btn-quiet");
    expect(within(items[0]).getByText(/Done\. detail machine/)).toBeTruthy();
    const python = screen.getByLabelText("python example").textContent ?? "";
    expect(python).toContain('base_url="http://fy.lan:7860/v1"');
    expect(python).toContain('api_key=os.environ["FORGEYARD_API_KEY"]');
    expect(python).toContain('model="qwen3-4b"');
    fireEvent.click(screen.getByRole("tab", { name: "curl" }));
    expect(screen.getByLabelText("curl example").textContent).toContain('-H "Authorization: Bearer $FORGEYARD_API_KEY"');
    expect(where()).toBe("/");
  });

  it("FY-WEB-102 a hidden or finished checklist sends home to Machines; Get started always shows it", async () => {
    server((url) => (url === "/api/onboarding" ? { status: 200, body: checklist(3, { hidden: true }) } : url === "/api/machines"
      ? { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } } : undefined));
    mount("/");
    await waitFor(() => expect(where()).toBe("/machines"));
    cleanup();
    server((url) => (url === "/api/onboarding" ? { status: 200, body: checklist(6) } : undefined));
    mount("/");
    await waitFor(() => expect(where()).toBe("/machines"));
    cleanup();
    server((url) => (url === "/api/onboarding" ? { status: 200, body: checklist(6) } : undefined));
    mount("/start");
    expect(await screen.findByText("All 6 steps done.")).toBeTruthy();
    cleanup();
    server(() => undefined); // an older server without the checklist: straight to Machines
    mount("/");
    await waitFor(() => expect(where()).toBe("/machines"));
  });

  it("FY-WEB-103 hiding and showing again is saved on the server; the user menu opens Get started", async () => {
    let hidden = false;
    const fetch = server((url, init) => {
      if (url === "/api/onboarding" && init?.method === "PUT") {
        hidden = JSON.parse(String(init.body)).hidden;
        return { status: 200, body: checklist(1, { hidden }) };
      }
      if (url === "/api/onboarding") return { status: 200, body: checklist(1, { hidden }) };
      return undefined;
    });
    mount("/start");
    const hide = await screen.findByRole("button", { name: "Hide this list" });
    await act(async () => fireEvent.click(hide));
    expect(await screen.findByText(/Hidden\. Forgeyard now opens on Machines/)).toBeTruthy();
    const put = fetch.mock.calls.find(([u, i]) => u === "/api/onboarding" && i?.method === "PUT");
    expect((put?.[1]?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
    const show = await screen.findByRole("button", { name: "Show it when I open Forgeyard" });
    await act(async () => fireEvent.click(show));
    await waitFor(() => expect(hidden).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: /owner/ }));
    expect(screen.getByRole("menuitem", { name: "Get started" }).getAttribute("href")).toBe("/start");
  });

  it("FY-WEB-104 people with the user role get the three steps; steps others do say who does them", async () => {
    const use: Checklist = { kind: "use", steps: [step("key", true), step("call", false, { blocked_by: "deploy", action: null }), step("usage", false, { blocked_by: "call" })],
      done: 1, total: 3, complete: false, hidden: false, gateway_url: "http://fy.lan:7860/v1", example: null };
    server((url) => (url === "/api/onboarding" ? { status: 200, body: use } : undefined), { ...OWNER, role: "user", is_owner: false });
    mount("/");
    expect(await screen.findByRole("heading", { name: "Use the models" })).toBeTruthy();
    expect(screen.getByText("1 of 3 done.")).toBeTruthy();
    expect(screen.getByLabelText("python example").textContent).toContain('model="MODEL_NAME"');
    expect(screen.getByText(/Nothing is deployed yet: replace/)).toBeTruthy();
    cleanup();
    const op = checklist(0, { steps: checklist(0).steps.map((s) => (s.id === "machine" ? { ...s, who: "An admin adds machines." } : s)) });
    server((url) => (url === "/api/onboarding" ? { status: 200, body: op } : undefined), { ...OWNER, role: "operator", is_owner: false });
    mount("/");
    const first = await screen.findByRole("listitem", { name: "Step 1: Add a machine" });
    expect(within(first).getByText("An admin adds machines.")).toBeTruthy();
    expect(within(first).getByRole("link", { name: "Go machine" }).className).not.toContain("btn-primary");
  });

  it("FY-WEB-105 the example call fits each kind of model", () => {
    const rerank = exampleCall("http://h/v1", "rr", "rerank");
    expect(rerank.python).toContain('"Bearer " + os.environ["FORGEYARD_API_KEY"]');
    expect(rerank.python.startsWith("import os\n")).toBe(true);
    expect(exampleCall("http://h/v1", "emb", "openai-embeddings").curl).toContain("http://h/v1/embeddings");
    expect(exampleCall("http://h/v1", "asr", "openai-audio").curl).toContain("-F model=asr -F file=@speech.mp3");
  });
});

// ------------------------------------------------------------------ add a model
const emptyCatalog: CatalogResponse = { can_manage: true, can_deploy: true, models: [], recipes: [],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } };

const recommended: RecommendedResponse = {
  groups: [{ id: "qwen3", title: "Qwen3", description: "Qwen models" }, { id: "llm", title: "Other open chat models", description: "Others" }],
  models: [
    { name: "qwen3-reranker-0.6b", title: "Qwen3 Reranker 0.6B", source: "hf://Qwen/Qwen3-Reranker-0.6B", revision: null, file: null, group: "qwen3",
      task: "rerank", format: "safetensors", recipe: "vllm-pooling", device: "gpu", download_gib: 1.1, memory_gib: 3.4, gated: false,
      engine_args: { "vllm-pooling": ["--hf-overrides", "{}"] }, note: "Re-orders results.", in_catalog: null,
      license: "apache-2.0", inputs: [], mmproj: null },
    { name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B (GGUF, for the CPU)", source: "hf://ggml-org/Qwen3-1.7B-GGUF", revision: null,
      file: "Qwen3-1.7B-Q4_K_M.gguf", group: "qwen3", task: "chat", format: "gguf", recipe: "llama-cpp", device: "cpu", download_gib: 1.2,
      memory_gib: 2.9, gated: false, engine_args: {}, note: "CPU.", in_catalog: "qwen-cpu",
      license: "apache-2.0", inputs: [], mmproj: null },
    { name: "gemma-3-4b-it", title: "Gemma 3 4B", source: "hf://google/gemma-3-4b-it", revision: null, file: null, group: "llm", task: "chat",
      format: "safetensors", recipe: "vllm", device: "gpu", download_gib: 8, memory_gib: 14.7, gated: true, engine_args: {}, note: "Google.", in_catalog: null,
      license: "gemma", inputs: ["image"], mmproj: null },
  ],
};

const suggestion: Suggestion = {
  recipe: "vllm-pooling", recipes: ["vllm-pooling"], device: "gpu", devices: ["gpu"], params: { gpus: 1 }, fits: true,
  estimate: { method: "safetensors-kv", device: "gpu", gpus: 1, per_gpu: 3.38 * GIB, ram: 0, amount: 3.38 * GIB, kv_tokens: 8192, notes: [],
    parts: [{ label: "Weights", bytes: 1.11 * GIB, detail: "" }, { label: "KV cache", bytes: 0.88 * GIB, detail: "" }] },
  machines: [{ name: "a100", status: "online", fits: true, reason: "", gpus: [4], short_by: null },
    { name: "cpu-2", status: "online", fits: false, reason: "no GPU", gpus: [], short_by: null }],
};

const inspected = { recipes: ["vllm-pooling"], suggestion, meta: { format: "safetensors", revision: "e61197ed45024b0ed8a2d74b80b4d909f1255473", ref: "main",
  task_hint: "rerank", size_bytes: 1.12 * GIB, file_count: 9, config: { layers: 28, kv_heads: 8, max_context: 40960, dtype: "bfloat16" },
  weights: { params: { BF16: 595776512 } } } };

function catalogServer(extra: (url: string, init?: RequestInit) => Reply | undefined) {
  return server((url, init) => {
    const r = extra(url, init);
    if (r) return r;
    if (url === "/api/models") return { status: 200, body: emptyCatalog };
    if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
    if (url === "/api/models/recommended") return { status: 200, body: recommended };
    return undefined;
  });
}

describe("add a model", () => {
  it("FY-WEB-106 a recommended model is inspected with its recipe, shows where it fits, and is added with its engine arguments", async () => {
    const fetch = catalogServer((url, init) => {
      if (url === "/api/models/inspect") return { status: 200, body: inspected };
      if (url === "/api/models" && init?.method === "POST") return { status: 201, body: { model: { name: "qwen3-reranker-0.6b" } } };
      return undefined;
    });
    mount("/models?tab=catalog");
    expect(await screen.findByRole("heading", { name: "Qwen3" })).toBeTruthy();
    expect(screen.getByText("In the catalog as qwen-cpu")).toBeTruthy();
    const gemma = screen.getByText("Gemma 3 4B").closest("li")!;
    expect(within(gemma).getByText(/Needs a Hugging Face token and accepting the licence/)).toBeTruthy();
    const rr = screen.getByText("Qwen3 Reranker 0.6B").closest("li")!;
    await act(async () => fireEvent.click(within(rr).getByRole("button", { name: "Choose" })));
    expect(await screen.findByText("Fits now on a100 (GPU #4).")).toBeTruthy();
    expect(screen.getByText("3.38 GiB")).toBeTruthy();
    const inspect = fetch.mock.calls.find(([u]) => u === "/api/models/inspect");
    expect(JSON.parse(String(inspect?.[1]?.body))).toEqual({ source: "hf://Qwen/Qwen3-Reranker-0.6B", task: "rerank", recipe: "vllm-pooling", device: "gpu" });
    expect((screen.getByLabelText("Name in the catalog") as HTMLInputElement).value).toBe("qwen3-reranker-0.6b");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Add to the catalog" })));
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/models" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ source: "hf://Qwen/Qwen3-Reranker-0.6B", name: "qwen3-reranker-0.6b",
      title: "Qwen3 Reranker 0.6B", task: "rerank", engine_args: { "vllm-pooling": ["--hf-overrides", "{}"] } });
    const deployIt = await screen.findByRole("link", { name: "Deploy it" });
    expect(deployIt.getAttribute("href")).toBe("/models/deploy?model=qwen3-reranker-0.6b&recipe=vllm-pooling&device=gpu");
  });

  it("FY-WEB-107 a gated model asks for a token and reads it again with it", async () => {
    const fetch = catalogServer((url, init) => {
      if (url === "/api/models/inspect") {
        const body = JSON.parse(String(init?.body));
        return body.token ? { status: 200, body: { ...inspected, suggestion: { ...suggestion, recipe: "vllm", recipes: ["vllm"] } } }
          : { status: 422, body: { detail: { code: "cannot_inspect", message: "google/gemma-3-4b-it is gated or private: give a Hugging Face token that may read it" } } };
      }
      return undefined;
    });
    mount("/models?tab=catalog");
    const gemma = (await screen.findByText("Gemma 3 4B")).closest("li")!;
    await act(async () => fireEvent.click(within(gemma).getByRole("button", { name: "Choose" })));
    expect(await screen.findByText(/is gated or private/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Hugging Face token"), { target: { value: "hf_abc" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Read it with this token" })));
    expect(await screen.findByText("Fits now on a100 (GPU #4).")).toBeTruthy();
    const calls = fetch.mock.calls.filter(([u]) => u === "/api/models/inspect");
    expect(calls).toHaveLength(2);
    expect(JSON.parse(String(calls[1][1]?.body)).token).toBe("hf_abc");
    expect(screen.queryByText(/is gated or private/)).toBeNull();
  });

  it("FY-WEB-108 searching Hugging Face goes through the server, waits for typing to pause, and a result can be chosen", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const results: SearchResponse = { cached: false, endpoint: "https://huggingface.co", results: [
        { id: "Qwen/Qwen3-0.6B", source: "hf://Qwen/Qwen3-0.6B", downloads: 20511631, likes: 1888, last_modified: "2025-07-26T03:46:27Z", gated: false,
          gated_mode: null, private: false, pipeline_tag: "text-generation", library: "transformers", task: "chat", format: "safetensors",
          params: 751632384, weights_bytes: 1503264768, in_catalog: null, recipes: ["vllm"] },
        { id: "BAAI/bge-m3", source: "hf://BAAI/bge-m3", downloads: 29334229, likes: 10, last_modified: null, gated: false, gated_mode: null,
          private: false, pipeline_tag: "sentence-similarity", library: null, task: "embed", format: "other", params: null, weights_bytes: null,
          in_catalog: null, recipes: [] }] };
      const fetch = catalogServer((url) => {
        if (url.startsWith("/api/models/search")) return { status: 200, body: results };
        if (url === "/api/models/inspect") return { status: 200, body: { ...inspected, meta: { ...inspected.meta, task_hint: "chat" } } };
        return undefined;
      });
      mount("/models?tab=catalog");
      fireEvent.click(await screen.findByRole("button", { name: "Search Hugging Face" }));
      const searches = () => fetch.mock.calls.filter(([u]) => String(u).startsWith("/api/models/search")).map(([u]) => u);
      fireEvent.change(screen.getByLabelText("Search Hugging Face"), { target: { value: "q" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(600); });
      expect(searches()).toEqual([]); // one letter is not worth asking Hugging Face
      fireEvent.change(screen.getByLabelText("Search Hugging Face"), { target: { value: "qwen3" } });
      fireEvent.change(screen.getByLabelText("Task"), { target: { value: "chat" } });
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(searches()).toEqual([]); // still typing
      await act(async () => { await vi.advanceTimersByTimeAsync(300); });
      expect(searches()).toEqual(["/api/models/search?q=qwen3&limit=20&task=chat"]);
      const row = (await screen.findByText("Qwen/Qwen3-0.6B")).closest("tr")!;
      expect(within(row).getByText("1.4 GiB")).toBeTruthy();
      expect(within(screen.getByText("BAAI/bge-m3").closest("tr")!).getByText("Cannot be served")).toBeTruthy();
      await act(async () => fireEvent.click(within(row).getByRole("button", { name: "Choose" })));
      expect(await screen.findByText("Fits now on a100 (GPU #4).")).toBeTruthy();
      expect((screen.getByLabelText("Name in the catalog") as HTMLInputElement).value).toBe("qwen3-0.6b");
    } finally {
      vi.useRealTimers();
    }
  });

  it("FY-WEB-109 without internet the search says so and offers the recommended list", async () => {
    catalogServer((url) => (url.startsWith("/api/models/search")
      ? { status: 503, body: { detail: { code: "hub_unreachable", message: "Cannot reach Hugging Face from the server (no route). The recommended models still work; a model can also be added by its source." } } }
      : undefined));
    mount("/models?tab=catalog");
    fireEvent.click(await screen.findByRole("button", { name: "Search Hugging Face" }));
    fireEvent.change(screen.getByLabelText("Search Hugging Face"), { target: { value: "whisper" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Search" })));
    expect(await screen.findByText(/Cannot reach Hugging Face from the server/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show the recommended models" }));
    expect(await screen.findByRole("heading", { name: "Qwen3" })).toBeTruthy();
  });

  it("FY-WEB-110 a model that does not fit anywhere says why but can still be added", async () => {
    catalogServer((url) => (url === "/api/models/inspect" ? { status: 200, body: { ...inspected, suggestion: { ...suggestion, fits: false,
      machines: [{ name: "a100", status: "online", fits: false, reason: "short", gpus: [], short_by: GIB }], summary: "Nothing can take a replica that needs 3.38 GiB per GPU. a100: short" } } }
      : undefined));
    mount("/models?tab=catalog");
    const rr = (await screen.findByText("Qwen3 Reranker 0.6B")).closest("li")!;
    await act(async () => fireEvent.click(within(rr).getByRole("button", { name: "Choose" })));
    expect(await screen.findByText(/Nothing can take a replica that needs 3.38 GiB per GPU/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Add to the catalog" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

// ------------------------------------------------------------------ empty pages
describe("empty pages say what to do", () => {
  it("FY-WEB-111 Playground, Usage, API keys and Workloads, by role", async () => {
    const fetch = server((url) => {
      if (url === "/api/routes") return { status: 200, body: { routes: [] } };
      if (url.startsWith("/api/usage")) return { status: 200, body: { rows: [], total: { requests: 0, errors: 0, limited: 0, tokens: 0, prompt_tokens: 0, completion_tokens: 0, latency_p50_ms: null, latency_p95_ms: null },
        everyone: false, is_admin: false, group_by: "model", from: "2026-11-04", to: "2026-11-10" } };
      if (url === "/api/onboarding/seen") return { status: 200, body: { ok: true } };
      if (url.startsWith("/api/keys")) return { status: 200, body: { keys: [], is_admin: false, gateway_url: "http://h/v1", models: [],
        limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null } } };
      if (url === "/api/workloads") return { status: 200, body: { workloads: [], server_time: "", can_manage: false } };
      return undefined;
    }, { ...OWNER, role: "user", is_owner: false });
    mount("/playground");
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();
    expect(screen.getByText(/ask one to deploy the model you need/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Deploy a model" })).toBeNull();
    cleanup();
    mount("/usage");
    expect(await screen.findByText(/No requests in this period/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Create an API key" }).getAttribute("href")).toBe("/keys");
    await waitFor(() => expect(fetch.mock.calls.some(([u, i]) => u === "/api/onboarding/seen" && i?.method === "POST")).toBe(true));
    cleanup();
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "Create a key" }));
    expect(await screen.findByLabelText("Name")).toBeTruthy();
    cleanup();
    mount("/workloads");
    expect(await screen.findByText(/A workload is a program you run on your machines/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "New workload" })).toBeNull();
  });

  it("FY-WEB-112 Models with an empty catalog leads to adding one; the deploy page says the catalog is empty", async () => {
    catalogServer(() => undefined);
    mount("/models");
    const add = await screen.findByRole("button", { name: "Add a model" });
    fireEvent.click(add);
    expect(await screen.findByRole("heading", { name: "Add a model" })).toBeTruthy();
    cleanup();
    catalogServer((url) => (url === "/api/machines" ? { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } } : undefined));
    mount("/models/deploy");
    expect(await screen.findByText(/The catalog is empty, so there is nothing to deploy yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Add a model to the catalog" }).getAttribute("href")).toBe("/models?tab=catalog");
  });
});
