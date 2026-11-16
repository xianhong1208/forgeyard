// @vitest-environment jsdom
// 1.4 (#92): Models → Engines: short cards with the variants behind Details, Add a custom engine (from a copy of a
// built-in one), Edit and Delete of custom engines; and the Catalog's short source line. FY-WEB-340 to 344.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { engineHardware, freeName, renameRecipe } from "../lib/engines";
import { quantTag, shortSource } from "../lib/models";
import type { CatalogResponse, Recipe } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const OPERATOR = { ...OWNER, id: 2, username: "olga", role: "operator", is_owner: false };
type Reply = { status: number; body?: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: Record<string, unknown> = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } } : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const variant = (name: string, vendor: "nvidia" | "amd" | "cpu", image: string | null, extra: Record<string, unknown> = {}) => ({
  name, vendor, runtime: image ? "container" as const : "process" as const, image, arch: null, compute_capability: null, driver: null,
  engine: image ? null : "https://example.org/sd.zip", ...extra,
});
const recipe = (over: Partial<Recipe>): Recipe => ({
  name: "speaches-tts", title: "speaches", description: "Small English voices (Kokoro-82M, Piper) behind an OpenAI-compatible /v1/audio/speech.",
  engine: "speaches", version: "0.8.3", tasks: ["tts"], formats: ["onnx"], api: { tts: "openai-speech" }, share: "vram", builtin: true,
  estimate: "files", devices: ["gpu", "cpu"], params: {},
  variants: [variant("cuda", "nvidia", "ghcr.io/speaches-ai/speaches:0.8.3-cuda", { compute_capability: ">=5.0", driver: ">=575" }),
    variant("cpu", "cpu", "ghcr.io/speaches-ai/speaches:0.8.3-cpu")],
  ...over,
} as Recipe);
const BUILTIN = [recipe({}), recipe({ name: "llama-cpp", engine: "llama.cpp", version: "1.0.0", tasks: ["chat", "tts"], share: "exclusive",
  variants: [variant("cuda", "nvidia", "ghcr.io/ggml-org/llama.cpp:server-cuda"), variant("rocm", "amd", "ghcr.io/ggml-org/llama.cpp:server-rocm",
    { arch: ["gfx90a", "gfx1100"] }), variant("cpu", "cpu", null)] })];
const CUSTOM = recipe({ name: "llama-cpp-custom", engine: "llama.cpp", version: "1.0.0", tasks: ["chat"], builtin: false, share: "exclusive",
  variants: [variant("vulkan", "amd", "ghcr.io/ggml-org/llama.cpp:server-vulkan")] });
const catalog = (recipes: Recipe[], admin = true): CatalogResponse => ({ models: [], recipes, can_manage: admin, can_deploy: true,
  sources: admin ? { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } : undefined });
const LLAMA_YAML = "# llama.cpp\nname: llama-cpp\ntitle: llama.cpp\nengine: llama.cpp\nvariants: []\n";

describe("Models → Engines (1.4, #92)", () => {
  it("FY-WEB-340 a card says the name, version, what it is, the hardware and what it serves; the variants are behind Details", async () => {
    server((url) => url === "/api/models" ? { status: 200, body: catalog(BUILTIN) } : undefined);
    mount("/models?tab=engines");
    const card = await screen.findByRole("article", { name: "Engine speaches-tts" });
    expect(within(card).getByText("speaches 0.8.3")).toBeTruthy();
    expect(within(card).getByText(/Small English voices/).getAttribute("title")).toMatch(/^Small English voices/);
    const facts = within(card).getByLabelText("Runs on: NVIDIA, CPU");
    expect(within(facts).getAllByText(/NVIDIA|CPU/).map((e) => e.textContent)).toEqual(["NVIDIA", "CPU"]);
    expect(within(facts).getByText("shares a GPU")).toBeTruthy();
    expect(within(card).getByText("Serves Text to speech")).toBeTruthy();
    const details = card.querySelector("details.engine-details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    const variants = within(card).getByRole("list", { name: "Variants of speaches-tts" });
    expect(details.contains(variants)).toBe(true);
    expect(within(variants).getByText("ghcr.io/speaches-ai/speaches:0.8.3-cuda")).toBeTruthy();
    expect(within(variants).getByText("cc >=5.0 · driver >=575")).toBeTruthy();
    fireEvent.click(within(card).getByText("Details"));
    await waitFor(() => expect(details.open).toBe(true));
    // AMD from a ROCm variant; an engine that does not share a GPU says nothing about it.
    const llama = screen.getByRole("article", { name: "Engine llama-cpp" });
    expect(within(llama).getByLabelText("Runs on: NVIDIA, AMD, CPU")).toBeTruthy();
    expect(within(llama).queryByText("shares a GPU")).toBeNull();
    expect(within(llama).getByText("Serves Chat, Text to speech")).toBeTruthy();
    // Built-in engines cannot be changed here.
    expect(within(card).queryByRole("button", { name: "Edit" })).toBeNull();
    expect(within(card).queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("FY-WEB-341 Add a custom engine starts from a copy of a built-in one under a free name, shows the server's refusal and adds it", async () => {
    const posts: unknown[] = [];
    let answer: Reply = { status: 422, body: { detail: { code: "invalid_recipe", message: "variants: at least one variant is needed" } } };
    const fetch = server((url, init) => {
      if (url === "/api/recipes" && init?.method === "POST") { posts.push(JSON.parse(String(init.body))); return answer; }
      if (url === "/api/recipes/llama-cpp") return { status: 200, body: { recipe: { ...BUILTIN[1], yaml: LLAMA_YAML } } };
      if (url === "/api/models") return { status: 200, body: catalog([...BUILTIN, CUSTOM]) };
      return undefined;
    });
    mount("/models?tab=engines");
    fireEvent.click(await screen.findByRole("button", { name: "Add a custom engine" }));
    const form = screen.getByRole("region", { name: "Add a custom engine" });
    expect(within(form).getByText(/^Define an engine with your own image and arguments — for example a newer version, copied from a built-in engine\. Built-in engines are updated with Forgeyard releases\.$/)).toBeTruthy();
    const yaml = within(form).getByLabelText("Engine recipe (YAML)") as HTMLTextAreaElement;
    expect((within(form).getByRole("button", { name: "Add engine" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(form).getByLabelText("Start from a built-in engine"), { target: { value: "llama-cpp" } });
    // llama-cpp-custom is taken: the copy is llama-cpp-custom-2.
    await waitFor(() => expect(yaml.value).toBe("# llama.cpp\nname: llama-cpp-custom-2\ntitle: llama.cpp\nengine: llama.cpp\nvariants: []\n"));
    await act(async () => fireEvent.click(within(form).getByRole("button", { name: "Add engine" })));
    expect(await within(form).findByRole("alert")).toBeTruthy();
    expect(within(form).getByRole("alert").textContent).toContain("variants: at least one variant is needed");
    expect(posts).toEqual([{ yaml: yaml.value }]);
    answer = { status: 201, body: { recipe: { ...CUSTOM, name: "llama-cpp-custom-2" } } };
    await act(async () => fireEvent.click(within(form).getByRole("button", { name: "Add engine" })));
    expect(await screen.findByText("Engine llama-cpp-custom-2 added.")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Add a custom engine" })).toBeNull();
    expect(fetch.mock.calls.filter(([u]) => u === "/api/models").length).toBeGreaterThan(1);  // reloaded
  });

  it("FY-WEB-342 a custom engine has Edit (its YAML, saved with PUT) and Delete (asks; a refusal is said on the card); operators get none of it", async () => {
    const puts: unknown[] = [];
    server((url, init) => {
      if (url === "/api/recipes/llama-cpp-custom" && init?.method === "PUT") { puts.push(JSON.parse(String(init.body))); return { status: 200, body: { recipe: CUSTOM } }; }
      if (url === "/api/recipes/llama-cpp-custom" && init?.method === "DELETE")
        return { status: 409, body: { detail: { code: "recipe_in_use", message: "llama-cpp-custom is used by chat. Delete those deployments first." } } };
      if (url === "/api/recipes/llama-cpp-custom") return { status: 200, body: { recipe: { ...CUSTOM, yaml: "name: llama-cpp-custom\n" } } };
      if (url === "/api/models") return { status: 200, body: catalog([...BUILTIN, CUSTOM]) };
      return undefined;
    });
    vi.stubGlobal("confirm", vi.fn(() => true));
    const view = mount("/models?tab=engines");
    const card = await screen.findByRole("article", { name: "Engine llama-cpp-custom" });
    expect(within(card).getByText("custom")).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("region", { name: "Change llama-cpp-custom" });
    expect(within(form).queryByLabelText("Start from a built-in engine")).toBeNull();
    const yaml = within(form).getByLabelText("Engine recipe (YAML)") as HTMLTextAreaElement;
    await waitFor(() => expect(yaml.value).toBe("name: llama-cpp-custom\n"));
    fireEvent.change(yaml, { target: { value: "name: llama-cpp-custom\nversion: \"1.0.1\"\n" } });
    await act(async () => fireEvent.click(within(form).getByRole("button", { name: "Save changes" })));
    expect(await screen.findByText("Engine llama-cpp-custom saved.")).toBeTruthy();
    expect(puts).toEqual([{ yaml: "name: llama-cpp-custom\nversion: \"1.0.1\"\n" }]);
    const again = screen.getByRole("article", { name: "Engine llama-cpp-custom" });
    await act(async () => fireEvent.click(within(again).getByRole("button", { name: "Delete" })));
    expect((await within(again).findByRole("alert")).textContent).toContain("is used by chat");
    view.unmount();
    cleanup();
    vi.unstubAllGlobals();
    server((url) => url === "/api/models" ? { status: 200, body: catalog([...BUILTIN, CUSTOM], false) } : undefined, OPERATOR);
    mount("/models?tab=engines");
    const theirs = await screen.findByRole("article", { name: "Engine llama-cpp-custom" });
    expect(screen.queryByRole("button", { name: "Add a custom engine" })).toBeNull();
    expect(within(theirs).queryByRole("button", { name: "Edit" })).toBeNull();
    expect(within(theirs).queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("FY-WEB-343 helpers: hardware from the variants, a copy's new name, a free name", () => {
    expect(engineHardware(recipe({ variants: [variant("c", "cpu", null), variant("r", "amd", "x"), variant("n", "nvidia", "y"), variant("n2", "nvidia", "z")] })))
      .toEqual(["nvidia", "amd", "cpu"]);
    expect(renameRecipe("# c\nname: vllm\ntitle: x\n  name: inner\n", "vllm-2")).toBe("# c\nname: vllm-2\ntitle: x\n  name: inner\n");
    expect(renameRecipe("title: x\n", "mine")).toBe("name: mine\ntitle: x\n");
    expect(freeName("a", ["b"])).toBe("a");
    expect(freeName("a", ["a", "a-2"])).toBe("a-3");
  });
});

describe("the catalog's source line (1.4, #92)", () => {
  it("FY-WEB-344 a GGUF file is named by its quantisation; the full source is the tooltip", () => {
    for (const [file, tag] of [
      ["Qwen3-1.7B-Q4_K_M.gguf", "Q4_K_M"],
      ["stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf", "Q8_0"],
      ["z_image_turbo-Q8_0.gguf", "Q8_0"],
      ["Qwen3-235B-A22B-Q4_K_M-00001-of-00003.gguf", "Q4_K_M"],
      ["Q4_K_M/Qwen3-235B-A22B-Q4_K_M-00002-of-00003.gguf", "Q4_K_M"],
      ["gemma-3-4b-it-BF16.gguf", "BF16"],
      ["ggml-model-f16.gguf", "F16"],
      ["Llama-3.2-1B-Instruct-IQ4_XS.gguf", "IQ4_XS"],
      ["chatterbox-turbo-q4_k_m.gguf", "Q4_K_M"],
      ["model_q2_k.gguf", "Q2_K"],
      ["Qwen2.5-Omni-3B-Q4_0.gguf", "Q4_0"],
      ["gpt-oss-20b-MXFP4.gguf", "MXFP4"],
      ["tiny.gguf", "tiny"],
      ["a-very-long-model-file-name-without-any-quant-tag.gguf", "a-very-long-model-file-name…"],
    ]) expect([file, quantTag(file)]).toEqual([file, tag]);
    const hf = shortSource({ kind: "hf", source: "hf://second-state/stable-diffusion-v1-5-GGUF", file: "stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
      revision: "031b5f5df991f511b3f5fa8fed6d99048ababb69", mmproj: null });
    expect(hf).toEqual({ text: "second-state/stable-diffusion-v1-5-GGUF · Q8_0",
      full: "hf://second-state/stable-diffusion-v1-5-GGUF · stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf @ 031b5f5df991f511b3f5fa8fed6d99048ababb69" });
    expect(shortSource({ kind: "hf", source: "hf://Qwen/Qwen3-4B", file: null, revision: "1cfa", mmproj: null }).text).toBe("Qwen/Qwen3-4B");
    expect(shortSource({ kind: "hf", source: "hf://Qwen/Qwen3-VL-2B-Instruct-GGUF", file: "Qwen3VL-2B-Instruct-Q4_K_M.gguf", revision: null,
      mmproj: { path: "mmproj-Qwen3VL-2B-Instruct-F16.gguf", size: 1 } }).full).toContain("+ mmproj-Qwen3VL-2B-Instruct-F16.gguf");
    expect(shortSource({ kind: "url", source: "https://mirror.example.org/models/q/Qwen3-1.7B-Q4_K_M.gguf", file: null, revision: null, mmproj: null }).text)
      .toBe("mirror.example.org · Qwen3-1.7B-Q4_K_M.gguf");
    expect(shortSource({ kind: "local", source: "local:///srv/models/my-model/", file: "weights/model-Q8_0.gguf", revision: null, mmproj: null }).text)
      .toBe("my-model · model-Q8_0.gguf");
    expect(shortSource({ kind: "local", source: "local:///srv/models/my-model", file: null, revision: null, mmproj: null }).text).toBe("my-model");
  });
});
