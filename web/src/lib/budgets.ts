// Monthly cloud budgets (1.2): per API key, person and provider, in micro-USD per UTC calendar month.

import { parsePrice, priceText, usd } from "./prices";

export type BudgetLevel = "ok" | "warning" | "exhausted";

// A budget with this month's spend; budget_micros null: no budget.
export type Budget = {
  budget_micros: number | null; spent_micros: number; reserved_micros: number; used: number | null; level: BudgetLevel | null;
};

export type BudgetKey = Budget & {
  id: string; name: string; prefix: string; status: string; user: { id: number; username: string | null }; locked_by: string | null;
};
export type BudgetPerson = Budget & { id: number; username: string; status: string };
export type BudgetProvider = Budget & { id: number; name: string; kind: string; enabled: boolean };

export type BudgetsResponse = {
  month: string; resets_at: string; is_admin: boolean; everyone: boolean;
  keys: BudgetKey[]; people: BudgetPerson[]; providers: BudgetProvider[];
};

export const LEVEL_LABEL: Record<BudgetLevel, string> = { ok: "within budget", warning: "past 80%", exhausted: "used up" };
export const LEVEL_TONE: Record<BudgetLevel, string> = { ok: "ok", warning: "run", exhausted: "bad" };

// How full the meter is, 0..100.
export function percent(b: Budget): number {
  if (b.budget_micros === null) return 0;
  if (b.budget_micros === 0) return 100;
  return Math.max(0, Math.min(100, Math.round((b.spent_micros / b.budget_micros) * 100)));
}

// "$1.20 of $5.00" (or "$1.20 this month, no budget").
export function spendText(b: Budget): string {
  return b.budget_micros === null ? `${usd(b.spent_micros)} this month, no budget` : `${usd(b.spent_micros)} of ${usd(b.budget_micros)}`;
}

// "December 1" in UTC, when this month's budgets start again.
export function resetText(resetsAt: string | undefined): string {
  if (!resetsAt) return "the first of next month";
  const d = new Date(resetsAt);
  return `${d.toLocaleDateString("en-US", { month: "long", day: "numeric", timeZone: "UTC" })}, 00:00 UTC`;
}

// What someone typed (USD, up to 6 decimals) to micro-USD; "" is no budget.
export function parseBudget(text: string): number | null | "bad" {
  return parsePrice(text);
}

export function budgetInput(micros: number | null | undefined): string {
  return priceText(micros);
}

// Budgets past 80% or used up, for a warning at the top of a page.
export function alerting<T extends Budget>(items: T[]): T[] {
  return items.filter((b) => b.level === "warning" || b.level === "exhausted");
}
