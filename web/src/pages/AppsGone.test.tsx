// @vitest-environment jsdom
// 1.4 (#88): apps left the console (deprecated; the API, the templates and /x/ go in 1.5). FY-WEB-330 to 332;
// the old addresses are FY-WEB-268, and Machines naming an app without a link FY-WEB-223 and 224.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { userHref } from "../lib/cache";
import type { ApiKey, KeysResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
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

// A key an admin gave every app at /x/ in 1.3: the setting stays in the API, the console no longer shows it.
const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", models: null, rpm: null, tpm: null, effective_rpm: null, effective_tpm: null,
  expires_at: null, created_at: "2026-11-10T10:00:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
  user: { id: 1, username: "owner" }, this_minute: { requests: 0, tokens: 0 }, cloud: "off",
  workloads: "listed", workload_list: ["web"], workloads_locked_by: "owner", ...over,
});

const keys = (): KeysResponse => ({
  keys: [key()], limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null }, is_admin: true,
  gateway_url: "https://forge.lan:7860/v1", models: [{ name: "chat", api: "openai-chat" }], cloud: { default: "off", max: "on" },
});

const bodies = (fetch: ReturnType<typeof server>, method: string) =>
  fetch.mock.calls.filter(([u, i]) => String(u).startsWith("/api/keys") && i?.method === method).map(([, i]) => JSON.parse(String(i?.body)));

describe("apps left the console (1.4, #88)", () => {
  it("FY-WEB-330 API keys has no Apps setting: not on a key's row, not when changing a key, not on a new key, and nothing is sent", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/keys" && init?.method === "POST") return { status: 201, body: { api_key: key({ id: "k2", name: "ci" }), key: "fyk_x", gateway_url: "https://forge.lan:7860/v1" } };
      if (init?.method === "PATCH") return { status: 200, body: { api_key: key() } };
      if (url.startsWith("/api/keys")) return { status: 200, body: keys() };
    });
    mount("/keys");
    const row = (await screen.findByText("laptop")).closest("tr") as HTMLElement;
    expect(within(row).queryByText(/Apps/)).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "Limits" }));
    expect(screen.queryByRole("group", { name: /Apps at \/x\// })).toBeNull();
    expect(screen.queryByText(/Apps at \/x\//)).toBeNull();
    expect(screen.queryByRole("button", { name: "Unlock apps" })).toBeNull();
    const rpm = screen.getByLabelText("Requests per minute");
    fireEvent.change(rpm, { target: { value: "30" } });
    fireEvent.click(within(rpm.closest("form") as HTMLElement).getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(bodies(fetch, "PATCH")).toEqual([{ rpm: 30 }]));
    cleanup();
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "New key" }));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "ci" } });
    expect(screen.queryByText(/Apps at \/x\//)).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: /apps at \/x\// })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(bodies(fetch, "POST")).toHaveLength(1));
    const sent = bodies(fetch, "POST")[0];
    expect("workloads" in sent || "workload_list" in sent).toBe(false);
  });

  it("FY-WEB-331 no menu, page or account menu offers apps; an old app address does not open an app page", async () => {
    server((url) => url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: true } } : undefined);
    mount("/apps/7");
    expect(await screen.findByRole("heading", { level: 1, name: "Machines" })).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).queryByRole("link", { name: /Apps|Workloads/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /owner/ }));
    expect(within(screen.getByRole("menu")).queryByText(/Apps|Workloads/)).toBeNull();
    expect(screen.queryByRole("link", { name: /New app/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: /^Apps$|^New app$/ })).toBeNull();
    // A machine's model cache names an app that uses a model, without a page to link to.
    expect(userHref({ kind: "workload", id: 7, name: "notebook" })).toBeNull();
    expect(userHref({ kind: "deployment", id: 3, name: "chat" })).toBe("/serving/3");
  });

  it("FY-WEB-332 the Playground offers vision models only, whatever the options say", async () => {
    server((url) => url === "/api/playground/vision/options"
      ? { status: 200, body: { models: [], tasks: [{ task: "detect", title: "Object detection", template: "object-detection", model: "PekingU/rtdetr_v2_r18vd" }],
        // what a 1.3 server answered: the console must not offer these
        workloads: [{ name: "detector", workload_id: 7, task: "detect", title: "Object detection", model: "m", licence: "apache-2.0",
          endpoint: "/v1/detect", path: "/x/detector", ready: 1, reason: null, max_body_mb: 32 }] } }
      : url === "/api/routes" ? { status: 200, body: { cloud: "off", is_admin: true, routes: [] } }
        : undefined);
    mount("/playground?tab=vision");
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();
    expect(screen.queryByText(/detector/)).toBeNull();
    expect(screen.queryByText("/x/detector")).toBeNull();
  });
});
