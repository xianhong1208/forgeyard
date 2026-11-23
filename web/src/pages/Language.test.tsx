// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import type { AdvisorReport, Finding } from "../lib/advisor";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: unknown = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "approval", user, tls: { mode: "forgeyard-ca", https: true } } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const { pathname } = useLocation();
  return <output data-testid="where">{pathname}</output>;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
        <Where />
      </AuthProvider>
    </MemoryRouter>,
  );
}

const finding = (over: Partial<Finding> = {}): Finding => ({
  id: "machine-offline-with-replicas", key: "machine-offline-with-replicas:machine:3", severity: "warning",
  title: "gpu-03 is offline with replicas on it", detail: "gpu-03 has been offline 2 h, and chat-a still has replicas there.",
  fix_url: "/machines/3", fix_label: "Open Machines", scope: "machine:3", pages: ["machines"], snoozable: true, ...over,
});
const report = (findings: Finding[]): AdvisorReport => ({
  findings, snoozed: [], counts: { error: 0, warning: findings.length, info: 0 }, badge: findings.length, snooze_days: 30,
  checked_at: "2026-11-23T00:00:00Z",
});

// As the server sends it: English, a fixed sentence and one that names a machine.
const step = (id: string, title: string, done: boolean, detail: string, action: { to: string; label: string } | null) =>
  ({ id, title, why: `why ${id}`, done, detail, action, blocked_by: null, who: null });
const CHECKLIST = {
  kind: "setup", done: 1, total: 2, complete: false, hidden: false, gateway_url: "https://gpu-01.example:7860/v1", trust_url: null, example: null,
  steps: [
    step("machine", "Add a machine", true, "1 machine online.", { to: "/machines", label: "Add a machine" }),
    step("model", "Add a model", false, "The catalog is empty.", { to: "/models", label: "Add a model" }),
  ],
};

function fleet(url: string): Reply | undefined {
  if (url === "/api/machines") return { status: 200, body: fixture };
  if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
  if (url === "/api/workloads") return { status: 200, body: { workloads: [], can_manage: true } };
  if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
  if (url === "/api/models") return { status: 200, body: { models: [], can_deploy: true, can_manage: true } };
  if (url === "/api/routes") return { status: 200, body: { routes: [] } };
  if (url === "/api/advisor") return { status: 200, body: report([finding()]) };
  if (url === "/api/onboarding") return { status: 200, body: CHECKLIST };
  return undefined;
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  document.documentElement.lang = "en";
  vi.unstubAllGlobals();
});

describe("the console's language", () => {
  it("FY-WEB-673 the account menu switches to 繁體中文 at once, keeps it in this browser, and the guide follows", async () => {
    server(fleet);
    const view = mount("/machines");
    expect(await screen.findByRole("heading", { level: 1, name: "Machines" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /owner/ }));
    const menu = screen.getByRole("menu");
    const group = within(menu).getByRole("group", { name: "Language" });
    expect(within(group).getByRole("menuitemradio", { name: "English" }).getAttribute("aria-checked")).toBe("true");
    const zh = within(group).getByRole("menuitemradio", { name: "繁體中文" });
    expect(zh.getAttribute("lang")).toBe("zh-TW");
    fireEvent.click(zh);

    // the page, the nav and the menu re-render in Chinese; nothing reloads
    expect(await screen.findByRole("heading", { level: 1, name: "機器" })).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "主選單" });
    expect(within(nav).getByRole("link", { name: "服務中" })).toBeTruthy();
    expect(within(screen.getByRole("menu")).getByRole("menuitemradio", { name: "繁體中文" }).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("menuitem", { name: /我的帳號與工作階段/ })).toBeTruthy();
    expect(document.documentElement.lang).toBe("zh-TW");
    expect(localStorage.getItem(LANG_KEY)).toBe("zh-TW");
    // machine tiles: kinds, memory and heartbeats in Chinese (the names and card models as they are)
    const box = await screen.findByRole("article", { name: "機器 a100-box" });
    expect(within(box).getByText(/心跳 \d+ 秒前/)).toBeTruthy();
    expect(within(box).getAllByRole("listitem")[0].getAttribute("title")).toBe("#0：可用 3.7 GiB，共 80 GiB");
    view.unmount();

    // a new page load in this browser keeps it, and help links open the guide in it
    mount("/machines");
    expect(await screen.findByRole("heading", { level: 1, name: "機器" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "怎麼加入機器？" }).getAttribute("href")).toBe("/guide/zh-TW/quick-start#3-add-a-machine");

    // and back: the narrow screens' menu has the same switch
    const narrow = within(screen.getByRole("navigation", { name: "主選單" })).getByRole("group", { name: "語言" });
    fireEvent.click(within(narrow).getByRole("button", { name: "English" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Machines" })).toBeTruthy();
    expect(within(narrow).getByRole("button", { name: "English" }).getAttribute("aria-pressed")).toBe("true");
    expect(localStorage.getItem(LANG_KEY)).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("FY-WEB-674 a Chinese browser gets the sign-in page in Chinese the first time; a wrong password says so in Chinese", async () => {
    vi.stubGlobal("navigator", { ...navigator, languages: ["zh-TW", "en"], language: "zh-TW" });
    server((url) => (url === "/api/auth/login" ? { status: 401, body: { detail: { code: "invalid_credentials", message: "Wrong username or password." } } } : undefined), null);
    mount("/login");
    expect(await screen.findByRole("heading", { level: 1, name: "登入" })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe("登入 · Forgeyard"));
    fireEvent.change(screen.getByLabelText("使用者名稱"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("密碼"), { target: { value: "not-the-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登入" }));
    expect(await screen.findByText("使用者名稱或密碼錯誤。")).toBeTruthy();
    expect(screen.getByRole("link", { name: "申請一個" }).getAttribute("href")).toBe("/register");
    expect(screen.getByRole("link", { name: "信任這台伺服器" }).getAttribute("href")).toBe("/trust");
    expect(localStorage.getItem(LANG_KEY)).toBeNull(); // detected, not stored: the browser's language still decides
  });

  it("FY-WEB-675 Setup health in zh-TW: the finding's kind in Chinese, the server's own words under it, the actions translated", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(fleet);
    mount("/start");
    const health = await screen.findByRole("region", { name: "設定健康檢查" });
    const item = await within(health).findByText("有機器離線，但上面還有副本");
    const row = item.closest("li")!;
    expect(row.textContent).toContain("伺服器的原文： gpu-03 is offline with replicas on it");
    expect(within(row).getByText(/gpu-03 has been offline 2 h/).getAttribute("lang")).toBe("en");
    expect(within(row).getByRole("link", { name: "開啟機器" }).getAttribute("href")).toBe("/machines/3");
    expect(within(row).getByRole("button", { name: "隱藏 30 天" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "設定健康檢查：1 個警告" })).toBeTruthy();
    // the checklist: titles by the step's id, fixed sentences by their text, the rest as sent
    expect(screen.getByRole("heading", { level: 1, name: "設定 Forgeyard" })).toBeTruthy();
    const model = screen.getByRole("listitem", { name: "步驟 2：加入模型" });
    expect(within(model).getByText("目錄是空的。")).toBeTruthy();
    expect(within(model).getByRole("link", { name: /加入模型/ }).getAttribute("href")).toBe("/models");
    expect(screen.getByRole("listitem", { name: "步驟 1：加入機器（已完成）" }).textContent).toContain("1 machine online.");
    expect(screen.getByText(/下一步：加入模型/)).toBeTruthy();
  });

  it("FY-WEB-676 Serving in zh-TW with nothing deployed: the empty page names every kind in Chinese", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(fleet);
    mount("/serving");
    expect(await screen.findByRole("heading", { level: 1, name: "服務中" })).toBeTruthy();
    expect(await screen.findByRole("heading", { level: 2, name: "還沒有部署" })).toBeTruthy();
    for (const kind of ["對話", "嵌入", "重排序", "語音轉文字", "文字轉語音", "圖片", "影片", "視覺"]) expect(screen.getByText(kind)).toBeTruthy();
    // The link waits for the catalog (can_deploy), which may answer after the deployments.
    expect((await screen.findAllByRole("link", { name: "部署模型" }))[0].getAttribute("href")).toBe("/serving/deploy");
  });

  it("FY-WEB-677 a page not translated yet still works in zh-TW: its own words in English, the frame in Chinese", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server((url) => (url === "/api/audit" || url.startsWith("/api/audit?") ? { status: 200, body: { events: [], next_before: null } } : fleet(url)));
    mount("/audit");
    expect(await screen.findByText("No events.")).toBeTruthy();
    expect(within(screen.getByRole("navigation", { name: "主選單" })).getByRole("link", { name: "稽核紀錄" })).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/audit");
  });
});
