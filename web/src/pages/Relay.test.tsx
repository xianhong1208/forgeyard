// @vitest-environment jsdom
// The server's relay (1.4, issue #35): progress "via the server" wherever a download shows, and the machine's
// Internet access setting saying what each choice does and whether its agent can relay yet.
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { downloadLine, targetDetail } from "../lib/downloads";
import { downloadFraction, downloadText } from "../lib/models";
import type { DownloadTarget, MachineCache, ModelDownload, Replica } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const GIB = 1024 ** 3;

const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 48 * GIB,
  total_bytes: 100 * GIB, used_bytes: 0, partial_bytes: 0, partial_files: 0, truncated: false, remove_note: null, models: [], engines: [] };

function dl(over: Partial<ModelDownload> = {}): ModelDownload {
  return { id: 9, model: { id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF" }, machine: { id: 1, name: "a100-box", connected: true },
    file: "Qwen3-1.7B-Q4_K_M.gguf", recipe: null, state: "downloading", via: "server",
    download: { phase: "model", done: 0.5 * GIB, total: 1.25 * GIB, via: "server" },
    reason: "downloading the model through the server: 0.50 of 1.25 GiB (40%)", requested_by: "owner",
    created_at: "2026-11-16T10:00:00Z", finished_at: null, waiting: null, ...over };
}

function target(over: Partial<DownloadTarget>): DownloadTarget {
  return { machine_id: 1, name: "strix", status: "online", connected: true, state: "ok", note: "", model_cached: false,
    need_bytes: 1.2 * GIB, free_bytes: 48 * GIB, variant: null, engine: null, engine_note: null, download: null, ...over };
}

function machinePage(features: string[], fetchVia: "auto" | "server" | "direct", downloads: ModelDownload[] = []) {
  const machines = { ...fixture, machines: fixture.machines.map((m) => ({ ...m, features, fetch_via: fetchVia })) };
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    let r: { status: number; body: unknown } = { status: 404, body: { detail: { code: "not_found", message: "Not found" } } };
    if (url === "/api/auth/state") r = { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } };
    else if (url === "/api/machines") r = { status: 200, body: machines };
    else if (url === "/api/machines/1/cache") r = { status: 200, body: cache };
    else if (url === "/api/downloads?machine_id=1") r = { status: 200, body: { downloads, can_download: true } };
    else if (url === "/api/machines/1/fetch-via" && init?.method === "PUT") {
      const next = JSON.parse(String(init.body)).fetch_via;
      machines.machines[0] = { ...machines.machines[0], fetch_via: next };
      r = { status: 200, body: { machine: machines.machines[0] } };
    }
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  render(<MemoryRouter initialEntries={["/machines/1"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
  return fetch;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("through the server", () => {
  it("FY-WEB-387 the words: a download through the server says so, and so does how it ended and the dialog's line", () => {
    const rep = (download: Replica["download"]) => ({ download }) as Replica;
    expect(downloadText(rep({ phase: "model", done: 0.5 * GIB, total: 1.25 * GIB, via: "server" })))
      .toBe("Downloading the model via the server: 512 MiB of 1.3 GiB");
    expect(downloadText(rep({ phase: "engine", done: 30 * 1024 ** 2, total: 0, via: "server" }))).toBe("Downloading the engine via the server: 30 MiB");
    expect(downloadText(rep({ phase: "model", done: GIB, total: 2 * GIB, via: "direct" }))).toBe("Downloading the model: 1 GiB of 2 GiB");
    expect(downloadFraction(rep({ phase: "model", done: 1, total: 4, via: "server" }))).toBe(0.25);
    expect(downloadLine(dl())).toBe("Downloading the model via the server: 512 MiB of 1.3 GiB (40%)");
    expect(downloadLine(dl({ state: "done", download: null, finished_at: new Date().toISOString() }))).toBe("Downloaded just now via the server");
    expect(downloadLine(dl({ state: "done", via: "direct", download: null, recipe: "llama-cpp", finished_at: new Date().toISOString() })))
      .toBe("Downloaded just now, with the llama-cpp engine");
    expect(targetDetail(target({ via: "server" }))).toBe("1.2 GiB to download · 48 GiB free · via the server");
    expect(targetDetail(target({ via: "auto" }))).toBe("1.2 GiB to download · 48 GiB free");
    expect(targetDetail(target({ via: "server", model_cached: true, need_bytes: 0 }))).toBe("model already here · 48 GiB free");
  });

  it("FY-WEB-388 a machine's page shows its download coming through the server", async () => {
    machinePage(["workloads", "models", "prefetch", "cache_admin", "relay"], "server", [dl()]);
    const bar = await screen.findByRole("progressbar", { name: "Download of qwen3-1.7b-gguf to a100-box" });
    expect(bar.getAttribute("aria-valuenow")).toBe("40");
    expect(screen.getByText("Downloading the model via the server: 512 MiB of 1.3 GiB (40%)")).toBeTruthy();
  });

  it("FY-WEB-389 the Internet access setting says what each choice does; an agent that cannot relay is told to update", async () => {
    machinePage(["workloads", "models", "prefetch", "cache_admin", "relay"], "auto");
    const section = (await screen.findByRole("heading", { name: /Models in cache/ })).closest("section") as HTMLElement;
    const select = await within(section).findByLabelText("Internet access for downloads") as HTMLSelectElement;
    expect(select.value).toBe("auto");
    const hint = () => (select.closest(".field") as HTMLElement).querySelector(".field-hint")?.textContent ?? "";
    expect(hint()).toContain("directly, and through the server once the source cannot be reached from here");
    expect(hint()).not.toContain("update the agent");
    fireEvent.change(select, { target: { value: "server" } });
    expect(await screen.findByText("a100-box's internet access for downloads: Always through the server.")).toBeTruthy();
    expect(await within(section).findByText(/Every model file and engine archive comes through the server/)).toBeTruthy();
    cleanup();
    machinePage(["workloads", "models", "prefetch", "cache_admin"], "server");
    const old = await screen.findByLabelText("Internet access for downloads") as HTMLSelectElement;
    expect((old.closest(".field") as HTMLElement).textContent).toContain("cannot download through the server yet: update the agent");
  });
});
