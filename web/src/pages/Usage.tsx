import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { BudgetMeter } from "../components/BudgetMeter";
import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { alerting, budgetInput, LEVEL_LABEL, parseBudget, resetText, type BudgetPerson, type BudgetsResponse } from "../lib/budgets";
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
  // 1.3: the media column shows once there are images, audio or video in the period.
  const media = (t?.media_items ?? 0) > 0;
  // Text to speech: characters spoken and the audio's length.
  const speech = (t?.characters ?? 0) > 0;
  // Images: how many, their megapixels and the time replicas spent making them (GPU time on a GPU).
  const images = (t?.images ?? 0) > 0;
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
      {tab === "cost" && <Budgets everyone={everyone} />}
      {tab === "cost" && data && <CostView data={data} />}
      {tab === "requests" && t && (
        <dl className="fleet usage-totals" aria-label="Totals">
          <div><dt>Requests</dt><dd>{num(t.requests)}</dd></div>
          <div><dt>Errors</dt><dd className={t.errors ? "usage-bad" : ""}>{num(t.errors)}{t.limited > 0 && <span className="muted small"> ({num(t.limited)} limited)</span>}</dd></div>
          <div><dt>Tokens</dt><dd>{num(t.tokens)} <span className="muted small">{num(t.prompt_tokens)} in · {num(t.completion_tokens)} out</span></dd></div>
          {media && <div><dt>Media items</dt><dd>{num(t.media_items)} <span className="muted small">images, audio, video</span></dd></div>}
          {speech && <div><dt>Speech</dt><dd>{num(t.characters)} <span className="muted small">characters · {audioLength(t.media_ms)} of audio</span></dd></div>}
          {images && <div><dt>Images</dt><dd>{num(t.images)} <span className="muted small">{megapixels(t.kilopixels)} · {busyTime(t.busy_ms)} busy</span></dd></div>}
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
                <tr><th>{GROUPS.find((g) => g.id === data.group_by)?.label ?? "Name"}</th><th>Requests</th><th>Errors</th><th>Tokens in</th><th>Tokens out</th>{media && <th>Media</th>}{speech && <th>Characters</th>}{speech && <th>Audio</th>}{images && <th>Images</th>}{images && <th>Busy</th>}<th>Avg</th><th>p50</th><th>p95</th></tr>
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
                    {media && <td data-label="Media" className="num" title="Images, audio clips and videos sent in chat">{num(r.media_items ?? 0)}</td>}
                    {speech && <td data-label="Characters" className="num" title="Characters turned into speech">{num(r.characters ?? 0)}</td>}
                    {speech && <td data-label="Audio" className="num" title="Length of the speech made (where its format says it)">{audioLength(r.media_ms)}</td>}
                    {images && <td data-label="Images" className="num" title="Images made, and their megapixels">{num(r.images ?? 0)}{r.images ? <span className="muted small"> · {megapixels(r.kilopixels)}</span> : null}</td>}
                    {images && <td data-label="Busy" className="num" title="Time replicas worked on these images, times the GPUs each holds">{busyTime(r.busy_ms)}</td>}
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

// This month's budgets: the person's own and their keys' (admins with Everyone: every person, key with a budget
// or spend, and provider), with warnings from 80% on. Admins set people's budgets here.
function Budgets({ everyone }: { everyone: boolean }) {
  const { data, error, reload } = useApi<BudgetsResponse>(`/budgets${everyone ? "?everyone=true" : ""}`);
  usePoll(reload, 10000);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return null;
  const people = data.people.filter((p) => p.budget_micros !== null || (data.everyone && p.spent_micros > 0) || data.is_admin);
  const keys = data.keys.filter((k) => k.budget_micros !== null);
  const providers = data.providers;
  const warn = [
    ...alerting(data.people).map((p) => ({ id: `p${p.id}`, text: data.everyone ? `${p.username}'s budget` : "Your budget", level: p.level })),
    ...alerting(keys).map((k) => ({ id: `k${k.id}`, text: `The budget of the key ${k.name}${data.everyone && k.user.username ? ` (${k.user.username})` : ""}`, level: k.level })),
    ...alerting(providers).map((p) => ({ id: `v${p.id}`, text: `The budget of the provider ${p.name}`, level: p.level })),
  ];
  const reset = resetText(data.resets_at);
  const any = people.some((p) => p.budget_micros !== null) || keys.length > 0 || providers.some((p) => p.budget_micros !== null);
  return (
    <section className="panel budget-panel" aria-labelledby="budgets-title">
      <h2 id="budgets-title">Budgets this month</h2>
      <p className="muted small">
        Cloud spend per calendar month ({data.month}, UTC), reserved before each request is sent. When a budget is used up, requests it covers stay
        local; ones only the cloud can serve are refused until {reset}. {data.is_admin ? "Admins set budgets for people here, for keys on API keys, and for providers on their page."
          : <>You set budgets for your keys on <Link to="/keys">API keys</Link>.</>}
      </p>
      {warn.map((w) => (
        <Notice key={w.id} tone={w.level === "exhausted" ? "error" : "info"}>{w.text} is {LEVEL_LABEL[w.level!]}{w.level === "exhausted" ? `: it keeps requests local until ${reset}.` : "."}</Notice>
      ))}
      {!any && !data.is_admin && <p className="muted small">No budget applies to you or your keys.</p>}
      {people.length > 0 && (
        <div className="budget-group">
          <h3>{data.everyone ? "People" : "You"}</h3>
          <div className="budget-grid">
            {people.map((p) => <PersonBudget key={p.id} p={p} admin={data.is_admin} onSaved={reload} />)}
          </div>
        </div>
      )}
      {keys.length > 0 && (
        <div className="budget-group">
          <h3>Keys</h3>
          <div className="budget-grid">
            {keys.map((k) => (
              <div key={k.id} className="budget-item">
                <div className="budget-who"><span className="strong">{k.name}</span><span className="mono small muted">{k.prefix}…</span>
                  {data.everyone && k.user.username && <span className="small muted">{k.user.username}</span>}</div>
                <BudgetMeter b={k} label={`Budget of the key ${k.name}`} />
              </div>
            ))}
          </div>
        </div>
      )}
      {providers.length > 0 && (
        <div className="budget-group">
          <h3>Providers</h3>
          <div className="budget-grid">
            {providers.map((p) => (
              <div key={p.id} className="budget-item">
                <div className="budget-who"><Link className="strong" to={`/providers/${p.id}`}>{p.name}</Link></div>
                <BudgetMeter b={p} label={`Budget of the provider ${p.name}`} />
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function PersonBudget({ p, admin, onSaved }: { p: BudgetPerson; admin: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(budgetInput(p.budget_micros));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const micros = parseBudget(value);
    if (micros === "bad") {
      setError("US dollars, like 25 or 2.50; empty: no budget.");
      return;
    }
    try {
      await api(`/budgets/people/${p.id}`, { method: "PUT", body: { budget_micros: micros } });
      setEditing(false);
      setError(null);
      onSaved();
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <div className="budget-item">
      <div className="budget-who"><span className="strong">{p.username}</span>
        {admin && !editing && <button type="button" className="btn btn-quiet btn-small" onClick={() => setEditing(true)}>{p.budget_micros === null ? "Set budget" : "Change"}</button>}
      </div>
      <BudgetMeter b={p} label={`Budget of ${p.username}`} />
      {editing && (
        <form onSubmit={save} aria-label={`Budget of ${p.username}`}>
          <label className="sr-only" htmlFor={`pb-${p.id}`}>Monthly budget of {p.username} (USD)</label>
          <input id={`pb-${p.id}`} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder="no budget" />
          <button type="submit" className="btn btn-small">Save</button>
          <button type="button" className="btn btn-quiet btn-small" onClick={() => { setEditing(false); setError(null); }}>Cancel</button>
          {error && <p className="field-error">{error}</p>}
        </form>
      )}
    </div>
  );
}

// Kilopixels as megapixels: "12.6 MP".
export function megapixels(kp: number | null | undefined): string {
  if (!kp) return "0 MP";
  return `${(kp / 1000).toLocaleString("en-US", { maximumFractionDigits: kp < 10000 ? 2 : 1 })} MP`;
}

// Busy milliseconds (GPU time on a GPU) as "2 h 05 min", "3 min 05 s" or "12.3 s".
export function busyTime(ms: number | null | undefined): string {
  if (!ms) return "—";
  const s = ms / 1000;
  if (s < 3600) return audioLength(ms);
  const h = Math.floor(s / 3600);
  return `${h} h ${String(Math.round((s - h * 3600) / 60)).padStart(2, "0")} min`;
}

// Milliseconds of audio as "1 min 05 s" or "12.3 s".
export function audioLength(ms: number | null | undefined): string {
  if (!ms) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${String(Math.round(s - m * 60)).padStart(2, "0")} s`;
}
