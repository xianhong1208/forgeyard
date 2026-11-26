// @vitest-environment jsdom
// Extension slots (1.6.1, #190; web/src/ext): an extension's pages, menu links, Usage tabs and Setup health cards show
// only while GET /api/info lists their features; its audit prefixes join the audit filter; Setup health names the
// loaded extensions. The slots are filled here the way an extension's build fills web/src/ext/index.ts.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";

vi.mock("../ext", () => ({
  routes: [{ path: "/quotas", feature: "quotas", element: createElement("p", null, "the quotas page") }],
  navItems: [{ to: "/quotas", feature: "quotas", label: () => "Quotas" },
    { to: "/licence", feature: "quotas", admin: true, label: () => "Licence" }],
  usageTabs: [{ id: "reports", feature: "reports", label: () => "Reports", render: () => createElement("p", null, "the reports tab") }],
  setupHealthCards: [{ id: "licence", feature: "quotas", render: () => createElement("p", null, "licence valid until 2027-11-26") }],
  teamPageTabs: [{ id: "team-quotas", feature: "quotas", label: () => "Team quotas",
    render: (team: { name: string }) => createElement("p", null, `the quotas of ${team.name}`) }],
}));

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
const REPORT = { findings: [], snoozed: [], counts: { error: 0, warning: 0, info: 0 }, badge: 0, snooze_days: 30,
  checked_at: "2026-11-26T00:00:00Z", extensions: [{ name: "sample", version: "0.1.0" }] };
const CHECKLIST = { kind: "setup", steps: [], done: 0, total: 0, complete: true, hidden: false, gateway_url: "https://fy.lan:7860/v1", example: null };

const TEAM = { id: 4, name: "ml", title: "Machine learning", created_by: "owner", created_at: "2026-11-26T08:00:00Z", my_role: null, can_manage: true,
  members: 0, managers: 0, keys: 0, deployments: 0, budget: { budget_micros: null, spent_micros: 0, reserved_micros: 0, used: null, level: null },
  member_list: [], key_list: [], deployment_list: [] };

function server(features: string[], user: Record<string, unknown> = OWNER, audit: string[] = []) {
  const fetch = vi.fn(async (url: string) => {
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user }
      : url === "/api/info" ? { version: "1.6.1", edition: "enterprise", features, audit_actions: audit }
        : url === "/api/advisor" ? REPORT
          : url === "/api/onboarding" ? CHECKLIST
            : url.startsWith("/api/audit") ? { events: [], next_before: null }
              : url === "/api/teams/ml" ? { team: TEAM, month: "2026-11", resets_at: "2026-12-01T00:00:00Z" } : null;
    return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 });
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

describe("extension slots", () => {
  it("FY-WEB-1031 a listed feature brings its menu link (admin-only ones to admins), its page and its Setup health card", async () => {
    server(["quotas"]);
    mount("/quotas");
    expect(await screen.findByText("the quotas page")).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Quotas" }).getAttribute("href")).toBe("/quotas");
    expect(within(nav).getByRole("link", { name: "Licence" })).toBeTruthy();
    cleanup();
    server(["quotas"], USER);
    mount("/machines");
    const nav2 = await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(nav2).getByRole("link", { name: "Quotas" })).toBeTruthy());
    expect(within(nav2).queryByRole("link", { name: "Licence" })).toBeNull();
    cleanup();
    server(["quotas"]);
    mount("/start");
    const panel = await screen.findByRole("region", { name: "Setup health" });
    expect(await within(panel).findByText("licence valid until 2027-11-26")).toBeTruthy();
    expect(within(panel).getByText("Extensions loaded: sample 0.1.0.")).toBeTruthy();
  });

  it("FY-WEB-1032 a feature the server does not list: no link, no card, and its page says it is not available here", async () => {
    server([]);
    mount("/quotas");
    expect(await screen.findByRole("heading", { name: "Not available here" })).toBeTruthy();
    expect(screen.queryByText("the quotas page")).toBeNull();
    expect(within(screen.getByRole("navigation", { name: "Main" })).queryByRole("link", { name: "Quotas" })).toBeNull();
    cleanup();
    server([]);
    mount("/start");
    const panel = await screen.findByRole("region", { name: "Setup health" });
    await within(panel).findByText("Extensions loaded: sample 0.1.0.");
    expect(within(panel).queryByText("licence valid until 2027-11-26")).toBeNull();
  });

  it("FY-WEB-1033 a Usage tab of a listed feature follows Requests and Cost and shows its own panel", async () => {
    server(["reports"]);
    mount("/usage");
    const tab = await screen.findByRole("tab", { name: "Reports" });
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Requests", "Cost", "Reports"]);
    fireEvent.click(tab);
    expect(await screen.findByText("the reports tab")).toBeTruthy();
    expect(screen.getByRole("tabpanel").textContent).toBe("the reports tab");
  });

  it("FY-WEB-1034 the audit filter gets the extension's action prefixes, shown as recorded", async () => {
    server([], OWNER, ["quota.", "user."]);
    mount("/audit");
    const select = await screen.findByLabelText("Show");
    await waitFor(() => expect(within(select).getByRole("option", { name: "quota." })).toBeTruthy());
    expect(within(select).getAllByRole("option").filter((o) => o.getAttribute("value") === "user.")).toHaveLength(1);
  });

  it("FY-WEB-1045 a team page section of a listed feature comes after the community's, given the team; none without it", async () => {
    server(["quotas"]);
    mount("/teams/ml");
    const section = await screen.findByRole("region", { name: "Team quotas" });
    expect(within(section).getByText("the quotas of ml")).toBeTruthy();
    cleanup();
    server([]);
    mount("/teams/ml");
    expect(await screen.findByRole("heading", { name: "Machine learning" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Team quotas" })).toBeNull();
  });
});
