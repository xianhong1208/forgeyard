// @vitest-environment jsdom
// 1.5.4: a deployment's "cloud fallback" is its "Routing and fallback": a later tier may hold another local
// deployment as well as a cloud model. The editor's summary follows the tiers on screen and says when they are
// not saved (it used to read the saved route: "one tier" while two were shown).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import type { Deployment, RouteInfo, RouteOptions, RouteTarget, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const GEMINI = { id: 3, name: "Gemini", kind: "gemini", external: true, enabled: true };
const local = (id: number, depId: number, name: string, tier = 0): RouteTarget => ({
  id, tier, kind: "local", weight: 1, enabled: true, upstream_model: name, cloud: false, provider_model: null,
  deployment: { id: depId, name, served_name: name, ready: 1, replicas: 1, concurrency: 2, context: 4096 },
});
const gemini = (id: number, tier: number): RouteTarget => ({
  id, tier, kind: "provider", weight: 1, enabled: true, upstream_model: "gemini-2.5-flash", cloud: true, deployment: null,
  provider_model: { id: 8, upstream_model: "gemini-2.5-flash", display_name: null, api: "openai-chat", context_length: null, provider: GEMINI },
});
const route = (targets: RouteTarget[], over: Partial<RouteInfo> = {}): RouteInfo => {
  const c = targets.some((t) => t.cloud);
  const l = targets.some((t) => !t.cloud);
  return { id: 5, served_name: "chat-q4", api: "openai-chat", upstream_model: null, deployment_id: 1, local_only: false, spill_on_long_prompt: false,
    wait_ms: 0, kind: c && l ? "mixed" : c ? "cloud" : "local", has_local: l, has_cloud: c, endpoints: [], usable: true, created_by: "owner",
    targets, ...over };
};

const OPTIONS: RouteOptions = {
  deployments: [{ id: 1, name: "chat-q4", served_name: "chat-q4", api: "openai-chat" }, { id: 2, name: "chat-q8", served_name: "chat-q8", api: "openai-chat" }],
  provider_models: [{ id: 8, upstream_model: "gemini-2.5-flash", display_name: null, api: "openai-chat", provider: GEMINI }],
};

const workload = { id: 11, name: "chat-q4", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: 1, replicas: 1,
  spec: { name: "chat-q4", kind: "service", runtime: { type: "model", recipe: "vllm", model: "m" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null },
  replica_list: [] } as unknown as Workload;

const DEP = { id: 1, name: "chat-q4", recipe: "vllm", served_name: "chat-q4", params: {},
  model: { id: 1, name: "chat-model", source: "hf://x/y", task: "chat", format: "safetensors", revision: null, file: null, size_bytes: 1 },
  estimate: { method: "safetensors-kv", device: "cpu", gpus: 0, per_gpu: 0, ram: 2 ** 30, amount: 2 ** 30, kv_tokens: 0, notes: [], parts: [] },
  has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z", route: { served_name: "chat-q4", api: "openai-chat", upstream_model: "chat-q4" },
  endpoints: [], workload } as unknown as Deployment;

// A server that keeps the deployment's own route: a PUT replaces it, as the real one does.
function world(start: RouteInfo, named?: RouteInfo) {
  let saved = start;
  const puts: { targets: { tier: number; kind: string; deployment_id?: number; provider_model_id?: number }[] }[] = [];
  const reply = (url: string, init?: RequestInit): Reply | undefined => {
    if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } };
    if (url === "/api/routes/5" && init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      puts.push(body);
      let id = 100;
      const targets = body.targets.map((t: { tier: number; deployment_id?: number; provider_model_id?: number }) =>
        t.deployment_id ? local(id++, t.deployment_id, t.deployment_id === 1 ? "chat-q4" : "chat-q8", t.tier) : gemini(id++, t.tier));
      saved = route(targets, { local_only: body.local_only, spill_on_long_prompt: body.spill_on_long_prompt, wait_ms: body.wait_ms });
      return { status: 200, body: { route: saved } };
    }
    if (url === "/api/routes/9" && named) return { status: 200, body: { route: named, is_admin: true } };
    if (url === "/api/route-options") return { status: 200, body: OPTIONS };
    if (url === "/api/routes") return { status: 200, body: { routes: [saved], cloud: "on", is_admin: true } };
    if (url === "/api/deployments/1") return { status: 200, body: { deployment: { ...DEP, routing: saved, also_in: [] }, events: [], server_time: "", can_manage: true } };
    return undefined;
  };
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const r = reply(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  }));
  return puts;
}

const mount = (path: string) => render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
const summary = (within_: HTMLElement) => within(within_).getByTestId("route-summary").textContent;
// The route's kind, beside the section's title.
const kind = (panel: HTMLElement) => panel.querySelector(".panel-head .chip")?.textContent;
const ONE_TIER = "Only one tier has a target: a request has nowhere else to go. Put another local deployment or a cloud model in a later tier.";
const RULES = "Moves to the next tier when no local replica is ready, every replica is busy, the engine answers 429 or 503. Never once the answer has started.";

describe("a deployment's routing and fallback (1.5.4)", () => {
  it("FY-WEB-970 a second local deployment goes in a later tier: the summary and kind follow the tiers on screen, marked not saved until saved", async () => {
    const puts = world(route([local(1, 1, "chat-q4")]));
    mount("/serving/1#routing");
    const panel = await screen.findByRole("region", { name: "Routing and fallback" });
    expect(panel.id).toBe("routing");
    expect(within(panel).getByText(/another local deployment or a cloud model\. Targets in one tier share the requests\./)).toBeTruthy();
    expect(summary(panel)).toBe(ONE_TIER);
    // The gateway panel names the section, and offers to add a tier.
    const gw = screen.getByRole("region", { name: "Through the gateway" });
    expect(within(gw).getByText("Routing and fallback")).toBeTruthy();
    expect(within(gw).getByRole("link", { name: "Add one" }).getAttribute("href")).toBe("#routing");

    fireEvent.click(within(panel).getByRole("button", { name: /Add a tier/ }));
    // An empty tier is dropped on save, so it does not count yet.
    expect(summary(panel)).toBe(ONE_TIER);
    fireEvent.change(await within(panel).findByLabelText("Add to tier 2"), { target: { value: "local:2" } });
    expect(summary(panel)).toBe(`${RULES}Not saved yet.`);
    expect(kind(panel)).toBe("local");
    // A cloud model after it: the kind says so before saving.
    fireEvent.click(within(panel).getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(await within(panel).findByLabelText("Add to tier 3"), { target: { value: "provider:8" } });
    expect(kind(panel)).toBe("local, then cloud");

    fireEvent.click(within(panel).getByRole("button", { name: "Save routing" }));
    await waitFor(() => expect(puts.map((p) => p.targets)).toEqual([[
      { tier: 0, kind: "local", weight: 1, enabled: true, deployment_id: 1 },
      { tier: 1, kind: "local", weight: 1, enabled: true, deployment_id: 2 },
      { tier: 2, kind: "provider", weight: 1, enabled: true, provider_model_id: 8 }]]));
    expect(await within(panel).findByText("Routing saved. It applies to the next request.")).toBeTruthy();
    await waitFor(() => expect(summary(panel)).toBe(RULES));
    await waitFor(() => expect(within(gw).getByRole("link", { name: "Change" })).toBeTruthy());
    expect(gw.textContent).toContain("then chat-q8 → then Gemini/gemini-2.5-flash");
  });

  it("FY-WEB-971 a change to the rules alone is marked not saved, and undoing it clears the mark", async () => {
    world(route([local(1, 1, "chat-q4"), local(2, 2, "chat-q8", 1)]));
    mount("/serving/1");
    const panel = await screen.findByRole("region", { name: "Routing and fallback" });
    expect(summary(panel)).toBe(RULES);
    const wait = within(panel).getByLabelText("Wait for a free local slot (ms)");
    fireEvent.change(wait, { target: { value: "250" } });
    expect(summary(panel)).toBe("Moves to the next tier when no local replica is ready, every replica is busy, the engine answers 429 or 503 (after waiting up to 250 ms for a slot). Never once the answer has started.Not saved yet.");
    fireEvent.change(wait, { target: { value: "0" } });
    expect(summary(panel)).toBe(RULES);
    // Two local tiers: still a local route, with Change (not Add one) in the gateway panel.
    expect(kind(panel)).toBe("local");
    expect(within(screen.getByRole("region", { name: "Through the gateway" })).getByRole("link", { name: "Change" })).toBeTruthy();
  });

  it("FY-WEB-972 a link to the old #cloud-fallback still opens the page at the section", async () => {
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.id); } as Element["scrollIntoView"];
    world(route([local(1, 1, "chat-q4")]));
    mount("/serving/1#cloud-fallback");
    await screen.findByRole("region", { name: "Routing and fallback" });
    await waitFor(() => expect(scrolled).toContain("routing"));
  });

  it("FY-WEB-973 in Traditional Chinese: 分流與備援, 新增一層, 儲存分流與備援, and the summary", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    world(route([local(1, 1, "chat-q4")]));
    mount("/serving/1");
    const panel = await screen.findByRole("region", { name: "分流與備援" });
    expect(within(panel).getByText("這個部署忙碌或停擺時，請求可以依序交給後面的層：另一個本機部署或雲端模型。同一層裡的目標一起分攤請求。能不能使用雲端，由 API 金鑰決定。")).toBeTruthy();
    expect(summary(panel)).toBe("只有一層有目標：請求沒有別的地方可去。把另一個本機部署或雲端模型放進後面的層。");
    expect(within(panel).getByRole("link", { name: "怎麼讓兩個本機部署分攤請求？" }).getAttribute("href")).toContain("share-requests-between-two-local-deployments");
    expect(within(panel).getByRole("link", { name: "怎麼讓部署溢出到雲端模型？" }).getAttribute("href")).toContain("let-a-deployment-spill-to-a-cloud-model");
    fireEvent.click(within(panel).getByRole("button", { name: /新增一層/ }));
    fireEvent.change(await within(panel).findByLabelText("加入第 2 層"), { target: { value: "local:2" } });
    expect(summary(panel)).toContain("尚未儲存。");
    expect(within(panel).getByRole("button", { name: "儲存分流與備援" })).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "經由閘道" })).getByText("分流與備援")).toBeTruthy();
  });

  it("FY-WEB-974 in English the help links point at the general how-to and the cloud one", async () => {
    world(route([local(1, 1, "chat-q4")]));
    mount("/serving/1");
    const panel = await screen.findByRole("region", { name: "Routing and fallback" });
    expect(within(panel).getByRole("link", { name: "How do I share requests between two local deployments?" }).getAttribute("href"))
      .toContain("/how-to#share-requests-between-two-local-deployments");
    expect(within(panel).getByRole("link", { name: "How do I let a deployment spill to a cloud model?" }).getAttribute("href"))
      .toContain("/cloud-providers#let-a-deployment-spill-to-a-cloud-model");
    expect(within(panel).queryByText(/Cloud fallback/)).toBeNull();
  });

  it("FY-WEB-975 a name of its own (Cloud → Cloud models): the summary follows the editor too", async () => {
    world(route([local(1, 1, "chat-q4")]), route([gemini(4, 0)], { id: 9, served_name: "flash", deployment_id: null }));
    mount("/cloud/models/9");
    await screen.findByRole("heading", { level: 1, name: "flash" });
    const status = document.querySelector(".wl-status") as HTMLElement;
    expect(summary(status)).toBe(ONE_TIER);
    expect(within(status).getByText("cloud only")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(await screen.findByLabelText("Add to tier 2"), { target: { value: "local:2" } });
    expect(summary(status)).toBe(`${RULES}Not saved yet.`);
    expect(within(status).getByText("local, then cloud")).toBeTruthy();
  });
});
