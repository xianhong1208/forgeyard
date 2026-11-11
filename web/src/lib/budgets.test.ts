import { describe, expect, it } from "vitest";

import { alerting, parseBudget, percent, resetText, spendText, type Budget } from "./budgets";

const b = (budget: number | null, spent: number, level: Budget["level"] = "ok"): Budget =>
  ({ budget_micros: budget, spent_micros: spent, reserved_micros: 0, used: null, level });

describe("budgets", () => {
  it("fills the meter by spend, never past 100%, and a $0 budget is full", () => {
    expect(percent(b(5_000_000, 1_000_000))).toBe(20);
    expect(percent(b(5_000_000, 9_000_000))).toBe(100);
    expect(percent(b(0, 0))).toBe(100);
    expect(percent(b(null, 3))).toBe(0);
  });

  it("says spend against the budget in dollars", () => {
    expect(spendText(b(5_000_000, 1_200_000))).toBe("$1.20 of $5.00");
    expect(spendText(b(null, 185))).toBe("$0.000185 this month, no budget");
  });

  it("reads dollars typed by hand, empty is no budget", () => {
    expect(parseBudget("25")).toBe(25_000_000);
    expect(parseBudget("$0.5")).toBe(500_000);
    expect(parseBudget("")).toBeNull();
    expect(parseBudget("-3")).toBe("bad");
  });

  it("names the reset day in UTC and picks the budgets to warn about", () => {
    expect(resetText("2026-12-01T00:00:00Z")).toBe("December 1, 00:00 UTC");
    expect(alerting([b(1, 0, "ok"), b(1, 1, "exhausted"), b(10, 9, "warning"), b(null, 0, null)]).length).toBe(2);
  });
});
