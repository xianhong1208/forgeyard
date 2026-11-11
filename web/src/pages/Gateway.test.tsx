// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { readEvents, snippets } from "../lib/gateway";
import type { ApiKey, KeysResponse, UsageResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body?: unknown; stream?: string[]; headers?: Record<string, string> };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: {} };
    if (r.stream) {
      const enc = new TextEncoder();
      const parts = r.stream;
      const body = new ReadableStream<Uint8Array>({
        start(c) { parts.forEach((p) => c.enqueue(enc.encode(p))); c.close(); },
      });
      return new Response(body, { status: r.status, headers: r.headers });
    }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}{l.search}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", models: null, rpm: null, tpm: null, effective_rpm: 60, effective_tpm: null,
  expires_at: null, created_at: "2026-11-10T10:00:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
  user: { id: 1, username: "owner" }, this_minute: { requests: 0, tokens: 0 }, cloud: "off", ...over,
});

const keys = (over: Partial<KeysResponse> = {}): KeysResponse => ({
  keys: [key()], limits: { default_rpm: 60, default_tpm: null, max_rpm: 120, max_tpm: null }, is_admin: true,
  gateway_url: "http://forge.lan:7860/v1", models: [{ name: "chat", api: "openai-chat" }, { name: "embed", api: "openai-embeddings" }],
  cloud: { default: "off", max: "off" }, ...over,
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("API keys", () => {
  it("creates a key, shows it once with ready snippets, and lists it by prefix", async () => {
    const posted: unknown[] = [];
    const fetch = server((url, init) => {
      if (url === "/api/keys" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { key: "fyk_SECRETVALUE0123456789", gateway_url: "http://forge.lan:7860/v1",
          api_key: key({ id: "k2", name: "ci", prefix: "fyk_SECRETVA", models: ["chat"] }) } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys() };
    });
    mount("/keys");
    expect(await screen.findByText("fyk_AbCdEfGh…")).toBeTruthy();
    expect(screen.getByText("http://forge.lan:7860/v1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /New key/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ci" } });
    fireEvent.click(screen.getByLabelText(/Only these/));
    fireEvent.click(within(screen.getByRole("group", { name: "Allowed models" })).getByLabelText("chat"));
    fireEvent.change(screen.getByLabelText("Requests per minute"), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("Expires"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));
    expect(await screen.findByText("Copy your key now")).toBeTruthy();
    expect(posted).toEqual([{ name: "ci", models: ["chat"], rpm: 30, tpm: null, expires_in_days: 30, cloud: "off", budget_micros: null }]);
    expect(screen.getByLabelText("The new API key").textContent).toBe("fyk_SECRETVALUE0123456789");
    const py = screen.getByLabelText("python example").textContent ?? "";
    expect(py).toContain('base_url="http://forge.lan:7860/v1"');
    expect(py).toContain('api_key="fyk_SECRETVALUE0123456789"');
    expect(py).toContain('model="chat"');
    fireEvent.click(screen.getByRole("tab", { name: "curl" }));
    expect(screen.getByLabelText("curl example").textContent).toContain("Authorization: Bearer fyk_SECRETVALUE0123456789");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText("fyk_SECRETVALUE0123456789")).toBeNull();
    expect(fetch.mock.calls.every(([, init]) => !(init?.headers as Record<string, string> | undefined)?.Authorization)).toBe(true);
  });

  it("revokes after asking, and admins switch to everyone's keys", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const deletes: string[] = [];
    const fetch = server((url, init) => {
      if (init?.method === "DELETE") {
        deletes.push(url);
        return { status: 200, body: { api_key: key({ status: "revoked" }) } };
      }
      if (url === "/api/keys?all=true") return { status: 200, body: keys({ keys: [key(), key({ id: "k9", name: "other", user: { id: 2, username: "ursula" } })] }) };
      if (url === "/api/keys") return { status: 200, body: keys() };
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(deletes).toEqual(["/api/keys/k1"]));
    expect(await screen.findByText("laptop is revoked.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Everyone's" }));
    expect(await screen.findByText("ursula")).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => u === "/api/keys?all=true")).toBe(true);
    expect(screen.getByRole("heading", { name: "Limits for keys" })).toBeTruthy();
  });

  it("an owner sees a limit an admin set as locked, cannot change it, and saves only the other one", async () => {
    const patches: unknown[] = [];
    const locked = key({ user: { id: 2, username: "ursula" }, rpm: 5, effective_rpm: 5, rpm_locked_by: "owner" });
    server((url, init) => {
      if (init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)));
        return { status: 200, body: { api_key: locked } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys({ is_admin: false, keys: [locked] }) };
    }, USER);
    mount("/keys");
    const chip = await screen.findByText(/set by owner/);
    expect(chip.getAttribute("title")).toBe("Set by the admin owner; only an admin can change it.");
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    const rpm = screen.getByLabelText("Requests per minute") as HTMLInputElement;
    expect(rpm.disabled).toBe(true);
    expect(screen.getByText(/only an admin can change it\./, { selector: "p" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Unlock/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("Tokens per minute"), { target: { value: "800" } });
    fireEvent.click(screen.getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(patches).toEqual([{ tpm: 800 }]));
  });

  it("an admin sees whose limit is locked and unlocks it", async () => {
    const patches: unknown[] = [];
    const theirs = key({ id: "k9", name: "other", user: { id: 2, username: "ursula" }, tpm: 100, effective_tpm: 100, tpm_locked_by: "owner" });
    server((url, init) => {
      if (init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)));
        return { status: 200, body: { api_key: { ...theirs, tpm: null, tpm_locked_by: null } } };
      }
      if (url === "/api/keys?all=true") return { status: 200, body: keys({ keys: [key(), theirs] }) };
      if (url === "/api/keys") return { status: 200, body: keys() };
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "Everyone's" }));
    await screen.findByText(/set by owner/);
    const row = screen.getByText("other").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Limits" }));
    expect((screen.getByLabelText("Tokens per minute") as HTMLInputElement).disabled).toBe(false);
    expect(screen.getByText(/Saving a value locks it for ursula/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Unlock tokens/ }));
    await waitFor(() => expect(patches).toEqual([{ tpm: null }]));
    expect(await screen.findByText(/unlocked; it is the default again/)).toBeTruthy();
  });

  it("users see only their keys, no admin switch and no defaults panel", async () => {
    server((url) => url.startsWith("/api/keys") ? { status: 200, body: keys({ is_admin: false }) } : undefined, USER);
    mount("/keys");
    expect(await screen.findByText("laptop")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Everyone's" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Limits for keys" })).toBeNull();
  });
});

const usage = (over: Partial<UsageResponse> = {}): UsageResponse => {
  const row = { id: "chat", name: "chat", requests: 12, errors: 1, limited: 1, prompt_tokens: 120, completion_tokens: 240, tokens: 360,
    latency_avg_ms: 420, latency_p50_ms: 380.5, latency_p95_ms: 1900 };
  return { from: "2026-11-04", to: "2026-11-10", days: 7, group_by: "model", total: { ...row, id: "total", name: "Total" }, rows: [row],
    everyone: false, is_admin: true, ...over };
};

describe("Usage", () => {
  it("people who are not admins see only their own usage", async () => {
    const fetch = server((url) => url.startsWith("/api/usage") ? { status: 200, body: usage({ is_admin: false }) } : undefined, USER);
    mount("/usage");
    expect(await screen.findByLabelText("Totals")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Everyone" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Person" })).toBeNull();
    expect(fetch.mock.calls.every(([u]) => !String(u).includes("everyone"))).toBe(true);
  });

  it("shows totals and rows, and changes the period, the grouping and whose usage", async () => {
    const fetch = server((url) => url.startsWith("/api/usage") ? { status: 200, body: usage() } : undefined);
    mount("/usage");
    const totals = await screen.findByLabelText("Totals");
    expect(within(totals).getByText("360")).toBeTruthy();
    expect(screen.getAllByText("1.9 s").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/usage?days=30&group=model")).toBe(true));
    expect(screen.queryByRole("button", { name: "Person" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Everyone" }));
    fireEvent.click(await screen.findByRole("button", { name: "Person" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/usage?days=30&group=user&everyone=true")).toBe(true));
  });

  it("has a Cost tab: cost per group, local and cloud, and the requests without a price", async () => {
    const base = { errors: 0, limited: 0, prompt_tokens: 100, completion_tokens: 50, tokens: 150, latency_avg_ms: 1, latency_p50_ms: 1, latency_p95_ms: 1 };
    const cloud = { ...base, id: "p:3", name: "openai", requests: 4, cost_micros: 1_850, unpriced: 2, cost_estimated: 1, cloud: true };
    const local = { ...base, id: "local", name: "Local replicas", requests: 12, cost_micros: 0, unpriced: 0, cloud: false };
    const total = { ...base, id: "total", name: "Total", requests: 16, cost_micros: 1_850, unpriced: 2, cost_estimated: 1,
      split: { local: { ...local, id: "local", name: "Local" }, cloud: { ...cloud, id: "cloud", name: "Cloud" } } };
    const fetch = server((url) => url.startsWith("/api/usage") ? { status: 200, body: usage({ group_by: url.includes("group=provider") ? "provider" : "day", total, rows: [cloud, local] }) } : undefined);
    mount("/usage");
    await screen.findByLabelText("Totals");
    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/usage?days=7&group=day")).toBe(true));
    const totals = await screen.findByLabelText("Cost totals");
    expect(within(totals).getAllByText("$0.00185").length).toBe(2);       // the total and the cloud part
    expect(within(totals).getByText("$0.00")).toBeTruthy();               // local
    expect(within(totals).getByText("75%")).toBeTruthy();                 // served locally
    expect(screen.getByText(/2 cloud requests have no price/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Set prices/ }).getAttribute("href")).toBe("/providers");
    expect(screen.getByText(/1 request was estimated/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Provider" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/usage?days=7&group=provider")).toBe(true));
    const row = (await screen.findAllByText("openai"))[0].closest("tr") as HTMLElement;
    expect(within(row).getByText("cloud")).toBeTruthy();
    expect(within(row).getByText("100%")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Requests" }));
    expect(await screen.findByLabelText("Totals")).toBeTruthy();
  });
});

const LOCAL = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [], usable: true };
const ROUTES = { cloud: "on", is_admin: true, routes: [
  { ...LOCAL, id: 2, served_name: "embed", api: "openai-embeddings", upstream_model: "embed", deployment_id: 2, endpoints: [] },
  { ...LOCAL, id: 1, served_name: "chat", api: "openai-chat", upstream_model: "chat", deployment_id: 1,
    endpoints: [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }] },
] };

describe("Playground", () => {
  it("streams a chat answer through the console endpoint, without an API key", async () => {
    const sent: { url: string; init?: RequestInit }[] = [];
    server((url, init) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/chat/completions") {
        sent.push({ url, init });
        return { status: 200, headers: { "Content-Type": "text/event-stream", "X-Forgeyard-Replica": "chat#1" }, stream: [
          'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n', 'data: {"choices":[{"delta":{"content":"lo"}}]}\n\ndata: {"choi',
          'ces":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n', "data: [DONE]\n\n"] };
      }
    });
    mount("/playground");
    expect(((await screen.findByLabelText("Model")) as HTMLSelectElement).value).toBe("chat");
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hi there" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const convo = screen.getByLabelText("Conversation");
    await waitFor(() => expect(within(convo).getByText("Hello")).toBeTruthy());
    expect(await within(convo).findByText(/replica 1 .* 5 in · 2 out/)).toBeTruthy();
    const [{ init }] = sent;
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Forgeyard"]).toBe("1");
    expect(headers.Authorization).toBeUndefined();
    const body = JSON.parse(String(init?.body));
    expect(body).toMatchObject({ model: "chat", stream: true, messages: [{ role: "user", content: "Hi there" }],
      chat_template_kwargs: { enable_thinking: false } });
  });

  it("shows the gateway's error, and a 401 goes to the sign-in page", async () => {
    let status = 503;
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/chat/completions") {
        return status === 503 ? { status: 503, body: { error: { message: "'chat' has no ready replica.", type: "service_unavailable", code: "no_ready_replica" } } }
          : { status: 401, body: { detail: { code: "session_ended", message: "Sign in to continue.", reason: "idle" } } };
      }
    });
    mount("/playground");
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("503: 'chat' has no ready replica.")).toBeTruthy();
    status = 401;
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "y" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).toContain("/login"));
    expect(screen.getByTestId("where").textContent).toContain("ended=idle");
  });
});

describe("cloud setting of keys (1.2)", () => {
  it("shows each key's cloud setting and creates one that may spill", async () => {
    const posted: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/keys" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { key: "fyk_X", gateway_url: "http://h/v1", api_key: key({ id: "k3", cloud: "fallback" }) } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys({ keys: [key(), key({ id: "k2", name: "spiller", cloud: "fallback" })] }) };
    });
    mount("/keys");
    const first = (await screen.findByText("laptop")).closest("tr") as HTMLElement;
    expect(within(first).getByText("Never")).toBeTruthy();
    expect(within(screen.getByText("spiller").closest("tr") as HTMLElement).getByText("Only to spill")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /New key/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "batch" } });
    const group = screen.getByRole("radiogroup", { name: "May this key use cloud providers?" });
    fireEvent.click(within(group).getByLabelText(/Only to spill/));
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(posted[0]).toMatchObject({ name: "batch", cloud: "fallback" }));
    expect(screen.getByRole("heading", { name: "Cloud for keys" })).toBeTruthy();
  });

  it("a user cannot go above the cap, and an admin's value is locked", async () => {
    const locked = key({ cloud: "fallback", cloud_locked_by: "owner" });
    server((url) => url.startsWith("/api/keys") ? { status: 200, body: keys({ is_admin: false, keys: [locked],
      cloud: { default: "off", max: "fallback" } }) } : undefined, USER);
    mount("/keys");
    expect(await screen.findByText(/set by owner/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    expect((screen.getByLabelText("Cloud providers") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /New key/ }));
    const group = screen.getByRole("radiogroup", { name: "May this key use cloud providers?" });
    expect((within(group).getByLabelText(/Allowed/) as HTMLInputElement).disabled).toBe(true);
    expect((within(group).getByLabelText(/Only to spill/) as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByRole("heading", { name: "Cloud for keys" })).toBeNull();
  });
});

describe("Playground and the cloud (1.2)", () => {
  it("lists only the models the person may use and names who answered", async () => {
    const routes = { cloud: "off", is_admin: false, routes: [
      { ...ROUTES.routes[1], kind: "mixed", has_cloud: true },
      { ...LOCAL, id: 7, served_name: "gpt", api: "openai-chat", upstream_model: null, deployment_id: null, endpoints: [], kind: "cloud",
        has_local: false, has_cloud: true, usable: false },
    ] };
    const sent: RequestInit[] = [];
    server((url, init) => {
      if (url === "/api/routes") return { status: 200, body: routes };
      if (url === "/api/playground/chat/completions") {
        sent.push(init as RequestInit);
        return { status: 200, headers: { "Content-Type": "text/event-stream", "X-Forgeyard-Target": "provider:openai/gpt-4o-mini",
          "X-Forgeyard-Spilled": "busy" }, stream: ['data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n', "data: [DONE]\n\n"] };
      }
    }, USER);
    mount("/playground");
    const select = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["chat"]);
    expect(select.options[0].textContent).toContain("local, then cloud");
    fireEvent.click(screen.getByLabelText(/Local only/));
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const convo = screen.getByLabelText("Conversation");
    expect(await within(convo).findByText(/openai\/gpt-4o-mini · spilled: busy/)).toBeTruthy();
    expect((sent[0].headers as Record<string, string>)["X-Forgeyard-Local-Only"]).toBe("1");
  });
});

describe("gateway helpers", () => {
  it("reads events split anywhere, with CRLF too", async () => {
    const enc = new TextEncoder();
    const chunks = ["data: a\r\n", "\r\ndata: b\n\nda", "ta: c"];
    const body = new ReadableStream<Uint8Array>({ start(c) { chunks.forEach((x) => c.enqueue(enc.encode(x))); c.close(); } });
    const got: string[] = [];
    await readEvents(body, (d) => got.push(d));
    expect(got).toEqual(["a", "b", "c"]);
  });

  it("writes a snippet per protocol", () => {
    expect(snippets("http://h/v1/", "fyk_x", "e", "openai-embeddings").python).toContain("client.embeddings.create(model=\"e\"");
    expect(snippets("http://h/v1", "fyk_x", "r", "rerank").curl).toContain("http://h/v1/rerank");
    expect(snippets("http://h/v1", "fyk_x", "a", "openai-audio").curl).toContain("-F model=a");
  });
});
