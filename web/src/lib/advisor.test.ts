import { describe, expect, it } from "vitest";

import { interpolate } from "../i18n/core";
import { asReport, findingDetail, forPage, type Finding } from "./advisor";

const REPORT = { findings: [], snoozed: [], counts: { error: 1, warning: 0, info: 2 }, badge: 1, snooze_days: 30, checked_at: "2026-11-11T00:00:00Z" };

describe("setup health report", () => {
  it("FY-WEB-147 only a report shaped like the advisor API's reaches the badge and the panels", () => {
    expect(asReport(REPORT)).toEqual(REPORT);
    expect(asReport({ ...REPORT, snoozed: undefined })?.snoozed).toEqual([]);
    for (const bad of [null, undefined, "x", {}, { findings: [] }, { ...REPORT, counts: undefined },
      { ...REPORT, counts: { error: 1 } }, { ...REPORT, badge: "1" }, { ...REPORT, findings: {} }]) {
      expect(asReport(bad)).toBeNull();
    }
  });

  it("FY-WEB-231 a machine's page shows the findings about that machine and its cards only (1.4)", () => {
    const f = (key: string, scope: string, pages: string[]): Finding => ({ id: key, key, severity: "warning", title: key, detail: "",
      fix_url: "", fix_label: "", scope, pages, snoozable: true });
    const all = [f("a", "machine:3", ["machines"]), f("b", "machine:31", ["machines"]), f("c", "gpu:3:4", ["models", "machines"]),
      f("d", "gpu:31:0", ["machines"]), f("e", "server", ["keys"])];
    expect(forPage(all, ["machine:3"]).map((x) => x.key)).toEqual(["a", "c"]);
    expect(forPage(all, ["machines"]).map((x) => x.key)).toEqual(["a", "b", "c", "d"]);
    expect(forPage(all, ["keys"]).map((x) => x.key)).toEqual(["e"]);
  });
});

describe("Chinese spacing next to names (1.6.0, #39)", () => {
  const detail = (id: string, facts: Record<string, unknown>) => findingDetail({ id, detail: "server words", facts }, "zh-TW").detail;

  it("FY-WEB-1029 the people findings put a space between a name and Chinese, none after 「和另外 N 人」", () => {
    expect(detail("registrations-pending", { people: ["bob"] })).toBe("bob 申請了帳號。請在「使用者」頁面核准或移除。");
    expect(detail("registrations-pending", { people: ["ann", "bob", "cy", "dee", "eve"] }))
      .toBe("ann、bob、cy 和另外 2 人申請了帳號。請在「使用者」頁面核准或移除。");
    expect(detail("admins-without-mfa", { admins: ["owner"], required: false }))
      .toBe("owner 只用密碼登入。管理員帳號能變更這裡的所有設定：請到「我的帳號」加入驗證器 App（每位管理員各自替自己的帳號加入）。");
    expect(detail("admins-without-mfa", { admins: ["ann", "bob", "cy", "dee"], required: false }))
      .toBe("ann、bob、cy 和另外 1 人只用密碼登入。管理員帳號能變更這裡的所有設定：請到「我的帳號」加入驗證器 App（每位管理員各自替自己的帳號加入）。");
    expect(detail("mfa-setup-pending", { waiting: [], overdue: ["ann", "bob", "cy", "dee", "eve"] }))
      .toMatch(/^5 人已經逾期（ann、bob、cy 和另外 2 人）：/);
    // English as written.
    expect(findingDetail({ id: "registrations-pending", detail: "x", facts: { people: ["bob"] } }, "en").detail).toBe("x");
  });

  it("FY-WEB-1030 interpolate keeps a space only between Latin or digits and Chinese, in Traditional Chinese only", () => {
    expect(interpolate("{a} 申請", { a: "bob" }, "zh-TW")).toBe("bob 申請");
    expect(interpolate("{a} 申請", { a: "2 人" }, "zh-TW")).toBe("2 人申請");
    expect(interpolate("{a} 申請", { a: "小明、" }, "zh-TW")).toBe("小明、申請");
    expect(interpolate("授權是 {a}。", { a: "未註明" }, "zh-TW")).toBe("授權是未註明。");
    expect(interpolate("授權是 {a}。", { a: "MIT" }, "zh-TW")).toBe("授權是 MIT。");
    expect(interpolate("在 {a} 之前", { a: 3 }, "zh-TW")).toBe("在 3 之前");
    expect(interpolate("{a} {b}", { a: "小明", b: "小華" }, "zh-TW")).toBe("小明小華");
    expect(interpolate("{a} {b}", { a: "小明", b: "bob" }, "zh-TW")).toBe("小明 bob");
    expect(interpolate("{a} 申請", { a: "小明" }, "en")).toBe("小明 申請");
    expect(interpolate("{missing} 申請", {}, "zh-TW")).toBe("{missing} 申請");
  });
});
