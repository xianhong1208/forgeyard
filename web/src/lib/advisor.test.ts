import { describe, expect, it } from "vitest";

import { asReport } from "./advisor";

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
});
