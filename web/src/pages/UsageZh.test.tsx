// @vitest-environment jsdom
// Usage in Traditional Chinese (1.5.1, #189): tabs, totals, the breakdown table, the chart and the budgets in Chinese;
// names of models, keys and people as they are (FY-WEB-800 to 806).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { setLang } from "../i18n/lang";
import type { SeriesLine, SeriesResponse } from "../lib/charts";
import type { UsageResponse, UsageRow } from "../lib/types";
import { addUp } from "./Usage";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

function server(route: (url: string) => Reply | undefined) {
  const fetch = vi.fn(async (url: string) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } } : route(url) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount() {
  setLang("zh-TW");
  return render(<MemoryRouter initialEntries={["/usage"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

function screenWidth(narrow: boolean) {
  vi.stubGlobal("matchMedia", (media: string) => ({ matches: narrow, media, addEventListener: () => undefined, removeEventListener: () => undefined }));
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  setLang("en");
  localStorage.clear();
});

const base = { errors: 1, limited: 1, prompt_tokens: 10, completion_tokens: 20, tokens: 30, latency_avg_ms: 100, latency_p50_ms: 90, latency_p95_ms: 300, unpriced: 0,
  cached_tokens: 0, spilled: 0, cost_micros: 0 };
const MODELS: UsageRow[] = Array.from({ length: 20 }, (_, i) => ({ ...base, id: `m${i + 1}`, name: `model-${String(i + 1).padStart(2, "0")}`, requests: 40 - i }));
const SOURCES: UsageRow[] = [{ ...base, id: "api", name: "API keys", requests: 30 }, { ...base, id: "console", name: "Console playground", requests: 12 }];

function usage(url: string, over: Partial<UsageResponse> = {}): UsageResponse {
  const group = new URLSearchParams(url.split("?")[1]).get("group") ?? "model";
  const rows = group === "source" ? SOURCES : MODELS;
  const total = { ...addUp(rows), id: "total", name: "Total", latency_avg_ms: 100, latency_p50_ms: 90, latency_p95_ms: 300 };
  return { from: "2026-11-17", to: "2026-11-23", days: 7, group_by: group, total, rows, everyone: false, is_admin: true, ...over };
}

const DAYS = ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"];
function series(url: string, lines: [string, string, number[]][]): SeriesResponse {
  const q = new URLSearchParams(url.split("?")[1]);
  const out: SeriesLine[] = lines.map(([id, name, values]) => ({ id, name, values, total: values.reduce((a, b) => a + b, 0), ...(id === "other" ? { other: true } : {}) }));
  return {
    step: (q.get("step") ?? "day") as SeriesResponse["step"], split: (q.get("split") ?? "none") as SeriesResponse["split"], metric: q.get("metric") ?? "requests",
    from: DAYS[0], to: DAYS[3], periods: DAYS.map((d, i) => ({ start: d, end: d, partial: i === 3 })), series: out,
    total: DAYS.map((_, i) => out.reduce((a, l) => a + l.values[i], 0)), totals: { requests: 10 }, omitted: 1, everyone: false, is_admin: true,
  };
}

const BUDGETS = { month: "2026-11", resets_at: "2026-12-01T00:00:00Z", everyone: false, is_admin: true, providers: [],
  people: [{ id: 1, username: "owner", status: "active", budget_micros: 25_000_000, spent_micros: 21_000_000, reserved_micros: 0, used: 0.84, level: "warning" }],
  keys: [{ id: "k1", name: "laptop", prefix: "fyk_ab", status: "active", user: { id: 1, username: "owner" }, locked_by: null,
    budget_micros: 5_000_000, spent_micros: 5_000_000, reserved_micros: 0, used: 1, level: "exhausted" }] };

function routes(over: Partial<UsageResponse> = {}, lines: [string, string, number[]][] = [["total", "Total", [3, 0, 5, 2]]]) {
  return server((url) => {
    if (url.startsWith("/api/usage/series")) return { status: 200, body: series(url, lines) };
    if (url.startsWith("/api/usage")) return { status: 200, body: usage(url, over) };
    if (url.startsWith("/api/budgets")) return { status: 200, body: BUDGETS };
    if (url === "/api/onboarding/seen") return { status: 200, body: {} };
  });
}

// "token" stays in English, as in the guide; units, p50 and names aside, a heading or button has no English word.
const LATIN_WORD = /\b(?![Tt]oken\b)[A-Za-z]{3,}\b/;

describe("Usage in Traditional Chinese", () => {
  it("FY-WEB-800 the Requests tab: header, tabs, periods, groups and totals in Chinese, no English word in a heading or button", async () => {
    screenWidth(false);
    routes();
    mount();
    expect(await screen.findByRole("heading", { level: 1, name: "用量" })).toBeTruthy();
    expect(screen.getByText("你經由閘道送出的請求，包括用你的 API 金鑰和在試用區送出的。")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "請求" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tab", { name: "費用" })).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "期間" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["今天", "7 天", "30 天"]);
    expect(within(screen.getByRole("group", { name: "分組依據" })).getAllByRole("button").map((b) => b.textContent))
      .toEqual(["模型", "金鑰", "日期", "來源", "本機或雲端"]);
    const dl = await screen.findByLabelText("總計");
    expect([...dl.querySelectorAll("dt")].map((d) => d.textContent)).toEqual(["請求", "錯誤", "Token 數", "延遲 p50", "延遲 p95"]);
    expect(dl.textContent).toContain("（20 個被限流）");
    expect(dl.textContent).toContain("輸入 200 · 輸出 400");
    expect(await screen.findByText(/^2026-11-17 至 2026-11-23（UTC）。延遲由閘道量測/)).toBeTruthy();
    for (const el of [...screen.getAllByRole("heading"), ...screen.getAllByRole("button")]) {
      if (el.closest(".topbar, nav")) continue;
      expect(el.textContent, el.textContent!).not.toMatch(LATIN_WORD);
    }
  });

  it("FY-WEB-801 the breakdown table: Chinese headings, the rest added up, the server's group names translated, model names untouched", async () => {
    screenWidth(false);
    routes();
    mount();
    const table = await screen.findByRole("table", { name: "依模型分組的請求" });
    const heads = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(heads).toEqual(["模型", "請求", "錯誤", "輸入 token", "輸出 token", "平均", "p50", "p95"]);
    const rows = within(table).getAllByRole("row").slice(1) as HTMLTableRowElement[];
    expect(rows[0].cells[0].textContent).toBe("model-01");
    expect(rows[0].cells[2].textContent).toBe("1 · 1 個被限流");
    expect(rows[15].cells[0].textContent).toBe("其他 5 個");
    expect(screen.getByRole("button", { name: "顯示全部 20 個" })).toBeTruthy();
    expect(screen.getByText("其餘 5 個加總在最後一列。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "顯示全部 20 個" }));
    expect(screen.getByRole("button", { name: "只顯示前 15 個" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "來源" }));
    const sources = await screen.findByRole("table", { name: "依來源分組的請求" });
    expect(within(sources).getAllByRole("row").slice(1).map((r) => (r as HTMLTableRowElement).cells[0].textContent)).toEqual(["API 金鑰", "主控台試用區"]);
  });

  it("FY-WEB-802 a phone: the table's sort control in Chinese", async () => {
    screenWidth(true);
    routes();
    mount();
    const list = await screen.findByRole("list", { name: "依模型分組的請求" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(16);
    const sort = screen.getByLabelText("排序依據") as HTMLSelectElement;
    expect([...sort.options].map((o) => o.textContent)).toEqual(["模型", "請求", "錯誤", "輸入 token", "輸出 token", "平均", "p50", "p95"]);
    expect(within(list).getAllByRole("listitem")[15].textContent).toContain("其他 5 個");
  });

  it("FY-WEB-803 the chart: title, controls, the readout, the legend and its table in Chinese; dates by Intl", async () => {
    screenWidth(false);
    routes({}, [["k1", "laptop (fyk_ab…)", [3, 0, 5, 2]], ["console", "Console playground", [1, 1, 0, 0]], ["other", "Other (2)", [0, 1, 1, 0]]]);
    mount();
    expect(await screen.findByRole("group", { name: /^每天的請求。用左右方向鍵逐天讀取。$/ })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("拆分依據"), { target: { value: "key" } });
    const chart = await screen.findByRole("group", { name: /^每天的請求，依金鑰。用左右方向鍵逐天讀取。$/ });
    expect(chart.getAttribute("aria-roledescription")).toBe("折線圖");
    expect(screen.getByRole("heading", { name: "隨時間變化" })).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "圖表檢視" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["每日", "每週", "每月"]);
    expect([...(screen.getByLabelText("拆分依據") as HTMLSelectElement).options].map((o) => o.textContent)).toEqual(["總計", "金鑰", "模型", "供應商"]);
    expect((screen.getByLabelText("顯示") as HTMLSelectElement).selectedOptions[0].textContent).toBe("請求");
    const legend = screen.getByRole("list", { name: "每天的請求，依金鑰：各條線及其總計" });
    expect(within(legend).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["laptop (fyk_ab…)10", "主控台試用區2", "其他（2）2"]);
    act(() => chart.focus());
    await waitFor(() => expect(chart.textContent).toMatch(/2026年11月23日 ?週一（至今）：laptop \(fyk_ab…\) 2、主控台試用區 0、其他（2） 0；總計 2/));   // ICU versions differ on the space
    expect(chart.querySelectorAll("text.lc-tick")[chart.querySelectorAll("text.lc-tick").length - 1].textContent).toBe("11月23日");
    fireEvent.click(screen.getByText("顯示數字"));
    const table = screen.getByRole("table", { name: "每天的請求，依金鑰" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["日期", "laptop (fyk_ab…)", "主控台試用區", "其他（2）", "總計"]);
    expect(screen.getByText(/^最近 30 天，2026-11-20 至 2026-11-23（UTC）。最新的一天還沒結束（虛線）。 前五大之外的會加總成「其他」。 1 個完全沒有用量的沒有列出。$/)).toBeTruthy();
  });

  it("FY-WEB-804 the Cost tab: totals, the unpriced warning, the cost table and its foot in Chinese", async () => {
    screenWidth(false);
    const total = { ...addUp(MODELS), id: "total", name: "Total", latency_avg_ms: 100, latency_p50_ms: 90, latency_p95_ms: 300, unpriced: 3, cost_estimated: 2,
      cost_micros: 1_500_000, split: { cloud: { ...base, id: "cloud", name: "Cloud", requests: 5, cost_micros: 1_500_000 }, local: { ...base, id: "local", name: "Local", requests: 9, cost_micros: 0 } } };
    routes({ group_by: "day", total, rows: [{ ...base, id: "2026-11-23", name: "2026-11-23", requests: 14, cost_micros: 1_500_000, unpriced: 3 }] });
    mount();
    fireEvent.click(await screen.findByRole("tab", { name: "費用" }));
    const dl = await screen.findByLabelText("費用總計");
    expect([...dl.querySelectorAll("dt")].map((d) => d.textContent)).toEqual(["費用", "雲端", "本機", "由本機服務", "沒有價格"]);
    expect(dl.textContent).toContain("5 個請求");
    expect(screen.getByText(/^3 個雲端請求沒有價格，所以上面的費用沒有算進它們。/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "為供應商的模型設定價格" })).toBeTruthy();
    const table = await screen.findByRole("table", { name: "依日期分組的費用" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["日期", "請求", "Token 數", "費用", "占比", "沒有價格"]);
    expect(screen.getByText(/（UTC），以美元計。.*有 2 個請求是估算的.*這是供應商收費的估算，不是他們的帳單。$/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "費用隨時間變化" })).toBeTruthy();
  });

  it("FY-WEB-805 budgets this month: the lead, the warnings and an admin's budget form in Chinese", async () => {
    screenWidth(false);
    routes();
    mount();
    fireEvent.click(await screen.findByRole("tab", { name: "費用" }));
    const panel = (await screen.findByRole("heading", { name: "本月預算" })).closest("section") as HTMLElement;
    expect(panel.textContent).toContain("每個日曆月（2026-11，UTC）的雲端支出");
    expect(panel.textContent).toContain("管理員在這裡設定使用者的預算");
    expect(within(panel).getByText(/^你的預算：/)).toBeTruthy();
    expect(within(panel).getByText(/^金鑰 laptop 的預算：.+，在 .+ 之前請求都會留在本機。$/)).toBeTruthy();
    expect(within(panel).getByRole("heading", { name: "你" })).toBeTruthy();
    expect(within(panel).getByRole("heading", { name: "金鑰" })).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "變更" }));
    const form = within(panel).getByRole("form", { name: "owner 的預算" });
    expect(within(form).getByLabelText("owner 的每月預算（美元）")).toBeTruthy();
    fireEvent.change(within(form).getByLabelText("owner 的每月預算（美元）"), { target: { value: "lots" } });
    fireEvent.click(within(form).getByRole("button", { name: "儲存" }));
    expect(await within(form).findByText("以美元輸入，例如 25 或 2.50；留空表示沒有預算。")).toBeTruthy();
    expect(within(form).getByRole("button", { name: "取消" })).toBeTruthy();
  });

  it("FY-WEB-806 nothing yet: the empty page in Chinese with its two links", async () => {
    screenWidth(false);
    routes({ rows: [] }, []);
    mount();
    const empty = (await screen.findByText("/v1", { selector: ".usage-empty code" })).closest("p")!;
    expect(empty.textContent).toBe("這段期間沒有請求。這個頁面會計算你的 API 金鑰經由 /v1 的每一次呼叫，以及試用區中的每一則訊息；新的請求幾秒內就會出現。");
    expect(screen.getByRole("link", { name: "建立 API 金鑰" }).getAttribute("href")).toBe("/keys");
    expect(screen.getByRole("link", { name: "開啟試用區" }).getAttribute("href")).toBe("/playground");
    expect(await screen.findByText(/^最近 30 天你的金鑰和試用區沒有請求。 請求經過閘道後，圖表就會填上。$/)).toBeTruthy();
  });
});
