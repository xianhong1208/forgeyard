// @vitest-environment jsdom
// 1.5.2 (#95 PR 2): "Try this version" on a built-in engine's page (admins only): the dialog (the copy's name, the
// variants included and left out and why, what the project signs, the warning and the release notes' mentions), the
// copy it makes and the link to its page, the server's refusals in both languages (a 502 says what failed), and a
// copy's page (where it came from, each variant's pinned digest and what was verified, what was left out).
// FY-WEB-873 to 878.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { copyName, tryError } from "../components/EngineTry";
import { makeTranslator } from "../i18n/react";
import { LANG_KEY } from "../i18n/lang";
import { ApiError } from "../lib/api";
import type { CatalogResponse, EnginePage, EngineUpdatesResponse, EngineUpdatesResult, Recipe } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const OPERATOR = { ...OWNER, id: 2, username: "olga", role: "operator", is_owner: false };
type Reply = { status: number; body?: unknown };

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

const D1 = "sha256:" + "ab".repeat(32);
const D2 = "sha256:" + "cd".repeat(32);
const variant = (name: string, vendor: "nvidia" | "amd" | "cpu", image: string | null, engine: string | null = null) => ({
  name, vendor, runtime: image ? "container" as const : "process" as const, image, arch: null, compute_capability: null, driver: null,
  engine, image_check: Boolean(image?.includes("@sha256:")),
});
const VLLM: Recipe = {
  name: "vllm", title: "vLLM", description: "", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"], api: { chat: "openai-chat" },
  share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"], params: {}, based_on: null, copy: null,
  variants: [variant("cuda", "nvidia", `vllm/vllm-openai:v0.32.0@${D1}`), variant("cuda-12.9", "nvidia", `vllm/vllm-openai:v0.32.0-cu129@${D1}`)],
} as Recipe;
const LLAMA: Recipe = { ...VLLM, name: "llama-cpp", title: "llama.cpp", engine: "llama.cpp", version: "0.5.0",
  variants: [variant("vulkan", "amd", `ghcr.io/ggml-org/llama.cpp:server-vulkan-v0.5.0@${D1}`)] } as Recipe;
const COPY: Recipe = {
  ...VLLM, name: "vllm-0-34-0", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm",
  variants: [variant("cuda", "nvidia", `vllm/vllm-openai:v0.34.0@${D2}`),
    variant("cpu-process", "cpu", null, "https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-ubuntu-x64.tar.gz")],
  copy: { from_version: "0.32.0", kind: "release", made_at: "2026-11-23T10:00:00+00:00", made_by: "owner",
    left_out: [{ variant: "cuda-12.9", code: "no_image", text: "No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.",
      params: { tag: "v0.34.0-cu129", repo: "docker.io/vllm/vllm-openai", last: "0.32.0" } }],
    archives: [{ variant: "cpu-process", release: "b12359", signature: { status: "verified", repository: "ggml-org/llama.cpp",
      workflow: ".github/workflows/release-publish.yml", ref: "refs/heads/master", run: "https://github.com/ggml-org/llama.cpp/actions/runs/1/attempts/1" } }] },
} as Recipe;

const RESULT: EngineUpdatesResult = {
  checked_at: new Date(Date.now() - 60_000).toISOString(), checked_by: "owner", seconds: 9, github_requests: 3,
  engines: {
    vllm: { recipe: "vllm", title: "vLLM", engine: "vllm", version: "0.32.0", kind: "release", status: "newer", newest: "0.34.0", errors: [],
      candidates: [{ version: "0.34.0", kind: "release",
        release: { tag: "v0.34.0", name: "v0.34.0", url: "https://github.com/vllm-project/vllm/releases/tag/v0.34.0", published_at: "2026-11-16T23:22:00Z" },
        variants: [
          { name: "cuda", runtime: "container", vendor: "nvidia", available: true, tag: "v0.34.0", digest: D2, image: `vllm/vllm-openai:v0.34.0@${D2}` },
          { name: "cuda-12.9", runtime: "container", vendor: "nvidia", available: false,
            why: { code: "no_image", text: "No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.", tag: "v0.34.0-cu129", repo: "docker.io/vllm/vllm-openai", last: "0.32.0" } },
        ],
        mentions: [{ flag: "--gpu-memory-utilization", lines: 5, excerpts: ["x"] }] }] },
    "llama-cpp": { recipe: "llama-cpp", title: "llama.cpp", engine: "llama.cpp", version: "0.5.0", kind: "release", status: "newer", newest: "1.0.0", errors: [],
      candidates: [{ version: "1.0.0", kind: "release", variants: [
        { name: "vulkan", runtime: "container", vendor: "amd", available: true, tag: "server-vulkan-v1.0.0-b12359-b1b770d", digest: D2 }], mentions: [] }] },
  },
};
const SIGNED = { vllm: { signed: false as const, text: "vLLM publishes no signatures or attestations for its images." },
  "llama-cpp": { signed: true as const, kind: "github-attestation", repository: "ggml-org/llama.cpp" } };

function page(r: Recipe): EnginePage {
  return { recipe: { ...r, yaml: `name: ${r.name}\n` }, can_manage: true, machines: [], deployments: [], models: [],
    pins: r.based_on ? { cuda: { recorded_at: "2026-11-23T10:00:00+00:00", recorded_by: "owner",
      signature: { status: "unsigned", note: "vLLM publishes no signatures or attestations for its images." } } } : {} };
}

function serve(opts: { admin?: boolean; onTry?: (url: string, body: unknown) => Reply } = {}) {
  const admin = opts.admin ?? true;
  const updates: EngineUpdatesResponse = { result: RESULT, can_check: admin, signatures: SIGNED };
  const recipes = [VLLM, LLAMA, COPY];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    let r: Reply | undefined;
    if (url === "/api/auth/state") r = { status: 200, body: { needs_setup: false, registration: "off", user: admin ? OWNER : OPERATOR } };
    else if (url === "/api/models") r = { status: 200, body: { models: [], recipes, can_manage: admin, can_deploy: true } as CatalogResponse };
    else if (url === "/api/engine-updates") r = { status: 200, body: updates };
    else if (/\/try$/.test(url) && init?.method === "POST") r = opts.onTry?.(url, JSON.parse(String(init.body)));
    else if (url.startsWith("/api/recipes/")) {
      const name = decodeURIComponent(url.slice("/api/recipes/".length).split("?")[0]);
      const found = recipes.find((x) => x.name === name);
      r = found ? { status: 200, body: page(found) } : { status: 404, body: { detail: { code: "not_found", message: "No such recipe." } } };
    } else if (url === "/api/machines") r = { status: 200, body: { machines: [] } };
    else if (url === "/api/deployments") r = { status: 200, body: { deployments: [] } };
    r = r ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

async function openDialog(label: string) {
  const v = await screen.findByRole("article", { name: label });
  fireEvent.click(within(v).getByRole("button", { name: "Try this version" }));
  return screen.getByRole("dialog");
}

describe("Try this version (1.5.2, #95 PR 2)", () => {
  it("FY-WEB-873 an admin tries vLLM 0.34.0: the dialog says what the copy holds, and the copy is linked once made", async () => {
    let sent: unknown = null;
    const fetch = serve({ onTry: (url, body) => {
      sent = { url, body };
      return { status: 201, body: { recipe: COPY, left_out: COPY.copy!.left_out, signatures: {} } };
    } });
    mount("/models/engines/vllm");
    const dlg = await openDialog("vLLM 0.34.0");
    expect(within(dlg).getByRole("heading", { name: "Try vLLM 0.34.0" })).toBeTruthy();
    expect((within(dlg).getByLabelText("Name of the copy") as HTMLInputElement).value).toBe("vllm-0-34-0");
    expect(within(dlg).getByText("included")).toBeTruthy();
    expect(within(dlg).getByText("left out")).toBeTruthy();
    expect(within(dlg).getByText("No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.")).toBeTruthy();
    expect(within(dlg).getByText("Machines that need a variant left out keep using the built-in vllm.")).toBeTruthy();
    expect(within(dlg).getByText("vLLM publishes no signatures or attestations for its images. The copy is pinned by digest only.")).toBeTruthy();
    expect(within(dlg).getByText(/can change flags, defaults and memory use.*measured on 0.32.0.*--gpu-memory-utilization/)).toBeTruthy();
    expect(within(dlg).getByRole("link", { name: /Read the release notes/ }).getAttribute("href")).toBe("https://github.com/vllm-project/vllm/releases/tag/v0.34.0");
    fireEvent.change(within(dlg).getByLabelText("Name of the copy"), { target: { value: "vllm-next " } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Make the copy" }));
    expect(await within(dlg).findByText("Made vllm-0-34-0, a copy of vllm 0.32.0 for 0.34.0.")).toBeTruthy();
    expect(sent).toEqual({ url: "/api/recipes/vllm/try", body: { version: "0.34.0", name: "vllm-next" } });
    expect(within(dlg).getByRole("link", { name: "Go to vllm-0-34-0" }).getAttribute("href")).toBe("/models/engines/vllm-0-34-0");
    expect(fetch.mock.calls.filter(([u]) => String(u).endsWith("/try")).length).toBe(1);
  });

  it("FY-WEB-874 a signed engine says it is verified; a refusal shows the server's reason, a 502 too", async () => {
    serve({ onTry: () => ({ status: 422, body: { detail: { code: "signature_failed", variant: "vulkan",
      message: "The signature of the image server-vulkan-v1.0.0 (sha256:cdcdcdcdcdcd…) did not verify: signed by https://github.com/someone/x. It is not taken." } } }) });
    mount("/models/engines/llama-cpp");
    const dlg = await openDialog("llama.cpp 1.0.0");
    expect(within(dlg).getByText(/ggml-org\/llama.cpp signs its images and release archives \(GitHub artifact attestations\)\. Forgeyard verifies each one now/)).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "Make the copy" }));
    expect(await within(dlg).findByText(/did not verify: signed by https:\/\/github.com\/someone\/x/)).toBeTruthy();
    expect(within(dlg).getByRole("button", { name: "Make the copy" })).toBeTruthy();    // nothing made: try again
    const en = makeTranslator("en");
    const zh = makeTranslator("zh-TW");
    const down = new ApiError(502, { code: "cannot_verify", message: "Could not refresh Sigstore's trust root: no network." });
    expect(tryError(en, down)).toBe("Could not refresh Sigstore's trust root: no network.");
    expect(tryError(zh, down)).toBe("目前無法驗證簽章，所以沒有建立複本：Could not refresh Sigstore's trust root: no network.");
    expect(tryError(zh, new ApiError(409, { code: "check_again", message: "0.35.0 is not among…" }))).toBe("上次的檢查結果已經不包含這個版本。請再檢查一次新版本。");
    expect(tryError(zh, new ApiError(409, { code: "something_new", message: "As the server said." }))).toBe("As the server said.");
  });

  it("FY-WEB-875 operators read the newer versions but have no Try button; a version with nothing to copy cannot be tried", async () => {
    serve({ admin: false });
    mount("/models/engines/vllm");
    const v = await screen.findByRole("article", { name: "vLLM 0.34.0" });
    expect(within(v).queryByRole("button", { name: "Try this version" })).toBeNull();
    cleanup();
    serve();
    RESULT.engines.vllm.candidates[0].variants[0].available = false;
    try {
      mount("/models/engines/vllm");
      const b = within(await screen.findByRole("article", { name: "vLLM 0.34.0" })).getByRole("button", { name: "Try this version" });
      expect((b as HTMLButtonElement).disabled).toBe(true);
      expect(b.getAttribute("title")).toBe("No variant has an image of this version, so there is nothing to copy.");
    } finally {
      RESULT.engines.vllm.candidates[0].variants[0].available = true;
    }
  });

  it("FY-WEB-876 a copy's page: where it came from, pinned digests and what was verified, what was left out", async () => {
    serve();
    mount("/models/engines/vllm-0-34-0");
    const sec = (await screen.findByRole("heading", { name: "Copy of vllm" })).closest("section")!;
    expect(screen.getByRole("navigation", { name: "On this page" }).textContent).toContain("Copy of vllm");
    expect(within(sec).getByText(/Made from the built-in/).textContent).toMatch(/vllm \(0.32.0\) for 0.34.0 with “Try this version” on .* by owner\. Not tested by Forgeyard\. The built-in vllm stays as it is/);
    expect(within(sec).getByRole("link", { name: "vllm" }).getAttribute("href")).toBe("/models/engines/vllm");
    const rows = within(sec).getAllByRole("row");
    expect(within(rows[1]).getByText("unsigned upstream").getAttribute("title")).toBe("vLLM publishes no signatures or attestations for its images.");
    expect(within(rows[1]).getByText("sha256:cdcdcdcd…")).toBeTruthy();
    expect(within(rows[1]).getByText(/pinned .* by owner/)).toBeTruthy();
    expect(within(rows[2]).getByText("verified")).toBeTruthy();
    expect(within(rows[2]).getByText("ggml-org/llama.cpp · .github/workflows/release-publish.yml · refs/heads/master")).toBeTruthy();
    expect(within(rows[2]).getByRole("link", { name: /the build/ }).getAttribute("href")).toBe("https://github.com/ggml-org/llama.cpp/actions/runs/1/attempts/1");
    expect(within(sec).getByText("Left out of this copy")).toBeTruthy();
    expect(within(sec).getByText("No v0.34.0-cu129 image in docker.io/vllm/vllm-openai; this variant's images stop at 0.32.0.")).toBeTruthy();
    // Its title already carries the version: said once.
    expect(screen.getByText("vLLM 0.34.0", { selector: ".lp-sub" })).toBeTruthy();
    expect(screen.queryByText("vLLM 0.34.0 0.34.0")).toBeNull();
    // The built-in's page has no Copy section.
    cleanup();
    serve();
    mount("/models/engines/vllm");
    await screen.findByRole("heading", { name: "vllm" });
    expect(screen.queryByRole("heading", { name: /^Copy of/ })).toBeNull();
  });

  it("FY-WEB-877 in Traditional Chinese: the button, the dialog and a copy's page read as Taiwan Mandarin", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    serve();
    mount("/models/engines/vllm");
    const v = await screen.findByRole("article", { name: "vLLM 0.34.0" });
    fireEvent.click(within(v).getByRole("button", { name: "試用這個版本" }));
    const dlg = screen.getByRole("dialog");
    expect(within(dlg).getByRole("heading", { name: "試用 vLLM 0.34.0" })).toBeTruthy();
    expect(within(dlg).getByText(/Forgeyard 會建立一個自訂推論引擎：vllm 的複本，改用 0.34.0 的映像檔，每個都以 digest 釘選。內建的 vllm 維持原樣/)).toBeTruthy();
    expect(within(dlg).getByLabelText("複本名稱")).toBeTruthy();
    expect(within(dlg).getByText("docker.io/vllm/vllm-openai 沒有 v0.34.0-cu129 映像檔；這個變體的映像檔只到 0.32.0。")).toBeTruthy();
    expect(within(dlg).getByText("vLLM 的映像檔沒有提供簽章或建置證明。這個複本只以 digest 釘選。")).toBeTruthy();
    expect(within(dlg).getByRole("button", { name: "建立複本" })).toBeTruthy();
    cleanup();
    serve();
    mount("/models/engines/vllm-0-34-0");
    const sec = (await screen.findByRole("heading", { name: "vllm 的複本" })).closest("section")!;
    expect(within(sec).getByText("上游未簽章")).toBeTruthy();
    expect(within(sec).getByText("已驗證")).toBeTruthy();
    expect(within(sec).getByText("這個複本未包含的變體")).toBeTruthy();
    expect(within(sec).getByText(/Forgeyard 沒有測試過這個複本。內建的 vllm 維持原樣/)).toBeTruthy();
  });

  it("FY-WEB-878 the copy's name follows the server's rule", () => {
    expect(copyName("vllm", "0.34.0")).toBe("vllm-0-34-0");
    expect(copyName("vllm-pooling", "0.34.0")).toBe("vllm-pooling-0-34-0");
    expect(copyName("sd-cpp", "master-1117-0d71f96")).toBe("sd-cpp-master-1117-0d71f96");
    expect(copyName("x".repeat(38), "1.2.3").length).toBeLessThanOrEqual(40);
    expect(copyName("x".repeat(38), "1.2.3").endsWith("-")).toBe(false);
  });
});
