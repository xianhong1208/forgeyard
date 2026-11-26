// Usage over time (1.4): the numbers behind the Usage page's line charts (GET /api/usage/series) and the
// helpers that turn them into axes, labels and colours.

import { labels, translate } from "../i18n/core";
import { formatNumber } from "../i18n/format";
import { getLang, locale, type Lang } from "../i18n/lang";
import { bytes } from "./machines";
import { usd } from "./prices";

export type Step = "day" | "week" | "month";
export type Split = "none" | "key" | "model" | "provider" | "user" | "team";

export type SeriesLine = { id: string; name: string; values: number[]; total: number; other?: boolean; cloud?: boolean };
export type Period = { start: string; end: string; partial: boolean };

export type SeriesResponse = {
  step: Step; split: Split; metric: string; from: string; to: string; periods: Period[]; series: SeriesLine[];
  total: number[]; totals: Record<string, number>; omitted: number; everyone: boolean; is_admin: boolean;
};

// How far back each view looks: 30 days, 13 weeks (a quarter), 12 months.
// The words are read when used, so they follow the language (#189).
const VIEW_LABEL = labels({ day: "usage.view.day", week: "usage.view.week", month: "usage.view.month" });
const VIEW_RANGE = labels({ day: "usage.viewRange.day", week: "usage.viewRange.week", month: "usage.viewRange.month" });
const view = (step: Step, periods: number) => ({
  step, periods, get label() { return VIEW_LABEL[step]; }, get range() { return VIEW_RANGE[step]; },
});
export const VIEWS: { step: Step; label: string; periods: number; range: string }[] = [view("day", 30), view("week", 13), view("month", 12)];

const SPLIT_LABEL = labels({ none: "usage.split.none", key: "usage.split.key", model: "usage.split.model", provider: "usage.split.provider",
  user: "usage.split.user", team: "usage.split.team" });
const split = (id: Split, admin?: boolean) => ({ id, ...(admin ? { admin } : {}), get label() { return SPLIT_LABEL[id]; } });
export const SPLITS: { id: Split; label: string; admin?: boolean }[] = [split("none"), split("key"), split("model"), split("provider"), split("user", true), split("team", true)];

// One step as a word in a sentence ("per day") and as a column heading ("Day").
export const STEP_WORD: Record<Step, string> = labels({ day: "usage.step.day", week: "usage.step.week", month: "usage.step.month" });
export const STEP_HEAD: Record<Step, string> = labels({ day: "usage.stepHead.day", week: "usage.stepHead.week", month: "usage.stepHead.month" });

type Kind = "count" | "usd" | "duration" | "bytes";

// What a chart can show. `always`: offered even when nothing was counted yet; the others once the range has some.
const METRIC_LABEL = labels({
  requests: "usage.metric.requests", tokens: "usage.metric.tokens", prompt_tokens: "usage.metric.prompt_tokens",
  completion_tokens: "usage.metric.completion_tokens", errors: "usage.metric.errors", media_items: "usage.metric.media_items",
  characters: "usage.metric.characters", images: "usage.metric.images", media_ms: "usage.metric.media_ms", busy_ms: "usage.metric.busy_ms",
  bytes_in: "usage.metric.bytes_in", bytes_out: "usage.metric.bytes_out", cost_micros: "usage.metric.cost_micros",
});
const metric = (id: keyof typeof METRIC_LABEL, kind: Kind, always?: boolean) =>
  ({ id, kind, ...(always ? { always } : {}), get label() { return METRIC_LABEL[id]; } });
export const METRICS: { id: string; label: string; kind: Kind; always?: boolean }[] = [
  metric("requests", "count", true),
  metric("tokens", "count", true),
  metric("prompt_tokens", "count", true),
  metric("completion_tokens", "count", true),
  metric("errors", "count", true),
  metric("media_items", "count"),
  metric("characters", "count"),
  metric("images", "count"),
  metric("media_ms", "duration"),
  metric("busy_ms", "duration"),
  metric("bytes_in", "bytes"),
  metric("bytes_out", "bytes"),
  metric("cost_micros", "usd"),
];

export function metricKind(id: string): Kind {
  return METRICS.find((m) => m.id === id)?.kind ?? "count";
}

// The Requests tab's choices: the counts always, a unit once some of it was counted (and the one chosen).
export function metricChoices(totals: Record<string, number> | undefined, chosen: string) {
  return METRICS.filter((m) => m.id !== "cost_micros" && (m.always || m.id === chosen || (totals?.[m.id] ?? 0) > 0));
}

// Axis ticks from 0: steps of 1, 2 or 5 times a power of ten, at least 1 (every metric counts whole units).
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = Math.max(1, (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag);
  const top = Math.ceil(max / step) * step;
  const out: number[] = [];
  for (let v = 0; v <= top + step / 2; v += step) out.push(Math.round(v * 1e6) / 1e6);
  return out;
}

// Which periods get a label on the x axis: at most `room`, evenly spaced, always the latest one.
export function labelIndexes(n: number, room: number): number[] {
  if (n <= 0) return [];
  const every = Math.max(1, Math.ceil(n / Math.max(1, room)));
  const out: number[] = [];
  for (let i = n - 1; i >= 0; i -= every) out.unshift(i);
  return out;
}

// Of axis labels spanning [left, right] pixels, the ones that do not overlap: kept from the last (the latest
// period) towards the first, each at least `gap` left of the one kept after it.
export function keepApart(spans: [number, number][], gap = 8): number[] {
  const out: number[] = [];
  let edge = Infinity;
  for (let k = spans.length - 1; k >= 0; k--) {
    if (spans[k][1] + gap <= edge) {
      out.unshift(k);
      edge = spans[k][0];
    }
  }
  return out;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parts(iso: string): [number, number, number] {
  const [y, m, d] = iso.split("-").map(Number);
  return [y, m, d];
}

const utc = (y: number, m: number, d: number) => new Date(Date.UTC(y, m - 1, d));
const dateFormats = new Map<string, Intl.DateTimeFormat>();
function df(lang: Lang, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${lang}|${JSON.stringify(opts)}`;
  let f = dateFormats.get(key);
  if (!f) dateFormats.set(key, (f = new Intl.DateTimeFormat(locale(lang), { ...opts, timeZone: "UTC" })));
  return f;
}

// A period on the axis: "Nov 12" (a day, or the Monday a week starts on), "Nov 2026" (a month); in Chinese
// Intl's month and day ("11月12日", "2026年11月").
export function periodLabel(p: Period, step: Step, lang: Lang = getLang()): string {
  const [y, m, d] = parts(p.start);
  if (lang !== "en") {
    return df(lang, step === "month" ? { year: "numeric", month: "short" } : { month: "short", day: "numeric" }).format(utc(y, m, d));
  }
  return step === "month" ? `${MONTHS[m - 1]} ${y}` : `${MONTHS[m - 1]} ${d}`;
}

// The same in full, for tooltips and the table: "Thu, Nov 12, 2026", "Nov 9 – 15, 2026", "November 2026";
// the period still going on says so. In Chinese Intl's dates, weekday and range ("2026年11月12日 週四").
export function periodTitle(p: Period, step: Step, lang: Lang = getLang()): string {
  const [y, m, d] = parts(p.start);
  if (lang !== "en") {
    const [ey, em, ed] = parts(p.end);
    const text = step === "month" ? df(lang, { year: "numeric", month: "long" }).format(utc(y, m, 1))
      : step === "week" ? translate(lang, "usage.chart.weekRange", {
        start: df(lang, { year: "numeric", month: "short", day: "numeric" }).format(utc(y, m, d)),
        end: df(lang, ey === y ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" }).format(utc(ey, em, ed)) })
        : df(lang, { year: "numeric", month: "short", day: "numeric", weekday: "short" }).format(utc(y, m, d));
    return p.partial ? translate(lang, "usage.chart.soFar", { period: text }) : text;
  }
  let text: string;
  if (step === "month") {
    text = `${new Date(Date.UTC(y, m - 1, 1)).toLocaleString("en-US", { month: "long", timeZone: "UTC" })} ${y}`;
  } else if (step === "week") {
    const [ey, em, ed] = parts(p.end);
    const end = em === m ? `${ed}` : `${MONTHS[em - 1]} ${ed}`;
    text = `${MONTHS[m - 1]} ${d} – ${end}, ${ey}`;
  } else {
    const wd = new Date(Date.UTC(y, m - 1, d)).toLocaleString("en-US", { weekday: "short", timeZone: "UTC" });
    text = `${wd}, ${MONTHS[m - 1]} ${d}, ${y}`;
  }
  return p.partial ? translate(lang, "usage.chart.soFar", { period: text }) : text;
}

// "12.3 s", "4.5 min", "2 h"; in Chinese Intl's units ("12.3 秒", "4.5 分鐘", "2 小時").
function duration(ms: number, lang: Lang = getLang()): string {
  const s = ms / 1000;
  const [v, unit, en] = !ms ? [0, "second", "s"] : s < 60 ? [Number(s.toFixed(s < 10 ? 1 : 0)), "second", "s"]
    : s < 3600 ? [Number((s / 60).toFixed(s < 600 ? 1 : 0)), "minute", "min"] : [Number((s / 3600).toFixed(s < 36000 ? 1 : 0)), "hour", "h"];
  if (lang === "en") return `${v} ${en}`;
  return formatNumber(v as number, lang, { style: "unit", unit: unit as string, unitDisplay: "short", maximumFractionDigits: 1 });
}

// A short number on the axis: "1.2K" / "1.2萬".
function compact(v: number, lang: Lang = getLang()): string {
  return formatNumber(v, lang, { notation: "compact", maximumFractionDigits: 1 });
}

// How wide a short text is on the chart, in Latin characters: a CJK character takes about one and a half.
export function textUnits(s: string): number {
  let n = 0;
  for (const ch of s) n += /[\u2E80-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF]/.test(ch) ? 1.5 : 1;
  return n;
}

// A value in full, for tooltips, the legend and the table.
export function formatValue(v: number, kind: Kind): string {
  if (kind === "usd") return usd(v);
  if (kind === "duration") return duration(v);
  if (kind === "bytes") return v ? bytes(v) : "0";
  return formatNumber(v);
}

// The dollar sign as the reader's language writes US dollars, as the tables (usd) do: "$" in English,
// "US$" in Chinese.
function dollarSign(lang: Lang = getLang()): string {
  return new Intl.NumberFormat(locale(lang), { style: "currency", currency: "USD" }).formatToParts(0)
    .find((p) => p.type === "currency")?.value ?? "$";
}

// A value on the axis, short: "1.2K", "$0.25", "3 min" (in Chinese "1.2萬", "US$0.25", "3 分鐘").
export function formatTick(v: number, kind: Kind): string {
  if (kind === "usd") {
    const dollars = v / 1_000_000;
    const sign = dollarSign();
    if (v === 0) return `${sign}0`;
    if (dollars >= 1000) return `${sign}${compact(dollars)}`;
    if (dollars >= 1) return `${sign}${Number(dollars.toFixed(2))}`;
    return `${sign}${Number(dollars.toPrecision(2))}`;
  }
  if (kind === "duration") return duration(v);
  if (kind === "bytes") return v ? bytes(v) : "0";
  return compact(v);
}

export const SLOTS = 6;   // --series-1 to --series-6 in styles.css; "Other" has its own grey

// Colour slots that follow each line, not its rank: a line keeps its slot from the last answer while it is
// still there, a new one takes the first free slot. `kept` maps a line's id to its slot and is updated.
export function assignSlots(ids: string[], kept: Map<string, number>): Map<string, number> {
  const out = new Map<string, number>();
  const used = new Set<number>();
  for (const id of ids) {
    const slot = kept.get(id);
    if (slot !== undefined && !used.has(slot)) {
      out.set(id, slot);
      used.add(slot);
    }
  }
  for (const id of ids) {
    if (out.has(id)) continue;
    let slot = 0;
    while (used.has(slot) && slot < SLOTS - 1) slot++;
    out.set(id, slot);
    used.add(slot);
  }
  kept.clear();
  out.forEach((v, k) => kept.set(k, v));
  return out;
}

// Is this the answer of /api/usage/series? (An older server, or a stand-in, answers something else.)
export function isSeries(x: unknown): x is SeriesResponse {
  const r = x as SeriesResponse | null;
  return !!r && Array.isArray(r.periods) && Array.isArray(r.series) && Array.isArray(r.total);
}

// The name of a row or line as the reader reads it (#189). The server names a few groups itself, in English
// ("Console playground", "Other (3)", "deleted key #4"); in Chinese those get their translation, every other
// name (models, keys, people, providers, days) stays as it is.
const ROW_NAME: Record<string, [string, "usage.row.total" | "usage.row.console" | "usage.row.api" | "usage.row.local" | "usage.row.deleted" | "usage.row.noTeam"]> = {
  total: ["Total", "usage.row.total"], console: ["Console playground", "usage.row.console"], api: ["API keys", "usage.row.api"],
  local: ["Local replicas", "usage.row.local"], deleted: ["Deleted providers", "usage.row.deleted"], none: ["No team", "usage.row.noTeam"],
};
export function rowName(id: string, name: string, lang: Lang = getLang()): string {
  if (lang === "en") return name;
  const fixed = ROW_NAME[id];
  if (fixed && fixed[0] === name) return translate(lang, fixed[1]);
  let m = /^Other \((\d+)\)$/.exec(name);
  if (id === "other" && m) return translate(lang, "usage.row.other", { count: Number(m[1]) });
  if ((m = /^deleted key #(\d+)$/.exec(name))) return translate(lang, "usage.row.deletedKey", { id: m[1] });
  if ((m = /^user #(\d+)$/.exec(name))) return translate(lang, "usage.row.user", { id: m[1] });
  if ((m = /^deleted provider model #(\d+)$/.exec(name))) return translate(lang, "usage.row.deletedModel", { id: m[1] });
  // 1.6.1 (#190): teams the server names itself.
  if ((m = /^deleted team #(\d+)$/.exec(name))) return translate(lang, "usage.row.deletedTeam", { id: m[1] });
  if ((m = /^team key \(deleted team #(\d+)\)$/.exec(name))) return translate(lang, "usage.row.teamKeyDeleted", { id: m[1] });
  if ((m = /^team key \((.+)\)$/.exec(name))) return translate(lang, "usage.row.teamKey", { name: m[1] });
  return name;
}
