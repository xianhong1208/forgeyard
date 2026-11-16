import { describe, expect, it } from "vitest";

import { assignSlots, formatTick, formatValue, isSeries, keepApart, labelIndexes, metricChoices, niceTicks, periodLabel, periodTitle } from "./charts";

describe("usage charts (1.4)", () => {
  it("FY-WEB-233 axis ticks start at 0 in whole steps of 1, 2 or 5; x labels are spread out and end on the latest period", () => {
    expect(niceTicks(0)).toEqual([0, 1]);
    expect(niceTicks(1)).toEqual([0, 1]);
    expect(niceTicks(3)).toEqual([0, 1, 2, 3]);
    expect(niceTicks(7)).toEqual([0, 2, 4, 6, 8]);
    expect(niceTicks(1234)).toEqual([0, 500, 1000, 1500]);
    expect(niceTicks(92_500_000, 4)).toEqual([0, 50_000_000, 100_000_000]);
    for (const max of [1, 9, 13, 77, 1001, 123_456]) {
      const t = niceTicks(max);
      expect(t[t.length - 1]).toBeGreaterThanOrEqual(max);
      expect(t.every((v) => Number.isInteger(v))).toBe(true);
    }
    expect(labelIndexes(30, 6)).toEqual([4, 9, 14, 19, 24, 29]);
    expect(labelIndexes(12, 20)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    expect(labelIndexes(1, 5)).toEqual([0]);
    expect(labelIndexes(0, 5)).toEqual([]);
    expect(labelIndexes(13, 0)).toHaveLength(1);
    // the last label ends at the plot's edge, so a centred one just before it gives way, and so on leftwards
    expect(keepApart([[0, 40], [44, 84], [90, 130], [100, 140]])).toEqual([1, 3]);
    expect(keepApart([[0, 40], [50, 90], [100, 140]])).toEqual([0, 1, 2]);
    expect(keepApart([])).toEqual([]);
  });

  it("FY-WEB-234 periods read as days, weeks from Monday and months, the latest one 'so far'; values in their unit", () => {
    const day = { start: "2026-11-12", end: "2026-11-12", partial: false };
    expect(periodLabel(day, "day")).toBe("Nov 12");
    expect(periodTitle(day, "day")).toBe("Thu, Nov 12, 2026");
    const week = { start: "2026-10-26", end: "2026-11-01", partial: false };
    expect(periodLabel(week, "week")).toBe("Oct 26");
    expect(periodTitle(week, "week")).toBe("Oct 26 – Nov 1, 2026");
    expect(periodTitle({ start: "2026-11-09", end: "2026-11-15", partial: true }, "week")).toBe("Nov 9 – 15, 2026 (so far)");
    const month = { start: "2026-11-01", end: "2026-11-15", partial: true };
    expect(periodLabel(month, "month")).toBe("Nov 2026");
    expect(periodTitle(month, "month")).toBe("November 2026 (so far)");
    expect(formatTick(1500, "count")).toBe("1.5K");
    expect(formatTick(250_000, "usd")).toBe("$0.25");
    expect(formatTick(1_200, "usd")).toBe("$0.0012");
    expect(formatTick(12_000_000, "usd")).toBe("$12");
    expect(formatTick(0, "usd")).toBe("$0");
    expect(formatTick(90_000, "duration")).toBe("1.5 min");
    expect(formatValue(1_850, "usd")).toBe("$0.00185");
    expect(formatValue(12345, "count")).toBe("12,345");
    expect(formatValue(7_200_000, "duration")).toBe("2 h");
    expect(formatValue(3 * 1024 * 1024, "bytes")).toBe("3 MiB");
  });

  it("FY-WEB-235 a line keeps its colour when the lines are re-ranked; a new one takes a free colour; units show once counted", () => {
    const kept = new Map<string, number>();
    const first = assignSlots(["a", "b", "c"], kept);
    expect([first.get("a"), first.get("b"), first.get("c")]).toEqual([0, 1, 2]);
    const again = assignSlots(["c", "d", "a"], kept);           // b left, d is new, c and a re-ranked
    expect([again.get("c"), again.get("d"), again.get("a")]).toEqual([2, 1, 0]);
    expect(kept.has("b")).toBe(false);
    const ids = metricChoices({ requests: 4, images: 2, characters: 0 }, "requests").map((m) => m.id);
    expect(ids).toContain("images");
    expect(ids).not.toContain("characters");
    expect(ids).not.toContain("cost_micros");
    expect(metricChoices(undefined, "busy_ms").map((m) => m.id)).toContain("busy_ms");   // the one chosen stays
    expect(isSeries({ periods: [], series: [], total: [] })).toBe(true);
    expect(isSeries({ rows: [], total: {} })).toBe(false);
    expect(isSeries(null)).toBe(false);
  });
});
