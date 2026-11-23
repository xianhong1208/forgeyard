import { IconArrowDown, IconArrowUp, IconChevronDown } from "@tabler/icons-react";
import { useId, useState, type ReactNode } from "react";

import { useT } from "../i18n/react";
import { useNarrow } from "../lib/useNarrow";

// The breakdown tables of Usage (1.4): one row per model, key, day, source, person or provider.
// - On every width: the top `limit` rows by the chosen column, the rest added up in one "N others" row, and
//   "Show all N". A column header sorts by that column (again: the other way round).
// - On a narrow screen: one line per row (the name, cut short with the full name on hover, and the chosen
//   number); the other columns open under it, from a button, instead of every column in the row.

export type BreakdownColumn<R> = {
  id: string;
  label: string;
  // What the column sorts by; without it the column does not sort. null sorts last both ways.
  value?: (r: R) => number | null | undefined;
  cell: (r: R) => ReactNode;
  className?: string;
  title?: string;
  dataLabel?: string;
};

export type BreakdownRow = { id: string; name: string };
export type Sort = { by: string; desc: boolean };

export const TOP_ROWS = 15;
export const NAME = "name";

type Props<R extends BreakdownRow> = {
  label: string;                 // the table's accessible name
  nameLabel: string;             // the first column's heading: Model, Key, Day, …
  rows: R[];
  columns: BreakdownColumn<R>[];
  metric: string;                // the column the rows are ranked by first, and the bar's
  sort?: Sort;                   // the first order, if not by `metric`, most first (Day: the latest first)
  others: (hidden: R[]) => R;    // the rows past the top ones, added up as one
  nameClass?: string;
  nameExtra?: (r: R) => ReactNode;
  className?: string;
  limit?: number;
};

export function sortRows<R extends BreakdownRow>(rows: R[], columns: BreakdownColumn<R>[], sort: Sort): R[] {
  const col = columns.find((c) => c.id === sort.by && c.value);
  const sign = sort.desc ? -1 : 1;
  const byName = (a: R, b: R) => a.name.localeCompare(b.name, "en", { numeric: true });
  return [...rows].sort((a, b) => {
    if (!col) return sign * byName(a, b);
    const x = col.value!(a), y = col.value!(b);
    const xn = x === null || x === undefined, yn = y === null || y === undefined;
    if (xn || yn) return xn && yn ? byName(a, b) : xn ? 1 : -1;
    return x === y ? byName(a, b) : sign * (x - y);
  });
}

// The top rows and the rest (none when there are no more than `limit`); all of them while `all`.
export function cutRows<R>(rows: R[], limit: number, all: boolean): { shown: R[]; hidden: R[] } {
  if (all) return { shown: rows, hidden: [] };
  return { shown: rows.slice(0, limit), hidden: rows.slice(limit) };
}

export function BreakdownTable<R extends BreakdownRow>({ label, nameLabel, rows, columns, metric, sort: first, others, nameClass, nameExtra, className, limit = TOP_ROWS }: Props<R>) {
  const t = useT();
  const narrow = useNarrow();
  const uid = useId();
  const [sort, setSort] = useState<Sort>(first ?? { by: metric, desc: true });
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const sorted = sortRows(rows, columns, sort);
  const { shown, hidden } = cutRows(sorted, limit, all);
  const rest = hidden.length ? others(hidden) : null;
  // The bar and the number on a narrow line follow the chosen column when it is a number, else `metric`.
  const lead = columns.find((c) => c.id === sort.by && c.value) ?? columns.find((c) => c.id === metric)!;
  const max = Math.max(1, ...rows.map((r) => lead.value?.(r) ?? 0));
  const bar = (r: R) => (
    <span className="bar usage-bar" aria-hidden="true"><span style={{ width: `${Math.round(((lead.value?.(r) ?? 0) / max) * 100)}%` }} /></span>
  );
  // The first order of a column: names A to Z (days: the latest first), numbers the most first.
  const firstOrder = (by: string): Sort => (by === NAME ? { by, desc: first?.by === NAME ? first.desc : false } : { by, desc: true });
  const sortBy = (by: string) => setSort((s) => (s.by === by ? { by, desc: !s.desc } : firstOrder(by)));
  const toggle = (id: string) => setOpen((o) => {
    const next = new Set(o);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const more = rows.length > limit && (
    <p className="usage-more">
      <button type="button" className="btn btn-quiet btn-small" aria-expanded={all} onClick={() => setAll((v) => !v)}>
        {all ? t("usage.table.top", { count: String(limit) }) : t("usage.table.all", { count: String(rows.length) })}
      </button>
      {!all && <span className="muted small">{t("usage.table.rest", { count: String(hidden.length) })}</span>}
    </p>
  );
  const othersName = (n: number) => t.plural("usage.table.others", n);

  if (narrow) {
    const sortable = columns.filter((c) => c.value);
    const item = (r: R, rest: boolean) => {
      const id = `${uid}-${rest ? "others" : r.id}`;
      const isOpen = open.has(rest ? "\0others" : r.id);
      return (
        <li key={rest ? "\0others" : r.id} className={`usage-item${isOpen ? " open" : ""}${rest ? " usage-others" : ""}`}>
          <button type="button" className="usage-item-head" aria-expanded={isOpen} aria-controls={id} onClick={() => toggle(rest ? "\0others" : r.id)}>
            <span className={`usage-item-name${rest ? " muted" : nameClass ? ` ${nameClass}` : ""}`} title={r.name}>{r.name}</span>
            {!rest && nameExtra?.(r)}
            <span className="usage-item-value">{lead.cell(r)}</span>
            <IconChevronDown className="usage-item-chev" size={16} aria-hidden="true" />
            {!rest && bar(r)}
          </button>
          {isOpen && (
            <dl className="usage-item-more" id={id}>
              <div className="usage-item-full"><dt>{nameLabel}</dt><dd className={rest ? "" : nameClass}>{r.name}</dd></div>
              {columns.filter((c) => c !== lead).map((c) => (
                <div key={c.id}><dt>{c.label}</dt><dd>{c.cell(r)}</dd></div>
              ))}
            </dl>
          )}
        </li>
      );
    };
    return (
      <div className={`usage-compact ${className ?? ""}`}>
        <div className="usage-sort">
          <label className="field-inline small">{t("usage.table.sortBy")}
            <select aria-label={t("usage.table.sortBy")} value={sort.by} onChange={(e) => setSort(firstOrder(e.target.value))}>
              <option value={NAME}>{nameLabel}</option>
              {sortable.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </label>
          {lead.id !== sort.by && <span className="muted small">{lead.label}</span>}
        </div>
        <ul className="usage-list" aria-label={label}>
          {shown.map((r) => item(r, false))}
          {rest && item({ ...rest, name: othersName(hidden.length) }, true)}
        </ul>
        {more}
      </div>
    );
  }

  const head = (id: string, text: string) => {
    const on = sort.by === id;
    const Arrow = sort.desc ? IconArrowDown : IconArrowUp;
    return (
      <th key={id} scope="col" aria-sort={on ? (sort.desc ? "descending" : "ascending") : "none"}>
        <button type="button" className={`th-sort${on ? " on" : ""}`} onClick={() => sortBy(id)}>
          {text}{on && <Arrow size={12} stroke={2} aria-hidden="true" />}
        </button>
      </th>
    );
  };
  return (
    <>
      <div className="table-wrap usage-wrap">
        <table className={`table usage-table ${className ?? ""}`} aria-label={label}>
          <thead>
            <tr>
              {head(NAME, nameLabel)}
              {columns.map((c) => (c.value ? head(c.id, c.label) : <th key={c.id} scope="col">{c.label}</th>))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id}>
                <td data-label={nameLabel}>
                  <div>
                    <span className={`usage-name${nameClass ? ` ${nameClass}` : ""}`} title={r.name}>{r.name}</span>
                    {nameExtra && <> {nameExtra(r)}</>}
                  </div>
                  {bar(r)}
                </td>
                {columns.map((c) => <td key={c.id} data-label={c.dataLabel ?? c.label} className={c.className} title={c.title}>{c.cell(r)}</td>)}
              </tr>
            ))}
            {rest && (
              <tr className="usage-others">
                <td data-label={nameLabel}><span className="muted">{othersName(hidden.length)}</span></td>
                {columns.map((c) => <td key={c.id} data-label={c.dataLabel ?? c.label} className={c.className}>{c.cell(rest)}</td>)}
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {more}
    </>
  );
}
