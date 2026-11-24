// @vitest-environment jsdom
// #189, page group 4: API keys, Cloud (providers, prices, budgets, cloud models, routes), Users, the audit log, my
// account and Trust this server, in Traditional Chinese, and the English text they had before.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { hasKey } from "../i18n/core";
import { LANG_KEY } from "../i18n/lang";
import { LEVEL_LABEL, resetText, spendText, type Budget } from "../lib/budgets";
import { CLOUD_POLICIES, policyLabel } from "../lib/cloudPolicy";
import { device } from "../lib/format";
import { foldLabel, keyLimits, keyModels, perMinute } from "../lib/gateway";
import { PRICE_LABEL, perMillion, SOURCE_LABEL, usd } from "../lib/prices";
import { CLOUD_LABEL, ROUTE_KIND_LABEL, spillRules, targetHeader, targetName, targetWhere } from "../lib/routing";
import type { ApiKey, KeysResponse, Provider, ProvidersResponse, RouteInfo, RouteTarget, TlsInfo } from "../lib/types";
import { ActionName, detailText } from "./Audit";
import { sessionLength } from "./Account";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: "2026-11-22T00:00:00Z" };
type Reply = { status: number; body?: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user: unknown = OWNER, tls = { mode: "forgeyard-ca", https: true }) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user, tls } }
      : route(url, init) ?? (url === "/api/advisor" || url === "/api/onboarding" ? { status: 404, body: {} } : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const zh = () => localStorage.setItem(LANG_KEY, "zh-TW");
// Headings, column heads, labels and buttons of the page itself (not the frame): any with Latin words also has Chinese
// in it, apart from names kept as they are (a provider's or a model's name as a heading).
const KEPT = /API|HTTPS|SHA-256|Forgeyard|OpenRouter|OpenAI|Base URL|\/models|token|agent|\.env/g;
function headingsInChinese(names: string[] = []) {
  const main = document.querySelector("main") ?? document.body;
  const bad = [...main.querySelectorAll("h1, h2, h3, th, legend, label, button")]
    .map((h) => names.reduce((s, n) => s.split(n).join(""), (h.textContent ?? "").replace(KEPT, "")).trim())
    .filter((s) => /[A-Za-z]{3,}/.test(s) && !/\p{Script=Han}/u.test(s));
  expect(bad).toEqual([]);
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const key = (over: Partial<ApiKey> = {}): ApiKey => ({
  id: "k1", name: "laptop", prefix: "fyk_AbCdEfGh", models: null, rpm: null, tpm: null, effective_rpm: 60, effective_tpm: 15000,
  expires_at: null, created_at: "2026-11-10T10:00:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
  user: { id: 1, username: "owner" }, this_minute: { requests: 0, tokens: 0 }, cloud: "off", ...over,
});
const keys = (over: Partial<KeysResponse> = {}): KeysResponse => ({
  keys: [key(), key({ id: "k2", name: "old", prefix: "fyk_OldOldOl", status: "revoked", revoked_by: "owner" })],
  limits: { default_rpm: 60, default_tpm: null, max_rpm: 120, max_tpm: null }, is_admin: true,
  gateway_url: "http://forge.lan:7860/v1", models: [{ name: "chat", api: "openai-chat" }],
  cloud: { default: "off", max: "fallback" }, ...over,
});
const budget = (b: number | null, spent: number, level: Budget["level"] = "ok"): Budget =>
  ({ budget_micros: b, spent_micros: spent, reserved_micros: 0, used: null, level });

const EGRESS = { proxy: false, allowed_hosts: [], ca_file: false, private_hosts: false, cooldown_s: 30, read_timeout_s: 300 };
const KINDS = [{ kind: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", base_url_hint: "", auth: "bearer", embeddings: true,
  models_endpoint: true, default_external: true, key_required: true, note: "Complete on Chat Completions." }];
const PROVIDER: Provider = {
  id: 3, name: "openai", kind: "openai", kind_label: "OpenAI", base_url: "https://api.openai.com/v1", has_key: true, key_hint: "…9f2c",
  external: true, allow_http: false, enabled: true, timeout_s: null, created_by: "owner", created_at: "2026-11-11T00:00:00Z",
  updated_at: "2026-11-11T00:00:00Z", headers: {}, budget: budget(50_000_000, 45_000_000, "warning"),
  models: [{ id: 8, provider_id: 3, upstream_model: "gpt-4o-mini", api: "openai-chat", display_name: null, context_length: 128000, drop_params: [],
    price_input: 150_000, price_output: 600_000, price_cache_read: 75_000, price_cache_write: null, price_source: "openrouter",
    cooldown_s: 12, in_flight: 1, sent: 4, created_at: "2026-11-11T00:00:00Z", routes: ["chat"] }],
};

const GEMINI = { id: 3, name: "Gemini", kind: "gemini", external: true, enabled: true };
const local = (id: number, depId: number, name: string, tier = 0): RouteTarget => ({
  id, tier, kind: "local", weight: 1, enabled: true, upstream_model: name, cloud: false, provider_model: null,
  deployment: { id: depId, name, served_name: name, ready: 1, replicas: 2, concurrency: 4, context: 4096 },
});
const cloud = (id: number, tier = 1): RouteTarget => ({
  id, tier, kind: "provider", weight: 1, enabled: true, upstream_model: "gemini-2.5-flash", cloud: true, deployment: null,
  provider_model: { id: 8, upstream_model: "gemini-2.5-flash", display_name: null, api: "openai-chat", context_length: null, provider: GEMINI },
});
const route = (over: Partial<RouteInfo> & Pick<RouteInfo, "id" | "served_name" | "targets">): RouteInfo => ({
  api: "openai-chat", upstream_model: null, deployment_id: null, local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "mixed",
  has_local: true, has_cloud: true, endpoints: [], usable: true, created_by: "owner", ...over,
});
const SMART = route({ id: 10, served_name: "smart", targets: [local(1, 1, "chat"), cloud(2)], wait_ms: 250, spill_on_long_prompt: true });

// Every action the server records (forgeyard/**/*.py: audit.record(db, "<action>", …)).
const ACTIONS = [
  "api_key.changed", "api_key.created", "api_key.revoked", "api_key_cloud.changed", "api_key_limits.changed", "auth.login", "auth.login_failed",
  "auth.logout", "auth.other_sessions_ended", "auth.password_changed", "auth.password_set_by_link", "auth.session_ended", "budget.exhausted",
  "budget.warning", "deployment.changed", "deployment.created", "deployment.deleted", "deployment.started", "deployment.stopped",
  "hf_token.cleared", "hf_token.set", "machine.gpu_allowlist_changed", "machine.gpu_keys_changed", "machine.join_refused",
  "machine.join_token_cancelled", "machine.join_token_created", "machine.joined", "machine.removed", "machine.revoked", "model.added",
  "model.cache_removed", "model.changed", "model.checksum_recorded", "model.download_cancelled", "model.download_requested", "model.removed",
  "model_sources.changed", "node.fetch_via_changed", "node.gpus_changed", "node.image_copied", "node.image_removed", "node.image_uploaded", "node.relay_copy_refused",
  "provider.changed", "provider.cloned", "provider.created", "provider.deleted", "provider.model_changed", "provider.model_removed",
  "provider.models_added", "provider.prices_refreshed", "recipe.created", "recipe.deleted", "recipe.updated", "route.changed", "route.created",
  "route.deleted", "secret_key.backup_confirmed", "secret_key.backup_downloaded", "secret_key.backup_failed", "setup.owner_created",
  "setup.owner_from_env", "setup.token_rejected", "user.approved", "user.budget_changed", "user.created", "user.deleted",
  "user.password_link_issued", "user.registered", "user.signed_out", "user.updated", "workload.created", "workload.deleted",
  "workload.gateway_changed", "workload.started", "workload.stopped",
];

describe("API keys in zh-TW", () => {
  it("FY-WEB-820 the keys page: title, table, chips, limits and the fold of revoked keys in Chinese; names and codes as they are", async () => {
    zh();
    server((url) => (url.startsWith("/api/keys") ? { status: 200, body: keys() } : undefined));
    mount("/keys");
    expect(await screen.findByRole("heading", { level: 1, name: "API 金鑰" })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe("API 金鑰 · Forgeyard"));
    expect(await screen.findByRole("heading", { name: "閘道位址" })).toBeTruthy();
    expect(screen.getByText(/在任何 OpenAI 用戶端把它設為/)).toBeTruthy();
    expect(screen.getByText("base_url")).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(
      ["金鑰", "模型", "限制", "雲端", "預算", "上次使用", "到期", "狀態", "操作"]);
    const row = within(table).getByText("laptop").closest("tr") as HTMLElement;
    expect(within(row).getByText("所有部署")).toBeTruthy();
    expect(within(row).getByText("每分鐘 60 次請求")).toBeTruthy();
    expect(within(row).getByText("每分鐘 1.5 萬個 token")).toBeTruthy();
    expect(within(row).getByText("永不").getAttribute("title")).toBe("請求永遠不會離開你的網路。對這把金鑰來說，只在雲端的模型不存在。");
    expect(within(row).getByText("啟用中")).toBeTruthy();
    expect(within(row).getAllByText("從未").length).toBe(2);
    expect(row.querySelector("td")?.getAttribute("data-label")).toBe("金鑰");
    fireEvent.click(screen.getByRole("button", { name: "顯示 1 把已撤銷的金鑰" }));
    expect(await screen.findByText("已撤銷")).toBeTruthy();
    expect(screen.getByText("由 owner")).toBeTruthy();
    expect(screen.getByRole("button", { name: "隱藏已撤銷的金鑰" })).toBeTruthy();
    // the admin panels
    expect(screen.getByRole("heading", { name: "金鑰的限制" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "雲端政策" })).toBeTruthy();
    const policy = screen.getByLabelText("政策") as HTMLSelectElement;
    expect([...policy.options].map((o) => o.textContent)).toEqual(["不使用雲端", "個人可自行選擇溢出", "預設溢出", "允許雲端", "自訂"]);
    expect(policy.value).toBe("opt-in");
    expect(screen.getByText("新金鑰預設為「永不」；個人可以把自己的金鑰設為「僅溢出時」。")).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-821 a new key in zh-TW: the form, its choices and the key shown once with the example", async () => {
    zh();
    server((url, init) => {
      if (url === "/api/keys" && init?.method === "POST") {
        return { status: 201, body: { key: "fyk_SECRETVALUE0123456789", gateway_url: "http://forge.lan:7860/v1",
          api_key: key({ id: "k3", name: "ci", prefix: "fyk_SECRETVA", expires_at: "2026-12-23T00:00:00Z" }) } };
      }
      if (url.startsWith("/api/keys")) return { status: 200, body: keys({ is_admin: false }) };
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: /新增金鑰/ }));
    expect(await screen.findByRole("heading", { name: "新增 API 金鑰" })).toBeTruthy();
    const expires = screen.getByLabelText("到期") as HTMLSelectElement;
    expect([...expires.options].map((o) => o.textContent)).toEqual(["永不", "7 天", "30 天", "90 天", "1 年"]);
    expect(screen.getByLabelText(/所有部署，包含之後新增的/)).toBeTruthy();
    expect(screen.getByText("留空：使用預設值；最多 120。")).toBeTruthy();
    const picks = screen.getByRole("radiogroup", { name: "這把金鑰可以使用雲端供應商嗎？" });
    expect(within(picks).getByText("允許").closest("label")?.getAttribute("title")).toBe("管理員尚未允許個人金鑰這樣設定");
    fireEvent.click(within(picks).getByText("僅溢出時"));
    expect(screen.getByLabelText("每月預算（美元）")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("每月預算（美元）"), { target: { value: "-1" } });
    fireEvent.change(screen.getByLabelText("名稱"), { target: { value: "ci" } });
    fireEvent.click(screen.getByRole("button", { name: "建立金鑰" }));
    expect(await screen.findByText("每月預算：以美元填寫，例如 25 或 2.50（留空：沒有預算）。")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("每月預算（美元）"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "建立金鑰" }));
    expect(await screen.findByRole("heading", { name: "現在就複製你的金鑰" })).toBeTruthy();
    expect(screen.getByText(/只會顯示這一次；Forgeyard 只保存它的雜湊值和前綴/)).toBeTruthy();
    expect(screen.getByText(/它將於.*到期。/)).toBeTruthy();
    expect(screen.getByRole("tablist", { name: "範例" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "複製範例" })).toBeTruthy();
  });

  it("FY-WEB-822 budget warnings and the edit form in zh-TW: amounts in US dollars the Taiwan way, locks named in Chinese", async () => {
    zh();
    const locked = key({ id: "k4", name: "ci", user: { id: 2, username: "ursula" }, rpm: 30, rpm_locked_by: "owner", cloud: "fallback",
      budget: budget(5_000_000, 5_000_000, "exhausted"), budget_locked_by: "owner" });
    server((url) => (url.startsWith("/api/keys") ? { status: 200, body: keys({
      keys: [key({ budget: budget(10_000_000, 9_000_000, "warning") }), locked],
      resets_at: "2026-12-01T00:00:00Z", person_budget: budget(20_000_000, 1_250_000) }) } : undefined));
    mount("/keys");
    expect(await screen.findByRole("heading", { name: "你的每月預算" })).toBeTruthy();
    expect(screen.getByText("本月你所有金鑰和試用區合計的雲端花費。它會在 UTC 時間 12月1日 00:00 重新計算。")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "你的每月預算：US$1.25，預算 US$20.00" })).toBeTruthy();
    expect(screen.getByText("owner 的 laptop（fyk_AbCdEfGh…）：預算超過 80%。")).toBeTruthy();
    expect(screen.getByText("ursula 的 ci（fyk_AbCdEfGh…）：預算已用完；在 UTC 時間 12月1日 00:00 之前，它的請求只走本機。")).toBeTruthy();
    const row = screen.getByText("ci", { selector: ".strong" }).closest("tr") as HTMLElement;
    expect(within(row).getAllByText("由 owner 設定").length).toBe(2);
    expect(within(row).getByText("已用完")).toBeTruthy();
    fireEvent.click(within(row).getByRole("button", { name: "限制" }));
    const form = screen.getByRole("form", { name: "ci 的限制" });
    expect(within(form).getByText("已對 ursula 鎖定（由 owner 設定）。清空即可解除鎖定。", { selector: "#f-rpm-k4-hint" })).toBeTruthy();
    expect(within(form).getByRole("button", { name: "解除請求數鎖定" })).toBeTruthy();
    expect(within(form).getByRole("button", { name: "解除預算鎖定" })).toBeTruthy();
  });

  it("FY-WEB-823 the key lines of lib/gateway in each language: limits, models, the fold", () => {
    const k = key({ effective_rpm: 1, effective_tpm: 1_500_000, models: ["chat", "embed"] });
    const revoked = [key({ status: "revoked" }), key({ status: "expired" })];
    expect(perMinute(3, "request")).toBe("3 requests/min");
    expect(perMinute(1, "request")).toBe("1 request/min");
    expect(perMinute(10_000, "token")).toBe("10k tokens/min");
    expect(keyLimits(k).map((l) => l.text)).toEqual(["1 request/min", "1.5M tokens/min"]);
    expect(keyLimits(key({ effective_rpm: null, effective_tpm: null }))[0].text).toBe("No limits");
    expect(keyModels(key())).toBe("All deployments");
    expect(foldLabel(revoked, false)).toBe("Show 2 revoked or expired keys");
    expect(foldLabel([revoked[1]], false)).toBe("Show 1 expired key");
    expect(foldLabel(revoked, true)).toBe("Hide revoked or expired keys");
    zh();
    expect(perMinute(3, "request")).toBe("每分鐘 3 次請求");
    expect(perMinute(10_000, "token")).toBe("每分鐘 1 萬個 token");
    expect(keyLimits(k).map((l) => l.text)).toEqual(["每分鐘 1 次請求", "每分鐘 150 萬個 token"]);
    expect(keyLimits(key({ effective_rpm: null, effective_tpm: null }))[0].text).toBe("無限制");
    expect(keyModels(key())).toBe("所有部署");
    expect(keyModels(k)).toBe("chat, embed");
    expect(foldLabel(revoked, false)).toBe("顯示 2 把已撤銷或已過期的金鑰");
    expect(foldLabel([revoked[1]], true)).toBe("隱藏已過期的金鑰");
    expect(CLOUD_LABEL.fallback).toBe("僅溢出時");
    expect(CLOUD_POLICIES.map((p) => p.label)).toEqual(["不使用雲端", "個人可自行選擇溢出", "預設溢出", "允許雲端"]);
    expect(policyLabel("custom")).toBe("自訂");
  });
});

describe("money and budgets", () => {
  it("FY-WEB-824 money stays in US dollars: the English form unchanged, Intl's Taiwan form (US$) in zh-TW", () => {
    const b = budget(5_000_000, 1_200_000);
    expect([usd(0), usd(185), usd(123_400), usd(1_234_567_890)]).toEqual(["$0.00", "$0.000185", "$0.1234", "$1,234.57"]);
    expect([perMillion(2_500_000), perMillion(75_000), perMillion(0), perMillion(1_500_000_000)]).toEqual(["$2.50", "$0.075", "free", "$1500.00"]);
    expect(spendText(b)).toBe("$1.20 of $5.00");
    expect(resetText("2026-12-01T00:00:00Z")).toBe("December 1, 00:00 UTC");
    expect(resetText(undefined)).toBe("the first of next month");
    expect(LEVEL_LABEL.warning).toBe("past 80%");
    expect(PRICE_LABEL.price_cache_read).toBe("Cached input");
    zh();
    expect([usd(0), usd(185), usd(123_400), usd(1_234_567_890)]).toEqual(["US$0.00", "US$0.000185", "US$0.1234", "US$1,234.57"]);
    expect([perMillion(2_500_000), perMillion(0), perMillion(1_500_000_000)]).toEqual(["US$2.50", "免費", "US$1500.00"]);
    expect(spendText(b)).toBe("US$1.20，預算 US$5.00");
    expect(spendText(budget(null, 185))).toBe("本月 US$0.000185，沒有預算");
    expect(resetText("2026-12-01T00:00:00Z")).toBe("UTC 時間 12月1日 00:00");
    expect(LEVEL_LABEL.exhausted).toBe("已用完");
    expect(PRICE_LABEL.price_cache_read).toBe("快取輸入");
    expect(SOURCE_LABEL.openrouter).toBe("來自 OpenRouter");
    expect(SOURCE_LABEL.unknown).toBeUndefined();
  });
});

describe("Cloud in zh-TW", () => {
  it("FY-WEB-825 providers: the list, the chips, what leaves this server and the add form in Chinese; server notes as sent", async () => {
    zh();
    server((url) => {
      if (url === "/api/providers") return { status: 200, body: { providers: [PROVIDER], kinds: KINDS, egress: EGRESS, is_admin: true } satisfies ProvidersResponse };
      if (url === "/api/providers/where") return { status: 200, body: { external: true, basis: "kind", reason: "OpenAI is a cloud API." } };
    });
    mount("/cloud");
    expect(await screen.findByRole("heading", { level: 1, name: "雲端" })).toBeTruthy();
    const tabs = screen.getByRole("tablist", { name: "雲端" });
    expect(within(tabs).getAllByRole("tab").map((x) => x.textContent)).toEqual(["供應商", "雲端模型"]);
    const row = (await screen.findByRole("link", { name: "openai" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("雲端")).toBeTruthy();
    expect(within(row).getByText("預算超過 80%")).toBeTruthy();
    expect(within(row).getByText("冷卻中 12 秒").getAttribute("title")).toBe("它最近失敗過，會被排在最後嘗試");
    expect(within(row).getByText(/US\$0.15 \/ US\$0.60/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "哪些連線會離開這台伺服器" })).toBeTruthy();
    expect(screen.getByText("直接連線（不經代理）；驗證 TLS；永不跟隨重新導向")).toBeTruthy();
    expect(screen.getByText("在 30 秒內排在最後嘗試；回答最多可花 300 秒")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /新增供應商/ }));
    expect(screen.getByRole("heading", { name: "新增供應商" })).toBeTruthy();
    expect(screen.getByRole("radiogroup", { name: "供應商種類" })).toBeTruthy();
    expect(screen.getByText("Complete on Chat Completions.")).toBeTruthy(); // the server's note, as sent
    expect(screen.getByText("在你的網路外（雲端）。")).toBeTruthy();
    const why = await screen.findByRole("status", { name: "為什麼勾選了這個" });
    expect(within(why).getByText("網路外")).toBeTruthy();
    expect(why.textContent).toContain("OpenAI is a cloud API.");
    fireEvent.click(screen.getByRole("checkbox", { name: /在你的網路外/ }));
    expect(why.textContent).toContain("你改過這裡：它是手動取消勾選的。");
    expect(screen.getByText("允許明文 HTTP（只限私有位址）。")).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-826 a provider's page: budget, models with prices, settings and the connection test in Chinese", async () => {
    zh();
    server((url, init) => {
      if (url === "/api/providers/3/test" && init?.method === "POST") return { status: 200, body: { ok: true, status: 200, ms: 1234, models: 2, sample: ["gpt-4o", "gpt-4o-mini"], message: null } };
      if (url === "/api/providers/3") return { status: 200, body: { provider: PROVIDER, kinds: KINDS, egress: EGRESS, is_admin: true } };
    });
    mount("/cloud/providers/3");
    expect(await screen.findByRole("heading", { level: 1, name: "openai" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /雲端供應商/ })).toBeTruthy();
    expect(screen.getByText(/https:\/\/api.openai.com\/v1 · 由 owner 新增於/)).toBeTruthy();
    expect(screen.getByText("金鑰 …9f2c")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "每月預算" })).toBeTruthy();
    expect(screen.getByText("本月預算已花掉超過 80%。")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "openai 的預算：US$45.00，預算 US$50.00" })).toBeTruthy();
    const models = screen.getByRole("heading", { name: "模型" }).closest("section") as HTMLElement;
    expect(within(models).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(
      ["模型", "提供", "每 100 萬個 token 的價格", "上下文", "使用它的路由", "伺服器啟動以來", "操作"]);
    expect(within(models).getByText("對話")).toBeTruthy();
    expect(within(models).getByText("輸入")).toBeTruthy();
    expect(within(models).getByText("快取")).toBeTruthy();
    expect(within(models).getByText(/^來自 OpenRouter/)).toBeTruthy();
    expect(within(models).getByText("已送出 4 個 · 進行中 1 個 · 冷卻中 12 秒")).toBeTruthy();
    fireEvent.click(within(models).getByRole("button", { name: "價格" }));
    const editor = screen.getByRole("form", { name: "gpt-4o-mini 的價格" });
    expect(within(editor).getByLabelText("快取寫入（$）")).toBeTruthy();
    fireEvent.change(within(editor).getByLabelText("輸入（$）"), { target: { value: "abc" } });
    fireEvent.click(within(editor).getByRole("button", { name: "儲存價格" }));
    expect(await within(editor).findByText("輸入：請填每 100 萬個 token 的美元金額，例如 2.50。")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "設定" })).toBeTruthy();
    expect(screen.getByLabelText("更換金鑰（目前為 …9f2c）")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /測試連線/ }));
    expect(await screen.findByText("連線成功，耗時 1234 毫秒：列出 2 個模型（gpt-4o, gpt-4o-mini）。")).toBeTruthy();
    headingsInChinese(["openai"]);
  });

  it("FY-WEB-827 refreshing prices from OpenRouter in zh-TW: matches, the change and what was applied", async () => {
    zh();
    const row = (over: object) => ({ model_id: 8, provider_id: 3, provider: "openai", kind: "openai", upstream_model: "gpt-4o-mini", api: "openai-chat",
      ref: "openai/gpt-4o-mini", match: "exact", manual: false, source: "manual", checked_at: null,
      before: { price_input: 150_000, price_output: 600_000, price_cache_read: null, price_cache_write: null },
      after: { price_input: 150_000, price_output: 700_000, price_cache_read: null, price_cache_write: null }, changed: true, ...over });
    server((url, init) => {
      if (url === "/api/prices/openrouter/preview") return { status: 200, body: { checked_at: "2026-11-23T00:00:00Z", listed: 1234, matched: 1, changed: 1,
        rows: [row({}), row({ model_id: 9, upstream_model: "o-mystery", ref: null, match: "none", after: null, changed: false })] } };
      if (url === "/api/prices/openrouter/apply" && init?.method === "POST") return { status: 200, body: { applied: [8], skipped: [{ model_id: 9, reason: "not_listed" }] } };
      if (url === "/api/providers") return { status: 200, body: { providers: [PROVIDER], kinds: KINDS, egress: EGRESS, is_admin: true } };
    });
    mount("/cloud");
    fireEvent.click(await screen.findByRole("button", { name: /從 OpenRouter 更新價格/ }));
    const dialog = await screen.findByRole("dialog", { name: "從 OpenRouter 更新價格" });
    expect(await within(dialog).findByText("OpenRouter 列出 1234 個模型 · 你的模型有 1 個對上 · 1 個價格不同。")).toBeTruthy();
    expect(within(dialog).getByText("相同 id").getAttribute("title")).toBe("OpenRouter 上 id 相同的模型。");
    expect(within(dialog).getByText("沒有對應")).toBeTruthy();
    expect(within(dialog).getByText("維持原樣")).toBeTruthy();
    expect(within(dialog).getAllByText("openai · 目前手動輸入").length).toBe(2);
    expect(within(dialog).getByLabelText("o-mystery 在 OpenRouter 上的 id")).toBeTruthy();
    expect(within(dialog).getByLabelText("把 OpenRouter 的價格套用到 openai/gpt-4o-mini")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "套用勾選的 1 個" }));
    expect(await screen.findByText("已把 OpenRouter 的價格套用到 1 個模型。略過：o-mystery: OpenRouter 沒有列出它。")).toBeTruthy();
  });

  it("FY-WEB-828 cloud models and a route's page in zh-TW: tiers, where each target is, and when a request moves on", async () => {
    zh();
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: { routes: [SMART], cloud: "on", is_admin: true } };
      if (url === "/api/routes/10") return { status: 200, body: { route: SMART, is_admin: true } };
      if (url === "/api/route-options") return { status: 200, body: { deployments: [], provider_models: [] } };
    });
    mount("/cloud?tab=models");
    const card = await screen.findByRole("article", { name: "模型名稱 smart" });
    expect(within(card).getByText("先本機，再雲端")).toBeTruthy();
    expect(within(card).getByText("對話")).toBeTruthy();
    expect(within(card).getByText("首先")).toBeTruthy();
    expect(within(card).getByText("1/2 就緒 · 每個同時處理 4 個")).toBeTruthy();
    expect(within(card).getAllByText("雲端").length).toBeGreaterThan(0);
    expect(screen.getByText(/用戶端把這個名稱當作/)).toBeTruthy();
    fireEvent.click(within(card).getByRole("link", { name: "編輯" }));
    expect(await screen.findByRole("heading", { level: 1, name: "smart" })).toBeTruthy();
    expect(screen.getByText("04 · 雲端 · 雲端模型")).toBeTruthy();
    expect(screen.getByText("對話 · 由 owner 建立")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "層" })).toBeTruthy();
    expect(screen.getByText("接著，第 2 層")).toBeTruthy();
    expect(screen.getByLabelText("Gemini/gemini-2.5-flash 的權重")).toBeTruthy();
    expect(screen.getByText(/沒有就緒的本機副本、每個副本都在忙、推論引擎回傳 429 或 503、或提示詞超過本機的上下文長度時，移到下一層（最多先等 250 毫秒/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "什麼時候往下移" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "儲存" })).toBeTruthy();
    headingsInChinese(["smart"]);
  });

  it("FY-WEB-829 lib/routing in each language: kinds, targets, spill rules and the target header", () => {
    const one = route({ id: 1, served_name: "x", targets: [local(1, 1, "chat")] });
    const off = { ...cloud(3), provider_model: { ...cloud(3).provider_model!, provider: { ...GEMINI, enabled: false } } };
    const gone = { ...cloud(4), provider_model: null };
    expect(ROUTE_KIND_LABEL.mixed).toBe("local, then cloud");
    expect(targetWhere(local(1, 1, "chat"))).toBe("1/2 ready · 4 at once each");
    expect(targetWhere(off)).toBe("cloud · provider disabled");
    expect(targetName(gone)).toBe("(removed)");
    expect(spillRules(SMART)).toBe("Moves to the next tier when no local replica is ready, every replica is busy, the engine answers 429 or 503, the prompt is longer than the local context (after waiting up to 250 ms for a slot). Never once the answer has started.");
    expect(spillRules({ ...SMART, wait_ms: 0, spill_on_long_prompt: false })).toBe("Moves to the next tier when no local replica is ready, every replica is busy, the engine answers 429 or 503. Never once the answer has started.");
    expect(spillRules(one)).toBe("Only one tier has a target: a request has nowhere else to go. Put another local deployment or a cloud model in a later tier.");
    expect(spillRules({ ...one, local_only: true })).toBe("Never leaves your network (local only).");
    expect(targetHeader("local:chat")).toBe("local chat");
    zh();
    expect(ROUTE_KIND_LABEL.cloud).toBe("僅雲端");
    expect(targetWhere(off)).toBe("雲端 · 供應商已停用");
    expect(targetName(gone)).toBe("（已移除）");
    expect(spillRules(one)).toBe("只有一層有目標：請求沒有別的地方可去。把另一個本機部署或雲端模型放進後面的層。");
    expect(spillRules({ ...SMART, wait_ms: 0, spill_on_long_prompt: false })).toBe("沒有就緒的本機副本、每個副本都在忙、或推論引擎回傳 429 或 503 時，移到下一層。回答一旦開始就不會再移。");
    expect(targetHeader("local:chat")).toBe("本機 chat");
    expect(targetHeader("provider:Gemini/x")).toBe("Gemini/x");
  });
});

describe("admin pages in zh-TW", () => {
  it("FY-WEB-830 Users: the list, roles, the pending notice and what a change says, in Chinese", async () => {
    zh();
    const pending = { ...OWNER, id: 3, username: "pat", role: "user", status: "pending", is_owner: false, has_password: false, last_login_at: null };
    const bob = { ...OWNER, id: 2, username: "bob", role: "user", is_owner: false, has_password: false };
    let users = [OWNER, bob, pending];
    server((url, init) => {
      if (url === "/api/users/2" && init?.method === "PATCH") { users = [OWNER, { ...bob, role: "operator" }, pending]; return { status: 200, body: {} }; }
      if (url === "/api/users") return { status: 200, body: { users } };
    });
    mount("/users");
    expect(await screen.findByRole("heading", { level: 1, name: "使用者" })).toBeTruthy();
    expect(await screen.findByText("有 1 個帳號在等待核准。")).toBeTruthy();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["使用者", "角色", "狀態", "上次登入", "操作"]);
    expect(screen.getByText("擁有者")).toBeTruthy();
    expect(screen.getByText("你")).toBeTruthy();
    expect(screen.getByText("尚未設定密碼")).toBeTruthy();
    expect(screen.getByText("待審核")).toBeTruthy();
    expect(screen.getByLabelText("owner 的角色").getAttribute("title")).toBe("擁有者一定是管理員");
    const role = screen.getByLabelText("bob 的角色") as HTMLSelectElement;
    expect([...role.options].map((o) => o.textContent)).toEqual(["管理員", "操作員", "使用者"]);
    expect(screen.getByRole("button", { name: "核准" })).toBeTruthy();
    fireEvent.change(role, { target: { value: "operator" } });
    expect(await screen.findByText("bob 現在是操作員。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /新增使用者/ }));
    expect(screen.getByRole("heading", { name: "新使用者" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "建立並取得連結" })).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-831 the audit log in zh-TW names each action with its code beside it; English shows the code alone; unknown codes as recorded", async () => {
    zh();
    server((url) => (url.startsWith("/api/audit") ? { status: 200, body: { next_before: 7, events: [
      { id: 9, at: "2026-11-22T10:00:00Z", action: "auth.login_failed", actor: null, target: "owner", ip: "10.0.0.9", detail: { reason: "wrong_password" } },
      { id: 8, at: "2026-11-22T09:00:00Z", action: "future.thing", actor: "owner", target: null, ip: null, detail: {} },
    ] } } : undefined));
    mount("/audit");
    expect(await screen.findByRole("heading", { level: 1, name: "稽核紀錄" })).toBeTruthy();
    const failed = await screen.findByText("登入失敗");
    expect(failed.className).toBe("action action-auth action-bad");
    expect(failed.parentElement?.getAttribute("title")).toBe("記錄為 auth.login_failed");
    expect(within(failed.parentElement!).getByText("auth.login_failed")).toBeTruthy();
    expect(screen.getByText("reason: wrong_password")).toBeTruthy(); // details as recorded
    expect(screen.getByText("future.thing").className).toBe("action action-future");
    const show = screen.getByLabelText("顯示") as HTMLSelectElement;
    expect([...show.options].slice(0, 3).map((o) => o.textContent)).toEqual(["全部", "登入與工作階段", "使用者變更"]);
    expect(screen.getByRole("button", { name: "載入更早的事件" })).toBeTruthy();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["時間", "事件", "操作者", "對象", "來源", "詳細資料"]);
    cleanup();
    localStorage.setItem(LANG_KEY, "en");
    const view = render(<ActionName action="auth.login_failed" />);
    expect(view.container.innerHTML).toBe('<span class="action action-auth action-bad">auth.login_failed</span>');
  });

  it("FY-WEB-836 audit details stay as recorded, a nested change as JSON instead of [object Object]", () => {
    expect(detailText({ key: "k1", models: ["chat", "embed"], cloud: "off" })).toBe("key: k1 · models: chat → embed · cloud: off");
    expect(detailText({ before: { default: "off", max: "off" }, after: { default: "off", max: "fallback" } }))
      .toBe('before: {"default":"off","max":"off"} · after: {"default":"off","max":"fallback"}');
    expect(detailText({ targets: [{ tier: 0 }, "x"], reason: null })).toBe('targets: {"tier":0} → x · reason: null');
  });

  it("FY-WEB-832 every action the server records has a name in both languages", () => {
    const missing = ACTIONS.filter((a) => !hasKey(`admin.action.${a}`));
    expect(missing).toEqual([]);
  });

  it("FY-WEB-833 my account in zh-TW: the facts, the password form, sessions with their device, and the session lengths", async () => {
    zh();
    server((url) => (url === "/api/auth/sessions" ? { status: 200, body: { idle_minutes: 120, max_days: 30, sessions: [
      { id: "s1", current: true, created_at: "2026-11-22T00:00:00Z", last_seen_at: "2026-11-22T00:00:00Z", expires_at: "2026-12-22T00:00:00Z", ip: null,
        user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0 Safari/537.36" },
      { id: "s2", current: false, created_at: "2026-11-21T00:00:00Z", last_seen_at: "2026-11-21T00:00:00Z", expires_at: "2026-12-21T00:00:00Z", ip: "10.0.0.2",
        user_agent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/605.1" },
    ] } } : undefined));
    mount("/account");
    expect(await screen.findByRole("heading", { name: "帳號" })).toBeTruthy();
    expect(screen.getByText("擁有者")).toBeTruthy();
    expect(screen.getAllByText("管理員").length).toBeGreaterThan(0);
    expect(await screen.findByText("工作階段閒置 2 小時 後結束，最長 30 天。")).toBeTruthy();
    expect(screen.getByText("Linux 上的 Chrome")).toBeTruthy();
    expect(screen.getByText("iOS 上的 Safari")).toBeTruthy();
    expect(screen.getByText("這個瀏覽器")).toBeTruthy();
    expect(screen.getByText("IP 不明")).toBeTruthy();
    expect(screen.getByRole("button", { name: "登出其他所有工作階段" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("新密碼"), { target: { value: "a-long-password-1" } });
    fireEvent.change(screen.getByLabelText("再輸入一次新密碼"), { target: { value: "different" } });
    expect(screen.getByText("兩次輸入的密碼不一樣。")).toBeTruthy();
    expect(sessionLength(90)).toBe("90 分鐘");
    expect(device(null)).toBe("不明裝置");
    localStorage.setItem(LANG_KEY, "en");
    expect([sessionLength(60), sessionLength(120), sessionLength(1), sessionLength(90)]).toEqual(["1 hour", "2 hours", "1 minute", "90 minutes"]);
    expect(device("Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0")).toBe("Chrome on Linux");
  });

  it("FY-WEB-834 Trust this server in zh-TW: why, the CA, the steps with their bold parts, programs, and the admin part", async () => {
    zh();
    vi.stubGlobal("navigator", { ...navigator, userAgent: "Mozilla/5.0 (Macintosh; Mac OS X 15_0) Safari/605.1", languages: ["zh-TW"] });
    const info: TlsInfo = {
      mode: "forgeyard-ca", https: true, hsts: false, server_url: "https://forge.lan:7860",
      ca: { name: "CN=Forgeyard CA forge a1b2c3,O=Forgeyard", expires_at: "2036-11-08T00:00:00Z", fingerprint: "sha256:" + "ab".repeat(32),
        fingerprint_colons: Array(32).fill("AB").join(":"), own: true },
      admin: { trust_command: "forgeyard agent trust --server https://forge.lan:7860", server_certificate: { expires_at: "2027-11-08T00:00:00Z", names: ["forge.lan"], ips: [], issuer: "Forgeyard CA" } },
    };
    server((url) => (url === "/api/tls" ? { status: 200, body: info } : undefined));
    mount("/trust");
    expect(await screen.findByRole("heading", { level: 1, name: "信任這台伺服器" })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe("信任這台伺服器 · Forgeyard"));
    expect(await screen.findByRole("heading", { name: "瀏覽器為什麼會警告" })).toBeTruthy();
    expect(screen.getByText(/建立了自己的小型憑證授權單位/)).toBeTruthy();
    expect(screen.getByText("SHA-256 指紋")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "安裝" })).toBeTruthy();
    const mac = screen.getByText("macOS").closest("details") as HTMLDetailsElement;
    expect(mac.open).toBe(true);
    expect(within(mac).getByText("Forgeyard CA forge a1b2c3").tagName).toBe("STRONG");
    expect(within(mac).getByText("永遠信任").tagName).toBe("STRONG");
    expect(screen.getByText("Linux（Chrome、系統）")).toBeTruthy();
    expect(screen.getByText("iPhone 與 iPad")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "程式與 agent" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "給管理員" })).toBeTruthy();
    expect(screen.getByText(/到期前 60 天會自動更新/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /下載金鑰備份…/ })).toBeTruthy();
    expect(await screen.findByRole("link", { name: "回到 Forgeyard" })).toBeTruthy(); // once the session is read
    headingsInChinese();
  });

  it("FY-WEB-835 a server without HTTPS says so in Chinese, with the settings named as they are", async () => {
    zh();
    server((url) => (url === "/api/tls" ? { status: 200, body: { mode: "off", https: false, hsts: false, server_url: "http://forge.lan:7860", ca: null, admin: {} } } : undefined),
      OWNER, { mode: "off", https: false });
    mount("/trust");
    const off = await screen.findByText("TLS=off");
    const box = off.closest("[role]") ?? off.parentElement!;
    expect(within(box as HTMLElement).getByText("這台伺服器沒有使用 HTTPS。").tagName).toBe("STRONG");
    expect(box.textContent).toContain("密碼、工作階段 cookie 和 API 金鑰都以未加密的方式在網路上傳送。");
    expect(off.tagName).toBe("CODE");
    expect(within(box as HTMLElement).getByText("TRUSTED_PROXIES").tagName).toBe("CODE");
  });
});
