import { useState } from "react";

import { Notice, PageHeader } from "../components/ui";
import { ms, num } from "../lib/gateway";
import type { UsageResponse, UsageRow } from "../lib/types";
import { useApi } from "../lib/useApi";
import { usePoll } from "./Workloads";

const DAYS = [1, 7, 30];
const GROUPS: { id: string; label: string; admin?: boolean }[] = [
  { id: "model", label: "Model" },
  { id: "key", label: "Key" },
  { id: "user", label: "Person", admin: true },
  { id: "day", label: "Day" },
  { id: "source", label: "Source" },
];

export function UsagePage() {
  const [days, setDays] = useState(7);
  const [group, setGroup] = useState("model");
  const [everyone, setEveryone] = useState(false);
  const { data, error, reload } = useApi<UsageResponse>(`/usage?days=${days}&group=${group}${everyone ? "&everyone=true" : ""}`);
  usePoll(reload, 10000);
  const t = data?.total;
  const max = Math.max(1, ...(data?.rows ?? []).map((r) => r.requests));
  const groups = GROUPS.filter((g) => !g.admin || (data?.is_admin && everyone));

  return (
    <>
      <PageHeader index="06 · Usage" title="Usage"
        lead={data?.everyone ? "Everyone's requests through the gateway and the playground." : "Your requests through the gateway, with your API keys and in the playground."} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="usage-controls">
        <div className="segmented" role="group" aria-label="Period">
          {DAYS.map((d) => (
            <button key={d} type="button" className={days === d ? "on" : ""} aria-pressed={days === d} onClick={() => setDays(d)}>
              {d === 1 ? "Today" : `${d} days`}
            </button>
          ))}
        </div>
        <div className="segmented" role="group" aria-label="Group by">
          {groups.map((g) => (
            <button key={g.id} type="button" className={group === g.id ? "on" : ""} aria-pressed={group === g.id} onClick={() => setGroup(g.id)}>
              {g.label}
            </button>
          ))}
        </div>
        {data?.is_admin && (
          <div className="segmented" role="group" aria-label="Whose usage">
            <button type="button" className={everyone ? "" : "on"} aria-pressed={!everyone} onClick={() => { setEveryone(false); if (group === "user") setGroup("model"); }}>Mine</button>
            <button type="button" className={everyone ? "on" : ""} aria-pressed={everyone} onClick={() => setEveryone(true)}>Everyone</button>
          </div>
        )}
      </div>
      {t && (
        <dl className="fleet usage-totals" aria-label="Totals">
          <div><dt>Requests</dt><dd>{num(t.requests)}</dd></div>
          <div><dt>Errors</dt><dd className={t.errors ? "usage-bad" : ""}>{num(t.errors)}{t.limited > 0 && <span className="muted small"> ({num(t.limited)} limited)</span>}</dd></div>
          <div><dt>Tokens</dt><dd>{num(t.tokens)} <span className="muted small">{num(t.prompt_tokens)} in · {num(t.completion_tokens)} out</span></dd></div>
          <div><dt>Latency p50</dt><dd>{ms(t.latency_p50_ms)}</dd></div>
          <div><dt>Latency p95</dt><dd>{ms(t.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {data && (
        data.rows.length === 0 ? (
          <div className="panel"><p className="muted" style={{ margin: 0 }}>Nothing in this period yet. Requests show up here within a few seconds.</p></div>
        ) : (
          <div className="table-wrap">
            <table className="table usage-table">
              <thead>
                <tr><th>{GROUPS.find((g) => g.id === data.group_by)?.label ?? "Name"}</th><th>Requests</th><th>Errors</th><th>Tokens in</th><th>Tokens out</th><th>Avg</th><th>p50</th><th>p95</th></tr>
              </thead>
              <tbody>
                {data.rows.map((r: UsageRow) => (
                  <tr key={r.id}>
                    <td data-label="Name">
                      <div className={data.group_by === "model" || data.group_by === "day" ? "mono" : ""}>{r.name}</div>
                      <span className="bar usage-bar" aria-hidden="true"><span style={{ width: `${Math.round((r.requests / max) * 100)}%` }} /></span>
                    </td>
                    <td data-label="Requests" className="num">{num(r.requests)}</td>
                    <td data-label="Errors" className="num">{num(r.errors)}{r.limited > 0 && <span className="muted small"> · {num(r.limited)} limited</span>}</td>
                    <td data-label="Tokens in" className="num">{num(r.prompt_tokens)}</td>
                    <td data-label="Tokens out" className="num">{num(r.completion_tokens)}</td>
                    <td data-label="Avg" className="num">{ms(r.latency_avg_ms)}</td>
                    <td data-label="p50" className="num">{ms(r.latency_p50_ms)}</td>
                    <td data-label="p95" className="num">{ms(r.latency_p95_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
      {data && <p className="muted small usage-foot">{data.from === data.to ? data.from : `${data.from} to ${data.to}`} (UTC). Latency is from the gateway: until the last byte of the answer, streamed ones included. Counts only, no billing.</p>}
    </>
  );
}
