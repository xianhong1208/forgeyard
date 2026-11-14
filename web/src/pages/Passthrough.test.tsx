// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { buildGateway, gatewayForm, keyWorkloads, xCurl } from "../lib/passthrough";
import machinesFixture from "../test/machines.json";
import type { ApiKey, KeysResponse, Workload, WorkloadGateway } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const gateway = (over: Partial<WorkloadGateway> = {}): WorkloadGateway => ({
  expose: true, port: null, read_timeout: 600, max_body_mb: 32, concurrency: null, key_concurrency: null, exposed: true,
  path: "/x/detector", port_name: "http", guarded: [0], unguarded: [], ...over,
});

const detector: Workload = {
  id: 9, name: "detector", kind: "service", runtime: "process", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:00Z", status: "running", ready: 1, replicas: 1,
  spec: {
    name: "detector", kind: "service", runtime: { type: "process", command: "python3", args: ["app.py"], workdir: null }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: null, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null,
  },
  replica_list: [{
    ordinal: 0, id: "r0", generation: 1, machine: { id: 1, name: "cpu-box", status: "online", connected: true }, gpus: [],
    state: "running", reason: null, exit_code: null, attempt: 0, restarts: 0, final: false, pid: 77, container: null,
    ports: [{ name: "http", port: null, host_port: 31000, address: "192.168.1.20" }], started_at: "2026-11-14T10:00:00Z",
    finished_at: null, updated_at: "2026-11-14T10:00:00Z",
  }],
  gateway: gateway(),
};

const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", models: null, rpm: null, tpm: null, effective_rpm: null, effective_tpm: null,
  expires_at: null, created_at: "2026-11-10T10:00:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
  user: { id: 1, username: "owner" }, this_minute: { requests: 0, tokens: 0 }, cloud: "off", workloads: "none", workload_list: null, ...over,
});

const keys = (over: Partial<KeysResponse> = {}): KeysResponse => ({
  keys: [key()], limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null }, is_admin: true,
  gateway_url: "https://forge.example:7860/v1", models: [{ name: "chat", api: "openai-chat" }], cloud: { default: "off", max: "off" },
  workloads: [{ name: "detector", exposed: true, path: "/x/detector" }, { name: "notes", exposed: false, path: "/x/notes" }], ...over,
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("passthrough helpers", () => {
  it("FY-WEB-180 the gateway form sends only what differs from the defaults", () => {
    expect(buildGateway(gatewayForm())).toEqual({ expose: false });
    expect(buildGateway({ ...gatewayForm(), expose: true })).toEqual({ expose: true });
    expect(buildGateway({ expose: true, port: "api", readTimeout: "60", maxBodyMb: "32", concurrency: "2", keyConcurrency: "" }))
      .toEqual({ expose: true, port: "api", read_timeout: 60, concurrency: 2 });
    expect(gatewayForm(gateway({ concurrency: 4 })).concurrency).toBe("4");
    expect(keyWorkloads(key())).toBe("none");
    expect(keyWorkloads(key({ workloads: "listed", workload_list: ["a", "b"] }))).toBe("a, b");
    expect(keyWorkloads(key({ workloads: "all" }))).toBe("every exposed one");
    const curl = xCurl("https://forge.example:7860/", "detector", "https://forge.example:7860/trust");
    expect(curl).toContain("curl --cacert forgeyard-ca.pem https://forge.example:7860/x/detector/");
    expect(curl).toContain('-H "Authorization: Bearer $FORGEYARD_API_KEY"');
  });
});

describe("workloads at /x/", () => {
  it("FY-WEB-181 a new service can be exposed; the limits stay collapsed until wanted", async () => {
    const posted: unknown[] = [];
    server((url, init) => {
      if (url === "/api/machines") return { status: 200, body: machinesFixture };
      if (url === "/api/workloads" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { workload: detector, events: [] } };
      }
      if (url.startsWith("/api/workloads/9")) return { status: 200, body: { workload: detector, events: [], server_time: "x", can_manage: true } };
    });
    mount("/workloads/new");
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "detector" } });
    fireEvent.change(screen.getByLabelText("Image"), { target: { value: "python:3.13-slim" } });
    const expose = screen.getByRole("checkbox", { name: /Expose at/ });
    expect(screen.getByText("/x/detector")).toBeTruthy();
    expect(screen.queryByText("Limits and timeouts")).toBeNull();
    fireEvent.click(expose);
    const more = screen.getByText("Limits and timeouts").closest("details")!;
    expect(more.open).toBe(false);
    fireEvent.change(within(more).getByLabelText("Requests at once per replica"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create and start" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    const body = posted[0] as Record<string, unknown>;
    expect(body.gateway).toEqual({ expose: true, concurrency: 2 });
    expect(body.name).toBe("detector");
  });

  it("FY-WEB-182 an exposed workload's page shows its address, a curl call and its limits, and can stop exposing it", async () => {
    const puts: unknown[] = [];
    server((url, init) => {
      if (url === "/api/workloads/9/gateway" && init?.method === "PUT") {
        puts.push(JSON.parse(String(init.body)));
        return { status: 200, body: { workload: { ...detector, gateway: gateway({ expose: false, exposed: false }) } } };
      }
      if (url.startsWith("/api/workloads/9/replicas")) return { status: 200, body: { source: "live", text: "", truncated: false, state: "running" } };
      if (url === "/api/workloads/9") return { status: 200, body: { workload: detector, events: [], server_time: "x", can_manage: true,
        gateway_base: "https://forge.example:7860", trust_url: null } };
    });
    mount("/workloads/9");
    const panel = (await screen.findByRole("heading", { name: "Gateway" })).closest("section")!;
    expect(within(panel).getByLabelText("Address of detector").textContent).toBe("https://forge.example:7860/x/detector");
    expect(within(panel).getByLabelText("curl call to detector").textContent).toContain("https://forge.example:7860/x/detector/");
    expect(within(panel).getByText(/read timeout 600 s/)).toBeTruthy();
    // Behind the guard the direct address is not a link any more.
    expect(screen.queryByRole("link", { name: /192\.168\.1\.20:31000/ })).toBeNull();
    expect(screen.getByText(/key guard/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Change" }));
    fireEvent.click(within(panel).getByRole("checkbox", { name: /Expose at/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts).toEqual([{ expose: false }]));
  });

  it("FY-WEB-183 a replica outside the key guard is called out", async () => {
    const w = { ...detector, gateway: gateway({ guarded: [], unguarded: [0] }) };
    server((url) => {
      if (url.startsWith("/api/workloads/9/replicas")) return { status: 200, body: { source: "live", text: "", truncated: false, state: "running" } };
      if (url === "/api/workloads/9") return { status: 200, body: { workload: w, events: [], server_time: "x", can_manage: true } };
    });
    mount("/workloads/9");
    expect(await screen.findByText(/is not\s+behind the agent's key guard yet/)).toBeTruthy();
  });

  it("FY-WEB-184 the list marks exposed workloads with their path", async () => {
    server((url) => (url === "/api/workloads" ? { status: 200, body: { workloads: [detector], server_time: "x", can_manage: true } } : undefined));
    mount("/workloads");
    const card = await screen.findByRole("article", { name: "Workload detector" });
    expect(within(card).getByText("/x/detector")).toBeTruthy();
  });
});

describe("a key's workloads setting", () => {
  it("FY-WEB-185 a new key calls no workload unless you choose; the choice is collapsed and sent only when made", async () => {
    const posted: unknown[] = [];
    server((url, init) => {
      if (url === "/api/keys" && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return { status: 201, body: { key: "fyk_NEW", gateway_url: "https://forge.example:7860/v1", api_key: key({ workloads: "listed", workload_list: ["detector"] }) } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys() };
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: /New key/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "vision" } });
    const more = screen.getByText(/Workloads at \/x\//, { selector: "summary" }).closest("details")!;
    expect(more.open).toBe(false);
    expect(within(more).getByLabelText(/^None/)).toHaveProperty("checked", true);
    fireEvent.click(within(more).getByLabelText(/Listed workloads/));
    const group = within(more).getByRole("group", { name: "Allowed workloads" });
    expect(within(group).getByText("not exposed")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create key" })).toHaveProperty("disabled", true);
    fireEvent.click(within(group).getByLabelText("/x/detector"));
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ name: "vision", workloads: "listed", workload_list: ["detector"] });
  });

  it("FY-WEB-186 a key's row shows the setting and its lock; an admin changes it and can unlock it", async () => {
    const patches: unknown[] = [];
    server((url, init) => {
      if (url === "/api/keys/k1" && init?.method === "PATCH") {
        patches.push(JSON.parse(String(init.body)));
        return { status: 200, body: { api_key: key() } };
      }
      if (url.startsWith("/api/keys")) {
        return { status: 200, body: keys({ keys: [key({ workloads: "all", workloads_locked_by: "boss", user: { id: 2, username: "ursula" } })] }) };
      }
    });
    mount("/keys");
    expect(await screen.findByText(/Workloads: every exposed one/)).toBeTruthy();
    expect(screen.getAllByText(/set by boss/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    const form = screen.getByRole("form", { name: "Limits of laptop" });
    fireEvent.click(within(form).getByLabelText(/^None/));
    fireEvent.click(within(form).getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(patches).toEqual([{ workloads: "none", workload_list: null }]));
    fireEvent.click(screen.getByRole("button", { name: "Limits" }));
    fireEvent.click(screen.getByRole("button", { name: /Unlock workloads/ }));
    await waitFor(() => expect(patches).toEqual([{ workloads: "none", workload_list: null }, { workloads: null }]));
  });
});
