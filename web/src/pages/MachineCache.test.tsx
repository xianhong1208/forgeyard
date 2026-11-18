// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { cacheRows, diskLine, diskShares, removeBlocked, removeQuestion } from "../lib/cache";
import type { MachineCache } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };
const GIB = 1024 ** 3;
const REV = "c".repeat(40);
const KEY = `hub/models--acme--gguf@${REV}`;

function cache(over: Partial<MachineCache> = {}): MachineCache {
  return {
    machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null,
    free_bytes: 50 * GIB, total_bytes: 100 * GIB, used_bytes: 7.5 * GIB, partial_bytes: 300 * 1024 ** 2, partial_files: 1,
    truncated: false, remove_note: null,
    models: [
      { key: KEY, kind: "hf", repo: "acme/gguf", revision: REV, bytes: 6 * GIB, files: 3, catalog: [{ id: 1, name: "qwen-q4" }],
        used_by: [{ name: "chat-q4", kind: "deployment", id: 7 }],
        parts: [
          { key: `${KEY}/m-Q4_K_M.gguf`, file: "m-Q4_K_M.gguf", bytes: 2 * GIB, catalog: [{ id: 1, name: "qwen-q4" }], used_by: [{ name: "chat-q4", kind: "deployment", id: 7 }] },
          { key: `${KEY}/m-Q8_0.gguf`, file: "m-Q8_0.gguf", bytes: 4 * GIB, catalog: [], used_by: [] },
        ] },
      { key: "url/" + "d".repeat(24), kind: "url", file: "tiny.gguf", bytes: 100 * 1024 ** 2, files: 1, catalog: [], used_by: [] },
    ],
    engines: [{ key: "engine/" + "e".repeat(16), id: "e".repeat(16), name: "llama-cpp", bytes: 30 * 1024 ** 2, used_by: [] }],
    ...over,
  };
}

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : url === "/api/machines" ? { status: 200, body: fixture }
        : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path = "/machines/1") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

async function section() {
  const heading = await screen.findByRole("heading", { name: /Models in cache/ });
  return heading.closest("section") as HTMLElement;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("a machine's model cache", () => {
  it("FY-WEB-241 lists each model with its size, its GGUF files under it, the engine, the disk and partial downloads", async () => {
    server((url) => (url === "/api/machines/1/cache" ? { status: 200, body: cache() } : undefined));
    mount();
    const box = await section();
    await within(box).findByText("acme/gguf");
    expect(within(box).getByText("7.5 GiB in the cache · 50 GiB free of 100 GiB")).toBeTruthy();
    const items = within(box).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("strong")?.textContent)).toEqual(["acme/gguf", "m-Q4_K_M.gguf", "m-Q8_0.gguf", "tiny.gguf", "llama-cpp"]);
    expect(within(items[0]).getByText("6 GiB")).toBeTruthy();
    expect(items[0].querySelector("small")!.textContent).toBe("revision cccccccccccc · 3 files · catalog: qwen-q4");
    // 1.4.3 (#140): the catalog model opens its page.
    expect(within(items[0]).getByRole("link", { name: "qwen-q4" }).getAttribute("href")).toBe("/models/qwen-q4");
    expect(items[1].className).toContain("cacherow-part");
    expect(within(box).getByText(/Partial downloads:/)).toBeTruthy();
    expect(within(box).getByRole("button", { name: "Remove partial downloads" })).toBeTruthy();
  });

  it("FY-WEB-242 Remove is off while a deployment uses it, naming it; otherwise it asks, then calls the API", async () => {
    let current = cache();
    const fetch = server((url, init) => {
      if (url === "/api/machines/1/cache") return { status: 200, body: current };
      if (url === "/api/machines/1/cache/remove" && init?.method === "POST") {
        current = cache({ models: [current.models[0]] });
        return { status: 200, body: { ok: true, freed_bytes: 100 * 1024 ** 2, cache: current } };
      }
      return undefined;
    });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mount();
    const box = await section();
    await within(box).findByText("acme/gguf");
    const used = within(box).getByRole("button", { name: "Remove acme/gguf" }) as HTMLButtonElement;
    expect(used.disabled).toBe(true);
    expect(used.title).toBe("In use by chat-q4. Stop it first.");
    expect(within(box).getAllByRole("link", { name: "in use: chat-q4" })[0].getAttribute("href")).toBe("/serving/7");
    expect((within(box).getByRole("button", { name: "Remove m-Q4_K_M.gguf" }) as HTMLButtonElement).disabled).toBe(true);
    const free = within(box).getByRole("button", { name: "Remove tiny.gguf" }) as HTMLButtonElement;
    expect(free.disabled).toBe(false);
    fireEvent.click(free);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("Remove tiny.gguf (100 MiB) from a100-box's cache?"));
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/1/cache/remove")).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(free);
    expect(await screen.findByText("Removed tiny.gguf from a100-box's cache: 100 MiB freed.")).toBeTruthy();
    const call = fetch.mock.calls.find(([u]) => u === "/api/machines/1/cache/remove");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ key: "url/" + "d".repeat(24) });
    await waitFor(() => expect(within(box).queryByText("tiny.gguf")).toBeNull());
  });

  it("FY-WEB-243 a refusal is shown; an older agent lists without sizes and without Remove", async () => {
    let body = cache();
    server((url, init) => {
      if (url === "/api/machines/1/cache") return { status: 200, body };
      if (url === "/api/machines/1/cache/remove" && init?.method === "POST") {
        return { status: 409, body: { detail: { code: "cache_busy", message: "a100-box: A download of this model is in progress on this machine." } } };
      }
      return undefined;
    });
    vi.stubGlobal("confirm", vi.fn(() => true));
    const view = mount();
    let box = await section();
    fireEvent.click(await within(box).findByRole("button", { name: "Remove m-Q8_0.gguf" }));
    expect(await screen.findByText("a100-box: A download of this model is in progress on this machine.")).toBeTruthy();
    view.unmount();
    body = cache({ sizes: false, used_bytes: null, partial_bytes: null, partial_files: 0, engines: [],
      remove_note: "This machine's agent reports no sizes and cannot remove models: update the agent.",
      models: [{ key: KEY, kind: "hf", repo: "acme/gguf", revision: REV, bytes: null, catalog: [], used_by: [] }] });
    mount();
    box = await section();
    expect(await within(box).findByText("This machine's agent reports no sizes and cannot remove models: update the agent.")).toBeTruthy();
    expect(within(box).getByText("size unknown")).toBeTruthy();
    expect(within(box).queryByRole("button", { name: /Remove/ })).toBeNull();
  });

  it("FY-WEB-244 the rows, the disk line and why Remove is off, from the listing", () => {
    const c = cache();
    const rows = cacheRows(c);
    expect(rows.map((r) => [r.title, r.part, r.kind])).toEqual([
      ["acme/gguf", false, "model"], ["m-Q4_K_M.gguf", true, "model"], ["m-Q8_0.gguf", true, "model"],
      ["tiny.gguf", false, "model"], ["llama-cpp", false, "engine"]]);
    // A revision with one GGUF file shows no file row: removing the revision is the same.
    expect(cacheRows(cache({ models: [{ ...c.models[0], parts: [c.models[0].parts![0]] }], engines: [] })).length).toBe(1);
    expect(removeBlocked(c, rows[2])).toBeNull();
    expect(removeBlocked({ ...c, remove_note: "The agent is not connected, so nothing can be removed now." }, rows[2]))
      .toBe("The agent is not connected, so nothing can be removed now.");
    expect(diskLine(cache({ used_bytes: null, total_bytes: null }))).toBe("50 GiB free");
    expect(removeQuestion("box", rows[0])).toBe("Remove acme/gguf (6 GiB) from box's cache? Files another revision shares stay. A deployment that needs it downloads it again.");
    expect(removeQuestion("box", rows[1])).toContain("The other files of this revision stay.");
    expect(removeQuestion("box", rows[4])).toBe("Remove the engine llama-cpp (30 MiB) from box's cache? A deployment that needs it downloads it again.");
    const s = diskShares(c)!;
    expect(s.cache).toBeCloseTo(0.075);
    expect(s.other).toBeCloseTo(0.425);
    expect(diskShares(cache({ total_bytes: null }))).toBeNull();
  });
});
