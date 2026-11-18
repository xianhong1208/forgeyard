// @vitest-environment jsdom
// A machine's engine images (1.4.2, issues #123 and #124): the images a recipe names, listed on the machine's page
// with their size, the recipes and the deployments that use them; Remove (admins) for one nothing uses, asked in its
// row first, with the space it gave back; why it is off (in use, the agent's own image, an agent to update, not an
// admin); and the copy dialog listing the images a recipe names first, the rest only on "Show all images".
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import {
  copyChoices, engineDiskLine, engineRemoveQuestion, recipesLine, removedLine,
  type EngineImage, type EngineImagesView, type ListedImage,
} from "../lib/imageUpload";
import type { Machine, MachineCache } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const GIB = 1024 ** 3;
const cache: MachineCache = { machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 48 * GIB,
  total_bytes: 100 * GIB, used_bytes: 0, partial_bytes: 0, partial_files: 0, truncated: false, remove_note: null, models: [], engines: [] };
const id = (c: string) => "sha256:" + c.repeat(64);
const LLAMA: EngineImage = { id: id("1"), tags: ["ghcr.io/ggml-org/llama.cpp:server-b12359"], content: id("e"), size: 1.9 * GIB,
  created: null, os: "linux", architecture: "amd64", agent: false, used_by: [], remove_note: null,
  matches: [{ recipe: "llama-cpp", variant: "cpu", image: "ghcr.io/ggml-org/llama.cpp:server-b12359@sha256:" + "9".repeat(64), how: "checked" }] };
const TTS: EngineImage = { ...LLAMA, id: id("2"), tags: ["registry.example/acme/tts:2.0"], size: 0.9 * GIB,
  matches: [{ recipe: "tts", variant: "cpu", image: "registry.example/acme/tts:2.0", how: "tag" }],
  used_by: [{ name: "tts-1", kind: "deployment", id: 7 }], remove_note: "In use by tts-1 on a100-box. Stop it first." };
const AGENT: EngineImage = { ...LLAMA, id: id("3"), tags: ["tonyhuang1101/forgeyard-agent:1.4.1"], size: 0.4 * GIB,
  matches: [{ recipe: "agent-named", variant: "cpu", image: "tonyhuang1101/forgeyard-agent:1.4.1", how: "tag" }],
  agent: true, remove_note: "The agent runs from this image." };
const VIEW: EngineImagesView = { machine_id: 1, listed: true, note: null, images: [LLAMA, TTS, AGENT], others: 2,
  free_bytes: 40 * GIB, total_bytes: 100 * GIB, can_remove: true, remove_note: null };
const OTHER: ListedImage = { id: id("4"), tags: ["someone/else:latest"], content: null, size: 50 * 1024 ** 2, created: null,
  os: "linux", architecture: "amd64", matches: [] };

function machines(features = ["workloads", "image_load", "image_save", "image_remove"]) {
  return fixture.machines.map((m) => ({ ...m, fetch_via: "auto", image_upload_max_bytes: 64 * GIB,
    runtime_info: { process: { ok: true }, container: { ok: true, engine: "docker" } },
    features, status: "online" })) as unknown as Machine[];
}

type Answer = { status: number; body: unknown };

function machinePage(list: Machine[], { role = "admin", view = VIEW as EngineImagesView, remove = { status: 200, body: {} } as Answer,
  source = [LLAMA, OTHER] as ListedImage[] } = {}) {
  const body = { ...fixture, can_manage: role === "admin", machines: list };
  let current = view;
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    let r: Answer = { status: 404, body: { detail: { code: "not_found", message: "Not found" } } };
    if (url === "/api/auth/state") r = { status: 200, body: { needs_setup: false, registration: "off", user: { ...OWNER, role } } };
    else if (url === "/api/machines") r = { status: 200, body };
    else if (url === "/api/machines/1/cache") r = { status: 200, body: cache };
    else if (url === "/api/downloads?machine_id=1") r = { status: 200, body: { downloads: [], can_download: true } };
    else if (url === "/api/machines/1/engine-images") r = { status: 200, body: current };
    else if (url === "/api/machines/2/images") r = { status: 200, body: { machine_id: 2, images: source } };
    else if (url === "/api/machines/1/engine-images/remove" && init?.method === "POST") {
      r = remove;
      if (remove.status === 200) current = { ...current, images: current.images.filter((i) => i.id !== LLAMA.id), free_bytes: 41.9 * GIB };
    }
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  render(<MemoryRouter initialEntries={["/machines/1"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
  return fetch;
}

async function imagesSection() {
  return (await screen.findByRole("heading", { name: /^Engine images/ })).closest("section") as HTMLElement;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe("a machine's engine images", () => {
  it("FY-WEB-440 the words: recipes, the question, what was freed, the disk, and the copy dialog's choices", () => {
    expect(recipesLine(LLAMA)).toBe("llama-cpp (cpu) · pinned, checked by its recorded ids");
    expect(recipesLine(TTS)).toBe("tts (cpu) · named by its tag");
    expect(engineRemoveQuestion("a100-box", LLAMA)).toBe(
      "Remove ghcr.io/ggml-org/llama.cpp:server-b12359 (1.9 GiB) from a100-box's container engine? A deployment that needs it pulls or loads it again.");
    expect(engineDiskLine({ free_bytes: 40 * GIB, total_bytes: 100 * GIB })).toBe("40 GiB free of 100 GiB");
    expect(engineDiskLine({ free_bytes: null, total_bytes: null })).toBeNull();
    const r = { ok: true as const, image: LLAMA, freed_bytes: 1.9 * GIB, free_bytes: null, total_bytes: null };
    expect(removedLine("a100-box", r)).toBe(
      "Removed ghcr.io/ggml-org/llama.cpp:server-b12359 from a100-box: up to 1.9 GiB freed (layers another image shares stay).");
    expect(removedLine("a100-box", { ...r, freed_bytes: GIB, free_bytes: 41 * GIB, total_bytes: 100 * GIB })).toBe(
      "Removed ghcr.io/ggml-org/llama.cpp:server-b12359 from a100-box: up to 1 GiB freed (layers another image shares stay); now 41 GiB free of 100 GiB.");
    expect(copyChoices([OTHER, LLAMA], false).map((i) => i.id)).toEqual([LLAMA.id]);
    expect(copyChoices([OTHER, LLAMA], true).map((i) => i.id)).toEqual([LLAMA.id, OTHER.id]);
    expect(copyChoices([OTHER], false)).toEqual([]);
  });

  it("FY-WEB-441 the list, and Remove asked in its row: the image goes, the space comes back; in use and the agent's own image stay", async () => {
    const removed = { ok: true, image: LLAMA, freed_bytes: 1.9 * GIB, free_bytes: 41.9 * GIB, total_bytes: 100 * GIB };
    const fetch = machinePage(machines(), { remove: { status: 200, body: removed } });
    const section = await imagesSection();
    const list = await within(section).findByRole("list", { name: "Engine images on a100-box" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(section).getByText("40 GiB free of 100 GiB")).toBeTruthy();
    expect(within(section).getByText(/2 other images are in this machine's engine\. No recipe names them/)).toBeTruthy();
    expect(within(list).getByRole("link", { name: "in use: tts-1" }).getAttribute("href")).toBe("/serving/7");
    expect(within(list).getByText("agent's own image")).toBeTruthy();
    const ttsBtn = within(list).getByRole("button", { name: "Remove registry.example/acme/tts:2.0" }) as HTMLButtonElement;
    expect(ttsBtn.disabled).toBe(true);
    expect(ttsBtn.title).toBe("In use by tts-1 on a100-box. Stop it first.");
    const agentBtn = within(list).getByRole("button", { name: "Remove tonyhuang1101/forgeyard-agent:1.4.1" }) as HTMLButtonElement;
    expect(agentBtn.disabled).toBe(true);
    expect(agentBtn.title).toBe("The agent runs from this image.");
    fireEvent.click(within(list).getByRole("button", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359" }));
    let ask = within(list).getByRole("group", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Cancel" }));
    expect(within(list).queryByRole("group")).toBeNull();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/1/engine-images/remove")).toBe(false);
    fireEvent.click(within(list).getByRole("button", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359" }));
    ask = within(list).getByRole("group", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359?" });
    expect(within(ask).getByText(/from a100-box's container engine\?/)).toBeTruthy();
    fireEvent.click(within(ask).getByRole("button", { name: /Remove the image/ }));
    expect(await screen.findByText("Removed ghcr.io/ggml-org/llama.cpp:server-b12359 from a100-box: up to 1.9 GiB freed (layers another image shares stay); now 41.9 GiB free of 100 GiB.")).toBeTruthy();
    const post = fetch.mock.calls.find(([u]) => u === "/api/machines/1/engine-images/remove");
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({ image: LLAMA.id });
    await waitFor(() => expect(within(section).queryByText("ghcr.io/ggml-org/llama.cpp:server-b12359")).toBeNull());
    expect(within(section).getByText("41.9 GiB free of 100 GiB")).toBeTruthy();
  });

  it("FY-WEB-442 a refusal in the server's words; no Remove for others; an agent to update; one that cannot list", async () => {
    machinePage(machines(), { remove: { status: 409, body: { detail: { code: "image_in_use",
      message: "a100-box: the engine keeps an image a container uses: someones-tts uses it" } } } });
    let section = await imagesSection();
    let list = await within(section).findByRole("list", { name: "Engine images on a100-box" });
    fireEvent.click(within(list).getByRole("button", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359" }));
    fireEvent.click(within(list).getByRole("button", { name: /Remove the image/ }));
    expect(await screen.findByText("a100-box: the engine keeps an image a container uses: someones-tts uses it")).toBeTruthy();
    expect(within(section).getByText("ghcr.io/ggml-org/llama.cpp:server-b12359")).toBeTruthy();
    cleanup();
    machinePage(machines(), { role: "operator", view: { ...VIEW, can_remove: false, remove_note: "Only admins can remove engine images." } });
    section = await imagesSection();
    list = await within(section).findByRole("list", { name: "Engine images on a100-box" });
    expect(within(list).queryByRole("button")).toBeNull();
    expect(within(section).queryByText("Only admins can remove engine images.")).toBeNull();
    cleanup();
    const note = "This machine's agent is too old to remove engine images. Update the agent.";
    machinePage(machines(["workloads", "image_load", "image_save"]), { view: { ...VIEW, remove_note: note,
      images: VIEW.images.map((i) => ({ ...i, remove_note: note })) } });
    section = await imagesSection();
    expect(await within(section).findByText(note)).toBeTruthy();
    expect((within(section).getByRole("button", { name: "Remove ghcr.io/ggml-org/llama.cpp:server-b12359" }) as HTMLButtonElement).disabled).toBe(true);
    cleanup();
    const fetch = machinePage(machines(["workloads", "image_load"]));
    section = await imagesSection();
    expect(within(section).getByText(/cannot list its engine images/)).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/1/engine-images")).toBe(false);
  });

  it("FY-WEB-443 the copy dialog lists the images a recipe names, the rest only on Show all images", async () => {
    const list = machines().map((m) => (m.id === 1 ? m : { ...m, features: ["workloads", "image_save"] }));
    machinePage(list, { source: [OTHER, LLAMA] });
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Copy from another machine/ }));
    const dlg = await screen.findByRole("dialog", { name: "Copy an image to a100-box" });
    const llama = await within(dlg).findByRole("radio", { name: /ghcr.io\/ggml-org\/llama.cpp:server-b12359/ }) as HTMLInputElement;
    expect(llama.checked).toBe(true);
    expect(within(dlg).getAllByRole("radio")).toHaveLength(1);
    expect(within(dlg).getByText("Engine images on strix (named by a recipe)")).toBeTruthy();
    const all = within(dlg).getByRole("checkbox", { name: /Show all images/ }) as HTMLInputElement;
    expect(all.checked).toBe(false);
    fireEvent.click(all);
    expect(within(dlg).getAllByRole("radio").map((r) => (r as HTMLInputElement).value)).toEqual([LLAMA.id, OTHER.id]);
    fireEvent.click(within(dlg).getByRole("radio", { name: /someone\/else:latest/ }));
    fireEvent.click(all);
    expect(within(dlg).getAllByRole("radio")).toHaveLength(1);
    expect((within(dlg).getByRole("radio") as HTMLInputElement).checked).toBe(true);
    cleanup();
    machinePage(list, { source: [OTHER] });
    fireEvent.click(within(await imagesSection()).getByRole("button", { name: /Copy from another machine/ }));
    const dlg2 = await screen.findByRole("dialog", { name: "Copy an image to a100-box" });
    expect(await within(dlg2).findByText("No image on strix is one a recipe names.")).toBeTruthy();
    expect((within(dlg2).getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dlg2).getByRole("checkbox", { name: /Show all images/ }));
    fireEvent.click(within(dlg2).getByRole("radio", { name: /someone\/else:latest/ }));
    expect((within(dlg2).getByRole("button", { name: "Copy" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
