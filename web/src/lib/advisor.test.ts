import { describe, expect, it } from "vitest";

import { asReport, forPage, type Finding } from "./advisor";

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
