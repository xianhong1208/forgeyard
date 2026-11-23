import { useState } from "react";

import { Notice, PageHeader } from "../components/ui";
import type { Key } from "../i18n/core";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { useApi } from "../lib/useApi";

type Event = {
  id: number;
  at: string;
  action: string;
  actor: string | null;
  target: string | null;
  ip: string | null;
  detail: Record<string, unknown>;
};
type Page = { events: Event[]; next_before: number | null };

const FILTERS: { value: string; label: Key }[] = [
  { value: "", label: "admin.audit.filter.all" },
  { value: "auth.", label: "admin.audit.filter.auth" },
  { value: "user.", label: "admin.audit.filter.user" },
  { value: "setup.", label: "admin.audit.filter.setup" },
  { value: "api_key", label: "admin.audit.filter.api_key" },
  { value: "machine.", label: "admin.audit.filter.machine" },
  { value: "workload.", label: "admin.audit.filter.workload" },
  { value: "deployment.", label: "admin.audit.filter.deployment" },
  { value: "model", label: "admin.audit.filter.model" },
  { value: "recipe.", label: "admin.audit.filter.recipe" },
  { value: "route.", label: "admin.audit.filter.route" },
  { value: "provider.", label: "admin.audit.filter.provider" },
  { value: "budget.", label: "admin.audit.filter.budget" },
];

// An event's action: English shows the code as recorded (admins search by it); another language names it
// (admin.action.<code>) with the code next to it. A code without a name is shown as recorded.
export function ActionName({ action }: { action: string }) {
  const t = useT();
  const cls = `action action-${action.split(".")[0]}${/failed|rejected/.test(action) ? " action-bad" : ""}`;
  const label = t.maybe(`admin.action.${action}`, action);
  if (t.lang === "en" || label === action) return <span className={cls}>{action}</span>;
  return (
    <span title={t("admin.audit.code", { code: action })}>
      <span className={cls}>{label}</span> <code className="small muted">{action}</code>
    </span>
  );
}

// The details as recorded. A nested value (a change's before and after) is shown as JSON, not "[object Object]".
export function detailText(d: Record<string, unknown>): string {
  const one = (v: unknown) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v));
  return Object.entries(d)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.map(one).join(" → ") : one(v)}`)
    .join(" · ");
}

function query(action: string, before?: number): string {
  const qs = new URLSearchParams({ limit: "50" });
  if (action) qs.set("action", action);
  if (before) qs.set("before", String(before));
  return `/audit?${qs}`;
}

// Keyed by the filter, so changing it starts from a fresh first page.
function EventTable({ filter }: { filter: string }) {
  const t = useT();
  const { data, error } = useApi<Page>(query(filter));
  const [older, setOlder] = useState<Page[]>([]);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const events = [...(data?.events ?? []), ...older.flatMap((p) => p.events)];
  const next = older.length ? older[older.length - 1].next_before : data?.next_before ?? null;

  const loadMore = async () => {
    if (!next) return;
    setLoading(true);
    try {
      const page = await api<Page>(query(filter, next));
      setOlder((o) => [...o, page]);
      setMoreError(null);
    } catch (e) {
      setMoreError(errorText(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      {(error || moreError) && <Notice tone="error">{error || moreError}</Notice>}
      <div className="table-wrap">
        <table className="table audit-table">
          <thead>
            <tr>
              <th>{t("admin.audit.col.when")}</th><th>{t("admin.audit.col.event")}</th><th>{t("admin.audit.col.who")}</th>
              <th>{t("admin.audit.col.target")}</th><th>{t("admin.audit.col.from")}</th><th>{t("admin.audit.col.detail")}</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td data-label={t("admin.audit.col.when")} className="nowrap" title={exact(e.at)}>{when(e.at)}</td>
                <td data-label={t("admin.audit.col.event")}>
                  <ActionName action={e.action} />
                </td>
                <td data-label={t("admin.audit.col.who")} className="mono">{e.actor ?? "—"}</td>
                <td data-label={t("admin.audit.col.target")} className="mono">{e.target ?? "—"}</td>
                <td data-label={t("admin.audit.col.from")} className="mono">{e.ip ?? "—"}</td>
                <td data-label={t("admin.audit.col.detail")} className="small muted">{detailText(e.detail) || "—"}</td>
              </tr>
            ))}
            {data && events.length === 0 && (
              <tr><td colSpan={6} className="muted">{t("admin.audit.none")}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {next && (
        <div className="load-more">
          <button type="button" className="btn" disabled={loading} onClick={() => void loadMore()}>{t("admin.audit.older")}</button>
        </div>
      )}
    </>
  );
}

export function AuditPage() {
  const t = useT();
  const [filter, setFilter] = useState("");
  return (
    <>
      <PageHeader
        index={t("admin.audit.index")}
        title={t("admin.audit.title")}
        lead={t("admin.audit.lead")}
        actions={
          <div className="field field-inline">
            <label htmlFor="audit-filter">{t("admin.audit.show")}</label>
            <select id="audit-filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
              {FILTERS.map((f) => <option key={f.value} value={f.value}>{t(f.label as "admin.audit.filter.all")}</option>)}
            </select>
          </div>
        }
      />
      <EventTable key={filter} filter={filter} />
    </>
  );
}
