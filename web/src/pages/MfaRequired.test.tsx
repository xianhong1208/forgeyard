// @vitest-environment jsdom
// Required two-factor sign-in in the console (1.6.0, #39, PR 2): the grace-period banner, the only page left after
// it, the admins' setting and the deadlines on Users, and My account's sessions after adding an authenticator.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { daysLeft } from "../components/MfaRequired";
import { formatDate, formatTime } from "../i18n/format";
import { LANG_KEY } from "../i18n/lang";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, has_mfa: false,
  created_at: "2026-11-09T00:00:00Z", last_login_at: null, mfa_enrolment: null as null | { by: string; due: boolean } };
const SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
const URI = `otpauth://totp/Forgeyard:owner?secret=${SECRET}&issuer=Forgeyard&algorithm=SHA1&digits=6&period=30`;
const CODES = Array.from({ length: 10 }, (_, i) => `abcd-efgh-jkmn-pq${"23456789ab"[i]}x`);
const FACTOR = { id: 7, kind: "totp" as const, name: "Pixel", created_at: "2026-11-25T00:00:00Z", last_used_at: "2026-11-25T00:00:00Z" };
const inDays = (d: number) => new Date(Date.now() + d * 86_400_000).toISOString();

type Reply = { status: number; body: unknown };
type Route = (url: string, init?: RequestInit) => Reply | undefined;

function server(route: Route, user: unknown = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r: Reply = route(url, init) ?? (url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url === "/api/auth/sessions" ? { status: 200, body: { sessions: [], idle_minutes: 120, max_days: 7 } }
        : url === "/api/machines" ? { status: 200, body: { machines: [] } }
          : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const calls = (fetch: ReturnType<typeof server>, path: string, method = "GET") =>
  fetch.mock.calls.filter(([u, i]) => u === path && (i?.method ?? "GET") === method);

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("people it is required of", () => {
  it("FY-WEB-1010 during the grace period: a banner with the date and days left on every page, a link to My account", async () => {
    server(() => undefined, { ...OWNER, mfa_enrolment: { by: inDays(3.5), due: false } });
    mount("/machines");
    const banner = await screen.findByText(/Two-factor sign-in is required for your account\. Add an authenticator app by .* \(4 days left\)/);
    expect(within(banner.closest(".notice") as HTMLElement).getByRole("link", { name: "Add it now" }).getAttribute("href")).toBe("/account");
    expect(screen.getByRole("link", { name: "Machines" })).toBeTruthy();
    expect(daysLeft(inDays(0.2))).toBe(1);
    expect(daysLeft("2026-11-30T00:00:00Z", Date.parse("2026-11-25T00:00:00Z"))).toBe(5);
    expect(daysLeft("2026-11-30T00:00:00Z", Date.parse("2026-11-24T23:00:00Z"))).toBe(6);
  });

  it("FY-WEB-1011 no banner without a deadline, or once the account has an authenticator", async () => {
    server(() => undefined, { ...OWNER, has_mfa: true, mfa_enrolment: { by: inDays(3), due: false } });
    mount("/machines");
    await screen.findByRole("link", { name: "Machines" });
    expect(screen.queryByText(/is required for your account/)).toBeNull();
    cleanup();
    server(() => undefined);
    mount("/machines");
    await screen.findByRole("link", { name: "Machines" });
    expect(screen.queryByText(/is required for your account/)).toBeNull();
  });

  it("FY-WEB-1012 after it: only the setup, whatever the page; the recovery codes stay until put away, then the page opens", async () => {
    let on = false;
    const fetch = server((url) => {
      if (url === "/api/auth/mfa/factors") return { status: 200, body: { factors: on ? [FACTOR] : [], recovery_codes_left: on ? 10 : 0, max_factors: 5 } };
      if (url === "/api/auth/mfa/totp/start") return { status: 200, body: { secret: SECRET, uri: URI, enrolment: "sealed", expires_in: 600 } };
      if (url === "/api/auth/mfa/totp") {
        on = true;
        return { status: 200, body: { factor: FACTOR, recovery_codes: CODES, factors: [FACTOR], recovery_codes_left: 10, max_factors: 5 } };
      }
      return undefined;
    }, { ...OWNER, mfa_enrolment: { by: "2026-11-20T12:00:00Z", due: true } });
    mount("/machines");
    expect(await screen.findByRole("heading", { level: 1, name: "Add two-factor sign-in to continue" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Machines" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Users" })).toBeNull();
    expect(calls(fetch, "/api/machines")).toHaveLength(0);
    expect(fetch.mock.calls.some(([u]) => String(u).startsWith("/api/advisor"))).toBe(false);
    expect(screen.getAllByRole("button", { name: "Sign out" }).length).toBeGreaterThan(0);
    const panel = screen.getByRole("heading", { name: "Two-factor sign-in" }).closest("section") as HTMLElement;
    fireEvent.click(await within(panel).findByRole("button", { name: "Add an authenticator app" }));
    fireEvent.change(await within(panel).findByLabelText("Code from the app"), { target: { value: "123456" } });
    fireEvent.change(within(panel).getByLabelText("Your password"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Turn on two-factor sign-in" }));
    expect(await within(panel).findByRole("list", { name: "Recovery codes" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "Add two-factor sign-in to continue" })).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "I have saved them" }));
    await waitFor(() => expect(screen.queryByRole("heading", { level: 1, name: "Add two-factor sign-in to continue" })).toBeNull());
    expect(await screen.findByRole("link", { name: "Machines" })).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/machines");
    expect(screen.queryByText(/is required for your account/)).toBeNull();
  });

  it("FY-WEB-1013 in Traditional Chinese: the banner, the page and the server's refusal", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(() => undefined, { ...OWNER, mfa_enrolment: { by: inDays(2.5), due: false } });
    mount("/machines");
    expect(await screen.findByText(/你的帳號必須開啟雙重驗證。請在 .* 之前加入驗證器 App（還剩 3 天），過了之後，這裡就只能做這件事。/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "現在就加入" })).toBeTruthy();
    cleanup();
    server((url) => (url === "/api/auth/mfa/factors" ? { status: 200, body: { factors: [], recovery_codes_left: 0, max_factors: 5 } } : undefined),
      { ...OWNER, mfa_enrolment: { by: "2026-11-20T12:00:00Z", due: true } });
    mount("/users");
    expect(await screen.findByRole("heading", { level: 1, name: "請先開啟雙重驗證" })).toBeTruthy();
    expect(screen.getByText(`你的帳號必須開啟雙重驗證，寬限期已在 ${formatDate("2026-11-20T12:00:00Z", "zh-TW")} 結束。請在這裡加入驗證器 App，加入之後，其他功能就會立刻恢復。`)).toBeTruthy();
    const { serverMessage } = await import("../i18n/server");
    expect(serverMessage("mfa_enrolment_required", "x")).toBe("請先到「我的帳號」加入驗證器 App：你的帳號必須開啟雙重驗證。");
  });
});

describe("admins", () => {
  const policy = (over: Record<string, unknown> = {}) =>
    ({ mfa_required: "off", mfa_grace_days: 7, max_grace_days: 90, in_grace_period: 0, setup_overdue: 0, admins_since: null, everyone_since: null, ...over });
  const people = [
    OWNER,
    { ...OWNER, id: 2, username: "erin", is_owner: false, mfa_enrolment: { by: "2026-12-01T00:00:00Z", due: false } },
    { ...OWNER, id: 3, username: "zed", is_owner: false, role: "user", mfa_enrolment: { by: "2026-11-20T12:00:00Z", due: true } },
  ];

  it("FY-WEB-1014 Users: the setting is saved as chosen, with the counts and each person's deadline", async () => {
    let saved = policy();
    const fetch = server((url, init) => {
      if (url === "/api/users") return { status: 200, body: { users: people } };
      if (url === "/api/users/mfa-policy" && init?.method === "PUT") {
        saved = policy({ ...JSON.parse(String(init.body)), in_grace_period: 1, setup_overdue: 1 });
        return { status: 200, body: saved };
      }
      if (url === "/api/users/mfa-policy") return { status: 200, body: saved };
      return undefined;
    }, { ...OWNER, has_mfa: true });
    mount("/users");
    const panel = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    const level = (await within(panel).findByLabelText("Who must use it")) as HTMLSelectElement;
    expect(level.value).toBe("off");
    expect(within(panel).queryByText(/still in the grace period/)).toBeNull();
    fireEvent.change(level, { target: { value: "everyone" } });
    fireEvent.change(within(panel).getByLabelText("Grace period (days)"), { target: { value: "14" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved the two-factor sign-in setting.")).toBeTruthy();
    expect(calls(fetch, "/api/users/mfa-policy", "PUT").map(([, i]) => JSON.parse(String(i?.body)))).toEqual([{ mfa_required: "everyone", mfa_grace_days: 14 }]);
    expect(await within(panel).findByText("1 person still in the grace period.")).toBeTruthy();
    expect(within(panel).getByText("1 person past it, who can only add an authenticator.")).toBeTruthy();
    expect(((await within(panel).findByLabelText("Who must use it")) as HTMLSelectElement).value).toBe("everyone");
    expect(within(panel).queryByText("You have no authenticator yet: add one in My account.")).toBeNull();
    const erin = screen.getAllByText("erin")[0].closest("tr") as HTMLElement;
    expect(within(erin).getByText(/^two-factor by /)).toBeTruthy();
    const zed = screen.getAllByText("zed")[0].closest("tr") as HTMLElement;
    expect(within(zed).getByText("two-factor overdue")).toBeTruthy();
  });

  it("FY-WEB-1015 an admin without an authenticator who requires it is told to add one", async () => {
    server((url) => (url === "/api/users" ? { status: 200, body: { users: [OWNER] } }
      : url === "/api/users/mfa-policy" ? { status: 200, body: policy() } : undefined));
    mount("/users");
    const level = (await screen.findByLabelText("Who must use it")) as HTMLSelectElement;
    expect(screen.queryByText("You have no authenticator yet: add one in My account.")).toBeNull();
    fireEvent.change(level, { target: { value: "admins" } });
    expect(screen.getByText("You have no authenticator yet: add one in My account.")).toBeTruthy();
  });
});

it("FY-WEB-1016 My account: adding an authenticator reloads the sessions, so this browser's two-factor chip shows at once", async () => {
  let on = false;
  const session = { id: "s1", current: true, created_at: "2026-11-25T00:00:00Z", last_seen_at: "2026-11-25T00:00:00Z",
    expires_at: "2026-12-02T00:00:00Z", ip: "127.0.0.1", user_agent: null };
  const fetch = server((url) => {
    if (url === "/api/auth/sessions") return { status: 200, body: { sessions: [{ ...session, two_factor: on }], idle_minutes: 120, max_days: 7 } };
    if (url === "/api/auth/mfa/factors") return { status: 200, body: { factors: on ? [FACTOR] : [], recovery_codes_left: on ? 10 : 0, max_factors: 5 } };
    if (url === "/api/auth/mfa/totp/start") return { status: 200, body: { secret: SECRET, uri: URI, enrolment: "sealed", expires_in: 600 } };
    if (url === "/api/auth/mfa/totp") {
      on = true;
      return { status: 200, body: { factor: FACTOR, recovery_codes: CODES, factors: [FACTOR], recovery_codes_left: 10, max_factors: 5 } };
    }
    return undefined;
  });
  mount("/account");
  const sessions = (await screen.findByRole("heading", { name: "Sessions" })).closest("section") as HTMLElement;
  await within(sessions).findByText("this browser");
  expect(within(sessions).queryByText("two-factor")).toBeNull();
  const panel = screen.getByRole("heading", { name: "Two-factor sign-in" }).closest("section") as HTMLElement;
  fireEvent.click(await within(panel).findByRole("button", { name: "Add an authenticator app" }));
  fireEvent.change(await within(panel).findByLabelText("Code from the app"), { target: { value: "123456" } });
  fireEvent.change(within(panel).getByLabelText("Your password"), { target: { value: "harbour-lantern-71" } });
  fireEvent.click(within(panel).getByRole("button", { name: "Turn on two-factor sign-in" }));
  expect(await within(sessions).findByText("two-factor")).toBeTruthy();
  expect(calls(fetch, "/api/auth/sessions").length).toBeGreaterThanOrEqual(2);
});

const FIRST = "2026-12-09T10:46:00+00:00";

it("FY-WEB-1017 Setup health's two-factor findings in Traditional Chinese: the detail written from its facts, names, counts and date", async () => {
  localStorage.setItem(LANG_KEY, "zh-TW");
  const base = { key: "", fix_url: "/account", fix_label: "Open My account", scope: "server", pages: ["users"], snoozable: true };
  const findings = [
    { ...base, id: "admins-without-mfa", key: "a@server", severity: "warning", title: "1 admin without two-factor sign-in",
      detail: "owner signs in with a password alone.", facts: { admins: ["owner"], required: true } },
    { ...base, id: "mfa-setup-pending", key: "p@server", severity: "info", title: "6 people still to add two-factor sign-in",
      detail: "1 person still in the grace period (owner; …).", fix_url: "/users", fix_label: "Open Users",
      facts: { waiting: ["owner"], overdue: ["ann", "bob", "cy", "dee", "eve"], first_ends: FIRST } },
  ];
  const counts = { error: 0, warning: 1, info: 1 };
  server((url) => (url === "/api/advisor" ? { status: 200, body: { findings, snoozed: [], counts, badge: 1, snooze_days: 30, checked_at: "2026-11-25T00:00:00Z" } }
    : url === "/api/users" ? { status: 200, body: { users: [OWNER] } }
      : url === "/api/users/mfa-policy" ? { status: 200, body: { mfa_required: "everyone", mfa_grace_days: 14, max_grace_days: 90, in_grace_period: 1, setup_overdue: 5 } }
        : undefined), { ...OWNER, has_mfa: true });
  mount("/users");
  expect(await screen.findByText("owner 只用密碼登入。管理員帳號能變更這裡的所有設定：請到「我的帳號」加入驗證器 App（每位管理員各自替自己的帳號加入）。"
    + "目前規定管理員必須開啟雙重驗證：寬限期過後，還沒有驗證器的管理員就只能先加入驗證器。")).toBeTruthy();
  const at = `${formatDate(FIRST, "zh-TW")} ${formatTime(FIRST, "zh-TW")}`;  // the reader's time zone
  const pending = await screen.findByText(`1 人還在寬限期內（owner；最早的期限是 ${at}）。5 人已經逾期（ann、bob、cy 和另外 2 人）：`
    + "加入驗證器之前，他們只能做這件事。每個人都要到「我的帳號」自己加入，管理員只能提醒他們。");
  expect(pending.getAttribute("lang")).toBeNull();
  expect(screen.queryByText(/signs in with a password alone/)).toBeNull();
  expect(screen.queryByText(/still in the grace period/)).toBeNull();
});

it("FY-WEB-1028 Setup health's people-waiting-for-approval finding in Traditional Chinese, from its facts", async () => {
  localStorage.setItem(LANG_KEY, "zh-TW");
  const findings = [
    { key: "r@server", id: "registrations-pending", severity: "info", title: "5 people waiting for approval",
      detail: "ann, bob, cy and 2 more asked for an account. Approve or remove them on Users.", fix_url: "/users",
      fix_label: "Open Users", scope: "server", pages: ["users"], snoozable: true,
      facts: { people: ["ann", "bob", "cy", "dee", "eve"] } },
    { key: "s@server", id: "registrations-pending", severity: "info", title: "1 person waiting for approval",
      detail: "zed asked for an account. Approve or remove them on Users.", fix_url: "/users",
      fix_label: "Open Users", scope: "other", pages: ["users"], snoozable: true, facts: { people: ["zed"] } },
  ];
  server((url) => (url === "/api/advisor" ? { status: 200, body: { findings, snoozed: [], counts: { error: 0, warning: 0, info: 2 }, badge: 0, snooze_days: 30, checked_at: "2026-11-25T00:00:00Z" } }
    : url === "/api/users" ? { status: 200, body: { users: [OWNER] } }
      : url === "/api/users/mfa-policy" ? { status: 200, body: { mfa_required: "off", mfa_grace_days: 7, max_grace_days: 90, in_grace_period: 0, setup_overdue: 0 } }
        : undefined), { ...OWNER, has_mfa: true });
  mount("/users");
  expect(await screen.findByText("ann、bob、cy 和另外 2 人 申請了帳號。請在「使用者」頁面核准或移除。")).toBeTruthy();
  expect(screen.getByText("zed 申請了帳號。請在「使用者」頁面核准或移除。")).toBeTruthy();
  expect(screen.queryByText(/asked for an account/)).toBeNull();
});
