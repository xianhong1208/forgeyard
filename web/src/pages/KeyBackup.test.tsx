// @vitest-environment jsdom
// Download a backup of the secret key from the console (1.4.2, issue #127).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { AdvisorReport, Finding } from "../lib/advisor";
import { backupFilename, passphraseProblem } from "../lib/keyBackup";
import type { SecretKeyInfo } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const FILE = "forgeyard-key-backup-ab12cd34-20261117.fykey";
const PASSPHRASE = "copper-kettle-moon-river";
const INFO: SecretKeyInfo = { kid: "ab12cd34", source: "file", includes_ca: true, tls_mode: "forgeyard-ca", has_password: true, passphrase_min: 12, backed_up: null };

const REMINDER: Finding = { id: "secret-key-backup", key: "secret-key-backup@secret-key:ab12cd34", severity: "warning",
  title: "Back up the secret key (key id ab12cd34)", detail: "Keep a copy.", fix_url: "/guide/en/how-to#back-up-and-restore",
  fix_label: "How to back up", scope: "secret-key:ab12cd34", pages: [], snoozable: true };

function report(findings: Finding[]): AdvisorReport {
  const warning = findings.length;
  return { findings, snoozed: [], counts: { error: 0, warning, info: 0 }, badge: warning, snooze_days: 30, checked_at: "2026-11-17T00:00:00Z" };
}

type Reply = { status: number; body: unknown; headers?: Record<string, string>; raw?: BodyInit };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, info: SecretKeyInfo = INFO) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r: Reply = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? (url === "/api/secret-key" ? { status: 200, body: info }
        : url === "/api/onboarding" ? { status: 200, body: { kind: "setup", steps: [], done: 0, total: 0, complete: true, hidden: false, gateway_url: "", example: null } }
          : url === "/api/machines" ? { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } }
            : { status: 404, body: {} });
    return new Response(r.raw ?? JSON.stringify(r.body), { status: r.status, headers: r.headers });
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

function fill(dialog: HTMLElement, password: string, passphrase: string, again = passphrase) {
  fireEvent.change(within(dialog).getByLabelText("Your password"), { target: { value: password } });
  fireEvent.change(within(dialog).getByLabelText("Backup passphrase"), { target: { value: passphrase } });
  fireEvent.change(within(dialog).getByLabelText("Passphrase again"), { target: { value: again } });
}

const posts = (fetch: ReturnType<typeof server>) => fetch.mock.calls.filter(([u]) => u === "/api/secret-key/backup");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("secret key backup", () => {
  it("FY-WEB-440 the passphrase rules and the file name match the server's", () => {
    expect(passphraseProblem("short", "short", "pw")).toMatch(/at least 12/);
    expect(passphraseProblem("aaaaaaaaaaab", "aaaaaaaaaaab", "pw")).toMatch(/more than two/);
    expect(passphraseProblem(PASSPHRASE, PASSPHRASE, PASSPHRASE)).toMatch(/not your password/);
    expect(passphraseProblem(PASSPHRASE, PASSPHRASE + "x", "pw")).toMatch(/not the same/);
    expect(passphraseProblem(PASSPHRASE, PASSPHRASE, "pw")).toBeNull();
    expect(backupFilename(`attachment; filename="${FILE}"`)).toBe(FILE);
    expect(backupFilename('attachment; filename="../../etc/passwd"')).toBeNull();
    expect(backupFilename(null)).toBeNull();
  });

  it("FY-WEB-441 from the reminder: password and passphrase, the file is saved, how to restore, and the reminder goes", async () => {
    let current = report([REMINDER]);
    const fetch = server((url) => {
      if (url === "/api/advisor") return { status: 200, body: current };
      if (url === "/api/secret-key/backup") {
        current = report([]);
        return { status: 200, body: null, raw: new Blob([new Uint8Array([70, 89, 75, 69, 89])]),
          headers: { "Content-Disposition": `attachment; filename="${FILE}"`, "Content-Type": "application/octet-stream" } };
      }
      return undefined;
    });
    const created = vi.fn(() => "blob:fake");
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.download); });
    mount("/start");
    fireEvent.click(await screen.findByRole("button", { name: "Download a backup…" }));
    const dialog = await screen.findByRole("dialog", { name: "Back up the secret key" });
    await within(dialog).findByText("ab12cd34");
    expect(within(dialog).getByText("included")).toBeTruthy();
    fill(dialog, "harbour-lantern-71", PASSPHRASE);
    fireEvent.click(within(dialog).getByRole("button", { name: "Download the backup" }));
    expect(await within(dialog).findByText(`Downloaded ${FILE}.`)).toBeTruthy();
    expect(clicks).toEqual([FILE]);
    expect(created).toHaveBeenCalledTimes(1);
    const [, init] = posts(fetch)[0];
    expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
    expect(JSON.parse(String(init?.body))).toEqual({ password: "harbour-lantern-71", passphrase: PASSPHRASE, passphrase_confirm: PASSPHRASE });
    expect(within(dialog).getByLabelText("Restore command").textContent).toBe(`uv run --no-sync python -m forgeyard secrets restore ${FILE}`);
    // The reminder is still on screen behind the dialog; closing it asks the advisor again.
    const before = fetch.mock.calls.filter(([u]) => u === "/api/advisor").length;
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    await screen.findByText("Nothing to look at.");
    expect(fetch.mock.calls.filter(([u]) => u === "/api/advisor").length).toBeGreaterThan(before);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("FY-WEB-442 a wrong password is said in the dialog, empties the password field and never signs out", async () => {
    const fetch = server((url) => {
      if (url === "/api/advisor") return { status: 200, body: report([REMINDER]) };
      if (url === "/api/secret-key/backup") return { status: 401, body: { detail: { code: "wrong_password", message: "That is not your password." } } };
      return undefined;
    });
    mount("/start");
    fireEvent.click(await screen.findByRole("button", { name: "Download a backup…" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("ab12cd34");
    fill(dialog, "not-it-at-all", PASSPHRASE);
    fireEvent.click(within(dialog).getByRole("button", { name: "Download the backup" }));
    expect(await within(dialog).findByText("That is not your password.")).toBeTruthy();
    expect((within(dialog).getByLabelText("Your password") as HTMLInputElement).value).toBe("");
    expect((within(dialog).getByLabelText("Backup passphrase") as HTMLInputElement).value).toBe(PASSPHRASE);
    expect(screen.getByTestId("where").textContent).toBe("/start");
    expect(posts(fetch)).toHaveLength(1);
  });

  it("FY-WEB-443 a passphrase that is too short or not typed the same twice is refused before anything is sent", async () => {
    const fetch = server((url) => (url === "/api/advisor" ? { status: 200, body: report([REMINDER]) } : undefined));
    mount("/start");
    fireEvent.click(await screen.findByRole("button", { name: "Download a backup…" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("ab12cd34");
    fill(dialog, "harbour-lantern-71", "too-short");
    fireEvent.click(within(dialog).getByRole("button", { name: "Download the backup" }));
    expect(await within(dialog).findByText(/at least 12 characters\./)).toBeTruthy();
    fill(dialog, "harbour-lantern-71", PASSPHRASE, PASSPHRASE + "!");
    fireEvent.click(within(dialog).getByRole("button", { name: "Download the backup" }));
    expect(await within(dialog).findByText("The two passphrases are not the same.")).toBeTruthy();
    expect(posts(fetch)).toHaveLength(0);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("FY-WEB-444 on the Trust page for admins; an account without a password is told why it cannot", async () => {
    server((url) => (url === "/api/tls" ? { status: 200, body: { mode: "forgeyard-ca", https: true, hsts: false, server_url: "https://forge.lan:7860", ca: null,
      admin: { trust_command: null, server_certificate: null } } } : url === "/api/advisor" ? { status: 200, body: report([]) } : undefined),
    { ...INFO, has_password: false, includes_ca: false, tls_mode: "own" });
    mount("/trust");
    fireEvent.click(await screen.findByRole("button", { name: "Download a key backup…" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/has no password to confirm this with/)).toBeTruthy();
    expect(within(dialog).getByText(/uses your own certificate/)).toBeTruthy();
    expect((within(dialog).getByRole("button", { name: "Download the backup" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
