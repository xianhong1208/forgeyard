// @vitest-environment jsdom
// 1.5.2 (#95): newer versions of the built-in engines: the check on Models → Engines (admins), the badges on the
// engine cards, the Newer versions section of an engine's page (variants covered or left out and why, release notes,
// flag mentions), both languages, and the server's reasons in the reader's language. FY-WEB-842 to 847.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { whyText } from "../components/EngineUpdates";
import { makeTranslator } from "../i18n/react";
import { LANG_KEY } from "../i18n/lang";
import type { CatalogResponse, EnginePage, EngineUpdatesResponse, EngineUpdatesResult, Recipe } from "../lib/types";

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
  localStorage.clear();
  vi.unstubAllGlobals();
});

const variant = (name: string, vendor: "nvidia" | "amd" | "cpu", image: string | null) => ({
  name, vendor, runtime: image ? "container" as const : "process" as const, image, arch: null, compute_capability: null, driver: null,
  engine: image ? null : "https://github.com/x/y/releases/download/b1/a.tar.gz",
});
const recipe = (name: string, engine: string, version: string, builtin = true): Recipe => ({
  name, title: engine, description: "", engine, version, tasks: ["chat"], formats: ["safetensors"], api: { chat: "openai-chat" },
  share: "exclusive", builtin, estimate: "safetensors-kv", devices: ["gpu"], params: {},
  variants: [variant("cuda", "nvidia", `vllm/vllm-openai:v${version}`), variant("cuda-12.9", "nvidia", `vllm/vllm-openai:v${version}-cu129`)],
} as Recipe);
const RECIPES = [recipe("vllm", "vllm", "0.32.0"), recipe("sd-cpp", "stable-diffusion.cpp", "master-1088"),
  recipe("llama-cpp", "llama.cpp", "1.0.0"), recipe("speaches-tts", "speaches", "0.8.3"), recipe("vision", "forgeyard-vision", "1.4"),
  recipe("vllm-custom", "vllm", "0.32.0", false)];
const catalog = (admin = true): CatalogResponse => ({ models: [], recipes: RECIPES, can_manage: admin, can_deploy: true,
  sources: admin ? { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } : undefined });

const DIGEST = "sha256:" + "ab".repeat(32);
const RESULT: EngineUpdatesResult = {
  checked_at: new Date(Date.now() - 3 * 60_000).toISOString(), checked_by: "owner", seconds: 21.4, github_requests: 5,
  engines: {
    vllm: { recipe: "vllm", title: "vLLM", engine: "vllm", version: "0.32.0", kind: "release", status: "newer", newest: "0.34.0", errors: [],
      candidates: [
        { version: "0.34.0", kind: "release", release: { tag: "v0.34.0", name: "v0.34.0", url: "https://github.com/vllm-project/vllm/releases/tag/v0.34.0", published_at: "2026-11-16T23:22:00Z" },
          variants: [
            { name: "cuda", runtime: "container", vendor: "nvidia", available: true, tag: "v0.34.0", digest: DIGEST, image: `vllm/vllm-openai:v0.34.0@${DIGEST}` },
            { name: "cuda-12.9", runtime: "container", vendor: "nvidia", available: false,
              why: { code: "no_image", text: "No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.", tag: "v0.34.0-cu129", repo: "docker.io/vllm/vllm-openai", last: "0.32.0" } },
          ],
          mentions: [{ flag: "--gpu-memory-utilization", lines: 5, excerpts: ["In most cases it's now best to not set `gpu_memory_utilization`."] }] },
        { version: "0.33.0", kind: "release", release: { tag: "v0.33.0rc3", name: "v0.33.0", url: "https://github.com/vllm-project/vllm/releases/tag/v0.33.0rc3", published_at: "2026-11-02T10:10:05Z" },
          variants: [{ name: "cuda", runtime: "container", vendor: "nvidia", available: true, tag: "v0.33.0", digest: DIGEST }], mentions: [] },
      ] },
    "sd-cpp": { recipe: "sd-cpp", title: "stable-diffusion.cpp", engine: "stable-diffusion.cpp", version: "master-1088", kind: "rolling", status: "newer_build", errors: [],
      candidates: [{ version: "master-1117-0d71f96", kind: "build", created: "2026-11-22T18:45:07Z", variants: [
        { name: "cuda", runtime: "container", vendor: "nvidia", available: true, tag: "master-cuda", digest: DIGEST },
        { name: "cpu-process", runtime: "process", vendor: "cpu", available: false, why: { code: "no_archive", text: "Release master-1117-0d71f96 has no sd-master-0d71f96-bin-Linux-Ubuntu-24.04-x86_64.zip, the archive the built-in uses; it has sd-master-0d71f96-bin-Linux-Ubuntu-26.04-x86_64.zip, built for another system.",
          release: "master-1117-0d71f96", archive: "sd-master-0d71f96-bin-Linux-Ubuntu-24.04-x86_64.zip", similar: ["sd-master-0d71f96-bin-Linux-Ubuntu-26.04-x86_64.zip"] } }] }] },
    "llama-cpp": { recipe: "llama-cpp", title: "llama.cpp", engine: "llama.cpp", version: "1.0.0", kind: "release", status: "up_to_date", newest: "1.0.0", errors: [], candidates: [] },
    "speaches-tts": { recipe: "speaches-tts", title: "speaches", engine: "speaches", version: "0.8.3", kind: "release", status: "could_not_check", candidates: [],
      errors: [{ where: "ghcr.io/speaches-ai/speaches", text: "ghcr.io: timed out" }] },
    vision: { recipe: "vision", title: "Vision", engine: "forgeyard-vision", version: "1.4", kind: "own", status: "not_checked", candidates: [], errors: [],
      why: { code: "own_image", text: "Forgeyard's own image: it moves with Forgeyard's releases." } },
  },
};

function enginePage(name: string): EnginePage {
  const r = RECIPES.find((x) => x.name === name)!;
  return { recipe: { ...r, yaml: `name: ${name}\n` }, can_manage: true, machines: [], deployments: [], models: [] };
}

function routes(updates: EngineUpdatesResponse, onCheck?: () => Reply) {
  return (url: string, init?: RequestInit): Reply | undefined => {
    if (url === "/api/models") return { status: 200, body: catalog(updates.can_check) };
    if (url === "/api/engine-updates") return { status: 200, body: updates };
    if (url === "/api/engine-updates/check" && init?.method === "POST") return onCheck?.();
    const m = url.match(/^\/api\/recipes\/([^?]+)/);
    if (m) return { status: 200, body: enginePage(decodeURIComponent(m[1])) };
    if (url === "/api/machines") return { status: 200, body: { machines: [] } };
    if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
    return undefined;
  };
}

describe("Newer engine versions (1.5.2, #95)", () => {
  it("FY-WEB-842 an admin checks from Models → Engines; the cards get badges that link to the engine's section", async () => {
    let checks = 0;
    const fetch = server(routes({ result: null, can_check: true }, () => { checks += 1; return { status: 200, body: { result: RESULT, can_check: true } }; }));
    mount("/models?tab=engines");
    expect(await screen.findByText("Not checked for newer versions yet.")).toBeTruthy();
    const vllm = screen.getByRole("article", { name: "Engine vllm" });
    expect(vllm.querySelector(".eu-badge")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Check for newer versions" }));
    expect(await screen.findByText("Checked: 2 with a newer version.")).toBeTruthy();
    expect(checks).toBe(1);
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/engine-updates/check" && (i as RequestInit).method === "POST")).toBe(true);
    const badge = within(vllm).getByRole("link", { name: "0.34.0 available" });
    expect(badge.getAttribute("href")).toBe("/models/engines/vllm#updates");
    expect(badge.getAttribute("title")).toBe("Newer versions: 0.34.0, 0.33.0");
    expect(within(screen.getByRole("article", { name: "Engine sd-cpp" })).getByRole("link", { name: "Newer build" })).toBeTruthy();
    expect(within(screen.getByRole("article", { name: "Engine llama-cpp" })).getByRole("link", { name: "Up to date" })).toBeTruthy();
    const speaches = within(screen.getByRole("article", { name: "Engine speaches-tts" })).getByRole("link", { name: "Could not check" });
    expect(speaches.getAttribute("title")).toBe("ghcr.io/speaches-ai/speaches: ghcr.io: timed out");
    // Not checked: Forgeyard's own image, and custom engines (even one named like a built-in's result).
    expect(screen.getByRole("article", { name: "Engine vision" }).querySelector(".eu-badge")).toBeNull();
    expect(screen.getByRole("article", { name: "Engine vllm-custom" }).querySelector(".eu-badge")).toBeNull();
    expect(screen.getByText(/Last checked .* by owner/)).toBeTruthy();
  });

  it("FY-WEB-843 an operator reads the last result and its badges but has no check button", async () => {
    server(routes({ result: RESULT, can_check: false }), OPERATOR);
    mount("/models?tab=engines");
    expect(await screen.findByText(/Last checked 3 min ago by owner/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Check for newer versions" })).toBeNull();
    await waitFor(() => expect(within(screen.getByRole("article", { name: "Engine vllm" })).getByRole("link", { name: "0.34.0 available" })).toBeTruthy());
    cleanup();
    server(routes({ result: null, can_check: false }), OPERATOR);
    mount("/models?tab=engines");
    expect(await screen.findByText("Not checked for newer versions yet: an admin can check.")).toBeTruthy();
  });

  it("FY-WEB-844 an engine's page lists each newer version: variants included or left out and why, notes, flag mentions", async () => {
    server(routes({ result: RESULT, can_check: true }));
    mount("/models/engines/vllm");
    const sec = (await screen.findByRole("heading", { name: /^Newer versions/ })).closest("section")!;
    await waitFor(() => expect(within(sec).getByRole("article", { name: "vLLM 0.34.0" })).toBeTruthy());
    expect(screen.getByRole("navigation", { name: "On this page" }).textContent).toContain("Newer versions");
    const v34 = within(sec).getByRole("article", { name: "vLLM 0.34.0" });
    expect(within(v34).getByText("1 of 2 variants included")).toBeTruthy();
    expect(within(v34).getByRole("link", { name: /Release notes/ }).getAttribute("href")).toBe("https://github.com/vllm-project/vllm/releases/tag/v0.34.0");
    expect(within(v34).getByText("included")).toBeTruthy();
    expect(within(v34).getByText("left out")).toBeTruthy();
    expect(within(v34).getByText("No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.")).toBeTruthy();
    expect(within(v34).getByText("v0.34.0")).toBeTruthy();
    expect(within(v34).getByText("Its release notes mention flags or settings this engine uses:")).toBeTruthy();
    expect(within(v34).getByText("--gpu-memory-utilization")).toBeTruthy();
    expect(within(v34).getByText("· 5 lines")).toBeTruthy();
    expect(within(v34).getByText(/remove|not set `gpu_memory_utilization`/)).toBeTruthy();
    const v33 = within(sec).getByRole("article", { name: "vLLM 0.33.0" });
    expect(within(v33).getByText("Its release notes mention none of the flags this engine passes.")).toBeTruthy();
    expect(within(sec).getByText(/Forgeyard's estimates were measured on 0.32.0/)).toBeTruthy();
    expect(within(sec).getByRole("button", { name: "Check again" })).toBeTruthy();
  });

  it("FY-WEB-845 up to date, a newer build without its process variant, not checked, and no section on a custom engine", async () => {
    server(routes({ result: RESULT, can_check: true }));
    mount("/models/engines/llama-cpp");
    expect(await screen.findByText("Up to date: 1.0.0 is the newest stable image.")).toBeTruthy();
    cleanup();
    server(routes({ result: RESULT, can_check: true }));
    mount("/models/engines/sd-cpp");
    const build = await screen.findByRole("article", { name: "stable-diffusion.cpp master-1117-0d71f96" });
    expect(within(build).getByText(/^built /)).toBeTruthy();
    expect(within(build).getByText(/has no sd-master-0d71f96-bin-Linux-Ubuntu-24.04-x86_64.zip.*Ubuntu-26.04.*another system/)).toBeTruthy();
    cleanup();
    server(routes({ result: RESULT, can_check: true }));
    mount("/models/engines/vision");
    expect(await screen.findByText("Forgeyard's own image: it moves with Forgeyard's releases.")).toBeTruthy();
    cleanup();
    server(routes({ result: RESULT, can_check: true }));
    mount("/models/engines/vllm-custom");
    await screen.findByRole("heading", { name: "vllm-custom" });
    expect(screen.queryByRole("heading", { name: /^Newer versions/ })).toBeNull();
    expect(screen.getByRole("navigation", { name: "On this page" }).textContent).not.toContain("Newer versions");
  });

  it("FY-WEB-846 in Traditional Chinese: 推論引擎 in the check, the reasons with their values, the server's text for an unknown code", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(routes({ result: null, can_check: true }, () => ({ status: 200, body: { result: RESULT, can_check: true } })));
    mount("/models?tab=engines");
    fireEvent.click(await screen.findByRole("button", { name: "檢查推論引擎有沒有新版本" }));
    expect(await screen.findByText("檢查完成：2 個推論引擎有新版本。")).toBeTruthy();
    expect(within(screen.getByRole("article", { name: /vllm$/ })).getByRole("link", { name: "有新版 0.34.0" })).toBeTruthy();
    const t = makeTranslator("zh-TW");
    const miss = RESULT.engines.vllm.candidates[0].variants[1].why;
    expect(whyText(t, miss)).toBe("docker.io/vllm/vllm-openai 沒有 v0.34.0-cu129 映像檔；這個變體的映像檔只到 0.32.0。");
    expect(whyText(t, { code: "something_new", text: "As the server said." })).toBe("As the server said.");
  });

  it("FY-WEB-847 a check that fails says why and the button comes back; reasons pick their wording by what they carry", async () => {
    let release: (r: Reply) => void = () => {};
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/engine-updates/check") {
        const r = await new Promise<Reply>((res) => { release = res; });
        return new Response(JSON.stringify(r.body), { status: r.status });
      }
      const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
        : routes({ result: null, can_check: true })(url, init) ?? { status: 404, body: {} };
      return new Response(JSON.stringify(r.body), { status: r.status });
    });
    vi.stubGlobal("fetch", fetch);
    mount("/models?tab=engines");
    fireEvent.click(await screen.findByRole("button", { name: "Check for newer versions" }));
    const busy = await screen.findByRole("button", { name: /Checking… \(up to a minute and a half\)/ });
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    release({ status: 409, body: { detail: { code: "check_running", message: "A check for newer engine versions is running already." } } });
    expect(await screen.findByText(/A check for newer engine versions is running already/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Check for newer versions" }) as HTMLButtonElement).disabled).toBe(false);
    const en = makeTranslator("en");
    expect(whyText(en, { code: "no_image", text: "x", tag: "v2-cu1", repo: "docker.io/a/b" })).toBe("No v2-cu1 image in docker.io/a/b.");
    expect(whyText(en, { code: "no_archive", text: "x", release: "r1", archive: "a.zip" })).toBe("Release r1 has no a.zip, the archive the built-in uses.");
    expect(whyText(en, { code: "no_archive", text: "x", release: "r1", archive: "a.zip", similar: ["b.zip", "c.zip"] }))
      .toBe("Release r1 has no a.zip, the archive the built-in uses; it has b.zip, c.zip, built for another system.");
    expect(whyText(en, null)).toBe("");
  });
});
