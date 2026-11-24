// @vitest-environment jsdom
// 1.4 (#81, #92): fewer places for one model. Serving lists what runs; Models is the library; a
// deployment's page holds its routing and fallback (its cloud fallback before 1.5.4); Cloud has Providers and Cloud models; the old addresses
// open the new places.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { fallbackProviders, ownNames, routeHome } from "../lib/routing";
import type { Deployment, ProvidersResponse, RouteInfo, RouteOptions, RouteTarget, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body?: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } } : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search + l.hash}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const where = () => screen.getByTestId("where").textContent;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const PROVIDER = { id: 3, name: "Gemini", kind: "gemini", external: true, enabled: true };
const local = (id: number, depId: number, name: string, tier = 0, enabled = true): RouteTarget => ({
  id, tier, kind: "local", weight: 1, enabled, upstream_model: name, cloud: false, provider_model: null,
  deployment: { id: depId, name, served_name: name, ready: 1, replicas: 1, concurrency: 2, context: 4096 },
});
const cloud = (id: number, tier = 1, enabled = true, provider = PROVIDER): RouteTarget => ({
  id, tier, kind: "provider", weight: 1, enabled, upstream_model: "gemini-2.5-flash", cloud: provider.external, deployment: null,
  provider_model: { id: 8, upstream_model: "gemini-2.5-flash", display_name: null, api: "openai-chat", context_length: null, provider },
});
const route = (over: Partial<RouteInfo> & Pick<RouteInfo, "id" | "served_name" | "targets">): RouteInfo => {
  const c = over.targets.some((t) => t.cloud);
  const l = over.targets.some((t) => !t.cloud);
  return { api: "openai-chat", upstream_model: null, deployment_id: null, local_only: false, spill_on_long_prompt: false, wait_ms: 0,
    kind: c && l ? "mixed" : c ? "cloud" : "local", has_local: l, has_cloud: c, endpoints: [], usable: true, created_by: "owner", ...over };
};

// accept-qwen3-4b with Gemini after it; plain has nothing after it; off has a switched-off Gemini tier.
const MIXED = route({ id: 5, served_name: "chat", deployment_id: 1, targets: [local(1, 1, "chat"), cloud(2)] });
const PLAIN = route({ id: 6, served_name: "embed", deployment_id: 2, api: "openai-embeddings", targets: [local(3, 2, "embed")] });
const OFF = route({ id: 7, served_name: "small", deployment_id: 4, targets: [local(6, 4, "small"), cloud(7, 1, false)] });
const CLOUD_ONLY = route({ id: 9, served_name: "flash", targets: [cloud(4, 0)] });
// A name of its own over chat, then OpenAI: listed before chat's own route, so the mark must come from chat's own.
const NAMED_MIXED = route({ id: 10, served_name: "smart", targets: [local(8, 1, "chat"), cloud(9, 1, true, { ...PROVIDER, id: 5, name: "openai" })] });
const ALIAS = route({ id: 11, served_name: "chat-pool", targets: [local(5, 1, "chat"), local(10, 4, "small")] });
// A provider inside your network (an own vLLM elsewhere) under a name of its own: still a cloud model's place.
const OWN_SERVER = route({ id: 12, served_name: "lab", targets: [cloud(11, 0, true, { ...PROVIDER, id: 4, name: "lab-vllm", external: false })] });
const ROUTES = [NAMED_MIXED, MIXED, PLAIN, OFF, CLOUD_ONLY, ALIAS, OWN_SERVER];

const OPTIONS: RouteOptions = {
  deployments: [{ id: 1, name: "chat", served_name: "chat", api: "openai-chat" }],
  provider_models: [{ id: 8, upstream_model: "gemini-2.5-flash", display_name: null, api: "openai-chat", provider: PROVIDER },
    { id: 12, upstream_model: "gpt-4o-mini", display_name: null, api: "openai-chat", provider: { ...PROVIDER, id: 5, name: "openai" } }],
};

const workload = (id: number, name: string): Workload => ({
  id: 10 + id, name, kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: 1, replicas: 1,
  spec: { name, kind: "service", runtime: { type: "model", recipe: "vllm", model: "m" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null },
  replica_list: [{ ordinal: 0, id: `r${id}`, generation: 1, machine: { id: 2, name: "cpu-01", status: "online", connected: true }, gpus: [],
    state: "healthy", reason: null, exit_code: null, attempt: 0, restarts: 0, final: false, pid: 1, container: null, ports: [],
    started_at: "2026-11-10T10:00:00Z", finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 0, download: null, runs: null }],
} as unknown as Workload);

const dep = (id: number, name: string, api = "openai-chat", over: Partial<Deployment> = {}): Deployment => ({
  id, name, recipe: "vllm", served_name: name, params: {},
  model: { id, name: `${name}-model`, source: "hf://x/y", task: api === "openai-chat" ? "chat" : "embed", format: "safetensors", revision: null, file: null, size_bytes: 1 },
  estimate: { method: "safetensors-kv", device: "cpu", gpus: 0, per_gpu: 0, ram: 2 ** 30, amount: 2 ** 30, kv_tokens: 0, notes: [], parts: [] },
  has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z", route: { served_name: name, api, upstream_model: name },
  endpoints: [], workload: workload(id, name), ...over,
} as Deployment);

const DEPLOYMENTS = [dep(1, "chat"), dep(2, "embed", "openai-embeddings"), dep(4, "small")];

function world(user = OWNER, extra?: (url: string, init?: RequestInit) => Reply | undefined) {
  const providers: ProvidersResponse = { providers: [], kinds: [], egress: { proxy: false, allowed_hosts: [], ca_file: false, private_hosts: false, cooldown_s: 30, read_timeout_s: 300 }, is_admin: true };
  return server((url, init) => extra?.(url, init) ?? (
    url === "/api/routes" ? { status: 200, body: { routes: ROUTES, cloud: "on", is_admin: user.role === "admin" } }
      : url === "/api/routes/5" ? { status: 200, body: { route: MIXED, is_admin: user.role === "admin" } }
        : url === "/api/routes/9" ? { status: 200, body: { route: CLOUD_ONLY, is_admin: user.role === "admin" } }
          : url === "/api/route-options" ? { status: 200, body: OPTIONS }
            : url === "/api/deployments" ? { status: 200, body: { deployments: DEPLOYMENTS, server_time: "", can_manage: true } }
              : url === "/api/deployments/1" ? { status: 200, body: { deployment: { ...DEPLOYMENTS[0], routing: MIXED, also_in: ["smart", "chat-pool"] }, events: [], server_time: "", can_manage: true } }
                : url === "/api/machines" ? { status: 200, body: { machines: [] } }
                  : url === "/api/models" ? { status: 200, body: { models: [], recipes: [], can_manage: true, can_deploy: true } }
                    : url === "/api/providers" ? { status: 200, body: providers }
                      : undefined), user);
}

describe("where each route lives", () => {
  it("FY-WEB-250 a deployment's own route is on its page; names an admin made are cloud models when a provider is in them, other names otherwise", () => {
    const { cloud: c, other } = ownNames(ROUTES);
    expect(c.map((r) => r.served_name)).toEqual(["smart", "flash", "lab"]);
    expect(other.map((r) => r.served_name)).toEqual(["chat-pool"]);
    expect(routeHome(MIXED)).toBe("/serving/1#routing");
    expect(routeHome(CLOUD_ONLY)).toBe("/cloud/models/9");
    expect(routeHome(ALIAS)).toBe("/cloud/models/11");
    // The list's mark: switched-on provider targets of the deployment's own route only.
    expect(fallbackProviders(ROUTES, 1)).toEqual(["Gemini"]);
    expect(fallbackProviders(ROUTES, 2)).toEqual([]);
    expect(fallbackProviders(ROUTES, 4)).toEqual([]);
    expect(fallbackProviders(ROUTES, 99)).toEqual([]);
  });
});

describe("navigation", () => {
  it("FY-WEB-251 the menu: Machines, Serving, Models, Cloud (admins only, as Providers was), the everyday pages, then the admin pages", async () => {
    world();
    const view = mount("/models");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const top = () => within(nav).getAllByRole("link").filter((a) => !a.className.includes("only-narrow")).map((a) => a.textContent);
    expect(top()).toEqual(["Machines", "Serving", "Models", "Cloud", "Playground", "API keys", "Usage", "Users", "Audit log"]);
    expect(within(nav).getByRole("link", { name: "Serving" }).getAttribute("href")).toBe("/serving");
    expect(within(nav).getByRole("link", { name: "Cloud" }).getAttribute("href")).toBe("/cloud");
    expect(within(nav).queryByRole("link", { name: /Providers|Apps|Workloads/ })).toBeNull();
    view.unmount();
    world(USER);
    mount("/models");
    const nav2 = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav2).getAllByRole("link").filter((a) => !a.className.includes("only-narrow")).map((a) => a.textContent))
      .toEqual(["Machines", "Serving", "Models", "Playground", "API keys", "Usage"]);
  });

  it("FY-WEB-252 old addresses open the new places, replacing the history entry", async () => {
    // Only addresses a released version (1.3.x) had; the names used while 1.4 was made are gone (FY-WEB-321).
    const cases: [string, string][] = [
      ["/providers", "/cloud"],
      ["/providers/3", "/cloud/providers/3"],
      ["/providers/3/", "/cloud/providers/3"],
      ["/providers/new/x", "/cloud"],
      ["/models?tab=routes", "/serving"],
      ["/models?tab=deployments", "/serving"],
      ["/models/routes/5", "/serving/1#routing"],
      ["/models/routes/9", "/cloud/models/9"],
      ["/models/routes/new", "/cloud/models/new"],
      ["/cloud/models/5", "/serving/1#routing"],
      // 1.4 (#92): what runs now is on Serving.
      ["/deployments", "/serving"],
      ["/models/deploy", "/serving/deploy"],
      ["/models/deploy?model=chat-model&machine=2", "/serving/deploy?model=chat-model&machine=2"],
      ["/models/deployments/1", "/serving/1"],
      ["/models/deployments/1#cloud-fallback", "/serving/1#cloud-fallback"],
      ["/models/deployments/1/edit", "/serving/1/edit"],
      ["/models/deployments/1/", "/serving/1"],
      ["/models/deployments/x/y", "/serving"],
    ];
    for (const [from, to] of cases) {
      world();
      const view = render(<MemoryRouter initialEntries={["/start", from]} initialIndex={1}><AuthProvider><App /><Where /><Back /></AuthProvider></MemoryRouter>);
      await waitFor(() => expect([from, where()]).toEqual([from, to]));
      // Replaced, not pushed: one step back is the page before the old link.
      fireEvent.click(screen.getByTestId("back"));
      await waitFor(() => expect([from, where()]).toEqual([from, "/start"]));
      view.unmount();
      cleanup();
      vi.unstubAllGlobals();
    }
  });
});

describe("Apps left the console (1.4, #88)", () => {
  it("FY-WEB-268 /workloads… (1.3) and /apps… open a model's deployment, or Machines for anything else, replacing the history entry", async () => {
    const cases: [string, string][] = [
      ["/workloads", "/machines"],
      ["/workloads/new", "/machines"],
      ["/workloads/new?template=vision-detect&gpu=1", "/machines"],
      ["/workloads/7", "/machines"],
      ["/apps", "/machines"],
      ["/apps/new?machine=2", "/machines"],
      ["/apps/7", "/machines"],
      ["/apps/7/x", "/machines"],
      // a model deployment's workload: its page is the deployment's
      ["/workloads/11", "/serving/1"],
      ["/apps/11", "/serving/1"],
      ["/workloads/11#x", "/serving/1#x"],
    ];
    for (const [from, to] of cases) {
      world();
      const view = render(<MemoryRouter initialEntries={["/start", from]} initialIndex={1}><AuthProvider><App /><Where /><Back /></AuthProvider></MemoryRouter>);
      await waitFor(() => expect([from, where()]).toEqual([from, to]));
      fireEvent.click(screen.getByTestId("back"));
      await waitFor(() => expect([from, where()]).toEqual([from, "/start"]));
      view.unmount();
      cleanup();
      vi.unstubAllGlobals();
    }
  });
});

function Back() {
  const navigate = useNavigate();
  return <button type="button" data-testid="back" onClick={() => void navigate(-1)}>back</button>;
}

describe("Serving", () => {
  it("FY-WEB-253 a deployment that can move on to a cloud model shows → its provider in the list, linked to its page", async () => {
    world();
    mount("/serving");
    const chat = await screen.findByRole("group", { name: "Deployment chat" });
    const mark = await within(chat).findByRole("link", { name: /→ Gemini/ });
    expect(mark.getAttribute("href")).toBe("/serving/1#routing");
    expect(within(screen.getByRole("group", { name: "Deployment embed" })).queryByText(/→/)).toBeNull();
    // Its Gemini tier is switched off: nothing to go to.
    expect(within(screen.getByRole("group", { name: "Deployment small" })).queryByText(/→/)).toBeNull();
    // Serving is one list: the library's tabs are on Models.
    expect(screen.queryByRole("tablist")).toBeNull();
  });
});

describe("a deployment's routing and fallback", () => {
  it("FY-WEB-254 is on the deployment's page: an admin adds a tier and saves the spill rules there", async () => {
    const puts: Record<string, unknown>[] = [];
    world(OWNER, (url, init) => {
      if (url === "/api/routes/5" && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body)));
        return { status: 200, body: { route: MIXED } };
      }
      return undefined;
    });
    mount("/serving/1#routing");
    const panel = await screen.findByRole("region", { name: "Routing and fallback" });
    expect(within(panel).getByText("Gemini/gemini-2.5-flash")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(await within(panel).findByLabelText("Add to tier 3"), { target: { value: "provider:12" } });
    fireEvent.change(within(panel).getByLabelText("Wait for a free local slot (ms)"), { target: { value: "250" } });
    fireEvent.click(within(panel).getByLabelText(/Spill a prompt that is too long/));
    fireEvent.click(within(panel).getByRole("button", { name: "Save routing" }));
    await waitFor(() => expect(puts).toEqual([{ local_only: false, spill_on_long_prompt: true, wait_ms: 250, targets: [
      { tier: 0, kind: "local", weight: 1, enabled: true, deployment_id: 1 },
      { tier: 1, kind: "provider", weight: 1, enabled: true, provider_model_id: 8 },
      { tier: 2, kind: "provider", weight: 1, enabled: true, provider_model_id: 12 }] }]));
    expect(await within(panel).findByText(/Routing saved/)).toBeTruthy();
    // The other names it answers as link to where they live.
    expect((await screen.findByRole("link", { name: "smart" })).getAttribute("href")).toBe("/cloud/models/10");
    expect(screen.getByRole("link", { name: "chat-pool" }).getAttribute("href")).toBe("/cloud/models/11");
  });

  it("FY-WEB-255 everyone else sees it, read-only, and never asks for the route options", async () => {
    const fetch = world(USER);
    mount("/serving/1");
    const panel = await screen.findByRole("region", { name: "Routing and fallback" });
    expect(within(panel).getByText("Gemini/gemini-2.5-flash")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: /Save/ })).toBeNull();
    expect((within(panel).getByLabelText(/Local only/) as HTMLInputElement).disabled).toBe(true);
    expect(fetch.mock.calls.some(([u]) => u === "/api/route-options")).toBe(false);
  });
});

describe("Cloud", () => {
  it("FY-WEB-256 has Providers and Cloud models; cloud models and other names are listed, a deployment's fallback only named", async () => {
    world();
    mount("/cloud");
    expect((await screen.findByRole("tab", { name: "Providers" })).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("heading", { level: 1, name: "Cloud" })).toBeTruthy();
    expect(await screen.findByText(/redirects never followed/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Cloud models" }));
    expect(where()).toBe("/cloud?tab=models");
    const models = await screen.findByRole("region", { name: "Cloud models" });
    expect(within(models).getAllByRole("article").map((a) => a.getAttribute("aria-label")))
      .toEqual(["Model name smart", "Model name flash", "Model name lab"]);
    const other = screen.getByRole("region", { name: "Other names for deployments" });
    expect(within(other).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["Model name chat-pool"]);
    // The deployments' own routes are not cards here: they are named with a link to their page.
    expect(screen.queryByRole("article", { name: "Model name chat" })).toBeNull();
    expect(screen.getByRole("link", { name: "chat" }).getAttribute("href")).toBe("/serving/1#routing");
    expect(screen.getByRole("link", { name: /New cloud model/ }).getAttribute("href")).toBe("/cloud/models/new");
  });

  it("FY-WEB-257 a new cloud model is made on Cloud and opens there; its page goes back to Cloud models", async () => {
    const posted: Record<string, unknown>[] = [];
    world(OWNER, (url, init) => {
      if (url === "/api/routes" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { route: CLOUD_ONLY } };
      }
      return undefined;
    });
    mount("/cloud/models/new");
    expect(await screen.findByRole("heading", { level: 1, name: "New cloud model" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Model name"), { target: { value: "flash" } });
    fireEvent.change(await screen.findByLabelText("Add to tier 1"), { target: { value: "provider:8" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(where()).toBe("/cloud/models/9"));
    expect(posted[0]).toMatchObject({ served_name: "flash", api: "openai-chat", targets: [{ tier: 0, kind: "provider", provider_model_id: 8 }] });
    expect(await screen.findByRole("heading", { level: 1, name: "flash" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Cloud models/ }).getAttribute("href")).toBe("/cloud?tab=models");
  });
});
