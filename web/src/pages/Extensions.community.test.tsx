// @vitest-environment jsdom
// The community edition (1.6.1, #190): web/src/ext holds nothing, so no page asks for /api/info except the audit log
// (for its filter), and nothing an extension would add is shown.
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { navItems, routes, setupHealthCards, teamPageTabs, usageTabs } from "../ext";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const REPORT = { findings: [], snoozed: [], counts: { error: 0, warning: 0, info: 0 }, badge: 0, snooze_days: 30,
  checked_at: "2026-11-26T00:00:00Z", extensions: [] };
const CHECKLIST = { kind: "setup", steps: [], done: 0, total: 0, complete: true, hidden: false, gateway_url: "https://fy.lan:7860/v1", example: null };

function server() {
  const fetch = vi.fn(async (url: string) => {
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user: OWNER }
      : url === "/api/info" ? { version: "1.6.1", edition: "community", features: [], audit_actions: [] }
        : url === "/api/advisor" ? REPORT : url === "/api/onboarding" ? CHECKLIST : null;
    return new Response(JSON.stringify(body ?? {}), { status: body ? 200 : 404 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("community edition", () => {
  it("FY-WEB-1035 the slots are empty: the menu, Usage and Setup health ask nothing more and show nothing more", async () => {
    expect([routes, navItems, usageTabs, setupHealthCards, teamPageTabs].map((l) => l.length)).toEqual([0, 0, 0, 0, 0]);
    const fetch = server();
    render(<MemoryRouter initialEntries={["/start"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
    const panel = await screen.findByRole("region", { name: "Setup health" });
    await within(panel).findByText("Nothing to look at.");
    expect(within(panel).queryByText(/Extensions loaded/)).toBeNull();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(
      ["/machines", "/serving", "/models", "/cloud", "/playground", "/keys", "/usage", "/teams", "/users", "/audit", "/start", "/guide", "/account"]);
    expect(fetch.mock.calls.some(([u]) => u === "/api/info")).toBe(false);
  });
});
