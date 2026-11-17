// @vitest-environment jsdom
// Copying an engine image from another machine (1.4.1, issue #104): which machines can send one, the machine page's
// "Copy from another machine" (admins only, and why it is off), and its dialog (the other machine's images, the
// copy, what was loaded, whether it is the same image, and a refusal in the server's words).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { copyBlocked, copySources, listedLine, sameLine, type CopiedImage, type ListedImage } from "../lib/imageUpload";
import type { Machine, MachineCache } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const GIB = 1024 ** 3;
const ID = "sha256:" + "4f".repeat(32);
const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 48 * GIB,
  total_bytes: 100 * GIB, used_bytes: 0, partial_bytes: 0, partial_files: 0, truncated: false, remove_note: null, models: [], engines: [] };
const LISTED: ListedImage = { id: ID, tags: ["docker.io/acme/vision:1.4.1"], content: "sha256:" + "c".repeat(64), size: 3.2 * GIB,
  created: "2026-11-16T10:00:00Z", os: "linux", architecture: "amd64",
  matches: [{ recipe: "vision", variant: "cpu", image: "docker.io/acme/vision:1.4.1", how: "tag" }] };

function machines(sourceFeatures: string[], sourceStatus = "online", targetFeatures = ["workloads", "image_load"]) {
  return fixture.machines.map((m) => ({ ...m, fetch_via: "auto", image_upload_max_bytes: 64 * GIB,
    runtime_info: { process: { ok: true }, container: { ok: true, engine: "docker" } },
    ...(m.id === 1 ? { features: targetFeatures, status: "online" } : { features: sourceFeatures, status: sourceStatus }) })) as unknown as Machine[];
}

function machinePage(list: Machine[], { role = "admin", copy = { status: 200, body: {} as unknown } } = {}) {
  const body = { ...fixture, can_manage: role === "admin", machines: list };
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    let r: { status: number; body: unknown } = { status: 404, body: { detail: { code: "not_found", message: "Not found" } } };
    if (url === "/api/auth/state") r = { status: 200, body: { needs_setup: false, registration: "off", user: { ...OWNER, role } } };
    else if (url === "/api/machines") r = { status: 200, body };
    else if (url === "/api/machines/1/cache") r = { status: 200, body: cache };
    else if (url === "/api/downloads?machine_id=1") r = { status: 200, body: { downloads: [], can_download: true } };
    else if (url === "/api/machines/2/images") r = { status: 200, body: { machine_id: 2, images: [LISTED] } };
    else if (url === "/api/machines/1/images/copy" && init?.method === "POST") r = copy;
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  render(<MemoryRouter initialEntries={["/machines/1"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
  return fetch;
}

async function imagesSection() {
  return (await screen.findByRole("heading", { name: "Engine images" })).closest("section") as HTMLElement;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("copying an image from another machine", () => {
  it("FY-WEB-400 the words: which machines can send, why a copy is off, an image's line, and whether it is the same image", () => {
    const list = machines(["workloads", "image_save"]);
    expect(copySources(list[0], list).map((m) => m.name)).toEqual(["strix"]);
    expect(copyBlocked(list[0], list)).toBeNull();
    expect(copySources(list[0], machines(["workloads", "image_load"]))).toEqual([]);
    expect(copySources(list[0], machines(["workloads", "image_save"], "offline"))).toEqual([]);
    expect(copyBlocked(list[0], machines(["workloads"]))).toBe(
      "No other machine can send an image now: it needs a connected agent of 1.4.1 or later with a container engine.");
    const noLoad = machines(["image_save"], "online", ["workloads"]);
    expect(copyBlocked(noLoad[0], noLoad)).toContain("cannot load images");
    expect(listedLine(LISTED)).toBe("4f4f4f4f4f4f · 3.2 GiB · linux/amd64");
    expect(listedLine({ ...LISTED, size: null, architecture: null })).toBe("4f4f4f4f4f4f");
    const loaded: CopiedImage = { id: ID, tags: [], content: null, os: null, architecture: null, matches: [], same_as_source: true };
    expect(sameLine(loaded, "strix")).toBe("The same image as on strix (same content id).");
    expect(sameLine({ ...loaded, same_as_source: false }, "strix")).toBe("Not the same image as on strix: what was loaded differs from what it has.");
    expect(sameLine({ ...loaded, same_as_source: null }, "strix")).toBe("");
  });

  it("FY-WEB-401 admins copy when another machine can send; otherwise the button says why, and others see none", async () => {
    machinePage(machines(["workloads", "image_save"]));
    let section = await imagesSection();
    const btn = within(section).getByRole("button", { name: /Copy from another machine/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    cleanup();
    machinePage(machines(["workloads"]));
    section = await imagesSection();
    const off = within(section).getByRole("button", { name: /Copy from another machine/ }) as HTMLButtonElement;
    expect(off.disabled).toBe(true);
    expect(off.title).toContain("No other machine can send an image now");
    cleanup();
    machinePage(machines(["workloads", "image_save"]), { role: "operator" });
    section = await imagesSection();
    expect(within(section).queryByRole("button", { name: /Copy from another machine/ })).toBeNull();
  });

  it("FY-WEB-402 the dialog: the other machine's images, the copy, then what was loaded and whether it is the same image", async () => {
    const result = { ok: true, bytes: 3.2 * GIB, source: { id: 2, name: "strix" }, images: [{ id: ID, tags: ["docker.io/acme/vision:1.4.1"],
      content: LISTED.content, os: "linux", architecture: "amd64", same_as_source: true, matches: LISTED.matches }] };
    const fetch = machinePage(machines(["workloads", "image_save"]), { copy: { status: 200, body: result } });
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Copy from another machine/ }));
    const dlg = await screen.findByRole("dialog", { name: "Copy an image to a100-box" });
    expect((within(dlg).getByLabelText("From") as HTMLSelectElement).value).toBe("2");
    const radio = await within(dlg).findByRole("radio", { name: /docker.io\/acme\/vision:1.4.1/ });
    expect((radio as HTMLInputElement).checked).toBe(true);
    expect(within(dlg).getByText("4f4f4f4f4f4f · 3.2 GiB · linux/amd64")).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "Copy" }));
    await within(dlg).findByText("The same image as on strix (same content id).");
    const post = fetch.mock.calls.find(([u]) => u === "/api/machines/1/images/copy");
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({ source_id: 2, image: ID });
    expect(within(dlg).getByText(/Named as the image of vision \(cpu\)/)).toBeTruthy();
    expect(await screen.findByText("Copied docker.io/acme/vision:1.4.1 from strix to a100-box.")).toBeTruthy();
    expect(within(dlg).getAllByRole("button", { name: "Close" })).toHaveLength(2);
    expect(within(dlg).queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it("FY-WEB-403 a refused copy shows the server's words and can be tried again", async () => {
    machinePage(machines(["workloads", "image_save"]), { copy: { status: 400, body: { detail: { code: "upload_broken",
      message: "The upload broke off after 1048576 bytes (ConnectionError: strix: it broke off)." } } } });
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Copy from another machine/ }));
    const dlg = await screen.findByRole("dialog", { name: "Copy an image to a100-box" });
    await within(dlg).findByRole("radio", { name: /docker.io\/acme\/vision:1.4.1/ });
    fireEvent.click(within(dlg).getByRole("button", { name: "Copy" }));
    await within(dlg).findByText(/The upload broke off after 1048576 bytes/);
    await waitFor(() => expect((within(dlg).getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(false));
  });
});
