// @vitest-environment jsdom
// The Playground's one model picker (1.4, #100): no Vision tab, the ready models grouped by kind like
// Serving, each kind opening its panel; 1.3's ?tab=vision address; the empty state (FY-WEB-380 to 386).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigationType } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { firstVision, pickerDetail, pickerGroups, routeKind } from "../lib/playground";
import type { Deployment, RouteInfo } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };

const UP = [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }];
const base = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [],
  usable: true, upstream_model: null, endpoints: UP };
let next = 0;
const route = (served_name: string, api: string, over: Record<string, unknown> = {}) =>
  ({ ...base, id: ++next, served_name, api, deployment_id: next, ...over }) as unknown as RouteInfo;

const VISION = { name: "det", task: "detect", title: "Object detection", endpoint: "/v1/vision/detect", deployment_id: 0, model: "PekingU/rtdetr_v2_r18vd",
  licence: "apache-2.0", ready: 1, running: true, reason: null };

// Every kind once, plus what must not be offered: a deployment with no ready replica, and a cloud model
// the person may not use.
function all(): RouteInfo[] {
  next = 0;
  return [
    route("det", "vision-detect"),
    route("qwen3-4b", "openai-chat"),
    route("bge-m3", "openai-embeddings"),
    route("gpt", "openai-chat", { kind: "cloud", deployment_id: null, endpoints: [], has_local: false, has_cloud: true }),
    route("qwen3-vl", "openai-chat"),
    route("starting", "openai-chat", { endpoints: [] }),
    route("not-mine", "openai-chat", { kind: "cloud", deployment_id: null, endpoints: [], has_local: false, has_cloud: true, usable: false }),
    route("dav2", "vision-depth"),
    route("reranker", "rerank"),
    route("whisper", "openai-audio"),
    route("kokoro", "openai-speech"),
    route("sd", "openai-images"),
    route("wan", "openai-videos"),
  ];
}
// qwen3-vl's deployment reads images: multimodal chat, as on Serving.
const dep = (id: number, task: string, modalities: string[] | null = null) => ({ id, model: { task, modalities } }) as unknown as Deployment;
const DEPLOYMENTS = [dep(2, "chat"), dep(5, "chat", ["image"])];

function server(routes: RouteInfo[], user = OWNER, extra?: (url: string) => unknown) {
  const fetch = vi.fn(async (url: string) => {
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user }
      : url === "/api/routes" ? { cloud: "off", is_admin: user.role === "admin", routes }
        : url === "/api/deployments" ? { deployments: DEPLOYMENTS, server_time: "", can_manage: true }
          : url === "/api/playground/vision/options" ? { models: [VISION, { ...VISION, name: "dav2", task: "depth", title: "Depth estimation" }], tasks: [] }
            : extra?.(url);
    return new Response(JSON.stringify(body ?? {}), { status: body === undefined ? 404 : 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  const how = useNavigationType();
  return <div data-testid="where">{l.pathname}{l.search} {how}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={["/start", path]} initialIndex={1}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const picker = async () => (await screen.findByLabelText("Model")) as HTMLButtonElement;

async function choose(name: RegExp) {
  fireEvent.click(await picker());
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Playground: one model picker (1.4, #100)", () => {
  it("FY-WEB-380 there are no tabs: one picker, the ready models grouped by kind in Serving's order, with its labels and icons", async () => {
    server(all());
    mount("/playground");
    const button = await picker();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
    // The first one offered: a chat model.
    expect(button.textContent).toContain("qwen3-4b");
    expect(button.textContent).toContain("Chat");
    expect(await screen.findByRole("heading", { name: "Chat" })).toBeTruthy();
    fireEvent.click(button);
    const list = screen.getByRole("listbox");
    const groups = within(list).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-labelledby"))).toEqual(
      ["chat", "multimodal", "embed", "rerank", "asr", "tts", "image", "video", "vision"].map((k) => `play-kind-${k}`));
    const label = (g: HTMLElement) => document.getElementById(g.getAttribute("aria-labelledby") ?? "")?.textContent;
    expect(groups.map(label)).toEqual(["Chat /v1/chat/completions", "Multimodal chat /v1/chat/completions", "Embeddings /v1/embeddings", "Rerank /v1/rerank",
      "Speech to text /v1/audio/transcriptions", "Text to speech /v1/audio/speech", "Images /v1/images/generations", "Video /v1/videos", "Vision /v1/vision/…"]);
    for (const g of groups) expect(document.getElementById(g.getAttribute("aria-labelledby") ?? "")?.querySelector("svg")).toBeTruthy();
    const names = (g: HTMLElement) => within(g).getAllByRole("option").map((o) => o.querySelector(".picker-option-name")?.textContent);
    expect(names(groups[0])).toEqual(["qwen3-4b", "gpt"]);
    expect(names(groups[1])).toEqual(["qwen3-vl"]);
    expect(names(groups[8])).toEqual(["det", "dav2"]);
    // Not ready, or a cloud model the person may not use: not offered.
    expect(within(list).queryByText("starting")).toBeNull();
    expect(within(list).queryByText("not-mine")).toBeNull();
    expect(within(groups[0]).getByRole("option", { name: /gpt/ }).textContent).toContain("cloud only");
    expect(within(groups[8]).getByRole("option", { name: /dav2/ }).textContent).toContain("depth");
    expect(within(groups[0]).getByRole("option", { name: /qwen3-4b/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("FY-WEB-381 picking a model opens its kind's panel and keeps it in the address", async () => {
    server(all(), OWNER, (url) => (url.startsWith("/api/playground/audio/voices") ? { voices: [], default: null } : undefined));
    mount("/playground");
    await choose(/bge-m3/);
    expect(await screen.findByRole("heading", { name: "Embeddings" })).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/playground?model=bge-m3 REPLACE");
    expect(screen.queryByRole("listbox")).toBeNull();
    await choose(/reranker/);
    expect(await screen.findByRole("heading", { name: "Rerank" })).toBeTruthy();
    await choose(/whisper/);
    expect(await screen.findByRole("heading", { name: "Speech to text" })).toBeTruthy();
    await choose(/kokoro/);
    expect(await screen.findByRole("heading", { name: "Speak" })).toBeTruthy();
    await choose(/qwen3-vl/);
    expect(await screen.findByRole("heading", { name: "Chat" })).toBeTruthy();
    expect((await picker()).textContent).toContain("Multimodal chat");
    await choose(/dav2/);
    const panel = (await screen.findByRole("heading", { name: "Depth estimation" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("POST /v1/vision/depth with model=dav2")).toBeTruthy();
    expect((await picker()).textContent).toContain("Vision");
    expect(screen.getByTestId("where").textContent).toBe("/playground?model=dav2 REPLACE");
    // The address opens that model again.
    cleanup();
    server(all());
    mount("/playground?model=det");
    expect(await screen.findByRole("heading", { name: "Object detection" })).toBeTruthy();
  });

  it("FY-WEB-382 1.3's Vision tab address opens the first ready vision model, replacing the old address", async () => {
    server(all());
    mount("/playground?tab=vision");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/playground?model=det REPLACE"));
    expect(await screen.findByRole("heading", { name: "Object detection" })).toBeTruthy();
    expect((await picker()).textContent).toContain("det");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
  });

  it("FY-WEB-383 the old address with no vision model ready opens the picker on the Playground", async () => {
    server(all().filter((r) => !r.api.startsWith("vision-")));
    mount("/playground?tab=vision");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/playground REPLACE"));
    const list = await screen.findByRole("listbox");
    expect(within(list).queryByRole("group", { name: /Vision/ })).toBeNull();
    expect(document.activeElement).toBe(list);
    // Escape closes it; the first chat model stays chosen.
    fireEvent.keyDown(list, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect((await picker()).textContent).toContain("qwen3-4b");
    // Any other old tab is dropped without opening anything.
    cleanup();
    server(all());
    mount("/playground?tab=models");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/playground REPLACE"));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("FY-WEB-384 nothing ready: the empty state sends deployers to Models → Catalog, says what is not ready yet, and others whom to ask", async () => {
    server([]);
    mount("/playground?tab=vision");
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();
    expect(screen.getByText(/No model is deployed yet/)).toBeTruthy();
    expect(screen.getByText("Models → Catalog")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Deploy from the catalog/ }).getAttribute("href")).toBe("/models");
    expect(screen.queryByLabelText("Model")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/playground REPLACE"));
    cleanup();
    next = 0;
    server([route("starting", "openai-chat", { endpoints: [] }), route("loading", "vision-detect", { endpoints: [] })]);
    mount("/playground");
    const empty = (await screen.findByText(/2 deployments are not ready: /)).closest("section") as HTMLElement;
    expect(within(empty).getByRole("link", { name: "Serving" }).getAttribute("href")).toBe("/serving");
    expect(screen.queryByLabelText("Model")).toBeNull();
    cleanup();
    server([], USER);
    mount("/playground");
    expect(await screen.findByText(/ask one to deploy the model you need/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Deploy from the catalog/ })).toBeNull();
  });

  it("FY-WEB-385 the picker by keyboard: arrows open and move, Enter picks, Home and End jump, a click outside closes", async () => {
    server(all());
    mount("/playground");
    const button = await picker();
    fireEvent.keyDown(button, { key: "ArrowDown" });
    const list = screen.getByRole("listbox");
    expect(list.getAttribute("aria-activedescendant")).toBe("play-model-opt-0");
    fireEvent.keyDown(list, { key: "ArrowDown" });
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(document.getElementById(list.getAttribute("aria-activedescendant") ?? "")?.textContent).toContain("qwen3-vl");
    fireEvent.keyDown(list, { key: "Enter" });
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(button.textContent).toContain("qwen3-vl");
    fireEvent.keyDown(button, { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "End" });
    fireEvent.keyDown(screen.getByRole("listbox"), { key: " " });
    expect(button.textContent).toContain("dav2");
    fireEvent.click(button);
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("FY-WEB-386 the helpers: a route's kind, what is offered, the small print, the first vision model", () => {
    const routes = all();
    expect(routes.map((r) => routeKind(r, DEPLOYMENTS))).toEqual(
      ["vision", "chat", "embed", "chat", "multimodal", "chat", "chat", "vision", "rerank", "asr", "tts", "image", "video"]);
    expect(routeKind(route("x", "something-new"), DEPLOYMENTS)).toBeNull();
    // Without the deployments (they did not load), a chat model is plain chat.
    expect(routeKind(routes[4], [])).toBe("chat");
    const groups = pickerGroups(routes, DEPLOYMENTS);
    expect(groups.flatMap(([, l]) => l).map((r) => r.served_name)).not.toContain("starting");
    expect(firstVision(groups)).toBe("det");
    expect(firstVision(pickerGroups(routes.filter((r) => r.api === "openai-chat"), DEPLOYMENTS))).toBeNull();
    expect(pickerDetail(routes[0])).toBe("object detection");
    expect(pickerDetail(routes[3])).toBe("cloud only");
    expect(pickerDetail({ ...routes[1], kind: "mixed" })).toBe("local, then cloud");
    expect(pickerDetail(routes[1])).toBe("");
  });
});
