// @vitest-environment jsdom
// "Download to…" (1.4, issue #35): the dialog from the Library and from a machine's page, progress and Cancel,
// and the machine's Internet access setting (admins).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { downloadLine, recentDownloads, targetDetail } from "../lib/downloads";
import { downloadText } from "../lib/models";
import type { CatalogResponse, DownloadTarget, DownloadTargets, MachineCache, ModelDownload } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const OPERATOR = { ...OWNER, id: 2, username: "op", role: "operator", is_owner: false };
type Reply = { status: number; body: unknown };
const GIB = 1024 ** 3;

const catalog: CatalogResponse = {
  can_manage: true, can_deploy: true,
  models: [{ id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF", source: "hf://ggml-org/Qwen3-1.7B-GGUF", kind: "hf", ref: "main",
    revision: "daeb8e2d", file: "Qwen3-1.7B-Q4_K_M.gguf", format: "gguf", task: "chat", task_label: "Chat", size_bytes: 1.2 * GIB,
    engine_args: {}, recipes: ["llama-cpp"], deployments: 0, created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: {}, gguf_files: [] }],
  recipes: [],
};

function target(over: Partial<DownloadTarget>): DownloadTarget {
  return { machine_id: 1, name: "a100-box", status: "online", connected: true, state: "ok", note: "", model_cached: false,
    need_bytes: 1.2 * GIB, free_bytes: 48 * GIB, variant: "cpu-process", engine: { kind: "archive", name: "llama.cpp-1.0.0", cached: false },
    engine_note: null, download: null, ...over };
}

function targets(engine = true): DownloadTargets {
  return {
    model: { id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF", size_bytes: 1.2 * GIB, kind: "hf", file: "Qwen3-1.7B-Q4_K_M.gguf" },
    recipes: ["llama-cpp"], recipe: "llama-cpp", can_download: true,
    machines: [
      target({ engine: engine ? { kind: "archive", name: "llama.cpp-1.0.0", cached: false } : null }),
      target({ machine_id: 2, name: "strix", state: "too_old", note: "strix's agent cannot download ahead: update the agent." }),
      target({ machine_id: 3, name: "cpu-b", state: "cached", note: "Already here with the engine.", model_cached: true }),
      target({ machine_id: 4, name: "cpu-c", state: "offline", note: "cpu-c is offline.", connected: false }),
    ],
  };
}

function dl(over: Partial<ModelDownload> = {}): ModelDownload {
  return { id: 9, model: { id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF" }, machine: { id: 1, name: "a100-box", connected: true },
    file: "Qwen3-1.7B-Q4_K_M.gguf", recipe: "llama-cpp", state: "downloading", via: "direct",
    download: { phase: "model", done: 0.5 * GIB, total: 1.25 * GIB, file: "Qwen3-1.7B-Q4_K_M.gguf" },
    reason: "downloading the model: 0.50 of 1.25 GiB (40%)", requested_by: "owner", created_at: "2026-11-16T10:00:00Z",
    finished_at: null, waiting: null, ...over };
}

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: { detail: { code: "not_found", message: "Not found" } } };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const body = (fetch: ReturnType<typeof server>, url: string) => {
  const call = fetch.mock.calls.find(([u, init]) => u === url && init?.method === "POST");
  return call ? JSON.parse(String(call[1]?.body)) : undefined;
};

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("Download to…", () => {
  it("FY-WEB-270 from the Library: the machines that can take it, the engine on by default, then the request", async () => {
    let list: ModelDownload[] = [];
    const fetch = server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/downloads" && init?.method === "POST") {
        list = [dl({ state: "queued", download: null, reason: null })];
        return { status: 201, body: { downloads: list, skipped: [] } };
      }
      if (url === "/api/downloads") return { status: 200, body: { downloads: list, can_download: true } };
      if (url.startsWith("/api/downloads/targets?model_id=5&engine=true")) return { status: 200, body: targets() };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
      if (url === "/api/machines") return { status: 200, body: fixture };
      return undefined;
    });
    mount("/models?tab=library");
    fireEvent.click(await screen.findByRole("button", { name: "Download to…" }));
    const dialog = await screen.findByRole("dialog", { name: /Download Qwen3 1.7B GGUF to/ });
    const row = (name: string) => within(dialog).getByText(name).closest("label") as HTMLElement;
    await within(dialog).findByText("a100-box");
    expect((within(dialog).getByRole("checkbox", { name: /Also the engine/ }) as HTMLInputElement).checked).toBe(true);
    expect(within(row("a100-box")).getByText("fits")).toBeTruthy();
    expect(within(row("a100-box")).getByText(/1.2 GiB to download · 48 GiB free · engine: llama.cpp-1.0.0 archive · cpu-process/)).toBeTruthy();
    const box = (name: string) => within(row(name)).getByRole("checkbox") as HTMLInputElement;
    expect(box("strix").disabled && box("cpu-b").disabled && box("cpu-c").disabled).toBe(true);
    expect(within(row("strix")).getByText("update the agent")).toBeTruthy();
    expect(within(row("cpu-b")).getByText("already here")).toBeTruthy();
    const go = within(dialog).getByRole("button", { name: "Download" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.click(box("a100-box"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Download to 1 machine" }));
    expect(await screen.findByText("Downloading qwen3-1.7b-gguf and the llama-cpp engine to a100-box.")).toBeTruthy();
    expect(body(fetch, "/api/downloads")).toEqual({ model_id: 5, machine_ids: [1], engine: true, recipe: "llama-cpp" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await screen.findByText("Waiting to start")).toBeTruthy();
  });

  it("FY-WEB-271 without the engine: the machines are asked again, and the request leaves the recipe out", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/downloads" && init?.method === "POST") return { status: 201, body: { downloads: [dl()], skipped: [] } };
      if (url === "/api/downloads") return { status: 200, body: { downloads: [], can_download: true } };
      if (url.startsWith("/api/downloads/targets?model_id=5&engine=true")) return { status: 200, body: targets() };
      if (url.startsWith("/api/downloads/targets?model_id=5&engine=false")) return { status: 200, body: targets(false) };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
      return undefined;
    });
    mount("/models?tab=library");
    fireEvent.click(await screen.findByRole("button", { name: "Download to…" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findAllByText(/engine: llama.cpp-1.0.0 archive/);
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /Also the engine/ }));
    await waitFor(() => expect(within(dialog).getByText("1.2 GiB to download · 48 GiB free")).toBeTruthy());
    expect(fetch.mock.calls.some(([u]) => String(u).startsWith("/api/downloads/targets?model_id=5&engine=false"))).toBe(true);
    fireEvent.click(within(within(dialog).getByText("a100-box").closest("label") as HTMLElement).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Download to 1 machine" }));
    expect(await screen.findByText("Downloading qwen3-1.7b-gguf to a100-box.")).toBeTruthy();
    expect(body(fetch, "/api/downloads")).toEqual({ model_id: 5, machine_ids: [1], engine: false });
  });

  it("FY-WEB-272 progress under the model, with the deployment's words, and Cancel after asking", async () => {
    let list = [dl(), dl({ id: 8, machine: { id: 3, name: "cpu-b", connected: true }, state: "failed", download: null,
      reason: "Qwen3-1.7B-Q4_K_M.gguf: download failed: cannot resolve huggingface.co", finished_at: "2026-11-16T10:05:00Z" })];
    const fetch = server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/downloads/9/cancel" && init?.method === "POST") {
        list = [dl({ state: "cancelled", download: null, reason: "cancelled by owner", finished_at: "2026-11-16T10:06:00Z" }), list[1]];
        return { status: 200, body: { download: list[0] } };
      }
      if (url === "/api/downloads") return { status: 200, body: { downloads: list, can_download: true } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
      return undefined;
    });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mount("/models?tab=library");
    const bar = await screen.findByRole("progressbar", { name: "Download of qwen3-1.7b-gguf to a100-box" });
    expect(bar.getAttribute("aria-valuenow")).toBe("40");
    expect(screen.getByText("Downloading the model: 512 MiB of 1.3 GiB (40%)")).toBeTruthy();
    expect(screen.getByText("Failed: Qwen3-1.7B-Q4_K_M.gguf: download failed: cannot resolve huggingface.co")).toBeTruthy();
    const cancel = screen.getByRole("button", { name: "Cancel the download to a100-box" });
    fireEvent.click(cancel);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("Cancel the download of qwen3-1.7b-gguf to a100-box?"));
    expect(fetch.mock.calls.some(([u]) => u === "/api/downloads/9/cancel")).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(cancel);
    expect(await screen.findByText("Cancelled the download of qwen3-1.7b-gguf to a100-box.")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("progressbar")).toBeNull());
    expect(screen.getByText(/^Cancelled by owner/)).toBeTruthy();
  });

  it("FY-WEB-273 from a machine's page: pick a model, this machine ticked; its downloads listed there", async () => {
    const machines = { ...fixture, can_manage: false,
      machines: fixture.machines.map((m) => ({ ...m, features: ["workloads", "models", "cache_admin", "prefetch"] })) };
    const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 48 * GIB,
      total_bytes: 100 * GIB, used_bytes: 0, partial_bytes: 0, partial_files: 0, truncated: false, remove_note: null, models: [], engines: [] };
    const fetch = server((url, init) => {
      if (url === "/api/machines") return { status: 200, body: machines };
      if (url === "/api/machines/1/cache") return { status: 200, body: cache };
      if (url === "/api/downloads?machine_id=1") return { status: 200, body: { downloads: [dl({ id: 3, model: { id: 6, name: "other", title: null } })], can_download: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url.startsWith("/api/downloads/targets?model_id=5")) return { status: 200, body: targets() };
      if (url === "/api/downloads" && init?.method === "POST") return { status: 201, body: { downloads: [dl()], skipped: [] } };
      return undefined;
    }, OPERATOR);
    mount("/machines/1");
    const section = (await screen.findByRole("heading", { name: /Models in cache/ })).closest("section") as HTMLElement;
    expect(await within(section).findByText("other")).toBeTruthy();
    expect(within(section).getByText("Downloading the model: 512 MiB of 1.3 GiB (40%)")).toBeTruthy();
    expect(within(section).queryByLabelText("Internet access for downloads")).toBeNull();  // admins only
    fireEvent.click(within(section).getByRole("button", { name: /Download a model here/ }));
    const dialog = await screen.findByRole("dialog", { name: /Download a model to/ });
    const pick = await within(dialog).findByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    await waitFor(() => expect(pick.value).toBe("5"));  // once the catalog has arrived
    await within(dialog).findByText("a100-box");
    const mine = within(within(dialog).getByText("a100-box").closest("label") as HTMLElement).getByRole("checkbox") as HTMLInputElement;
    expect(mine.checked).toBe(true);
    fireEvent.click(within(dialog).getByRole("button", { name: "Download to 1 machine" }));
    await waitFor(() => expect(body(fetch, "/api/downloads")).toEqual({ model_id: 5, machine_ids: [1], engine: true, recipe: "llama-cpp" }));
  });

  it("FY-WEB-274 an older agent cannot download ahead; admins set the machine's Internet access", async () => {
    const machines = { ...fixture, machines: fixture.machines.map((m) => ({ ...m, features: ["workloads", "models"], fetch_via: "auto" })) };
    const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: false, ok: true, reason: null, free_bytes: 48 * GIB,
      total_bytes: 100 * GIB, used_bytes: null, partial_bytes: null, partial_files: 0, truncated: false,
      remove_note: "This machine's agent reports no sizes and cannot remove models: update the agent.", models: [], engines: [] };
    const fetch = server((url, init) => {
      if (url === "/api/machines") return { status: 200, body: machines };
      if (url === "/api/machines/1/cache") return { status: 200, body: cache };
      if (url === "/api/downloads?machine_id=1") return { status: 200, body: { downloads: [], can_download: true } };
      if (url === "/api/machines/1/fetch-via" && init?.method === "PUT") return { status: 200, body: { machine: { ...machines.machines[0], fetch_via: "server" } } };
      return undefined;
    });
    mount("/machines/1");
    const section = (await screen.findByRole("heading", { name: /Models in cache/ })).closest("section") as HTMLElement;
    const button = await within(section).findByRole("button", { name: /Download a model here/ }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe("This machine's agent cannot download ahead: update the agent.");
    const select = within(section).getByLabelText("Internet access for downloads") as HTMLSelectElement;
    expect(select.value).toBe("auto");
    fireEvent.change(select, { target: { value: "server" } });
    expect(await screen.findByText("a100-box's internet access for downloads: Always through the server.")).toBeTruthy();
    const put = fetch.mock.calls.find(([u, i]) => u === "/api/machines/1/fetch-via" && i?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ fetch_via: "server" });
  });

  it("FY-WEB-275 the words: progress for any download, how one ended, the machines' details, the latest per machine", () => {
    expect(downloadText({ download: { phase: "image", done: 2 * 1024 ** 2, total: 0, layers: 7, layers_done: 3 } })).toBe("Pulling the image: 2 MiB, 3 of 7 layers");
    expect(downloadLine(dl({ state: "queued", download: null, reason: "waiting for another download on this machine to finish" })))
      .toBe("waiting for another download on this machine to finish");
    expect(downloadLine(dl({ machine: { id: 1, name: "a100-box", connected: false }, waiting: "waiting for a100-box to come online; the download goes on from where it was" })))
      .toBe("Downloading the model: 512 MiB of 1.3 GiB (40%) · waiting for a100-box to come online; the download goes on from where it was");
    expect(downloadLine(dl({ state: "done", download: null, finished_at: new Date().toISOString() }))).toBe("Downloaded just now, with the llama-cpp engine");
    expect(targetDetail(target({ engine: { kind: "image", name: "ghcr.io/ggml-org/llama.cpp:server-v1.0.0@sha256:" + "a".repeat(64), cached: true }, variant: "cpu" })))
      .toBe("1.2 GiB to download · 48 GiB free · engine: image llama.cpp:server-v1.0.0 (already here) · cpu");
    expect(targetDetail(target({ engine: null, engine_note: "no variant of vllm for this machine's hardware", model_cached: true })))
      .toBe("model already here · 48 GiB free · no engine: no variant of vllm for this machine's hardware");
    const list = [dl({ id: 4 }), dl({ id: 3, state: "done" }), dl({ id: 2, state: "failed", machine: { id: 2, name: "b", connected: true } }),
      dl({ id: 1, state: "failed" })];
    expect(recentDownloads(list, "machine").map((d) => d.id)).toEqual([4, 2]);
  });
});
