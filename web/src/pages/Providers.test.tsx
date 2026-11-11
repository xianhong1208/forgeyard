// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { Provider, ProvidersResponse, RouteInfo, RouteOptions } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };

function server(route: (url: string, init?: RequestInit) => { status: number; body?: unknown } | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } } : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const KINDS = [
  { kind: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", base_url_hint: "", auth: "bearer", embeddings: true, models_endpoint: true, default_external: true, key_required: true, note: "Complete on Chat Completions." },
  { kind: "anthropic", label: "Anthropic", base_url: "https://api.anthropic.com/v1", base_url_hint: "", auth: "anthropic", embeddings: false, models_endpoint: true, default_external: true, key_required: true, note: "No prompt caching, no thinking returned." },
];
const EGRESS = { proxy: false, allowed_hosts: [], ca_file: false, private_hosts: false, cooldown_s: 30, read_timeout_s: 300 };
const PROVIDER: Provider = {
  id: 3, name: "openai", kind: "openai", kind_label: "OpenAI", base_url: "https://api.openai.com/v1", has_key: true, key_hint: "…9f2c",
  external: true, allow_http: false, enabled: true, timeout_s: null, created_by: "owner", created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z", headers: {},
  models: [{ id: 8, provider_id: 3, upstream_model: "gpt-4o-mini", api: "openai-chat", display_name: null, context_length: null, drop_params: [],
    price_input: null, price_output: null, price_cache_read: null, price_cache_write: null, price_source: null, cooldown_s: 12, in_flight: 0,
    sent: 4, created_at: "2026-11-11T00:00:00Z", routes: ["chat"] }],
};

describe("Providers", () => {
  it("lists providers with the key's last characters only, and adds one from a preset", async () => {
    const posted: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/providers" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { provider: { ...PROVIDER, id: 4, name: "anthropic" } } };
      }
      if (url === "/api/providers") return { status: 200, body: { providers: [PROVIDER], kinds: KINDS, egress: EGRESS, is_admin: true } satisfies ProvidersResponse };
    });
    mount("/providers");
    const row = (await screen.findByRole("link", { name: "openai" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("…9f2c")).toBeTruthy();
    expect(within(row).getByText("cloud")).toBeTruthy();
    expect(within(row).getByText(/cooling 12 s/)).toBeTruthy();
    expect(screen.getByText(/redirects never followed/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Add a provider/ }));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Kind of provider" })).getByLabelText("Anthropic"));
    expect(screen.getByText(/No prompt caching/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "sk-ant-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
    await waitFor(() => expect(posted[0]).toMatchObject({ name: "Anthropic", kind: "anthropic", api_key: "sk-ant-secret", external: true,
      base_url: null }));
    expect(await screen.findByText(/anthropic added/)).toBeTruthy();
    expect(document.body.textContent).not.toContain("sk-ant-secret");
  });

  it("is for admins only", async () => {
    server(() => undefined, USER);
    mount("/providers");
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
  });
});

const ROUTE: RouteInfo = {
  id: 5, served_name: "chat", api: "openai-chat", upstream_model: "chat", deployment_id: 1, local_only: false, spill_on_long_prompt: false,
  wait_ms: 0, kind: "local", has_local: true, has_cloud: false, endpoints: [], usable: true,
  targets: [{ id: 1, tier: 0, kind: "local", weight: 1, enabled: true, upstream_model: "chat", cloud: false, provider_model: null,
    deployment: { id: 1, name: "chat", served_name: "chat", ready: 1, replicas: 1, concurrency: 2, context: 4096 } }],
};
const OPTIONS: RouteOptions = {
  deployments: [{ id: 1, name: "chat", served_name: "chat", api: "openai-chat" }],
  provider_models: [{ id: 8, upstream_model: "gpt-4o-mini", display_name: null, api: "openai-chat",
    provider: { id: 3, name: "openai", kind: "openai", external: true, enabled: true } }],
};

describe("Route editor", () => {
  it("adds a cloud tier after the deployment and saves the spill rules", async () => {
    const puts: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/routes/5" && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body)));
        return { status: 200, body: { route: ROUTE } };
      }
      if (url === "/api/routes/5") return { status: 200, body: { route: ROUTE, is_admin: true } };
      if (url === "/api/route-options") return { status: 200, body: OPTIONS };
    });
    mount("/models/routes/5");
    expect(await screen.findByText("1/1 ready · 2 at once each")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Add a tier/ }));
    fireEvent.change(screen.getByLabelText("Add to tier 2"), { target: { value: "provider:8" } });
    expect(screen.getByText("openai/gpt-4o-mini")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Wait for a free local slot (ms)"), { target: { value: "250" } });
    fireEvent.click(screen.getByLabelText(/Spill a prompt that is too long/));
    fireEvent.click(screen.getByRole("button", { name: "Save routing" }));
    await waitFor(() => expect(puts).toEqual([{ local_only: false, spill_on_long_prompt: true, wait_ms: 250, targets: [
      { tier: 0, kind: "local", weight: 1, enabled: true, deployment_id: 1 },
      { tier: 1, kind: "provider", weight: 1, enabled: true, provider_model_id: 8 }] }]));
    expect(await screen.findByText(/Routing saved/)).toBeTruthy();
  });

  it("is read-only for everyone else", async () => {
    server((url) => url === "/api/routes/5" ? { status: 200, body: { route: ROUTE, is_admin: false } } : undefined, USER);
    mount("/models/routes/5");
    expect(await screen.findByText("1/1 ready · 2 at once each")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save routing" })).toBeNull();
    expect((screen.getByLabelText(/Local only/) as HTMLInputElement).disabled).toBe(true);
  });
});
