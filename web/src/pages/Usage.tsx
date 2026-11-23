import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";

import { BreakdownTable, NAME, type BreakdownColumn } from "../components/BreakdownTable";
import { BudgetMeter } from "../components/BudgetMeter";
import { LineChart } from "../components/LineChart";
import { Notice, PageHeader } from "../components/ui";
import { labels, t as tNow } from "../i18n/core";
import { useT, type Translator } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { alerting, budgetInput, LEVEL_LABEL, parseBudget, resetText, type BudgetPerson, type BudgetsResponse } from "../lib/budgets";
import { ms, num } from "../lib/gateway";
import { STEP_WORD, assignSlots, isSeries, metricChoices, metricKind, METRICS, rowName, SPLITS, VIEWS, type SeriesResponse, type Split, type Step } from "../lib/charts";
import { bytes } from "../lib/machines";
import { share, usd } from "../lib/prices";
import type { UsageResponse, UsageRow } from "../lib/types";
import { useApi } from "../lib/useApi";
import { usePoll } from "../lib/poll";

const DAYS = [1, 7, 30];
// The words are read when shown, so they follow the language (#189).
const GROUP_LABEL = labels({ model: "usage.group.model", key: "usage.group.key", user: "usage.group.user", day: "usage.group.day",
  source: "usage.group.source", target: "usage.group.target", provider: "usage.group.provider" });
type GroupId = keyof typeof GROUP_LABEL;
const group = (id: GroupId, admin?: boolean) => ({ id, ...(admin ? { admin } : {}), get label() { return GROUP_LABEL[id]; } });
const GROUPS: { id: string; label: string; admin?: boolean }[] = [
  group("model"), group("key"), group("user", true), group("day"), group("source"), group("target"),
];
const COST_GROUPS: { id: string; label: string; admin?: boolean }[] = [
  group("day"), group("key"), group("user", true), group("model"), group("provider"),
];

// The rows with the names the server gives a few groups in the reader's language.
function named<R extends { id: string; name: string }>(rows: R[]): R[] {
  return rows.map((r) => {
    const name = rowName(r.id, r.name);
    return name === r.name ? r : { ...r, name };
  });
}

export function UsagePage() {
  const t = useT();
  const [tab, setTab] = useState<"requests" | "cost">("requests");
  const [days, setDays] = useState(7);
  const [group, setGroup] = useState("model");
  const [costGroup, setCostGroup] = useState("day");
  const [everyone, setEveryone] = useState(false);
  const by = tab === "cost" ? costGroup : group;
  const { data, error, reload } = useApi<UsageResponse>(`/usage?days=${days}&group=${by}${everyone ? "&everyone=true" : ""}`);
  usePoll(reload, 10000);
  const sum = data?.total;
  // 1.3: the media column shows once there are images, audio or video in the period.
  const media = (sum?.media_items ?? 0) > 0;
  // Text to speech: characters spoken and the audio's length.
  const speech = (sum?.characters ?? 0) > 0;
  // Images: how many, their megapixels and the time replicas spent making them (GPU time on a GPU).
  const images = (sum?.images ?? 0) > 0;
  // Workloads at /x/ (1.3): request and answer bodies, and the time replicas spent on them.
  const traffic = (sum?.bytes_in ?? 0) + (sum?.bytes_out ?? 0) > 0;
  // Video (1.3): the length made (media_ms on rows without characters spoken) and the GPU time.
  const video = (data?.rows ?? []).some((r) => (r.media_ms ?? 0) > 0 && !(r.characters ?? 0));
  const length = speech || video;
  const lengthLabel = speech && video ? t("usage.col.audioVideo") : video ? t("usage.video") : t("usage.col.audio");
  const busy = images || traffic || video;
  const nameLabel = GROUPS.find((g) => g.id === data?.group_by)?.label ?? t("usage.name");
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
      <PageHeader index={t("usage.index")} title={t("usage.title")}
        lead={data?.everyone ? t("usage.lead.everyone") : t("usage.lead.mine")} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="tabs" role="tablist" aria-label={t("usage.tabs")}>
        {(["requests", "cost"] as const).map((id) => (
          <button key={id} type="button" role="tab" id={`usage-tab-${id}`} aria-selected={tab === id} aria-controls="usage-panel"
            className={tab === id ? "on" : ""} onClick={() => setTab(id)}>
            {id === "requests" ? t("usage.tab.requests") : t("usage.tab.cost")}
          </button>
        ))}
      </div>
      <div className="usage-controls">
        <div className="segmented" role="group" aria-label={t("usage.period")}>
          {DAYS.map((d) => (
            <button key={d} type="button" className={days === d ? "on" : ""} aria-pressed={days === d} onClick={() => setDays(d)}>
              {d === 1 ? t("usage.today") : t("usage.days", { count: d })}
            </button>
          ))}
        </div>
        <div className="segmented" role="group" aria-label={t("usage.groupBy")}>
          {groups.map((g) => (
            <button key={g.id} type="button" className={by === g.id ? "on" : ""} aria-pressed={by === g.id} onClick={() => setBy(g.id)}>
              {g.label}
            </button>
          ))}
        </div>
        {data?.is_admin && (
          <div className="segmented" role="group" aria-label={t("usage.whose")}>
            <button type="button" className={everyone ? "" : "on"} aria-pressed={!everyone} onClick={() => { setEveryone(false); if (group === "user") setGroup("model"); if (costGroup === "user") setCostGroup("day"); }}>{t("usage.mine")}</button>
            <button type="button" className={everyone ? "on" : ""} aria-pressed={everyone} onClick={() => setEveryone(true)}>{t("usage.everyone")}</button>
          </div>
        )}
      </div>
      <div id="usage-panel" role="tabpanel" aria-labelledby={`usage-tab-${tab}`}>
      {tab === "cost" && <Budgets everyone={everyone} />}
      {tab === "cost" && data && <CostView data={data} chart={chart} />}
      {tab === "requests" && sum && (
        <dl className="fleet usage-totals" aria-label={t("usage.totals")}>
          <div><dt>{t("usage.requests")}</dt><dd>{num(sum.requests)}</dd></div>
          <div><dt>{t("usage.errors")}</dt><dd className={sum.errors ? "usage-bad" : ""}>{num(sum.errors)}{sum.limited > 0 && <span className="muted small">{t("usage.limited", { count: num(sum.limited) })}</span>}</dd></div>
          <div><dt>{t("usage.tokens")}</dt><dd>{num(sum.tokens)} <span className="muted small">{t("usage.inOut", { in: num(sum.prompt_tokens), out: num(sum.completion_tokens) })}</span></dd></div>
          {media && <div><dt>{t("usage.mediaItems")}</dt><dd>{num(sum.media_items)} <span className="muted small">{t("usage.mediaKinds")}</span></dd></div>}
          {speech && <div><dt>{t("usage.speech")}</dt><dd>{num(sum.characters)} <span className="muted small">{t("usage.speechDetail", { length: audioLength(sum.media_ms) })}</span></dd></div>}
          {video && <div><dt>{t("usage.video")}</dt><dd>{audioLength(sum.media_ms)} <span className="muted small">{t(speech ? "usage.videoDetailSpeech" : "usage.videoDetail", { busy: busyTime(sum.busy_ms) })}</span></dd></div>}
          {images && <div><dt>{t("usage.images")}</dt><dd>{num(sum.images)} <span className="muted small">{t("usage.busyDetail", { amount: megapixels(sum.kilopixels), busy: busyTime(sum.busy_ms) })}</span></dd></div>}
          {traffic && <div><dt>{t("usage.appTraffic")}</dt><dd>{bytes(sum.bytes_in)} <span className="muted small">{t("usage.trafficDetail", { out: bytes(sum.bytes_out) })}</span></dd></div>}
          <div><dt>{t("usage.latencyP50")}</dt><dd>{ms(sum.latency_p50_ms)}</dd></div>
          <div><dt>{t("usage.latencyP95")}</dt><dd>{ms(sum.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {tab === "requests" && data && chart}
      {tab === "requests" && data && (
        data.rows.length === 0 ? (
          <div className="panel usage-empty">
            <p className="muted">
              {t.rich("usage.empty", { v1: <code>/v1</code>, whose: data.everyone ? t("usage.empty.anyone") : t("usage.empty.your") })}
            </p>
            <p className="row-gap">
              <Link to="/keys" className="btn">{t("usage.createKey")}</Link>
              <Link to="/playground" className="btn btn-quiet">{t("usage.openPlayground")}</Link>
            </p>
          </div>
        ) : (
          <BreakdownTable key={`requests-${data.group_by}`} label={t("usage.byName", { name: nameLabel.toLowerCase() })} nameLabel={nameLabel} rows={named(data.rows)}
            columns={requestColumns(t, { media, speech, length, lengthLabel, images, traffic, busy })} metric="requests"
            sort={data.group_by === "day" ? { by: NAME, desc: true } : undefined} others={addUp}
            nameClass={data.group_by === "model" || data.group_by === "day" ? "mono" : undefined} />
        )
      )}
      {tab === "requests" && data && <p className="muted small usage-foot">{t("usage.foot.requests", { range: dates(t, data.from, data.to) })}</p>}
      </div>
    </>
  );
}

// "2026-11-17" or "2026-11-11 to 2026-11-17": the period, as the server's UTC days.
function dates(t: Translator, from: string, to: string): string {
  return from === to ? from : t("usage.range", { from, to });
}

function CostView({ data, chart }: { data: UsageResponse; chart: ReactNode }) {
  const t = useT();
  const sum = data.total;
  const cloud = sum.split?.cloud;
  const local = sum.split?.local;
  const unpriced = sum.unpriced ?? 0;
  const estimated = sum.cost_estimated ?? 0;
  const label = COST_GROUPS.find((g) => g.id === data.group_by)?.label ?? t("usage.name");
  return (
    <>
      <dl className="fleet usage-totals" aria-label={t("usage.costTotals")}>
        <div><dt>{t("usage.cost")}</dt><dd>{usd(sum.cost_micros)}</dd></div>
        <div><dt>{t("usage.cloud")}</dt><dd>{usd(cloud?.cost_micros)} <span className="muted small">{t("usage.nRequests", { count: num(cloud?.requests ?? 0) })}</span></dd></div>
        <div><dt>{t("usage.local")}</dt><dd>{usd(local?.cost_micros)} <span className="muted small">{t("usage.nRequests", { count: num(local?.requests ?? 0) })}</span></dd></div>
        <div><dt>{t("usage.servedLocally")}</dt><dd>{share(local?.requests ?? 0, sum.requests)} <span className="muted small">{t("usage.ofRequests")}</span></dd></div>
        <div><dt>{t("usage.withoutPrice")}</dt><dd className={unpriced ? "usage-bad" : ""}>{num(unpriced)} <span className="muted small">{t("usage.requestsWord")}</span></dd></div>
      </dl>
      {unpriced > 0 && (
        <Notice tone="error">
          {t.plural("usage.unpriced", unpriced)}{" "}
          {data.is_admin ? <Link to="/cloud">{t("usage.setPrices")}</Link> : t("usage.adminPrices")}
        </Notice>
      )}
      {chart}
      {data.rows.length === 0 ? (
        <div className="panel usage-empty"><p className="muted">{t("usage.costEmpty")}</p></div>
      ) : (
        <BreakdownTable key={`cost-${data.group_by}`} label={t("usage.costBy", { name: label.toLowerCase() })} nameLabel={label} rows={named(data.rows)} className="cost-table"
          columns={costColumns(t, sum.cost_micros ?? 0)} metric="cost" sort={data.group_by === "day" ? { by: NAME, desc: true } : undefined} others={addUp}
          nameClass={data.group_by === "model" || data.group_by === "day" ? "mono" : undefined}
          nameExtra={(r) => r.cloud !== undefined && <span className={`chip ${r.cloud ? "chip-tone-wait" : "chip-tone-ok"}`}>{r.cloud ? t("usage.chip.cloud") : t("usage.chip.local")}</span>} />
      )}
      <p className="muted small usage-foot">
        {t("usage.foot.cost", { range: dates(t, data.from, data.to) })}
        {estimated > 0 && t.plural("usage.estimated", estimated)}
        {" "}{t("usage.notInvoice")}
      </p>
    </>
  );
}

// The columns of the Requests tab's table; the ones of 1.3's units only once the period has some.
type Units = { media: boolean; speech: boolean; length: boolean; lengthLabel: string; images: boolean; traffic: boolean; busy: boolean };

function requestColumns(t: Translator, u: Units): BreakdownColumn<UsageRow>[] {
  const cols: (BreakdownColumn<UsageRow> | false)[] = [
    { id: "requests", label: t("usage.requests"), className: "num", value: (r) => r.requests,
      cell: (r) => <span>{num(r.requests)}{r.spilled ? <span className="muted small">{t("usage.col.spilled", { count: num(r.spilled) })}</span> : null}</span> },
    { id: "errors", label: t("usage.errors"), className: "num", value: (r) => r.errors,
      cell: (r) => <span>{num(r.errors)}{r.limited > 0 && <span className="muted small">{t("usage.col.limited", { count: num(r.limited) })}</span>}</span> },
    { id: "prompt_tokens", label: t("usage.col.tokensIn"), className: "num", value: (r) => r.prompt_tokens,
      cell: (r) => <span>{num(r.prompt_tokens)}{r.cached_tokens ? <span className="muted small">{t("usage.col.cached", { count: num(r.cached_tokens) })}</span> : null}</span> },
    { id: "completion_tokens", label: t("usage.col.tokensOut"), className: "num", value: (r) => r.completion_tokens, cell: (r) => num(r.completion_tokens) },
    u.media && { id: "media_items", label: t("usage.col.media"), className: "num", title: t("usage.col.mediaTitle"), value: (r) => r.media_items ?? 0, cell: (r) => num(r.media_items ?? 0) },
    u.speech && { id: "characters", label: t("usage.col.characters"), className: "num", title: t("usage.col.charactersTitle"), value: (r) => r.characters ?? 0, cell: (r) => num(r.characters ?? 0) },
    u.length && { id: "media_ms", label: u.lengthLabel, className: "num", title: t("usage.col.lengthTitle"),
      value: (r) => r.media_ms ?? 0, cell: (r) => audioLength(r.media_ms) },
    u.images && { id: "images", label: t("usage.images"), className: "num", title: t("usage.col.imagesTitle"), value: (r) => r.images ?? 0,
      cell: (r) => <>{num(r.images ?? 0)}{r.images ? <span className="muted small"> · {megapixels(r.kilopixels)}</span> : null}</> },
    u.traffic && { id: "bytes", label: t("usage.col.bytes"), dataLabel: t("usage.col.bytesShort"), className: "num", title: t("usage.col.bytesTitle"),
      value: (r) => (r.bytes_in ?? 0) + (r.bytes_out ?? 0), cell: (r) => (r.bytes_in || r.bytes_out ? `${bytes(r.bytes_in)} / ${bytes(r.bytes_out)}` : "—") },
    u.busy && { id: "busy_ms", label: t("usage.col.busy"), className: "num", title: t("usage.col.busyTitle"),
      value: (r) => r.busy_ms ?? 0, cell: (r) => busyTime(r.busy_ms) },
    { id: "latency_avg_ms", label: t("usage.col.avg"), className: "num", value: (r) => r.latency_avg_ms, cell: (r) => ms(r.latency_avg_ms) },
    { id: "latency_p50_ms", label: "p50", className: "num", value: (r) => r.latency_p50_ms, cell: (r) => ms(r.latency_p50_ms) },
    { id: "latency_p95_ms", label: "p95", className: "num", value: (r) => r.latency_p95_ms, cell: (r) => ms(r.latency_p95_ms) },
  ];
  return cols.filter((c): c is BreakdownColumn<UsageRow> => !!c);
}

function costColumns(t: Translator, total: number): BreakdownColumn<UsageRow>[] {
  return [
    { id: "requests", label: t("usage.requests"), className: "num", value: (r) => r.requests, cell: (r) => num(r.requests) },
    { id: "tokens", label: t("usage.tokens"), className: "num", value: (r) => r.tokens,
      cell: (r) => <span>{num(r.tokens)}{r.cached_tokens ? <span className="muted small">{t("usage.col.cached", { count: num(r.cached_tokens) })}</span> : null}</span> },
    { id: "cost", label: t("usage.cost"), className: "num strong", value: (r) => r.cost_micros ?? 0, cell: (r) => usd(r.cost_micros) },
    { id: "share", label: t("usage.col.share"), className: "num", cell: (r) => share(r.cost_micros ?? 0, total) },
    { id: "unpriced", label: t("usage.col.unpriced"), dataLabel: t("usage.col.unpricedShort"), className: "num", value: (r) => r.unpriced ?? 0,
      cell: (r) => (r.unpriced ? <span className="usage-bad">{num(r.unpriced)}</span> : <span className="muted">0</span>) },
  ];
}

// The rows past the top ones as one: their counts added up. Latencies do not add up, so they show as "—" (the
// totals above have the period's).
export function addUp(rows: UsageRow[]): UsageRow {
  const out: Record<string, unknown> = { id: "others", name: tNow("usage.others"), latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null };
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
  const t = useT();
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
  const lines = (data?.series ?? []).map((l) => ({ ...l, name: rowName(l.id, l.name), slot: slots.get(l.id) ?? 0 }));
  const label = METRICS.find((m) => m.id === metric)?.label ?? metric;
  // The title names what is drawn: until the next answer arrives, the one before stays, dimmed.
  const drawn = data ? METRICS.find((m) => m.id === data.metric) : undefined;
  const what = data?.metric === "cost_micros" ? t("usage.cost") : drawn?.label ?? data?.metric ?? "";
  const title = !data ? "" : data.split === "none" ? t("usage.chart.per", { metric: what, step: STEP_WORD[data.step] })
    : t("usage.chart.perBy", { metric: what, step: STEP_WORD[data.step], split: (SPLITS.find((s) => s.id === data.split)?.label ?? data.split).toLowerCase() });
  const shown = data && data.metric === metric && data.step === step && data.split === by;
  return (
    <section className="panel usage-chart" aria-labelledby="usage-chart-title">
      <div className="usage-chart-head">
        <h2 id="usage-chart-title">{cost ? t("usage.chart.costTitle") : t("usage.chart.title")}</h2>
        <div className="usage-chart-controls">
          {!cost && (
            <label className="field-inline small">{t("usage.chart.show")}
              <select value={metric} onChange={(e) => set({ metric: e.target.value })} aria-label={t("usage.chart.show")}>
                {metricChoices(data?.totals, metric).map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </label>
          )}
          <label className="field-inline small">{t("usage.chart.splitBy")}
            <select value={by} onChange={(e) => set({ split: e.target.value as Split })} aria-label={t("usage.chart.splitBy")}>
              {SPLITS.filter((s) => !s.admin || people).map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </label>
          <div className="segmented" role="group" aria-label={t("usage.chart.view")}>
            {VIEWS.map((v) => (
              <button key={v.step} type="button" className={step === v.step ? "on" : ""} aria-pressed={step === v.step} onClick={() => set({ step: v.step })}>{v.label}</button>
            ))}
          </div>
        </div>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {data && lines.length === 0 && (
        <p className="muted usage-chart-empty">
          {cost ? t("usage.chart.noCost", { range: span.range })
            : t(data.everyone ? "usage.chart.none" : "usage.chart.noneMine", { metric: label.toLowerCase(), range: span.range })} {t("usage.chart.fills")}
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
          {t("usage.chart.foot", { range: span.range[0].toUpperCase() + span.range.slice(1), from: data.from, to: data.to,
            weeks: step === "week" ? t("usage.chart.weeks") : "", step: STEP_WORD[step] })}
          {data.series.some((l) => l.other) && <>{" "}{t("usage.chart.other")}</>}
          {data.omitted > 0 && <>{" "}{t.plural("usage.chart.omitted", data.omitted)}</>}
        </p>
      )}
    </section>
  );
}

// This month's budgets: the person's own and their keys' (admins with Everyone: every person, key with a budget
// or spend, and provider), with warnings from 80% on. Admins set people's budgets here.
function Budgets({ everyone }: { everyone: boolean }) {
  const t = useT();
  const { data, error, reload } = useApi<BudgetsResponse>(`/budgets${everyone ? "?everyone=true" : ""}`);
  usePoll(reload, 10000);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return null;
  const people = data.people.filter((p) => p.budget_micros !== null || (data.everyone && p.spent_micros > 0) || data.is_admin);
  // 1.4: a revoked key leaves the budget lists and warnings; its spend stays in its person's and provider's.
  const keys = data.keys.filter((k) => k.budget_micros !== null && k.status !== "revoked");
  const providers = data.providers;
  const warn = [
    ...alerting(data.people).map((p) => ({ id: `p${p.id}`, text: data.everyone ? t("usage.budgets.person", { name: p.username }) : t("usage.budgets.yours"), level: p.level })),
    ...alerting(keys).map((k) => ({ id: `k${k.id}`, level: k.level,
      text: data.everyone && k.user.username ? t("usage.budgets.keyOf", { name: k.name, user: k.user.username }) : t("usage.budgets.key", { name: k.name }) })),
    ...alerting(providers).map((p) => ({ id: `v${p.id}`, text: t("usage.budgets.provider", { name: p.name }), level: p.level })),
  ];
  const reset = resetText(data.resets_at);
  const any = people.some((p) => p.budget_micros !== null) || keys.length > 0 || providers.some((p) => p.budget_micros !== null);
  return (
    <section className="panel budget-panel" aria-labelledby="budgets-title">
      <h2 id="budgets-title">{t("usage.budgets.title")}</h2>
      <p className="muted small">
        {t("usage.budgets.lead", { month: data.month, reset })} {data.is_admin ? t("usage.budgets.admin")
          : t.rich("usage.budgets.user", { link: (text) => <Link to="/keys">{text}</Link> })}
      </p>
      {warn.map((w) => (
        <Notice key={w.id} tone={w.level === "exhausted" ? "error" : "info"}>{w.level === "exhausted"
          ? t("usage.budgets.levelLocal", { who: w.text, level: LEVEL_LABEL[w.level!], reset }) : t("usage.budgets.level", { who: w.text, level: LEVEL_LABEL[w.level!] })}</Notice>
      ))}
      {!any && !data.is_admin && <p className="muted small">{t("usage.budgets.none")}</p>}
      {people.length > 0 && (
        <div className="budget-group">
          <h3>{data.everyone ? t("usage.budgets.people") : t("usage.budgets.you")}</h3>
          <div className="budget-grid">
            {people.map((p) => <PersonBudget key={p.id} p={p} admin={data.is_admin} onSaved={reload} />)}
          </div>
        </div>
      )}
      {keys.length > 0 && (
        <div className="budget-group">
          <h3>{t("usage.budgets.keys")}</h3>
          <div className="budget-grid">
            {keys.map((k) => (
              <div key={k.id} className="budget-item">
                <div className="budget-who"><span className="strong">{k.name}</span><span className="mono small muted">{k.prefix}…</span>
                  {data.everyone && k.user.username && <span className="small muted">{k.user.username}</span>}</div>
                <BudgetMeter b={k} label={t("usage.budgets.ofKey", { name: k.name })} />
              </div>
            ))}
          </div>
        </div>
      )}
      {providers.length > 0 && (
        <div className="budget-group">
          <h3>{t("usage.budgets.providers")}</h3>
          <div className="budget-grid">
            {providers.map((p) => (
              <div key={p.id} className="budget-item">
                <div className="budget-who"><Link className="strong" to={`/cloud/providers/${p.id}`}>{p.name}</Link></div>
                <BudgetMeter b={p} label={t("usage.budgets.ofProvider", { name: p.name })} />
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function PersonBudget({ p, admin, onSaved }: { p: BudgetPerson; admin: boolean; onSaved: () => void }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(budgetInput(p.budget_micros));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const micros = parseBudget(value);
    if (micros === "bad") {
      setError(t("usage.budgets.bad"));
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
        {admin && !editing && <button type="button" className="btn btn-quiet btn-small" onClick={() => setEditing(true)}>{p.budget_micros === null ? t("usage.budgets.set") : t("usage.budgets.change")}</button>}
      </div>
      <BudgetMeter b={p} label={t("usage.budgets.of", { name: p.username })} />
      {editing && (
        <form onSubmit={save} aria-label={t("usage.budgets.of", { name: p.username })}>
          <label className="sr-only" htmlFor={`pb-${p.id}`}>{t("usage.budgets.monthly", { name: p.username })}</label>
          <input id={`pb-${p.id}`} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder={t("usage.budgets.noBudget")} />
          <button type="submit" className="btn btn-small">{t("usage.budgets.save")}</button>
          <button type="button" className="btn btn-quiet btn-small" onClick={() => { setEditing(false); setError(null); }}>{t("common.cancel")}</button>
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
