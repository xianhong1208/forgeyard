import { useState } from "react";

import { Notice, PageHeader } from "../components/ui";
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

const FILTERS = [
  { value: "", label: "Everything" },
  { value: "auth.", label: "Sign-ins and sessions" },
  { value: "user.", label: "User changes" },
  { value: "setup.", label: "Setup" },
];

function detailText(d: Record<string, unknown>): string {
  return Object.entries(d)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(" → ") : String(v)}`)
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
            <tr><th>When</th><th>Event</th><th>Who</th><th>Target</th><th>From</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td data-label="When" title={exact(e.at)}>{when(e.at)}</td>
                <td data-label="Event">
                  <span className={`action action-${e.action.split(".")[0]}${/failed|rejected/.test(e.action) ? " action-bad" : ""}`}>{e.action}</span>
                </td>
                <td data-label="Who" className="mono">{e.actor ?? "—"}</td>
                <td data-label="Target" className="mono">{e.target ?? "—"}</td>
                <td data-label="From" className="mono">{e.ip ?? "—"}</td>
                <td data-label="Detail" className="small muted">{detailText(e.detail) || "—"}</td>
              </tr>
            ))}
            {data && events.length === 0 && (
              <tr><td colSpan={6} className="muted">No events.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {next && (
        <div className="load-more">
          <button type="button" className="btn" disabled={loading} onClick={() => void loadMore()}>Load older events</button>
        </div>
      )}
    </>
  );
}

export function AuditPage() {
  const [filter, setFilter] = useState("");
  return (
    <>
      <PageHeader
        index="04 · Audit log"
        title="Audit log"
        lead="Sign-ins, failed sign-ins and every change to accounts, newest first."
        actions={
          <div className="field field-inline">
            <label htmlFor="audit-filter">Show</label>
            <select id="audit-filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
              {FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </div>
        }
      />
      <EventTable key={filter} filter={filter} />
    </>
  );
}
