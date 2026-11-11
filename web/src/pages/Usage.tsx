import { useEffect, useState } from "react";
import { Link } from "react-router";

import { Notice, PageHeader } from "../components/ui";
import { api } from "../lib/api";
import { ms, num } from "../lib/gateway";
import { share, usd } from "../lib/prices";
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
  { id: "target", label: "Local or cloud" },
];
const COST_GROUPS: { id: string; label: string; admin?: boolean }[] = [
  { id: "day", label: "Day" },
  { id: "key", label: "Key" },
  { id: "user", label: "Person", admin: true },
  { id: "model", label: "Model" },
  { id: "provider", label: "Provider" },
];

export function UsagePage() {
  const [tab, setTab] = useState<"requests" | "cost">("requests");
  const [days, setDays] = useState(7);
  const [group, setGroup] = useState("model");
  const [costGroup, setCostGroup] = useState("day");
  const [everyone, setEveryone] = useState(false);
  const by = tab === "cost" ? costGroup : group;
  const { data, error, reload } = useApi<UsageResponse>(`/usage?days=${days}&group=${by}${everyone ? "&everyone=true" : ""}`);
  usePoll(reload, 10000);
  const t = data?.total;
  const max = Math.max(1, ...(data?.rows ?? []).map((r) => r.requests));
  const groups = (tab === "cost" ? COST_GROUPS : GROUPS).filter((g) => !g.admin || (data?.is_admin && everyone));
  const setBy = tab === "cost" ? setCostGroup : setGroup;
  // The last step of the users' Get started list is opening this page.
  useEffect(() => {
    api("/onboarding/seen", { method: "POST", body: { step: "usage" } }).catch(() => undefined);
  }, []);

  return (
    <>
      <PageHeader index="06 · Usage" title="Usage"
        lead={data?.everyone ? "Everyone's requests through the gateway and the playground." : "Your requests through the gateway, with your API keys and in the playground."} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="tabs" role="tablist" aria-label="Usage">
        {(["requests", "cost"] as const).map((id) => (
          <button key={id} type="button" role="tab" id={`usage-tab-${id}`} aria-selected={tab === id} aria-controls="usage-panel"
            className={tab === id ? "on" : ""} onClick={() => setTab(id)}>
            {id === "requests" ? "Requests" : "Cost"}
          </button>
        ))}
      </div>
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
            <button key={g.id} type="button" className={by === g.id ? "on" : ""} aria-pressed={by === g.id} onClick={() => setBy(g.id)}>
              {g.label}
            </button>
          ))}
        </div>
        {data?.is_admin && (
          <div className="segmented" role="group" aria-label="Whose usage">
            <button type="button" className={everyone ? "" : "on"} aria-pressed={!everyone} onClick={() => { setEveryone(false); if (group === "user") setGroup("model"); if (costGroup === "user") setCostGroup("day"); }}>Mine</button>
            <button type="button" className={everyone ? "on" : ""} aria-pressed={everyone} onClick={() => setEveryone(true)}>Everyone</button>
          </div>
        )}
      </div>
      <div id="usage-panel" role="tabpanel" aria-labelledby={`usage-tab-${tab}`}>
      {tab === "cost" && data && <CostView data={data} />}
      {tab === "requests" && t && (
        <dl className="fleet usage-totals" aria-label="Totals">
          <div><dt>Requests</dt><dd>{num(t.requests)}</dd></div>
          <div><dt>Errors</dt><dd className={t.errors ? "usage-bad" : ""}>{num(t.errors)}{t.limited > 0 && <span className="muted small"> ({num(t.limited)} limited)</span>}</dd></div>
          <div><dt>Tokens</dt><dd>{num(t.tokens)} <span className="muted small">{num(t.prompt_tokens)} in · {num(t.completion_tokens)} out</span></dd></div>
          <div><dt>Latency p50</dt><dd>{ms(t.latency_p50_ms)}</dd></div>
          <div><dt>Latency p95</dt><dd>{ms(t.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {tab === "requests" && data && (
        data.rows.length === 0 ? (
          <div className="panel usage-empty">
            <p className="muted">
              No requests in this period. This page counts every call through <code>/v1</code> with {data.everyone ? "anyone's" : "your"} API
              keys and every message in the Playground; new ones show up within a few seconds.
            </p>
            <p className="row-gap">
              <Link to="/keys" className="btn">Create an API key</Link>
              <Link to="/playground" className="btn btn-quiet">Open the Playground</Link>
            </p>
          </div>
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
                    <td data-label="Requests" className="num"><span>{num(r.requests)}{r.spilled ? <span className="muted small"> · {num(r.spilled)} spilled</span> : null}</span></td>
                    <td data-label="Errors" className="num"><span>{num(r.errors)}{r.limited > 0 && <span className="muted small"> · {num(r.limited)} limited</span>}</span></td>
                    <td data-label="Tokens in" className="num"><span>{num(r.prompt_tokens)}{r.cached_tokens ? <span className="muted small"> · {num(r.cached_tokens)} cached</span> : null}</span></td>
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
      {tab === "requests" && data && <p className="muted small usage-foot">{data.from === data.to ? data.from : `${data.from} to ${data.to}`} (UTC). Latency is from the gateway: until the last byte of the answer, streamed ones included.</p>}
      </div>
    </>
  );
}

function CostView({ data }: { data: UsageResponse }) {
  const t = data.total;
  const cloud = t.split?.cloud;
  const local = t.split?.local;
  const unpriced = t.unpriced ?? 0;
  const estimated = t.cost_estimated ?? 0;
  const max = Math.max(1, ...data.rows.map((r) => r.cost_micros ?? 0));
  const label = COST_GROUPS.find((g) => g.id === data.group_by)?.label ?? "Name";
  return (
    <>
      <dl className="fleet usage-totals" aria-label="Cost totals">
        <div><dt>Cost</dt><dd>{usd(t.cost_micros)}</dd></div>
        <div><dt>Cloud</dt><dd>{usd(cloud?.cost_micros)} <span className="muted small">{num(cloud?.requests ?? 0)} requests</span></dd></div>
        <div><dt>Local</dt><dd>{usd(local?.cost_micros)} <span className="muted small">{num(local?.requests ?? 0)} requests</span></dd></div>
        <div><dt>Served locally</dt><dd>{share(local?.requests ?? 0, t.requests)} <span className="muted small">of requests</span></dd></div>
        <div><dt>Without a price</dt><dd className={unpriced ? "usage-bad" : ""}>{num(unpriced)} <span className="muted small">requests</span></dd></div>
      </dl>
      {unpriced > 0 && (
        <Notice tone="error">
          {num(unpriced)} cloud request{unpriced === 1 ? " has" : "s have"} no price, so the cost above leaves {unpriced === 1 ? "it" : "them"} out.{" "}
          {data.is_admin ? <Link to="/providers">Set prices on the provider's models</Link> : "An admin sets the prices of cloud models."}
        </Notice>
      )}
      {data.rows.length === 0 ? (
        <div className="panel usage-empty"><p className="muted">No requests in this period. Requests to local replicas cost nothing; cloud requests are priced from their provider model's prices.</p></div>
      ) : (
        <div className="table-wrap">
          <table className="table usage-table cost-table">
            <thead><tr><th>{label}</th><th>Requests</th><th>Tokens</th><th>Cost</th><th>Share</th><th>Without a price</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.id}>
                  <td data-label={label}>
                    <div>
                      <span className={data.group_by === "model" || data.group_by === "day" ? "mono" : ""}>{r.name}</span>
                      {r.cloud !== undefined && <> <span className={`chip ${r.cloud ? "chip-tone-wait" : "chip-tone-ok"}`}>{r.cloud ? "cloud" : "local"}</span></>}
                      <span className="bar usage-bar" aria-hidden="true"><span style={{ width: `${Math.round(((r.cost_micros ?? 0) / max) * 100)}%` }} /></span>
                    </div>
                  </td>
                  <td data-label="Requests" className="num">{num(r.requests)}</td>
                  <td data-label="Tokens" className="num"><span>{num(r.tokens)}{r.cached_tokens ? <span className="muted small"> · {num(r.cached_tokens)} cached</span> : null}</span></td>
                  <td data-label="Cost" className="num strong">{usd(r.cost_micros)}</td>
                  <td data-label="Share" className="num">{share(r.cost_micros ?? 0, t.cost_micros ?? 0)}</td>
                  <td data-label="Unpriced" className="num">{r.unpriced ? <span className="usage-bad">{num(r.unpriced)}</span> : <span className="muted">0</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small usage-foot">
        {data.from === data.to ? data.from : `${data.from} to ${data.to}`} (UTC), in US dollars. Local replicas and providers inside your network cost nothing.
        Each cloud request is priced when it finishes, from its tokens and the model's prices at that time, or from the provider's own figure when it reports one (OpenRouter).
        {estimated > 0 && ` ${num(estimated)} request${estimated === 1 ? " was" : "s were"} estimated: the provider does not report cached tokens (Anthropic), or a cache price was missing.`}
        {" "}An estimate of what the providers charge, not their invoice.
      </p>
    </>
  );
}
