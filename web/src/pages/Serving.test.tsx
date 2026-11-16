// @vitest-environment jsdom
// 1.4 (#92): Serving is the models running now (with Deploy a model and each deployment's page), and
// Models is the library: Catalog, Engines and, for admins, Sources. FY-WEB-320 to 325.
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { libraryTab } from "./Models";
import type { CatalogResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const OPERATOR = { ...OWNER, id: 2, username: "olga", role: "operator", is_owner: false };
const USER = { ...OWNER, id: 3, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body: unknown };

const CATALOG: CatalogResponse = {
  can_manage: true, can_deploy: true, models: [],
  recipes: [{ name: "vllm", title: "vLLM", description: "Chat models", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"],
    api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"], params: {},
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: ">=7.0", driver: ">=580", engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
};

function server(user: Record<string, unknown> = OWNER, extra?: (url: string) => Reply | undefined) {
  const admin = user.role === "admin";
  const fetch = vi.fn(async (url: string) => {
    const r: Reply = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : extra?.(url) ?? (url === "/api/models"
        ? { status: 200, body: { ...CATALOG, can_manage: admin, can_deploy: user.role !== "user", sources: admin ? CATALOG.sources : undefined } }
        : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: user.role !== "user" } }
          : url === "/api/routes" ? { status: 200, body: { routes: [], cloud: "off", is_admin: admin } }
            : { status: 404, body: {} });
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

describe("Serving (1.4, #92)", () => {
  it("FY-WEB-320 Serving lists what runs, deploys under /serving and is the menu's active entry on a deployment's page", async () => {
    server();
    const view = mount("/serving");
    expect(await screen.findByRole("heading", { level: 1, name: "Serving" })).toBeTruthy();
    expect(await screen.findByText("No deployments yet")).toBeTruthy();
    expect((await screen.findByRole("link", { name: "Deploy a model" })).getAttribute("href")).toBe("/serving/deploy");
    // The catalog is empty: the empty state sends admins to the library to add one.
    expect((await screen.findByRole("link", { name: "Add a model" })).getAttribute("href")).toBe("/models");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Serving" }).className).toContain("active");
    expect(within(nav).getByRole("link", { name: "Models" }).className).not.toContain("active");
    view.unmount();
    mount("/serving/deploy");
    expect(await screen.findByRole("heading", { level: 1, name: "Deploy a model" })).toBeTruthy();
    const nav2 = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav2).getByRole("link", { name: "Serving" }).className).toContain("active");
    expect(screen.getByRole("link", { name: "← Serving" }).getAttribute("href")).toBe("/serving");
  });

  it("FY-WEB-321 the names used only while 1.4 was made no longer redirect: they land on the Catalog and stay", async () => {
    for (const from of ["/models?tab=serving", "/models?tab=serving&route=chat", "/models?tab=library", "/models?tab=model-library"]) {
      const fetch = server();
      const view = mount(from);
      expect((await screen.findByRole("tab", { name: "Catalog (0)" })).getAttribute("aria-selected")).toBe("true");
      expect(where()).toBe(from);
      // Nothing looks the route up any more (1.4's ?tab=serving&route= did).
      expect(fetch.mock.calls.some(([u]) => u === "/api/routes")).toBe(false);
      view.unmount();
      cleanup();
      vi.unstubAllGlobals();
    }
    expect(libraryTab("serving", true)).toBe("catalog");
    expect(libraryTab("routes", true)).toBeNull();
    expect(libraryTab("deployments", false)).toBeNull();
    expect(libraryTab(null, true)).toBe("catalog");
    expect(libraryTab("engines", false)).toBe("engines");
  });

  it("FY-WEB-322 Sources is for admins only: operators and users get Catalog and Engines, and ?tab=sources shows the Catalog", async () => {
    expect(libraryTab("sources", true)).toBe("sources");
    expect(libraryTab("sources", false)).toBe("catalog");
    for (const user of [OPERATOR, USER]) {
      server(user);
      const view = mount("/models?tab=sources");
      expect((await screen.findByRole("tab", { name: "Catalog (0)" })).getAttribute("aria-selected")).toBe("true");
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Catalog (0)", "Engines"]);
      expect(screen.queryByRole("heading", { name: "Sources" })).toBeNull();
      expect(screen.queryByLabelText(/Hosts for GGUF URLs/)).toBeNull();
      view.unmount();
      cleanup();
      vi.unstubAllGlobals();
    }
    server(OWNER);
    mount("/models?tab=sources");
    expect(await screen.findByRole("heading", { name: "Sources" })).toBeTruthy();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Catalog (0)", "Engines", "Sources"]);
  });

  it("FY-WEB-323 the deployments' findings are on Serving, not on the library", async () => {
    const f = { id: "replica-margin-low", key: "replica-margin-low@gpu:2:4", severity: "warning", title: "GPU #4 on a100 is almost full",
      detail: "d", fix_url: "/serving", fix_label: "Open Serving", scope: "gpu:2:4", pages: ["serving", "machines"], snoozable: true };
    const report = { findings: [f], snoozed: [], counts: { error: 0, warning: 1, info: 0 }, badge: 1, snooze_days: 30, checked_at: "2026-11-16T00:00:00Z" };
    const advisor = (url: string) => (url === "/api/advisor" ? { status: 200, body: report } : undefined);
    server(OWNER, advisor);
    const view = mount("/serving");
    expect(await screen.findByText("GPU #4 on a100 is almost full")).toBeTruthy();
    view.unmount();
    cleanup();
    vi.unstubAllGlobals();
    const fetch = server(OWNER, advisor);
    mount("/models");
    expect(await screen.findByRole("tab", { name: "Catalog (0)" })).toBeTruthy();
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/advisor")).toBe(true));
    expect(screen.queryByText("GPU #4 on a100 is almost full")).toBeNull();
  });

  it("FY-WEB-324 page numbers follow the menu: 01 Machines … 09 Audit log, and the pages under Serving are 02", async () => {
    const pages: [string, string, string][] = [
      ["/machines", "Machines", "01 · Machines"],
      ["/serving", "Serving", "02 · Serving"],
      ["/serving/deploy", "Deploy a model", "02 · Serving"],
      ["/models", "Models", "03 · Models"],
      ["/cloud", "Cloud", "04 · Cloud"],
      ["/playground", "Playground", "05 · Playground"],
      ["/keys", "API keys", "06 · API keys"],
      ["/usage", "Usage", "07 · Usage"],
      ["/users", "Users", "08 · Users"],
      ["/audit", "Audit log", "09 · Audit log"],
    ];
    for (const [path, title, index] of pages) {
      server();
      const view = mount(path);
      const h = await screen.findByRole("heading", { level: 1, name: title });
      expect([path, h.parentElement?.querySelector(".eyebrow")?.textContent]).toEqual([path, index]);
      view.unmount();
      cleanup();
      vi.unstubAllGlobals();
    }
  });
});
