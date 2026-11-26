// @vitest-environment jsdom
// Single sign-on in the console (1.6.0, #39, PR 3): "Sign in with <name>", the page the provider sends people back to,
// My account's linked account, the fresh sign-in in place of a password, and the admins' provider settings.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import { away, isFresh, listsToMapping, mappingToLists } from "../lib/sso";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, has_mfa: false,
  created_at: "2026-11-09T00:00:00Z", last_login_at: null, mfa_enrolment: null };
const SOLO = { ...OWNER, id: 5, username: "sam", is_owner: false, has_password: false };
const IDP_URL = "https://idp.example.test/realms/forge/protocol/openid-connect/auth?state=s";
const PROVIDER = { id: 1, kind: "oidc", name: "Keycloak", issuer: "https://idp.example.test/realms/forge", client_id: "forgeyard",
  has_client_secret: true, scopes: "openid email profile", role_claim: "groups", role_mapping: { "fy-admins": "admin", "fy-ops": "operator" },
  new_accounts: "approve", allowed_domains: ["example.com"], enabled: true, linked_accounts: 2 };
const LINKED = { id: 9, provider: "Keycloak", email: "sam@example.com", created_at: "2026-11-20T00:00:00Z", last_login_at: "2026-11-25T00:00:00Z" };
const soon = () => new Date(Date.now() + 240_000).toISOString();

type Reply = { status: number; body: unknown };
type Route = (url: string, init?: RequestInit) => Reply | undefined;
type State = { user: unknown; sso?: { name: string } | null; session?: { auth_method: string; fresh_until: string | null } | null };

function server(route: Route, state: State = { user: OWNER, sso: { name: "Keycloak" } }) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r: Reply = route(url, init) ?? (url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", ...state } }
      : url === "/api/auth/sessions" ? { status: 200, body: { sessions: [], idle_minutes: 120, max_days: 7 } }
        : url === "/api/auth/mfa/factors" ? { status: 200, body: { factors: [], recovery_codes_left: 0, max_factors: 5 } }
          : url === "/api/users" ? { status: 200, body: { users: [OWNER] } }
            : url === "/api/users/mfa-policy" ? { status: 200, body: { mfa_required: "off", mfa_grace_days: 7, max_grace_days: 90, in_grace_period: 0, setup_overdue: 0 } }
              : url === "/api/machines" ? { status: 200, body: { machines: [] } }
                : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search}</div>;
}

function mount(path: string) {
  return render(<StrictMode><MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter></StrictMode>);
}

const calls = (fetch: ReturnType<typeof server>, path: string, method = "GET") =>
  fetch.mock.calls.filter(([u, i]) => u === path && (i?.method ?? "GET") === method);
const bodyOf = (call: unknown[]) => JSON.parse(String((call[1] as RequestInit).body));

let went: string[] = [];
beforeEach(() => {
  went = [];
  away.go = (url: string) => { went.push(url); };
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("signing in", () => {
  it("FY-WEB-1018 the sign-in page offers the provider by name and leaves for it with the page to come back to", async () => {
    const fetch = server((url) => (url === "/api/auth/sso/start" ? { status: 200, body: { url: IDP_URL } } : undefined),
      { user: null, sso: { name: "Keycloak" } });
    mount("/login?next=%2Fserving");
    fireEvent.click(await screen.findByRole("button", { name: "Sign in with Keycloak" }));
    await waitFor(() => expect(went).toEqual([IDP_URL]));
    expect(bodyOf(calls(fetch, "/api/auth/sso/start", "POST")[0])).toEqual({ purpose: "login", next: "/serving" });
    // The password form is still there.
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });

  it("FY-WEB-1019 without a provider there is no such button", async () => {
    server(() => undefined, { user: null, sso: null });
    mount("/login");
    await screen.findByRole("button", { name: "Sign in" });
    expect(screen.queryByRole("button", { name: /Sign in with/ })).toBeNull();
  });

  it("FY-WEB-1020 the callback page sends code and state once (even in StrictMode), drops them from the address and opens next", async () => {
    let signedIn = false;
    const fetch = server((url) => {
      if (url === "/api/auth/sso/finish") {
        signedIn = true;
        return { status: 200, body: { user: OWNER, next: "/serving", purpose: "login" } };
      }
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: signedIn ? OWNER : null, sso: { name: "Keycloak" } } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
      return undefined;
    });
    mount("/sso/callback?code=the-code&state=the-state");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/serving"));
    const posts = calls(fetch, "/api/auth/sso/finish", "POST");
    expect(posts).toHaveLength(1);
    expect(bodyOf(posts[0])).toEqual({ state: "the-state", code: "the-code", error: null, error_description: null });
  });

  it("FY-WEB-1021 a new account waiting for approval, a refused sign-in, and the page opened by hand", async () => {
    server((url) => (url === "/api/auth/sso/finish"
      ? { status: 403, body: { detail: { code: "pending_approval", message: "Your account is waiting for an admin to approve it.", username: "alice.w" } } }
      : undefined), { user: null, sso: { name: "Keycloak" } });
    mount("/sso/callback?code=c&state=s");
    expect(await screen.findByText("Your account alice.w is made and waits for an admin to approve it. Sign in again once they have.")).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/sso/callback");
    cleanup();
    server((url) => (url === "/api/auth/sso/finish"
      ? { status: 400, body: { detail: { code: "sso_state_invalid", message: "This sign-in took too long or was already used. Start again." } } }
      : undefined), { user: null, sso: { name: "Keycloak" } });
    mount("/sso/callback?code=c&state=s");
    expect(await screen.findByRole("heading", { name: "The sign-in did not finish" })).toBeTruthy();
    expect(screen.getByText("This sign-in took too long or was already used. Start again.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to sign in" })).toBeTruthy();
    cleanup();
    const fetch = server(() => undefined, { user: null, sso: { name: "Keycloak" } });
    mount("/sso/callback");
    expect(await screen.findByText(/Your identity provider opens this page/)).toBeTruthy();
    expect(calls(fetch, "/api/auth/sso/finish", "POST")).toHaveLength(0);
  });
});

describe("My account", () => {
  it("FY-WEB-1022 link: the password (or a code) first, then off to the provider; a linked account shows its e-mail and can be unlinked", async () => {
    let linked = false;
    const fetch = server((url, init) => {
      if (url === "/api/auth/sso/identities") return { status: 200, body: { identities: linked ? [LINKED] : [], provider: { name: "Keycloak" }, fresh_until: null } };
      if (url === "/api/auth/sso/start") return { status: 200, body: { url: IDP_URL } };
      if (url === "/api/auth/sso/unlink" && init?.method === "POST") {
        linked = false;
        return { status: 200, body: { ok: true } };
      }
      return undefined;
    });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "Single sign-on" })).closest("section") as HTMLElement;
    fireEvent.change(within(panel).getByLabelText("Your password, or a code from your authenticator app"), { target: { value: "harbour-lantern-71" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Link Keycloak" }));
    await waitFor(() => expect(went).toEqual([IDP_URL]));
    expect(bodyOf(calls(fetch, "/api/auth/sso/start", "POST")[0])).toEqual({ purpose: "link", next: "/account", password: "harbour-lantern-71" });
    cleanup();
    linked = true;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/account");
    const p2 = (await screen.findByRole("heading", { name: "Single sign-on" })).closest("section") as HTMLElement;
    expect(await within(p2).findByText(/sam@example\.com/)).toBeTruthy();
    fireEvent.click(within(p2).getByRole("button", { name: "Unlink" }));
    expect(await screen.findByText("Your Keycloak account is unlinked.")).toBeTruthy();
    expect(calls(fetch, "/api/auth/sso/unlink", "POST")).toHaveLength(1);
  });

  it("FY-WEB-1023 an account without a password: no password form, the only link cannot be unlinked, two-factor asks for a fresh sign-in first", async () => {
    server((url) => (url === "/api/auth/sso/identities"
      ? { status: 200, body: { identities: [LINKED], provider: { name: "Keycloak" }, fresh_until: null } }
      : url === "/api/auth/sso/start" ? { status: 200, body: { url: IDP_URL } } : undefined),
    { user: SOLO, sso: { name: "Keycloak" }, session: { auth_method: "sso", fresh_until: null } });
    mount("/account");
    const panel = (await screen.findByRole("heading", { name: "Single sign-on" })).closest("section") as HTMLElement;
    expect(within(panel).getByRole("button", { name: "Unlink" }).hasAttribute("disabled")).toBe(true);
    expect(screen.queryByRole("heading", { name: "Change password" })).toBeNull();
    expect(screen.getByText("Your account has no password: you sign in through your identity provider.")).toBeTruthy();
    const mfa = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    expect(within(mfa).queryByRole("button", { name: "Add an authenticator app" })).toBeNull();
    fireEvent.click(await within(mfa).findByRole("button", { name: "Sign in again with Keycloak" }));
    await waitFor(() => expect(went).toEqual([IDP_URL]));
    cleanup();
    // Back from the provider: fresh for a few minutes, so adding one needs no password.
    server((url) => (url === "/api/auth/sso/identities"
      ? { status: 200, body: { identities: [LINKED], provider: { name: "Keycloak" }, fresh_until: soon() } } : undefined),
    { user: SOLO, sso: { name: "Keycloak" }, session: { auth_method: "sso", fresh_until: soon() } });
    mount("/account");
    const mfa2 = (await screen.findByRole("heading", { name: "Two-factor sign-in" })).closest("section") as HTMLElement;
    expect(await within(mfa2).findByRole("button", { name: "Add an authenticator app" })).toBeTruthy();
  });
});

describe("admins", () => {
  it("FY-WEB-1024 Users shows the redirect URI and saves the provider: roles from three lists, a kept secret is not sent", async () => {
    let saved: Record<string, unknown> | null = null;
    const fetch = server((url, init) => {
      if (url === "/api/identity-provider" && init?.method === "PUT") {
        saved = JSON.parse(String(init.body));
        return { status: 200, body: { provider: PROVIDER, redirect_uri: "https://forge.example.test/sso/callback", public_url_set: true, available: true, ca_file: false } };
      }
      if (url === "/api/identity-provider") return { status: 200, body: { provider: PROVIDER, redirect_uri: "https://forge.example.test/sso/callback", public_url_set: true, available: true, ca_file: false } };
      if (url === "/api/identity-provider/check") return { status: 200, body: { ok: true, issuer: PROVIDER.issuer, authorization_endpoint: "a", token_endpoint: "t", jwks_uri: "j", algorithms: ["RS256"] } };
      return undefined;
    });
    mount("/users");
    const panel = (await screen.findByRole("heading", { name: "Single sign-on (OIDC)" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText("https://forge.example.test/sso/callback")).toBeTruthy();
    expect((within(panel).getByLabelText("Values that make an admin") as HTMLInputElement).value).toBe("fy-admins");
    expect(within(panel).getByText("A secret is stored. Leave this empty to keep it.")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Check" }));
    expect(await within(panel).findByText(`Found ${PROVIDER.issuer}: it signs ID tokens with RS256.`)).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Values that make an operator"), { target: { value: "fy-ops, ml-team" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Single sign-on is saved.")).toBeTruthy();
    expect(saved).not.toBeNull();
    expect(saved!.role_mapping).toEqual({ "fy-admins": "admin", "fy-ops": "operator", "ml-team": "operator" });
    expect("client_secret" in saved!).toBe(false);
    expect(saved!.allowed_domains).toEqual(["example.com"]);
    expect(calls(fetch, "/api/identity-provider", "PUT")).toHaveLength(1);
  });

  it("FY-WEB-1025 the mapping lists and freshness helpers", () => {
    expect(mappingToLists({ a: "admin", b: "operator", c: "admin" })).toEqual({ admin: "a, c", operator: "b", user: "" });
    expect(listsToMapping({ admin: "a", operator: "a, b", user: " " })).toEqual({ a: "admin", b: "operator" });
    expect(isFresh(null)).toBe(false);
    expect(isFresh("2026-11-25T00:05:00Z", Date.parse("2026-11-25T00:04:00Z"))).toBe(true);
    expect(isFresh("2026-11-25T00:05:00Z", Date.parse("2026-11-25T00:06:00Z"))).toBe(false);
  });

  it("FY-WEB-1026 an admin without a password downloads the key backup after a fresh sign-in, with no password field", async () => {
    const info = { kid: "ab12cd34", source: "file", includes_ca: true, tls_mode: "forgeyard-ca", has_password: false, has_mfa: false,
      sso_linked: true, fresh_until: null as string | null, passphrase_min: 12, backed_up: null };
    const reminder = { id: "secret-key-backup", key: "secret-key-backup@secret-key:ab12cd34", severity: "warning",
      title: "Back up the secret key (key id ab12cd34)", detail: "Keep a copy.", fix_url: "/guide/en/how-to#back-up-and-restore",
      fix_label: "How to back up", scope: "secret-key:ab12cd34", pages: [], snoozable: true };
    const advisor = { findings: [reminder], snoozed: [], counts: { error: 0, warning: 1, info: 0 }, badge: 1, snooze_days: 30,
      checked_at: "2026-11-17T00:00:00Z" };
    server((url) => (url === "/api/secret-key" ? { status: 200, body: info } : url === "/api/advisor" ? { status: 200, body: advisor }
      : url === "/api/auth/sso/start" ? { status: 200, body: { url: IDP_URL } }
        : url === "/api/onboarding" ? { status: 200, body: { kind: "setup", steps: [], done: 0, total: 0, complete: true, hidden: false, gateway_url: "", example: null } }
          : undefined),
    { user: { ...SOLO, role: "admin" }, sso: { name: "Keycloak" }, session: { auth_method: "sso", fresh_until: null } });
    mount("/start");
    fireEvent.click(await screen.findByRole("button", { name: "Download a backup…" }));
    const dialog = await screen.findByRole("dialog", { name: "Back up the secret key" });
    await within(dialog).findByText("ab12cd34");
    expect(within(dialog).queryByLabelText("Your password")).toBeNull();
    expect((within(dialog).getByRole("button", { name: "Download the backup" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Sign in again with Keycloak" }));
    await waitFor(() => expect(went).toEqual([IDP_URL]));
  });

  it("FY-WEB-1027 in Traditional Chinese: the sign-in button, the callback's refusal and the settings", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(() => undefined, { user: null, sso: { name: "Keycloak" } });
    mount("/login");
    expect(await screen.findByRole("button", { name: "使用 Keycloak 登入" })).toBeTruthy();
    cleanup();
    server((url) => (url === "/api/auth/sso/finish"
      ? { status: 403, body: { detail: { code: "sso_domain_not_allowed", message: "This server does not take new accounts for that e-mail address. Ask an admin." } } }
      : undefined), { user: null, sso: { name: "Keycloak" } });
    mount("/sso/callback?code=c&state=s");
    expect(await screen.findByText("這台伺服器不為這個電子郵件地址建立帳號。請洽管理員。")).toBeTruthy();
    cleanup();
    server((url) => (url === "/api/identity-provider"
      ? { status: 200, body: { provider: null, redirect_uri: "http://localhost/sso/callback", public_url_set: false, available: true, ca_file: false } } : undefined));
    mount("/users");
    const panel = (await screen.findByRole("heading", { name: "單一登入（OIDC）" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText(/沒有設定 PUBLIC_URL/)).toBeTruthy();
    expect(within(panel).getByLabelText("簽發者 URL")).toBeTruthy();
  });
});
