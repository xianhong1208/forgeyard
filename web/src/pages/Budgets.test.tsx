// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { BudgetsResponse } from "../lib/budgets";
import type { ApiKey, KeysResponse, Provider, UsageResponse } from "../lib/types";

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

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const B = (budget: number | null, spent: number, level: "ok" | "warning" | "exhausted" | null, reserved = 0) =>
  ({ budget_micros: budget, spent_micros: spent, reserved_micros: reserved, used: budget ? spent / budget : null, level });

const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", models: null, rpm: null, tpm: null, effective_rpm: null, effective_tpm: null,
  expires_at: null, created_at: "2026-11-10T10:00:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
  user: { id: 2, username: "ursula" }, this_minute: { requests: 0, tokens: 0 }, cloud: "fallback",
  budget: B(5_000_000, 4_250_000, "warning", 130), budget_locked_by: null, ...over,
});

const keys = (over: Partial<KeysResponse> = {}): KeysResponse => ({
  keys: [key()], limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null }, is_admin: false,
  gateway_url: "https://forge.lan:7860/v1", models: [{ name: "chat", api: "openai-chat" }], cloud: { default: "off", max: "on" },
  month: "2026-11", resets_at: "2026-12-01T00:00:00Z", person_budget: B(null, 0, null), ...over,
});

describe("budgets on API keys", () => {
  it("shows spend against a key's budget with a meter, warns past 80%, and the owner sets it in dollars", async () => {
    const patches: unknown[] = [];
    server((url, init) => {
      if (init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)));
        return { status: 200, body: { api_key: key() } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys() };
    }, USER);
    mount("/keys");
    const meter = await screen.findByRole("meter", { name: /Budget of laptop/ });
    expect(meter.getAttribute("aria-valuenow")).toBe("85");
    const row = meter.closest("tr") as HTMLElement;
    expect(within(row).getByText("$4.25 of $5.00")).toBeTruthy();
    expect(within(row).getByText("past 80%")).toBeTruthy();
    expect(within(row).getByText(/\$0.00013 reserved/)).toBeTruthy();
    expect(screen.getByText(/laptop \(fyk_AbCdEfGh…\): budget past 80%/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    const field = screen.getByLabelText("Monthly budget (USD)") as HTMLInputElement;
    expect(field.value).toBe("5");
    fireEvent.change(field, { target: { value: "12.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(patches).toEqual([{ budget_micros: 12_500_000 }]));
  });

  it("a budget an admin set is locked for the owner; the admin unlocks it; a used-up person budget is said", async () => {
    const locked = key({ budget: B(1_000_000, 1_000_000, "exhausted"), budget_locked_by: "owner" });
    server((url) => (url.startsWith("/api/keys") ? { status: 200, body: keys({ keys: [locked],
      person_budget: B(20_000_000, 20_100_000, "exhausted") }) } : undefined), USER);
    mount("/keys");
    expect(await screen.findByText(/Your monthly cloud budget is used up: until December 1, 00:00 UTC/)).toBeTruthy();
    expect(screen.getByRole("meter", { name: /Your monthly budget/ }).getAttribute("aria-valuenow")).toBe("100");
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    expect((screen.getByLabelText("Monthly budget (USD)") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: /Unlock budget/ })).toBeNull();
    cleanup();

    const patches: unknown[] = [];
    server((url, init) => {
      if (init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)));
        return { status: 200, body: { api_key: key() } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys({ keys: [locked], is_admin: true }) };
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "Limits" }));
    fireEvent.click(screen.getByRole("button", { name: /Unlock budget/ }));
    await waitFor(() => expect(patches).toEqual([{ budget_micros: null }]));
  });

  it("refuses a budget that is not an amount of dollars", async () => {
    const fetch = server((url) => (url.startsWith("/api/keys") ? { status: 200, body: keys() } : undefined), USER);
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "Limits" }));
    fireEvent.change(screen.getByLabelText("Monthly budget (USD)"), { target: { value: "ten" } });
    fireEvent.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByText(/Monthly budget: US dollars/)).toBeTruthy();
    expect(fetch.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "PATCH")).toBe(false);
  });
});

const usage = (): UsageResponse => {
  const row = { id: "total", name: "Total", requests: 0, errors: 0, limited: 0, prompt_tokens: 0, completion_tokens: 0, tokens: 0,
    latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null, cost_micros: 0, unpriced: 0 };
  return { from: "2026-11-05", to: "2026-11-11", days: 7, group_by: "day", total: row, rows: [], everyone: true, is_admin: true };
};

const budgets = (): BudgetsResponse => ({
  month: "2026-11", resets_at: "2026-12-01T00:00:00Z", is_admin: true, everyone: true,
  people: [{ id: 1, username: "owner", status: "active", ...B(null, 0, null) }, { id: 2, username: "ursula", status: "active", ...B(10_000_000, 10_000_000, "exhausted") }],
  keys: [{ id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", status: "active", user: { id: 2, username: "ursula" }, locked_by: null, ...B(5_000_000, 4_100_000, "warning") }],
  providers: [{ id: 3, name: "openai", kind: "openai", enabled: true, ...B(100_000_000, 12_000_000, "ok") }],
});

describe("budgets on Usage → Cost", () => {
  it("shows every budget this month with warnings, and an admin sets a person's budget", async () => {
    const puts: [string, unknown][] = [];
    const fetch = server((url, init) => {
      if (init?.method === "PUT") {
        puts.push([url, JSON.parse(String(init.body))]);
        return { status: 200, body: {} };
      }
      if (url.startsWith("/api/budgets")) return { status: 200, body: budgets() };
      if (url.startsWith("/api/usage")) return { status: 200, body: usage() };
    });
    mount("/usage");
    fireEvent.click(await screen.findByRole("tab", { name: "Cost" }));
    const panel = (await screen.findByRole("heading", { name: "Budgets this month" })).closest("section") as HTMLElement;
    expect(within(panel).getByText(/ursula's budget is used up: it keeps requests local until December 1/)).toBeTruthy();
    expect(within(panel).getByText(/The budget of the key laptop \(ursula\) is past 80%/)).toBeTruthy();
    expect(within(panel).getByRole("meter", { name: /Budget of the provider openai/ }).getAttribute("aria-valuenow")).toBe("12");
    expect(within(panel).getByRole("link", { name: "openai" }).getAttribute("href")).toBe("/providers/3");
    fireEvent.click(within(panel).getByRole("button", { name: "Set budget" }));
    fireEvent.change(within(panel).getByLabelText("Monthly budget of owner (USD)"), { target: { value: "50" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toEqual([["/api/budgets/people/1", { budget_micros: 50_000_000 }]]));
    expect(fetch.mock.calls.some(([u]) => u === "/api/budgets")).toBe(true);
  });

  it("people who are not admins see their own budgets and cannot set them", async () => {
    server((url) => {
      if (url.startsWith("/api/budgets")) {
        return { status: 200, body: { ...budgets(), is_admin: false, everyone: false, providers: [],
          people: [{ id: 2, username: "ursula", status: "active", ...B(10_000_000, 2_000_000, "ok") }] } };
      }
      if (url.startsWith("/api/usage")) return { status: 200, body: { ...usage(), is_admin: false, everyone: false } };
    }, USER);
    mount("/usage");
    fireEvent.click(await screen.findByRole("tab", { name: "Cost" }));
    const panel = (await screen.findByRole("heading", { name: "Budgets this month" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("$2.00 of $10.00")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: /Set budget|Change/ })).toBeNull();
    expect(within(panel).getByRole("link", { name: "API keys" })).toBeTruthy();
  });
});

const PROVIDER: Provider = {
  id: 3, name: "openai", kind: "openai", kind_label: "OpenAI", base_url: "https://api.openai.com/v1", has_key: true, key_hint: "…9f2c",
  external: true, allow_http: false, enabled: true, timeout_s: null, created_by: "owner", created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z", headers: {}, models: [], budget: B(20_000_000, 20_000_000, "exhausted"),
};
const KINDS = [{ kind: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", base_url_hint: "", auth: "bearer", embeddings: true,
  models_endpoint: true, default_external: true, key_required: true, note: "" }];
const EGRESS = { proxy: false, allowed_hosts: [], ca_file: false, private_hosts: false, cooldown_s: 30, read_timeout_s: 300 };

describe("budgets on a provider", () => {
  it("shows a used-up budget, flags it in the list, and an admin changes it", async () => {
    const puts: [string, unknown][] = [];
    server((url, init) => {
      if (init?.method === "PUT") {
        puts.push([url, JSON.parse(String(init.body))]);
        return { status: 200, body: {} };
      }
      if (url === "/api/providers/3") return { status: 200, body: { provider: PROVIDER, kinds: KINDS, egress: EGRESS, is_admin: true } };
      if (url === "/api/providers") return { status: 200, body: { providers: [PROVIDER], kinds: KINDS, egress: EGRESS, is_admin: true } };
    });
    mount("/providers/3");
    const panel = (await screen.findByRole("heading", { name: "Monthly budget" })).closest("section") as HTMLElement;
    expect(within(panel).getByText(/budget is used up: openai is skipped/)).toBeTruthy();
    expect(within(panel).getByText("$20.00 of $20.00")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Monthly budget (USD)"), { target: { value: "" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save budget" }));
    await waitFor(() => expect(puts).toEqual([["/api/budgets/providers/3", { budget_micros: null }]]));
    cleanup();
    mount("/providers");
    const row = (await screen.findByRole("link", { name: "openai" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("budget used up")).toBeTruthy();
  });
});
