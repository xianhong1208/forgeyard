// @vitest-environment jsdom
// Two-factor sign-in in the console (1.6.0, #39): the code step of signing in, My account's authenticator apps and
// recovery codes, an admin's reset on Users, and the code in the secret-key backup.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import { fixLabel } from "../lib/advisor";
import { groupKey, isAppCode, qrModules, reauthBody, recoveryFile } from "../lib/mfa";
import type { MfaStatus } from "../lib/mfa";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, has_mfa: false,
  created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
const URI = `otpauth://totp/Forgeyard:owner?secret=${SECRET}&issuer=Forgeyard&algorithm=SHA1&digits=6&period=30`;
const CODES = Array.from({ length: 10 }, (_, i) => `abcd-efgh-jkmn-pq${i}${i}`.replace(/[01]/g, "2"));
const FACTOR = { id: 7, kind: "totp" as const, name: "Pixel", created_at: "2026-11-25T00:00:00Z", last_used_at: "2026-11-25T00:00:00Z" };

type Reply = { status: number; body: unknown; headers?: Record<string, string> };
type Route = (url: string, init?: RequestInit) => Reply | undefined;

function server(route: Route, user: unknown = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r: Reply = route(url, init) ?? (url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url === "/api/auth/sessions" ? { status: 200, body: { sessions: [], idle_minutes: 120, max_days: 7 } }
        : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const bodyOf = (fetch: ReturnType<typeof server>, path: string) =>
  fetch.mock.calls.filter(([u, i]) => u === path && i?.method === "POST").map(([, i]) => JSON.parse(String(i?.body)));
const err = (status: number, code: string, extra: Record<string, unknown> = {}): Reply => ({ status, body: { detail: { code, message: `${code} message`, ...extra } } });

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("signing in with two-factor", () => {
  async function toCodeStep(route: Route) {
    let signedIn = false;
    const fetch = server((url, init) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: signedIn ? OWNER : null } };
      if (url === "/api/auth/login") return { status: 200, body: { mfa_required: true, challenge: "ch-1", expires_in: 300 } };
      const r = route(url, init);
      if (r && r.status === 200 && url === "/api/auth/mfa") signedIn = true;
      return r;
    });
    mount("/login");
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Two-factor sign-in" });
    return fetch;
  }

  it("FY-WEB-1000 the right password asks for the code; the code signs in and goes on", async () => {
    const fetch = await toCodeStep((url) => (url === "/api/auth/mfa" ? { status: 200, body: { user: { ...OWNER, has_mfa: true }, recovery_codes_left: null } } : undefined));
    expect(screen.getByText("Type the 6-digit code your authenticator app shows for Forgeyard.")).toBeTruthy();
    expect((screen.getByLabelText("Code") as HTMLInputElement).autocomplete).toBe("one-time-code");
    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "123 456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).not.toBe("/login"));
    expect(bodyOf(fetch, "/api/auth/mfa")).toEqual([{ challenge: "ch-1", code: "123 456" }]);
  });

  it("FY-WEB-1001 a wrong code says how many tries are left; the last one, or an expired sign-in, goes back to the password", async () => {
    let left = 2;
    await toCodeStep((url) => (url === "/api/auth/mfa" ? err(400, "mfa_code_invalid", { attempts_left: --left }) : undefined));
    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("That code is not right. 1 try left.")).toBeTruthy();
    expect((screen.getByLabelText("Code") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Code"), { target: { value: "000001" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("That sign-in took too long or had too many wrong codes. Sign in again.")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
  });

  it("FY-WEB-1002 a recovery code instead of the app, and back to the app", async () => {
    const fetch = await toCodeStep((url) => (url === "/api/auth/mfa" ? { status: 200, body: { user: OWNER, recovery_codes_left: 9 } } : undefined));
    fireEvent.click(screen.getByRole("button", { name: "No phone at hand? Use a recovery code" }));
    expect(screen.getByText("Type one of your recovery codes. Each one works once.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: CODES[0] } });
    fireEvent.click(screen.getByRole("button", { name: "Use a code from the app" }));
    expect((screen.getByLabelText("Code") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "No phone at hand? Use a recovery code" }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: CODES[0] } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(screen.getByTestId("where").textContent).not.toBe("/login"));
    expect(bodyOf(fetch, "/api/auth/mfa")).toEqual([{ challenge: "ch-1", code: CODES[0] }]);
  });
});

describe("my account", () => {
  const status = (factors = [FACTOR], left = 10): MfaStatus => ({ factors, recovery_codes_left: left, max_factors: 5 });

  it("FY-WEB-1003 adding the first authenticator: QR code, setup key, code and password, then the recovery codes once", async () => {
    let on = false;
    const fetch = server((url) => {
      if (url === "/api/auth/mfa/factors") return { status: 200, body: on ? status() : status([], 0) };
      if (url === "/api/auth/mfa/totp/start") return { status: 200, body: { secret: SECRET, uri: URI, enrolment: "sealed", expires_in: 600 } };
      if (url === "/api/auth/mfa/totp") {
        on = true;
        return { status: 200, body: { factor: FACTOR, recovery_codes: CODES, ...status() } };
      }
      return undefined;
    });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText("off")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Add an authenticator app" }));
    const qr = await within(panel).findByRole("img", { name: "QR code to add Forgeyard to your authenticator app" });
    expect(qr.querySelector("path")?.getAttribute("d")?.length).toBeGreaterThan(100);
    expect(within(panel).getByLabelText("Setup key").textContent).toBe("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP");
    expect(within(panel).getByLabelText("Setup link").textContent).toBe(URI);
    fireEvent.change(within(panel).getByLabelText("Code from the app"), { target: { value: "123456" } });
    fireEvent.change(within(panel).getByLabelText("Name"), { target: { value: "Pixel" } });
    fireEvent.change(within(panel).getByLabelText("Your password"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Turn on two-factor sign-in" }));
    expect(await screen.findByText("Two-factor sign-in is on.")).toBeTruthy();
    expect(bodyOf(fetch, "/api/auth/mfa/totp")).toEqual([{ enrolment: "sealed", new_code: "123456", name: "Pixel", password: "harbour-lantern-71" }]);
    const list = within(panel).getByRole("list", { name: "Recovery codes" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(CODES);
    expect(within(panel).getByText(/they are shown only this once/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "I have saved them" }));
    await waitFor(() => expect(within(panel).queryByRole("list", { name: "Recovery codes" })).toBeNull());
    expect(await within(panel).findByText("on")).toBeTruthy();
    expect(within(panel).getByText("Pixel")).toBeTruthy();
    expect(within(panel).getByText("10 recovery codes left.")).toBeTruthy();
  });

  it("FY-WEB-1004 removing or making new codes asks again: six digits go as a code, anything else as the password", async () => {
    let factors = [FACTOR];
    const fetch = server((url) => {
      if (url === "/api/auth/mfa/factors") return { status: 200, body: status(factors, 3) };
      if (url === "/api/auth/mfa/recovery-codes") return { status: 200, body: { recovery_codes: CODES, ...status(factors) } };
      if (url === "/api/auth/mfa/factors/7/remove") {
        factors = [];
        return { status: 200, body: status([], 0) };
      }
      return undefined;
    }, { ...OWNER, has_mfa: true });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText("3 recovery codes left.")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "New recovery codes" }));
    expect(within(panel).getByText("Make a new set of recovery codes? The ones you have now stop working.")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Your password, or a current code from the app"), { target: { value: "654 321" } });
    fireEvent.click(within(panel).getAllByRole("button", { name: "New recovery codes" }).at(-1) as HTMLElement);
    expect(await screen.findByText("New recovery codes made; the old ones no longer work.")).toBeTruthy();
    expect(bodyOf(fetch, "/api/auth/mfa/recovery-codes")).toEqual([{ code: "654321" }]);
    expect(within(within(panel).getByRole("list", { name: "Recovery codes" })).getAllByRole("listitem")).toHaveLength(10);
    fireEvent.click(within(panel).getByRole("button", { name: "I have saved them" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Remove" }));
    expect(within(panel).getByText("Remove Pixel?")).toBeTruthy();
    expect(within(panel).getByText(/It is your last authenticator/)).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Your password, or a current code from the app"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(within(panel).getAllByRole("button", { name: "Remove" }).at(-1) as HTMLElement);
    expect(await screen.findByText("Removed Pixel. Two-factor sign-in is off.")).toBeTruthy();
    expect(bodyOf(fetch, "/api/auth/mfa/factors/7/remove")).toEqual([{ password: "harbour-lantern-71" }]);
    expect(await within(panel).findByRole("button", { name: "Add an authenticator app" })).toBeTruthy();
  });

  it("FY-WEB-1005 a wrong password is said where it was typed, and the field empties", async () => {
    server((url) => {
      if (url === "/api/auth/mfa/factors") return { status: 200, body: status() };
      if (url === "/api/auth/mfa/factors/7/remove") return err(400, "wrong_password");
      return undefined;
    }, { ...OWNER, has_mfa: true });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    fireEvent.click(await within(panel).findByRole("button", { name: "Remove" }));
    const box = within(panel).getByLabelText("Your password, or a current code from the app") as HTMLInputElement;
    fireEvent.change(box, { target: { value: "not-it-at-all" } });
    fireEvent.click(within(panel).getAllByRole("button", { name: "Remove" }).at(-1) as HTMLElement);
    expect(await within(panel).findByText("wrong_password message")).toBeTruthy();
    expect(box.value).toBe("");
    expect(screen.getByTestId("where").textContent).toBe("/account");
  });
});

describe("admins", () => {
  const USERS = [
    { ...OWNER, has_mfa: true },
    { id: 2, username: "erin", email: null, role: "admin", status: "active", is_owner: false, has_password: true, has_mfa: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null },
    { id: 3, username: "dana", email: null, role: "user", status: "active", is_owner: false, has_password: true, has_mfa: false, created_at: "2026-11-09T00:00:00Z", last_login_at: null },
  ];

  it("FY-WEB-1006 Users marks two-factor and offers Reset two-factor only for someone else who has it, never the owner", async () => {
    server((url) => (url === "/api/users" ? { status: 200, body: { users: USERS } } : undefined),
      { ...OWNER, id: 2, username: "erin", is_owner: false, has_mfa: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/users");
    const table = await screen.findByRole("table");
    const row = (name: string) => within(table).getByText(name, { selector: ".mono" }).closest("tr") as HTMLElement;
    await within(table).findByText("dana");
    expect(within(row("owner")).getByText("two-factor")).toBeTruthy();
    expect(within(row("dana")).queryByText("two-factor")).toBeNull();
    expect(within(row("owner")).queryByRole("button", { name: "Reset two-factor" })).toBeNull();  // the owner's own
    expect(within(row("erin")).queryByRole("button", { name: "Reset two-factor" })).toBeNull();   // yourself
    expect(within(row("dana")).queryByRole("button", { name: "Reset two-factor" })).toBeNull();   // has none
    // Another admin with two-factor:
    cleanup();
    const fetch = server((url) => (url === "/api/users" ? { status: 200, body: { users: USERS } }
      : url === "/api/users/2/mfa-reset" ? { status: 200, body: { factors_removed: 1, sessions_ended: 2 } } : undefined), { ...OWNER, has_mfa: true });
    mount("/users");
    const t2 = await screen.findByRole("table");
    await within(t2).findByText("erin");
    fireEvent.click(within(within(t2).getByText("erin", { selector: ".mono" }).closest("tr") as HTMLElement).getByRole("button", { name: "Reset two-factor" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringMatching(/^Reset two-factor sign-in for erin\?/));
    expect(await screen.findByText("Reset two-factor sign-in for erin; their sessions ended.")).toBeTruthy();
    expect(fetch.mock.calls.filter(([u, i]) => u === "/api/users/2/mfa-reset" && i?.method === "POST")).toHaveLength(1);
  });

  it("FY-WEB-1007 the secret-key backup asks for a code when the admin has two-factor, and sends it", async () => {
    const fetch = server((url) => {
      if (url === "/api/secret-key") return { status: 200, body: { kid: "ab12cd34", source: "file", includes_ca: false, tls_mode: "off", has_password: true, has_mfa: true, passphrase_min: 12, backed_up: null } };
      if (url === "/api/secret-key/backup") return err(400, "mfa_code_invalid");
      if (url === "/api/advisor") return { status: 200, body: { findings: [], snoozed: [], counts: { error: 0, warning: 0, info: 0 }, badge: 0, snooze_days: 30, checked_at: "" } };
      if (url === "/api/tls") return { status: 200, body: { mode: "off", https: false, hsts: false, server_url: "http://forge.lan:7860", ca: null, admin: { trust_command: null, server_certificate: null } } };
      return undefined;
    });
    mount("/trust");
    fireEvent.click(await screen.findByRole("button", { name: "Download a key backup…" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("ab12cd34");
    fireEvent.change(within(dialog).getByLabelText("Your password"), { target: { value: "harbour-lantern-71" } });
    fireEvent.change(within(dialog).getByLabelText("Code from your authenticator app"), { target: { value: "123 456" } });
    fireEvent.change(within(dialog).getByLabelText("Backup passphrase"), { target: { value: "copper-kettle-moon-river" } });
    fireEvent.change(within(dialog).getByLabelText("Passphrase again"), { target: { value: "copper-kettle-moon-river" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Download the backup" }));
    expect(await within(dialog).findByText("mfa_code_invalid message")).toBeTruthy();
    expect((within(dialog).getByLabelText("Code from your authenticator app") as HTMLInputElement).value).toBe("");
    const [, init] = fetch.mock.calls.find(([u]) => u === "/api/secret-key/backup") ?? [];
    expect(JSON.parse(String(init?.body)).code).toBe("123456");
  });
});

describe("in Traditional Chinese, and the helpers", () => {
  it("FY-WEB-1008 the code step and the panel read in Chinese", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server((url) => (url === "/api/auth/mfa/factors" ? { status: 200, body: { factors: [FACTOR], recovery_codes_left: 1, max_factors: 5 } } : undefined),
      { ...OWNER, has_mfa: true });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "雙重驗證" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText("已開啟")).toBeTruthy();
    expect(within(panel).getByText("還剩 1 組備用碼。")).toBeTruthy();
    expect(within(panel).getByText("登入時要輸入密碼，以及下列任一個 App 的驗證碼。")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "產生新的備用碼" })).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "再加入一個驗證器" })).toBeTruthy();
    cleanup();
    server((url) => (url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: null } }
      : url === "/api/auth/login" ? { status: 200, body: { mfa_required: true, challenge: "c", expires_in: 300 } } : undefined));
    mount("/login");
    fireEvent.change(await screen.findByLabelText("使用者名稱"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("密碼"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(screen.getByRole("button", { name: "登入" }));
    expect(await screen.findByRole("heading", { name: "雙重驗證" })).toBeTruthy();
    expect(screen.getByText("請輸入驗證器 App 為 Forgeyard 顯示的 6 位數驗證碼。")).toBeTruthy();
    expect(screen.getByLabelText("驗證碼")).toBeTruthy();
    expect(screen.getByRole("button", { name: "手機不在身邊？改用備用碼" })).toBeTruthy();
    expect(fixLabel("Open My account")).toBe("前往「我的帳號」");  // Setup health's admins-without-mfa
  });

  it("FY-WEB-1009 a QR code has its three finder patterns and the size of its version; codes, keys and the codes file", () => {
    const m = qrModules(URI);
    const n = m.length;
    const inner = n - 8;
    expect((inner - 17) % 4).toBe(0);           // 4 × version + 17 modules, plus a quiet zone of 4 on each side
    expect(m[0].every((v) => !v) && m[n - 1].every((v) => !v)).toBe(true);
    const finder = (top: number, left: number) => [0, 1, 2, 3, 4, 5, 6].every((i) =>
      m[top][left + i] && m[top + 6][left + i] && m[top + i][left] && m[top + i][left + 6] && m[top + 3][left + 3]);
    expect(finder(4, 4) && finder(4, n - 11) && finder(n - 11, 4)).toBe(true);
    expect(finder(n - 11, n - 11)).toBe(false);
    expect(isAppCode("123456") && isAppCode("123 456")).toBe(true);
    expect(isAppCode("12345") || isAppCode("1234567") || isAppCode("abcdef")).toBe(false);
    expect(reauthBody("123 456")).toEqual({ code: "123456" });
    expect(reauthBody("harbour-lantern-71")).toEqual({ password: "harbour-lantern-71" });
    expect(groupKey(SECRET)).toBe("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP");
    expect(recoveryFile("Header", ["a", "b"])).toBe("Header\n\na\nb\n");
  });
});
