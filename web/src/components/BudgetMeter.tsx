import { LEVEL_LABEL, LEVEL_TONE, percent, spendText, type Budget } from "../lib/budgets";
import { usd } from "../lib/prices";

// Spend this month against a budget: a meter, "$x of $y", and a chip from 80% on.
export function BudgetMeter({ b, label, compact = false }: { b: Budget; label: string; compact?: boolean }) {
  if (b.budget_micros === null) {
    return compact ? <span className="muted">none</span> : <span className="muted small">{spendText(b)}</span>;
  }
  const pct = percent(b);
  const level = b.level ?? "ok";
  return (
    <div className={`budget budget-${level}${compact ? " budget-compact" : ""}`}>
      <div className="budget-line">
        <span className="budget-text">{spendText(b)}</span>
        {level !== "ok" && <span className={`chip chip-tone-${LEVEL_TONE[level]}`}>{LEVEL_LABEL[level]}</span>}
      </div>
      <span className="bar budget-bar" role="meter" aria-label={`${label}: ${spendText(b)}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span style={{ width: `${pct}%` }} />
      </span>
      {b.reserved_micros > 0 && <div className="muted small">{usd(b.reserved_micros)} reserved by requests in flight</div>}
    </div>
  );
}
