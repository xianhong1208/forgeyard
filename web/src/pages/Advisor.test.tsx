// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { backupKid, fixHref, forPage, summary, worst, type AdvisorReport, type Finding } from "../lib/advisor";
import type { Checklist } from "../lib/onboarding";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body: unknown };

function finding(id: string, severity: Finding["severity"], over: Partial<Finding> = {}): Finding {
  const scope = over.scope ?? "server";
  return { id, key: `${id}@${scope}`, severity, title: `title ${id}`, detail: `detail ${id}`, fix_url: "/keys", fix_label: `Fix ${id}`,
    scope, pages: [], snoozable: severity !== "error", ...over };
}

function report(findings: Finding[], snoozed: Finding[] = []): AdvisorReport {
  const counts = { error: 0, warning: 0, info: 0 };
  for (const f of findings) counts[f.severity] += 1;
  return { findings, snoozed, counts, badge: counts.error + counts.warning, snooze_days: 30, checked_at: "2026-11-11T00:00:00Z" };
}

const CHECKLIST: Checklist = { kind: "setup", steps: [], done: 0, total: 0, complete: true, hidden: false,
  gateway_url: "https://fy.lan:7860/v1", example: null };
const MACHINES = { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: Record<string, unknown> = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? (url === "/api/onboarding" ? { status: 200, body: CHECKLIST }
        : url === "/api/machines" ? { status: 200, body: MACHINES } : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.hash}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const advisorCalls = (fetch: ReturnType<typeof server>) => fetch.mock.calls.filter(([u]) => String(u).startsWith("/api/advisor"));

describe("setup health", () => {
  it("FY-WEB-141 admins get a nav badge with errors plus warnings, and the Setup health panel lists every finding", async () => {
    const r = report([finding("route-no-target", "error", { scope: "route:4" }), finding("agent-outdated", "warning"),
      finding("budget-zero", "info", { fix_url: "/guide/en/cloud-providers#budgets", fix_label: "Read about budgets" })]);
    server((url) => (url === "/api/advisor" ? { status: 200, body: r } : undefined));
    mount("/start");
    const badge = await screen.findByRole("link", { name: "Setup health: 1 error, 1 warning" });
    expect(badge.textContent).toBe("2");
    expect(badge.getAttribute("href")).toBe("/start#setup-health");
    expect(badge.className).toContain("health-error");
    const panel = await screen.findByRole("region", { name: "Setup health" });
    expect(within(panel).getByText("1 error, 1 warning, 1 note")).toBeTruthy();
    const rows = within(panel).getAllByRole("listitem");
    expect(rows.map((li) => li.getAttribute("data-key"))).toEqual(["route-no-target@route:4", "agent-outdated@server", "budget-zero@server"]);
    expect(within(rows[0]).queryByRole("button", { name: /Snooze/ })).toBeNull();
    expect(within(rows[0]).getByText("Errors stay until fixed")).toBeTruthy();
    expect(within(rows[2]).getByRole("link", { name: "Read about budgets" }).getAttribute("href")).toMatch(/^\/guide\/(en|zh-TW)\/cloud-providers#budgets$/);
    fireEvent.click(badge);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/start#setup-health"));
  });

  it("FY-WEB-142 snoozing a warning moves it behind 'Show 1 snoozed', and Show again brings it back", async () => {
    const warn = finding("agent-outdated", "warning", { scope: "machine:3" });
    let current = report([warn]);
    const fetch = server((url, init) => {
      if (url === "/api/advisor") return { status: 200, body: current };
      if (url === "/api/advisor/snooze") {
        const body = JSON.parse(String(init?.body));
        current = body.snooze ? report([], [{ ...warn, snoozed_until: "2026-12-11T00:00:00+00:00" }]) : report([warn]);
        return { status: 200, body: current };
      }
      return undefined;
    });
    mount("/start");
    const panel = await screen.findByRole("region", { name: "Setup health" });
    fireEvent.click(await within(panel).findByRole("button", { name: "Snooze 30 days" }));
    await within(panel).findByText("Nothing to look at.");
    const post = fetch.mock.calls.find(([u]) => u === "/api/advisor/snooze");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ key: "agent-outdated@machine:3", snooze: true });
    expect(screen.queryByRole("link", { name: /^Setup health:/ })).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Show 1 snoozed" }));
    expect(within(panel).getByText("Snoozed until 2026-12-11.")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Show again" }));
    await within(panel).findByRole("button", { name: "Snooze 30 days" });
    expect(await screen.findByRole("link", { name: "Setup health: 1 warning" })).toBeTruthy();
  });

  it("FY-WEB-143 the secret-key reminder is ticked off with the key id it names", async () => {
    let current = report([finding("secret-key-backup", "warning", { scope: "secret-key:ab12cd34", title: "Back up the secret key (key id ab12cd34)" })]);
    const fetch = server((url) => {
      if (url === "/api/advisor") return { status: 200, body: current };
      if (url === "/api/advisor/secret-key-backup") {
        current = report([]);
        return { status: 200, body: current };
      }
      return undefined;
    });
    mount("/start");
    fireEvent.click(await screen.findByRole("button", { name: "I have a backup of key id ab12cd34" }));
    await screen.findByText("Nothing to look at.");
    const post = fetch.mock.calls.find(([u]) => u === "/api/advisor/secret-key-backup");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ kid: "ab12cd34" });
  });

  it("FY-WEB-144 a page shows only its own findings as a banner, the first three and a link to the rest", async () => {
    const mine = [1, 2, 3, 4].map((i) => finding("agent-outdated", "warning", { scope: `machine:${i}`, title: `machine ${i} is old`, pages: ["machines"] }));
    server((url) => (url === "/api/advisor"
      ? { status: 200, body: report([...mine, finding("key-cloud-without-budget", "warning", { pages: ["keys"], title: "a key thing" })]) }
      : undefined));
    mount("/machines");
    const banner = await screen.findByRole("region", { name: "Setup health" });
    expect(banner.className).toContain("advisor-warning");
    expect(within(banner).getAllByRole("listitem").map((li) => li.getAttribute("data-key")))
      .toEqual(["agent-outdated@machine:1", "agent-outdated@machine:2", "agent-outdated@machine:3"]);
    expect(within(banner).queryByText("a key thing")).toBeNull();
    expect(within(banner).getByText(/And 1 more under/)).toBeTruthy();
    expect(within(banner).getByRole("link", { name: "Get started → Setup health" }).getAttribute("href")).toBe("/start#setup-health");
  });

  it("FY-WEB-145 people who are not admins never ask for the advisor and see none of it", async () => {
    const fetch = server((url) => (url === "/api/advisor" ? { status: 200, body: report([finding("x", "error", { pages: ["machines"] })]) } : undefined), USER);
    mount("/start");
    await screen.findByRole("heading", { name: "Set up Forgeyard" });
    mount("/machines");
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/machines")).toBe(true));
    expect(advisorCalls(fetch)).toEqual([]);
    expect(screen.queryByRole("region", { name: "Setup health" })).toBeNull();
    expect(screen.queryByRole("link", { name: /^Setup health:/ })).toBeNull();
  });

  it("FY-WEB-146 helpers: page filter, worst severity, guide links in the reader's language, summary, key id", () => {
    const a = finding("a", "info", { pages: ["keys"] });
    const b = finding("b", "error", { pages: ["route:4", "models"] });
    expect(forPage([a, b], ["route:4"])).toEqual([b]);
    expect(forPage([a, b], ["users"])).toEqual([]);
    expect(worst([a, b])).toBe("error");
    expect(worst([])).toBeNull();
    expect(fixHref("/guide/en/how-to#back-up-and-restore", "zh-TW")).toBe("/guide/zh-TW/how-to#back-up-and-restore");
    expect(fixHref("/guide/en/reference", "en")).toBe("/guide/en/reference");
    expect(fixHref("/providers/2")).toBe("/providers/2");
    expect(summary({ error: 0, warning: 2, info: 1 })).toBe("2 warnings, 1 note");
    expect(summary({ error: 0, warning: 0, info: 0 })).toBe("Nothing to look at");
    expect(backupKid(finding("secret-key-backup", "warning", { scope: "secret-key:ab12cd34" }))).toBe("ab12cd34");
    expect(backupKid(a)).toBeNull();
  });
});
