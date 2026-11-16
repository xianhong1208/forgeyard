import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";

import { BreakdownTable, NAME, type BreakdownColumn } from "../components/BreakdownTable";
import { BudgetMeter } from "../components/BudgetMeter";
import { LineChart } from "../components/LineChart";
import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { alerting, budgetInput, LEVEL_LABEL, parseBudget, resetText, type BudgetPerson, type BudgetsResponse } from "../lib/budgets";
import { ms, num } from "../lib/gateway";
import { assignSlots, isSeries, metricChoices, metricKind, METRICS, SPLITS, VIEWS, type SeriesResponse, type Split, type Step } from "../lib/charts";
import { bytes } from "../lib/machines";
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
  // 1.3: the media column shows once there are images, audio or video in the period.
  const media = (t?.media_items ?? 0) > 0;
  // Text to speech: characters spoken and the audio's length.
  const speech = (t?.characters ?? 0) > 0;
  // Images: how many, their megapixels and the time replicas spent making them (GPU time on a GPU).
  const images = (t?.images ?? 0) > 0;
  // Workloads at /x/ (1.3): request and answer bodies, and the time replicas spent on them.
  const traffic = (t?.bytes_in ?? 0) + (t?.bytes_out ?? 0) > 0;
  // Video (1.3): the length made (media_ms on rows without characters spoken) and the GPU time.
  const video = (data?.rows ?? []).some((r) => (r.media_ms ?? 0) > 0 && !(r.characters ?? 0));
  const length = speech || video;
  const lengthLabel = speech && video ? "Audio / video" : video ? "Video" : "Audio";
  const busy = images || traffic || video;
  const nameLabel = GROUPS.find((g) => g.id === data?.group_by)?.label ?? "Name";
  const groups = (tab === "cost" ? COST_GROUPS : GROUPS).filter((g) => !g.admin || (data?.is_admin && everyone));
  const setBy = tab === "cost" ? setCostGroup : setGroup;
  // The chart's view stays when switching tabs.
  const [chartView, setChartView] = useState<ChartView>({ step: "day", split: "none", metric: "requests" });
  const chart = <UsageChart cost={tab === "cost"} everyone={everyone} admin={!!data?.is_admin} view={chartView} onChange={setChartView} />;
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
      {tab === "cost" && data && <CostView data={data} chart={chart} />}
      {tab === "requests" && t && (
        <dl className="fleet usage-totals" aria-label="Totals">
          <div><dt>Requests</dt><dd>{num(t.requests)}</dd></div>
          <div><dt>Errors</dt><dd className={t.errors ? "usage-bad" : ""}>{num(t.errors)}{t.limited > 0 && <span className="muted small"> ({num(t.limited)} limited)</span>}</dd></div>
          <div><dt>Tokens</dt><dd>{num(t.tokens)} <span className="muted small">{num(t.prompt_tokens)} in · {num(t.completion_tokens)} out</span></dd></div>
          {media && <div><dt>Media items</dt><dd>{num(t.media_items)} <span className="muted small">images, audio, video</span></dd></div>}
          {speech && <div><dt>Speech</dt><dd>{num(t.characters)} <span className="muted small">characters · {audioLength(t.media_ms)} of audio</span></dd></div>}
          {video && <div><dt>Video</dt><dd>{audioLength(t.media_ms)} <span className="muted small">made{speech ? " (with the audio)" : ""} · {busyTime(t.busy_ms)} busy</span></dd></div>}
          {images && <div><dt>Images</dt><dd>{num(t.images)} <span className="muted small">{megapixels(t.kilopixels)} · {busyTime(t.busy_ms)} busy</span></dd></div>}
          {traffic && <div><dt>App traffic</dt><dd>{bytes(t.bytes_in)} <span className="muted small">in · {bytes(t.bytes_out)} out</span></dd></div>}
          <div><dt>Latency p50</dt><dd>{ms(t.latency_p50_ms)}</dd></div>
          <div><dt>Latency p95</dt><dd>{ms(t.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {tab === "requests" && data && chart}
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
          <BreakdownTable key={`requests-${data.group_by}`} label={`Requests by ${nameLabel.toLowerCase()}`} nameLabel={nameLabel} rows={data.rows}
            columns={requestColumns({ media, speech, length, lengthLabel, images, traffic, busy })} metric="requests"
            sort={data.group_by === "day" ? { by: NAME, desc: true } : undefined} others={addUp}
            nameClass={data.group_by === "model" || data.group_by === "day" ? "mono" : undefined} />
        )
      )}
      {tab === "requests" && data && <p className="muted small usage-foot">{data.from === data.to ? data.from : `${data.from} to ${data.to}`} (UTC). Latency is from the gateway: until the last byte of the answer, streamed ones included.</p>}
      </div>
    </>
  );
}

function CostView({ data, chart }: { data: UsageResponse; chart: ReactNode }) {
  const t = data.total;
  const cloud = t.split?.cloud;
  const local = t.split?.local;
  const unpriced = t.unpriced ?? 0;
  const estimated = t.cost_estimated ?? 0;
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
          {data.is_admin ? <Link to="/cloud">Set prices on the provider's models</Link> : "An admin sets the prices of cloud models."}
        </Notice>
      )}
      {chart}
      {data.rows.length === 0 ? (
        <div className="panel usage-empty"><p className="muted">No requests in this period. Requests to local replicas cost nothing; cloud requests are priced from their provider model's prices.</p></div>
      ) : (
        <BreakdownTable key={`cost-${data.group_by}`} label={`Cost by ${label.toLowerCase()}`} nameLabel={label} rows={data.rows} className="cost-table"
          columns={costColumns(t.cost_micros ?? 0)} metric="cost" sort={data.group_by === "day" ? { by: NAME, desc: true } : undefined} others={addUp}
          nameClass={data.group_by === "model" || data.group_by === "day" ? "mono" : undefined}
          nameExtra={(r) => r.cloud !== undefined && <span className={`chip ${r.cloud ? "chip-tone-wait" : "chip-tone-ok"}`}>{r.cloud ? "cloud" : "local"}</span>} />
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

// The columns of the Requests tab's table; the ones of 1.3's units only once the period has some.
type Units = { media: boolean; speech: boolean; length: boolean; lengthLabel: string; images: boolean; traffic: boolean; busy: boolean };

function requestColumns(u: Units): BreakdownColumn<UsageRow>[] {
  const cols: (BreakdownColumn<UsageRow> | false)[] = [
    { id: "requests", label: "Requests", className: "num", value: (r) => r.requests,
      cell: (r) => <span>{num(r.requests)}{r.spilled ? <span className="muted small"> · {num(r.spilled)} spilled</span> : null}</span> },
    { id: "errors", label: "Errors", className: "num", value: (r) => r.errors,
      cell: (r) => <span>{num(r.errors)}{r.limited > 0 && <span className="muted small"> · {num(r.limited)} limited</span>}</span> },
    { id: "prompt_tokens", label: "Tokens in", className: "num", value: (r) => r.prompt_tokens,
      cell: (r) => <span>{num(r.prompt_tokens)}{r.cached_tokens ? <span className="muted small"> · {num(r.cached_tokens)} cached</span> : null}</span> },
    { id: "completion_tokens", label: "Tokens out", className: "num", value: (r) => r.completion_tokens, cell: (r) => num(r.completion_tokens) },
    u.media && { id: "media_items", label: "Media", className: "num", title: "Images, audio clips and videos sent in chat", value: (r) => r.media_items ?? 0, cell: (r) => num(r.media_items ?? 0) },
    u.speech && { id: "characters", label: "Characters", className: "num", title: "Characters turned into speech", value: (r) => r.characters ?? 0, cell: (r) => num(r.characters ?? 0) },
    u.length && { id: "media_ms", label: u.lengthLabel, className: "num", title: "Length of the speech or video made (speech: where its format says it)",
      value: (r) => r.media_ms ?? 0, cell: (r) => audioLength(r.media_ms) },
    u.images && { id: "images", label: "Images", className: "num", title: "Images made, and their megapixels", value: (r) => r.images ?? 0,
      cell: (r) => <>{num(r.images ?? 0)}{r.images ? <span className="muted small"> · {megapixels(r.kilopixels)}</span> : null}</> },
    u.traffic && { id: "bytes", label: "Bytes in / out", dataLabel: "Bytes", className: "num", title: "Request and answer bodies of apps at /x/",
      value: (r) => (r.bytes_in ?? 0) + (r.bytes_out ?? 0), cell: (r) => (r.bytes_in || r.bytes_out ? `${bytes(r.bytes_in)} / ${bytes(r.bytes_out)}` : "—") },
    u.busy && { id: "busy_ms", label: "Busy", className: "num", title: "Time replicas worked on these requests (images and videos: times the GPUs each holds)",
      value: (r) => r.busy_ms ?? 0, cell: (r) => busyTime(r.busy_ms) },
    { id: "latency_avg_ms", label: "Avg", className: "num", value: (r) => r.latency_avg_ms, cell: (r) => ms(r.latency_avg_ms) },
    { id: "latency_p50_ms", label: "p50", className: "num", value: (r) => r.latency_p50_ms, cell: (r) => ms(r.latency_p50_ms) },
    { id: "latency_p95_ms", label: "p95", className: "num", value: (r) => r.latency_p95_ms, cell: (r) => ms(r.latency_p95_ms) },
  ];
  return cols.filter((c): c is BreakdownColumn<UsageRow> => !!c);
}

function costColumns(total: number): BreakdownColumn<UsageRow>[] {
  return [
    { id: "requests", label: "Requests", className: "num", value: (r) => r.requests, cell: (r) => num(r.requests) },
    { id: "tokens", label: "Tokens", className: "num", value: (r) => r.tokens,
      cell: (r) => <span>{num(r.tokens)}{r.cached_tokens ? <span className="muted small"> · {num(r.cached_tokens)} cached</span> : null}</span> },
    { id: "cost", label: "Cost", className: "num strong", value: (r) => r.cost_micros ?? 0, cell: (r) => usd(r.cost_micros) },
    { id: "share", label: "Share", className: "num", cell: (r) => share(r.cost_micros ?? 0, total) },
    { id: "unpriced", label: "Without a price", dataLabel: "Unpriced", className: "num", value: (r) => r.unpriced ?? 0,
      cell: (r) => (r.unpriced ? <span className="usage-bad">{num(r.unpriced)}</span> : <span className="muted">0</span>) },
  ];
}

// The rows past the top ones as one: their counts added up. Latencies do not add up, so they show as "—" (the
// totals above have the period's).
export function addUp(rows: UsageRow[]): UsageRow {
  const out: Record<string, unknown> = { id: "others", name: "Others", latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null };
  for (const r of rows) {
    for (const [k, v] of Object.entries(r)) {
      if (typeof v !== "number" || k.startsWith("latency_")) continue;
      out[k] = ((out[k] as number | undefined) ?? 0) + v;
    }
  }
  return out as UsageRow;
}

// Usage over time (1.4): one metric per day, week or month as a line chart, in total or per key, model, provider
// or person (admins, Everyone). The Cost tab charts cost; the Requests tab chooses what to chart.
type ChartView = { step: Step; split: Split; metric: string };

function UsageChart({ cost, everyone, admin, view: { step, split, metric: chosen }, onChange }: {
  cost: boolean; everyone: boolean; admin: boolean; view: ChartView; onChange: (v: ChartView) => void;
}) {
  const set = (v: Partial<ChartView>) => onChange({ step, split, metric: chosen, ...v });
  const metric = cost ? "cost_micros" : chosen;
  const span = VIEWS.find((v) => v.step === step)!;
  const people = admin && everyone;
  const by = split === "user" && !people ? "none" : split;
  const { data: raw, error, reload } = useApi<SeriesResponse>(
    `/usage/series?step=${step}&periods=${span.periods}&split=${by}&metric=${metric}${everyone ? "&everyone=true" : ""}`);
  usePoll(reload, 10000);
  const data = isSeries(raw) ? raw : null;
  // Colours follow each line while it stays on the chart (another metric or view re-ranks them): worked out
  // once per answer, from the slots of the answer before when the split is the same.
  const [colours, setColours] = useState<{ data: SeriesResponse | null; slots: Map<string, number> }>({ data: null, slots: new Map() });
  let slots = colours.slots;
  if (data && colours.data !== data) {
    const kept = colours.data?.split === data.split ? new Map(colours.slots) : new Map<string, number>();
    slots = assignSlots(data.series.filter((l) => !l.other).map((l) => l.id), kept);
    setColours({ data, slots });
  }
  const lines = (data?.series ?? []).map((l) => ({ ...l, slot: slots.get(l.id) ?? 0 }));
  const label = METRICS.find((m) => m.id === metric)?.label ?? metric;
  // The title names what is drawn: until the next answer arrives, the one before stays, dimmed.
  const drawn = data ? METRICS.find((m) => m.id === data.metric) : undefined;
  const title = data ? `${data.metric === "cost_micros" ? "Cost" : drawn?.label ?? data.metric} per ${data.step}${data.split === "none" ? ""
    : `, by ${(SPLITS.find((s) => s.id === data.split)?.label ?? data.split).toLowerCase()}`}` : "";
  const shown = data && data.metric === metric && data.step === step && data.split === by;
  return (
    <section className="panel usage-chart" aria-labelledby="usage-chart-title">
      <div className="usage-chart-head">
        <h2 id="usage-chart-title">{cost ? "Cost over time" : "Over time"}</h2>
        <div className="usage-chart-controls">
          {!cost && (
            <label className="field-inline small">Show
              <select value={metric} onChange={(e) => set({ metric: e.target.value })} aria-label="Show">
                {metricChoices(data?.totals, metric).map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </label>
          )}
          <label className="field-inline small">Split by
            <select value={by} onChange={(e) => set({ split: e.target.value as Split })} aria-label="Split by">
              {SPLITS.filter((s) => !s.admin || people).map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
          <div className="segmented" role="group" aria-label="Chart view">
            {VIEWS.map((v) => (
              <button key={v.step} type="button" className={step === v.step ? "on" : ""} aria-pressed={step === v.step} onClick={() => set({ step: v.step })}>{v.label}</button>
            ))}
          </div>
        </div>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {data && lines.length === 0 && (
        <p className="muted usage-chart-empty">
          {cost ? `No cost in ${span.range}: local replicas cost nothing, and no request went to a priced cloud model.`
            : `No ${label.toLowerCase()} in ${span.range}${data.everyone ? "" : " with your keys or in the Playground"}.`} The chart fills in as requests come through the gateway.
        </p>
      )}
      {data && lines.length > 0 && (
        <div className={shown ? "" : "usage-chart-stale"}>
          <LineChart title={title} periods={data.periods} step={data.step} lines={lines} total={data.total} kind={metricKind(data.metric)}
            legend={data.split !== "none"} />
        </div>
      )}
      {data && lines.length > 0 && (
        <p className="muted small usage-foot">
          {span.range[0].toUpperCase() + span.range.slice(1)}, {data.from} to {data.to} (UTC){step === "week" ? ", weeks from Monday" : ""}. The latest {step} is still going on (dashed).
          {data.series.some((l) => l.other) && " Past the five largest, the rest are added up as Other."}
          {data.omitted > 0 && ` ${data.omitted} with none ${data.omitted === 1 ? "is" : "are"} left out.`}
        </p>
      )}
    </section>
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
  // 1.4: a revoked key leaves the budget lists and warnings; its spend stays in its person's and provider's.
  const keys = data.keys.filter((k) => k.budget_micros !== null && k.status !== "revoked");
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
                <div className="budget-who"><Link className="strong" to={`/cloud/providers/${p.id}`}>{p.name}</Link></div>
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
