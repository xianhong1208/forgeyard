// @vitest-environment jsdom
// Engine images through the server and uploaded to a machine (1.4, issue #35 PR 4): the progress words of an image
// coming through the server, the Internet access hint, and a machine's Engine images section with its Upload an
// image dialog (admins only; the size limit; progress while it sends; what was loaded and which recipe's image it is).
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { downloadLine } from "../lib/downloads";
import { matchLine, uploadLine, type LoadedImage } from "../lib/imageUpload";
import { downloadFraction, downloadText } from "../lib/models";
import type { MachineCache, ModelDownload, Replica } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const MIB = 1024 ** 2;
const GIB = 1024 ** 3;
const PINNED = "ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d@sha256:784bb87c7b22cf8653322f3680f33e1fd013d122d817441d5d2fc6410dee68da";
const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 48 * GIB,
  total_bytes: 100 * GIB, used_bytes: 0, partial_bytes: 0, partial_files: 0, truncated: false, remove_note: null, models: [], engines: [] };

// XMLHttpRequest as the dialog uses it: the test drives its upload events and its answer.
class FakeXHR {
  static last: FakeXHR | null = null;
  method = ""; url = ""; headers: Record<string, string> = {}; body: unknown = null; status = 0; responseText = "";
  upload: { onprogress: ((e: { loaded: number }) => void) | null; onload: (() => void) | null } = { onprogress: null, onload: null };
  onload: (() => void) | null = null; onerror: (() => void) | null = null; onabort: (() => void) | null = null;
  constructor() { FakeXHR.last = this; }
  open(method: string, url: string) { this.method = method; this.url = url; }
  setRequestHeader(k: string, v: string) { this.headers[k] = v; }
  send(body: unknown) { this.body = body; }
  abort() { this.onabort?.(); }
  answer(status: number, body: unknown) { this.status = status; this.responseText = JSON.stringify(body); this.onload?.(); }
}

function machinePage(features: string[], { role = "admin", status = "online", max = 64 * GIB } = {}) {
  const machines = { ...fixture, can_manage: role === "admin", machines: fixture.machines.map((m) => ({ ...m, features, status, fetch_via: "auto",
    image_upload_max_bytes: max, runtime_info: { process: { ok: true }, container: { ok: true, engine: "docker" } } })) };
  const fetch = vi.fn(async (url: string) => {
    let r: { status: number; body: unknown } = { status: 404, body: { detail: { code: "not_found", message: "Not found" } } };
    if (url === "/api/auth/state") r = { status: 200, body: { needs_setup: false, registration: "off", user: { ...OWNER, role } } };
    else if (url === "/api/machines") r = { status: 200, body: machines };
    else if (url === "/api/machines/1/cache") r = { status: 200, body: cache };
    else if (url === "/api/downloads?machine_id=1") r = { status: 200, body: { downloads: [], can_download: true } };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
  render(<MemoryRouter initialEntries={["/machines/1"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
  return fetch;
}

async function imagesSection() {
  return (await screen.findByRole("heading", { name: "Engine images" })).closest("section") as HTMLElement;
}

function pick(name: string, size: number) {
  const file = new File(["x"], name, { type: "application/gzip" });
  Object.defineProperty(file, "size", { value: size });
  fireEvent.change(screen.getByLabelText("Image file"), { target: { files: [file] } });
  return file;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  FakeXHR.last = null;
});

describe("engine images through the server", () => {
  it("FY-WEB-360 an image coming through the server says so, layer by layer, then that it is being loaded", () => {
    const rep = (download: Replica["download"]) => ({ download }) as Replica;
    expect(downloadText(rep({ phase: "image", done: 120 * MIB, total: 300 * MIB, layers: 7, layers_done: 3, via: "server" })))
      .toBe("Pulling the image via the server: 120 MiB of 300 MiB, 3 of 7 layers");
    expect(downloadText(rep({ phase: "image", done: 300 * MIB, total: 300 * MIB, layers: 7, layers_done: 7, unpacking: true, via: "server" })))
      .toBe("Pulling the image via the server: loading it, 7 layers");
    expect(downloadText(rep({ phase: "image", done: 0, total: 0, via: "server" }))).toBe("Pulling the image via the server");
    expect(downloadText(rep({ phase: "image", done: 120 * MIB, total: 300 * MIB, layers: 7, layers_done: 3 })))
      .toBe("Pulling the image: 120 MiB of 300 MiB");
    expect(downloadFraction(rep({ phase: "image", done: 75, total: 300, via: "server" }))).toBe(0.25);
    const d = { id: 9, model: { id: 5, name: "qwen3-1.7b-gguf", title: null }, machine: { id: 1, name: "strix", connected: true }, file: null,
      recipe: "llama-cpp", state: "downloading", via: "server", reason: null, requested_by: "owner", created_at: "2026-11-16T10:00:00Z",
      finished_at: null, waiting: null, download: { phase: "image", done: 150 * MIB, total: 300 * MIB, layers: 4, layers_done: 2, via: "server" } } as ModelDownload;
    expect(downloadLine(d)).toBe("Pulling the image via the server: 150 MiB of 300 MiB, 2 of 4 layers (50%)");
  });

  it("FY-WEB-361 the Internet access hint covers pinned images, tells an agent that cannot relay images to update, and points to the upload", async () => {
    machinePage(["workloads", "models", "prefetch", "cache_admin", "relay", "image_load"]);
    const select = await screen.findByLabelText("Internet access for downloads") as HTMLSelectElement;
    const hint = (select.closest(".field") as HTMLElement).querySelector(".field-hint")?.textContent ?? "";
    expect(hint).toContain("Downloads models, engine archives and pinned engine images directly");
    expect(hint).toContain("Its agent pulls engine images directly: update the agent to get them through the server too.");
    expect(hint).toContain("An image that is not pinned (a tag only) cannot come through the server: upload it below.");
    cleanup();
    machinePage(["workloads", "models", "prefetch", "cache_admin", "relay", "relay_images", "image_load"]);
    const again = await screen.findByLabelText("Internet access for downloads") as HTMLSelectElement;
    expect((again.closest(".field") as HTMLElement).textContent).not.toContain("update the agent");
  });
});

describe("Upload an image", () => {
  it("FY-WEB-362 admins upload to a machine whose agent can load images; otherwise the button says why, and others see none", async () => {
    machinePage(["workloads", "image_load"]);
    let section = await imagesSection();
    expect(within(section).getByText(/save it on a machine that has it/)).toBeTruthy();
    expect((within(section).getByRole("button", { name: /Upload an image/ }) as HTMLButtonElement).disabled).toBe(false);
    cleanup();
    machinePage(["workloads"]);
    section = await imagesSection();
    const off = within(section).getByRole("button", { name: /Upload an image/ }) as HTMLButtonElement;
    expect(off.disabled).toBe(true);
    expect(off.title).toBe("a100-box's agent cannot load images: it has no container engine, or it is too old (update the agent).");
    cleanup();
    machinePage(["workloads", "image_load"], { status: "offline" });
    section = await imagesSection();
    expect((within(section).getByRole("button", { name: /Upload an image/ }) as HTMLButtonElement).title).toContain("is offline");
    cleanup();
    machinePage(["workloads", "image_load"], { role: "operator" });
    section = await imagesSection();
    expect(within(section).queryByRole("button", { name: /Upload an image/ })).toBeNull();
  });

  it("FY-WEB-363 the dialog: the size limit, progress while it sends, then what was loaded and which recipe's image it is", async () => {
    machinePage(["workloads", "image_load"], { max: 2 * GIB });
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Upload an image/ }));
    const dialog = screen.getByRole("dialog", { name: "Upload an image to a100-box" });
    expect(within(dialog).getByText(/Up to 2 GiB/)).toBeTruthy();
    const upload = within(dialog).getByRole("button", { name: /^Upload$/ }) as HTMLButtonElement;
    expect(upload.disabled).toBe(true);
    pick("big.tar", 3 * GIB);
    expect(within(dialog).getByText(/larger than the limit of 2 GiB \(IMAGE_UPLOAD_MAX_GB\)/)).toBeTruthy();
    expect(upload.disabled).toBe(true);
    pick("llama vulkan.tar.gz", GIB);
    expect(upload.disabled).toBe(false);
    fireEvent.click(upload);
    const xhr = FakeXHR.last as FakeXHR;
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/machines/1/images");
    expect(xhr.headers).toEqual({ "X-Forgeyard": "1", "Content-Type": "application/octet-stream", "X-Filename": "llama%20vulkan.tar.gz" });
    xhr.upload.onprogress?.({ loaded: 0.25 * GIB });
    const bar = await within(dialog).findByRole("progressbar", { name: "Upload of llama vulkan.tar.gz to a100-box" });
    expect(bar.getAttribute("aria-valuenow")).toBe("25");
    expect(within(dialog).getByText("Sending 256 MiB of 1 GiB (25%)")).toBeTruthy();
    xhr.upload.onload?.();
    expect(await within(dialog).findByText("Loading it into a100-box's container engine…")).toBeTruthy();
    xhr.answer(200, { ok: true, bytes: GIB, images: [{ id: "sha256:" + "ab".repeat(32), tags: ["ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d"],
      content: null, os: "linux", architecture: "amd64", matches: [{ recipe: "llama-cpp", variant: "vulkan", image: PINNED, how: "checked" }] }] });
    const list = await within(dialog).findByRole("list", { name: "Loaded images" });
    expect(within(list).getByText("ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d")).toBeTruthy();
    expect(within(list).getByText("abababababab · linux/amd64")).toBeTruthy();
    expect(within(list).getByText("The pinned image of llama-cpp (vulkan), checked by its recorded ids. Deployments here use it without a pull.")).toBeTruthy();
    expect(await screen.findByText("Loaded ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d on a100-box.")).toBeTruthy();
    expect(within(dialog).getAllByRole("button", { name: "Close" })).toHaveLength(2);  // the corner's and the foot's
  });

  it("FY-WEB-364 a refused upload shows the server's words; what a loaded image is, in every case", async () => {
    machinePage(["workloads", "image_load"]);
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Upload an image/ }));
    const dialog = screen.getByRole("dialog");
    pick("x.tar", 10 * MIB);
    fireEvent.click(within(dialog).getByRole("button", { name: /^Upload$/ }));
    (FakeXHR.last as FakeXHR).answer(422, { detail: { code: "image_load_failed", message: "a100-box: docker load failed: invalid tar header" } });
    expect(await within(dialog).findByText("a100-box: docker load failed: invalid tar header")).toBeTruthy();
    const img = (over: Partial<LoadedImage>): LoadedImage => ({ id: "sha256:" + "12".repeat(32), tags: ["mine:1"], content: null, os: "linux",
      architecture: "amd64", matches: [], ...over });
    expect(matchLine(img({}))).toBe("Not the image of any recipe. A custom recipe can name it by its tag.");
    expect(matchLine(img({ matches: [{ recipe: "vision", variant: "cpu", image: "tonyhuang1101/forgeyard-vision:1.4.0", how: "tag" }] })))
      .toBe("Named as the image of vision (cpu). That recipe names it by a tag only, so it is used unchecked.");
    expect(matchLine(img({ matches: [{ recipe: "llama-cpp", variant: "vulkan", image: PINNED, how: "checked" }] })))
      .toBe("The pinned image of llama-cpp (vulkan), checked by its recorded ids. Its tag is not the recipe's: run docker tag 121212121212 "
        + "ghcr.io/ggml-org/llama.cpp:server-vulkan-v1.0.0-b12359-b1b770d on the machine to use it.");
    expect(uploadLine("sending", 0, 0, "x")).toBe("Sending 0 KiB of 0 KiB (0%)");
  });
});
