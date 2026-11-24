// @vitest-environment jsdom
// 1.4.3 (#140): a page per catalog model (/models/<name>) and per engine (/models/engines/<name>), layout A+: header,
// facts, jump links, Machines right after Overview, Files and Parameters folded to one line; the display name, Remove,
// Edit / Delete / Copy as a custom engine; the routes beside them; the links from every place that names a model or an
// engine. FY-WEB-470 to 478.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { catalogKind, digestOf, engineHref, filesLine, machineFit, modelHref, pinned, presenceText, stale, whereText } from "../lib/pages";
import fixture from "../test/machines.json";
import type { CatalogModel, CatalogResponse, EnginePage, MachineVerdict, ModelPage, Recipe } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 3, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body?: unknown };

const variant = (name: string, vendor: "nvidia" | "amd" | "cpu", image: string | null, extra: Record<string, unknown> = {}) => ({
  name, vendor, runtime: image ? "container" as const : "process" as const, image, arch: null, compute_capability: null, driver: null,
  engine: image ? null : "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-bin.zip", ...extra,
});
const LLAMA: Recipe = {
  name: "llama-cpp", title: "llama.cpp", description: "GGUF (quantised) models with llama-server.", engine: "llama.cpp", version: "1.0.0",
  tasks: ["chat", "tts"], formats: ["gguf"], api: { chat: "openai-chat", tts: "openai-speech" }, share: "exclusive", builtin: true, estimate: "gguf",
  devices: ["gpu", "cpu"],
  params: { context: { type: "int", default: 4096, min: 512, label: "Context length", help: "Tokens per request" },
    kv_cache: { type: "choice", default: "f16", choices: ["f16", "q8_0"], advanced: true } },
  variants: [variant("cuda", "nvidia", "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:a8cf841eef4e9d6ea28bb4d88eea1004032a2bf0d1302bff7b8d2100e801ed16", { image_check: true }),
    variant("cpu-process", "cpu", null)],
} as Recipe;
const CUSTOM: Recipe = { ...LLAMA, name: "llama-cpp-vulkan", builtin: false, params: {},
  variants: [variant("vulkan", "amd", "forgeyard-local/llama.cpp:server-vulkan-b11977")] } as Recipe;

const MODEL: CatalogModel = {
  id: 7, name: "qwen3-1.7b-gguf", title: null, source: "hf://ggml-org/Qwen3-1.7B-GGUF", kind: "hf", ref: "main", revision: "daeb8e2d528a760970442092",
  file: "Qwen3-1.7B-Q4_K_M.gguf", format: "gguf", task: "chat", task_label: "Chat", size_bytes: 1282439264, engine_args: {}, recipes: ["llama-cpp", "llama-cpp-vulkan"],
  deployments: 0, created_by: "owner", created_at: "2026-11-10T00:00:00", summary: { params: 2031739904, architecture: "qwen3", layers: 28, max_context: 40960 },
  gguf_files: [], license: "apache-2.0", modalities: [],
} as CatalogModel;

const verdict = (machine_id: number, name: string, fits: boolean, extra: Partial<MachineVerdict> = {}): MachineVerdict =>
  ({ machine_id, name, status: "online", fits, reason: fits ? "" : "no GPU it may use", short_by: null, cards: [], ...extra });
const EST = (device: "gpu" | "cpu") => ({ method: "gguf", device, gpus: device === "gpu" ? 1 : 0, per_gpu: device === "gpu" ? 3 * 1024 ** 3 : 0,
  ram: device === "cpu" ? 2.5 * 1024 ** 3 : 0, amount: 1, parts: [], notes: [], kv_tokens: 4096 });

function modelPage(over: Partial<ModelPage> = {}, admin = true): ModelPage {
  return {
    model: MODEL, recipes: [LLAMA, CUSTOM], can_manage: admin, can_deploy: admin,
    files: { recorded: true, downloaded_count: 1, downloaded_bytes: 1282439264, files: [
      { path: "Qwen3-1.7B-Q4_K_M.gguf", size: 1282439264, role: "model file", downloaded: true },
      { path: "Qwen3-1.7B-Q8_0.gguf", size: 2165000000, role: "other quantisation", downloaded: false }] },
    machines: [
      { machine_id: 1, name: "a100-box", status: "online", connected: true, download: null,
        cache: { reported: true, cached: true, bytes: 1282439264, files: 1, used_by: [{ kind: "deployment", id: 3, name: "chat-small" }] },
        engines: { "llama-cpp": { variant: "cuda", runtime: "container", kind: "image", engine_name: LLAMA.variants[0].image, present: true, listed_at: "2026-11-18T01:00:00+00:00", note: null },
          "llama-cpp-vulkan": { variant: null, runtime: null, kind: null, engine_name: null, present: null, listed_at: null, note: "no variant for this machine's hardware" } } },
      { machine_id: 2, name: "strix", status: "offline", connected: false, download: null,
        cache: { reported: true, cached: false, bytes: null, files: null, used_by: [] },
        engines: { "llama-cpp": { variant: "cpu-process", runtime: "process", kind: "program", engine_name: "llama.cpp-1.0.0", present: false, listed_at: null, note: null },
          "llama-cpp-vulkan": { variant: "vulkan", runtime: "container", kind: "image", engine_name: CUSTOM.variants[0].image, present: null, listed_at: null, note: "Its images were never listed: they are once its agent is connected." } } },
    ],
    fits: {
      "llama-cpp": { gpu: { estimate: EST("gpu"), fits: true, machines: [verdict(1, "a100-box", true, { leftover: 40 * 1024 ** 3, gpus: [4] }), verdict(2, "strix", false, { status: "offline" })] },
        cpu: { estimate: EST("cpu"), fits: true, machines: [verdict(1, "a100-box", true), verdict(2, "strix", true, { status: "offline" })] } },
      "llama-cpp-vulkan": { gpu: { estimate: EST("gpu"), fits: false, machines: [verdict(1, "a100-box", false, { reason: "no variant of llama-cpp-vulkan for this machine's hardware" }),
        verdict(2, "strix", false, { status: "offline", reason: "offline" })] } },
    },
    deployments: [],
    ...over,
  };
}

function enginePage(r: Recipe, over: Partial<EnginePage> = {}, admin = true): EnginePage {
  return {
    recipe: { ...r, yaml: `name: ${r.name}\ntitle: llama.cpp\n` }, can_manage: admin,
    machines: [
      { machine_id: 1, name: "a100-box", status: "online", connected: true, can_list: true, variant: "cuda", runtime: "container", kind: "image",
        engine_name: LLAMA.variants[0].image, present: true, listed_at: new Date().toISOString(), note: null },
      { machine_id: 2, name: "strix", status: "offline", connected: false, can_list: false, variant: "cpu-process", runtime: "process", kind: "program",
        engine_name: "llama.cpp-1.0.0", present: false, listed_at: null, note: null },
    ],
    deployments: [], models: [{ id: 7, name: MODEL.name, title: null }],
    ...over,
  };
}

const CATALOG = (admin = true): CatalogResponse => ({ models: [MODEL], recipes: [LLAMA, CUSTOM], can_manage: admin, can_deploy: admin });

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: Record<string, unknown> = OWNER) {
  const admin = user.role === "admin";
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? (url === "/api/models" ? { status: 200, body: CATALOG(admin) }
        : url === "/api/machines" ? { status: 200, body: fixture }
          : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: admin } }
            : url === "/api/downloads" ? { status: 200, body: { downloads: [], can_download: admin } }
              : url === "/api/routes" ? { status: 200, body: { routes: [], cloud: "off", is_admin: admin } }
                : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search}</div>;
}
const where = () => screen.getByTestId("where").textContent;

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const sectionOrder = () => Array.from(document.querySelectorAll("section.lp-sec")).map((s) => s.id);
const jumpLinks = () => Array.from(screen.getByRole("navigation", { name: "On this page" }).querySelectorAll("a")).map((a) => a.textContent);

describe("model and engine pages (1.4.3, #140)", () => {
  it("FY-WEB-470 the helpers: addresses, kinds, pins, the verdict per machine, the files line, presence and a stale listing", () => {
    expect(modelHref("qwen3-1.7b-gguf")).toBe("/models/qwen3-1.7b-gguf");
    expect(engineHref("llama cpp")).toBe("/models/engines/llama%20cpp");
    expect(catalogKind({ task: "chat", modalities: ["image"] })).toBe("multimodal");
    expect(catalogKind({ task: "segment", modalities: [] })).toBe("vision");
    expect(catalogKind({ task: "embed", modalities: null })).toBe("embed");
    expect(pinned({ runtime: "container", image: LLAMA.variants[0].image })).toBe("digest");
    expect(pinned({ runtime: "container", image: "acme/x:1" })).toBe("tag");
    expect(pinned({ runtime: "process", image: null })).toBeNull();
    expect(digestOf(LLAMA.variants[0].image)).toBe("sha256:a8cf841e");
    const fit = modelPage().fits["llama-cpp"];
    // A GPU that fits wins; else the CPU when it fits; else the GPU's reason; else the CPU's.
    expect(machineFit(fit, 1)?.device).toBe("gpu");
    expect(machineFit(fit, 2)).toMatchObject({ device: "cpu", verdict: { fits: true } });
    expect(machineFit({ gpu: fit.gpu, cpu: { machines: [verdict(2, "strix", false)] } }, 2)?.device).toBe("gpu");
    expect(machineFit({ cpu: fit.cpu }, 1)?.device).toBe("cpu");
    expect(machineFit(undefined, 1)).toBeNull();
    expect(filesLine(modelPage().files, MODEL.file)).toMatchObject({ count: 1, others: 1, used: { path: "Qwen3-1.7B-Q4_K_M.gguf" } });
    expect(presenceText("image", true)).toBe("image here");
    expect(presenceText("program", false)).toMatch(/^program not here/);
    expect(presenceText("image", null)).toBe("image: not known yet");
    expect(presenceText("command", null)).toBe("installed command");
    expect(whereText("image", false)).toBe("pulled when a deployment starts there");
    expect(whereText("program", true)).toBe("in its model cache");
    expect(whereText("image", null)).toBe("not listed yet");
    const now = Date.parse("2026-11-18T12:00:00Z");
    const row = enginePage(LLAMA).machines[0];
    expect(stale({ machines: [{ ...row, listed_at: "2026-11-18T11:55:00+00:00" }] }, now)).toBe(false);
    expect(stale({ machines: [{ ...row, listed_at: "2026-11-18T11:00:00+00:00" }] }, now)).toBe(true);
    expect(stale({ machines: [{ ...row, listed_at: null }] }, now)).toBe(true);
    expect(stale({ machines: [{ ...row, listed_at: null, can_list: false }] }, now)).toBe(false);
  });

  it("FY-WEB-471 /models/<name> opens a model's page and /models/engines/<name> an engine's; the old addresses beside them still work", async () => {
    server((url) => url === "/api/models/qwen3-1.7b-gguf" ? { status: 200, body: modelPage() }
      : url === "/api/recipes/llama-cpp" ? { status: 200, body: enginePage(LLAMA) } : undefined);
    mount("/models/qwen3-1.7b-gguf");
    expect(await screen.findByRole("heading", { level: 1, name: "qwen3-1.7b-gguf" })).toBeTruthy();
    cleanup();
    mount("/models/engines/llama-cpp");
    expect(await screen.findByRole("heading", { level: 1, name: "llama-cpp" })).toBeTruthy();
    cleanup();
    mount("/models/deploy?model=qwen3-1.7b-gguf");
    await waitFor(() => expect(where()).toBe("/serving/deploy?model=qwen3-1.7b-gguf"));
    cleanup();
    mount("/models/routes/5");
    await waitFor(() => expect(where()).toBe("/cloud/models/5"));
    cleanup();
    mount("/models/engines");
    await waitFor(() => expect(where()).toBe("/models?tab=engines"));
    cleanup();
    server((url) => url === "/api/models/gone" ? { status: 404, body: { detail: { code: "not_found", message: "No model 'gone' in the catalog." } } } : undefined);
    mount("/models/gone");
    expect(await screen.findByText("No model 'gone' in the catalog.")).toBeTruthy();
  });

  it("FY-WEB-472 a model's page: header, facts, jump links, Machines right after Overview, Files folded to one line with Show / Hide", async () => {
    server((url) => url === "/api/models/qwen3-1.7b-gguf" ? { status: 200, body: modelPage() } : undefined);
    mount("/models/qwen3-1.7b-gguf");
    await screen.findByRole("heading", { level: 1, name: "qwen3-1.7b-gguf" });
    // No display name: the name is the heading, and the line under it is the source only.
    expect(document.querySelector(".lp-sub")!.textContent).toBe("ggml-org/Qwen3-1.7B-GGUF · Q4_K_M");
    expect(document.querySelector(".lp-header .eyebrow")!.textContent).toBe("Model · Chat");
    expect(jumpLinks()).toEqual(["Overview", "Machines", "Deployments", "Engines", "Files"]);
    expect(sectionOrder()).toEqual(["overview", "machines", "deployments", "engines", "files"]);
    expect(document.querySelector(".mk-facts")!.textContent).toContain("needs 3.00 GiB per GPU with llama-cpp's defaults");
    expect(screen.getByRole("link", { name: /Hugging Face/ }).getAttribute("href")).toBe("https://huggingface.co/ggml-org/Qwen3-1.7B-GGUF");
    // Machines: the cache, whether it fits (on a GPU, or on the CPU), the engine there.
    const machines = screen.getByRole("table");
    const a100 = within(machines).getByText("a100-box").closest("tr")!;
    expect(within(a100).getByText("in cache")).toBeTruthy();
    expect(within(a100).getByText("used by chat-small")).toBeTruthy();
    expect(within(a100).getByText("fits")).toBeTruthy();
    expect(within(a100).getByText(/on GPU #4/)).toBeTruthy();
    expect(within(a100).getByText("image here")).toBeTruthy();
    expect(within(a100).getByRole("link", { name: "Deploy here" }).getAttribute("href")).toBe("/serving/deploy?model=qwen3-1.7b-gguf&recipe=llama-cpp&machine=1");
    const strix = within(machines).getByText("strix").closest("tr")!;
    expect(within(strix).getByText("would fit on the CPU")).toBeTruthy();
    expect(within(strix).getByText("not cached")).toBeTruthy();
    expect(within(strix).queryByRole("link", { name: "Deploy here" })).toBeNull();
    expect(within(strix).queryByRole("button", { name: "Download here" })).toBeNull();
    // The other engine's verdicts.
    fireEvent.click(screen.getByRole("radio", { name: "llama-cpp-vulkan" }));
    await waitFor(() => expect(within(a100).getByText("does not fit")).toBeTruthy());
    expect(within(strix).getByText("image: not known yet")).toBeTruthy();
    // Engines link to their pages.
    const engines = document.getElementById("engines")!;
    expect(within(engines).getAllByRole("link").map((l) => l.getAttribute("href"))).toEqual(["/models/engines/llama-cpp", "/models/engines/llama-cpp-vulkan"]);
    // Files: one line, then the list.
    const files = document.getElementById("files")!;
    expect(files.textContent).toContain("1 file downloaded");
    expect(files.textContent).toContain("1 other file in the repository, not downloaded");
    expect(within(files).queryByRole("table")).toBeNull();
    fireEvent.click(within(files).getByRole("button", { name: "Show files" }));
    expect(within(files).getByText("Qwen3-1.7B-Q8_0.gguf")).toBeTruthy();
    expect(within(files).getByText("other quantisation, not downloaded")).toBeTruthy();
    fireEvent.click(within(files).getByRole("button", { name: "Hide files" }));
    expect(within(files).queryByRole("table")).toBeNull();
  });

  it("FY-WEB-473 an admin sets the display name (the name stays); a plain user sees no Display name, Deploy, Download to… or Remove", async () => {
    let title: string | null = null;
    const fetch = server((url, init) => {
      if (url === "/api/models/7" && init?.method === "PATCH") {
        title = (JSON.parse(String(init.body)) as { title: string }).title || null;
        return { status: 200, body: { model: { ...MODEL, title } } };
      }
      return url === "/api/models/qwen3-1.7b-gguf" ? { status: 200, body: modelPage({ model: { ...MODEL, title } }) } : undefined;
    });
    mount("/models/qwen3-1.7b-gguf");
    fireEvent.click(await screen.findByRole("button", { name: "Display name" }));
    const input = screen.getByRole("textbox", { name: "Display name" }) as HTMLInputElement;
    expect(input.placeholder).toBe("qwen3-1.7b-gguf");
    fireEvent.change(input, { target: { value: "  Qwen3 1.7B  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("qwen3-1.7b-gguf is shown as “Qwen3 1.7B”.")).toBeTruthy();
    expect(await screen.findByRole("heading", { level: 1, name: "Qwen3 1.7B" })).toBeTruthy();
    expect(fetch.mock.calls.find(([u, i]) => u === "/api/models/7" && i?.method === "PATCH")?.[1]?.body).toBe(JSON.stringify({ title: "Qwen3 1.7B" }));
    expect(screen.getByText(/^qwen3-1\.7b-gguf · /)).toBeTruthy();
    cleanup();
    server((url) => url === "/api/models/qwen3-1.7b-gguf" ? { status: 200, body: modelPage({}, false) } : undefined, USER);
    mount("/models/qwen3-1.7b-gguf");
    await screen.findByRole("heading", { level: 1, name: "qwen3-1.7b-gguf" });
    for (const name of ["Display name", "Remove", "Download to…"]) expect(screen.queryByRole("button", { name })).toBeNull();
    expect(screen.queryByRole("link", { name: "Deploy" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Deploy here" })).toBeNull();
  });

  it("FY-WEB-474 Remove asks, deletes the model and goes back to the catalog; the server's refusal is shown", async () => {
    let answer: Reply = { status: 409, body: { detail: { code: "model_in_use", message: "qwen3-1.7b-gguf is deployed as chat. Delete those deployments first." } } };
    const fetch = server((url, init) => url === "/api/models/7" && init?.method === "DELETE" ? answer
      : url === "/api/models/qwen3-1.7b-gguf" ? { status: 200, body: modelPage() } : undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/models/qwen3-1.7b-gguf");
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(await screen.findByText(/is deployed as chat/)).toBeTruthy();
    answer = { status: 200, body: { ok: true } };
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(where()).toBe("/models"));
    expect(fetch.mock.calls.filter(([u, i]) => u === "/api/models/7" && i?.method === "DELETE")).toHaveLength(2);
  });

  it("FY-WEB-475 an engine's page: jump links, Variants then Machines, Parameters folded, the image or program per machine", async () => {
    server((url) => url === "/api/recipes/llama-cpp" ? { status: 200, body: enginePage(LLAMA) } : undefined);
    mount("/models/engines/llama-cpp");
    await screen.findByRole("heading", { level: 1, name: "llama-cpp" });
    expect(document.querySelector(".lp-header .eyebrow")!.textContent).toBe("Engine · Built in");
    expect(jumpLinks()).toEqual(["Overview", "Trials", "Variants", "Newer versions", "Machines", "Parameters", "Deployments and models"]);
    expect(sectionOrder()).toEqual(["overview", "trials", "variants", "updates", "machines", "params", "deployments", "models"]);
    const variants = document.getElementById("variants")!;
    expect(within(variants).getByText("by digest")).toBeTruthy();
    expect(within(variants).getByText("sha256:a8cf841e…")).toBeTruthy();
    expect(within(variants).getByText("release archive (SHA-256)")).toBeTruthy();
    const machines = document.getElementById("machines")!;
    const a100 = within(machines).getByText("a100-box").closest("tr")!;
    expect(within(a100).getByText("here")).toBeTruthy();
    expect(within(a100).getByText(/^in its container engine · listed /)).toBeTruthy();
    const strix = within(machines).getByText("strix").closest("tr")!;
    expect(within(strix).getByText("not here")).toBeTruthy();
    expect(within(strix).getByText("downloaded when a deployment starts there")).toBeTruthy();
    const params = document.getElementById("params")!;
    expect(params.textContent).toContain("2 parameters · 1 advanced");
    expect(within(params).queryByRole("table")).toBeNull();
    fireEvent.click(within(params).getByRole("button", { name: "Show parameters" }));
    expect(within(params).getByText("Context length")).toBeTruthy();
    expect(within(params).getByText("f16 · q8_0")).toBeTruthy();
    const models = document.getElementById("models")!;
    await waitFor(() => expect(within(models).getByRole("link").getAttribute("href")).toBe("/models/qwen3-1.7b-gguf"));
    // A built-in engine: Copy as a custom engine, no Edit or Delete.
    expect(screen.getByRole("button", { name: "Copy as a custom engine" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("FY-WEB-476 a listing missing or older than 10 minutes is asked again once (?refresh=true), and List again asks again", async () => {
    const old = enginePage(LLAMA, { machines: [{ ...enginePage(LLAMA).machines[0], listed_at: "2026-11-01T00:00:00+00:00", present: false }] });
    const fresh = enginePage(LLAMA);
    const fetch = server((url) => url === "/api/recipes/llama-cpp" ? { status: 200, body: old }
      : url === "/api/recipes/llama-cpp?refresh=true" ? { status: 200, body: fresh } : undefined);
    mount("/models/engines/llama-cpp");
    await screen.findByRole("heading", { level: 1, name: "llama-cpp" });
    const machines = document.getElementById("machines")!;
    await waitFor(() => expect(within(machines).getByText("here")).toBeTruthy());
    const asked = () => fetch.mock.calls.filter(([u]) => u === "/api/recipes/llama-cpp?refresh=true").length;
    expect(asked()).toBe(1);
    fireEvent.click(within(machines).getByRole("button", { name: "List again" }));
    await waitFor(() => expect(asked()).toBe(2));
    cleanup();
    // A fresh listing is not asked again.
    const f2 = server((url) => url === "/api/recipes/llama-cpp" ? { status: 200, body: fresh } : undefined);
    mount("/models/engines/llama-cpp");
    await screen.findByRole("heading", { level: 1, name: "llama-cpp" });
    await new Promise((r) => setTimeout(r, 30));
    expect(f2.mock.calls.filter(([u]) => String(u).includes("refresh")).length).toBe(0);
  });

  it("FY-WEB-477 Copy as a custom engine opens the form on a renamed copy and opens the new engine's page; a custom engine has Edit and Delete", async () => {
    const posts: unknown[] = [];
    server((url, init) => {
      if (url === "/api/recipes" && init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return { status: 201, body: { recipe: { ...LLAMA, name: "llama-cpp-custom", builtin: false } } };
      }
      if (url === "/api/recipes/llama-cpp-vulkan" && init?.method === "DELETE") return { status: 200, body: { ok: true } };
      return url === "/api/recipes/llama-cpp" ? { status: 200, body: enginePage(LLAMA) }
        : url === "/api/recipes/llama-cpp-custom" ? { status: 200, body: enginePage({ ...LLAMA, name: "llama-cpp-custom", builtin: false }) }
          : url === "/api/recipes/llama-cpp-vulkan" ? { status: 200, body: enginePage(CUSTOM) } : undefined;
    });
    mount("/models/engines/llama-cpp");
    fireEvent.click(await screen.findByRole("button", { name: "Copy as a custom engine" }));
    const yaml = await screen.findByLabelText("Engine recipe (YAML)") as HTMLTextAreaElement;
    await waitFor(() => expect(yaml.value).toContain("name: llama-cpp-custom"));
    fireEvent.click(screen.getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(where()).toBe("/models/engines/llama-cpp-custom"));
    expect(posts).toHaveLength(1);
    expect(await screen.findByRole("heading", { level: 1, name: "llama-cpp-custom" })).toBeTruthy();
    cleanup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/models/engines/llama-cpp-vulkan");
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("heading", { name: "Change llama-cpp-vulkan" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(where()).toBe("/models?tab=engines"));
  });

  it("FY-WEB-478 the catalog's names, its Engines column, the engine cards and a deployment's row link to the pages", async () => {
    server(() => undefined);
    mount("/models");
    const row = (await screen.findByRole("link", { name: "qwen3-1.7b-gguf" })).closest("tr")!;
    expect(within(row).getByRole("link", { name: "qwen3-1.7b-gguf" }).getAttribute("href")).toBe("/models/qwen3-1.7b-gguf");
    expect(within(row).getByRole("link", { name: "llama-cpp-vulkan" }).getAttribute("href")).toBe("/models/engines/llama-cpp-vulkan");
    cleanup();
    server(() => undefined);
    mount("/models?tab=engines");
    const card = await screen.findByRole("article", { name: "Engine llama-cpp" });
    expect(within(card).getByRole("link", { name: "llama-cpp" }).getAttribute("href")).toBe("/models/engines/llama-cpp");
  });

  it("FY-WEB-503 a zero-shot detector's page and catalog row say it finds what you name instead of its classes (1.4.4, #142)", async () => {
    const zs = { ...MODEL, name: "gdino", task: "detect", task_label: "Object detection", format: "safetensors", file: null, recipes: ["vision"],
      summary: { architecture: "GroundingDinoForObjectDetection" }, vision: { task: "detect", architecture: "GroundingDinoForObjectDetection", zero_shot: true } } as CatalogModel;
    const rt = { ...zs, id: 8, name: "rtdetr", summary: { architecture: "RtDetrV2ForObjectDetection" },
      vision: { task: "detect", architecture: "RtDetrV2ForObjectDetection", labels: 80, label_examples: ["person", "bicycle"] } } as CatalogModel;
    server((url) => url === "/api/models/gdino" ? { status: 200, body: modelPage({ model: zs, fits: {} }) }
      : url === "/api/models" ? { status: 200, body: { can_manage: true, can_deploy: true, models: [zs, rt], recipes: [],
        sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } } as CatalogResponse } : undefined);
    mount("/models/gdino");
    expect(await screen.findByText("Finds what you name")).toBeTruthy();
    expect(screen.getByText(/each request lists what to find/)).toBeTruthy();
    cleanup();
    mount("/models");
    expect(await screen.findByText("Finds what you name")).toBeTruthy();
    expect(screen.getByText("80 classes")).toBeTruthy();
  });
});

