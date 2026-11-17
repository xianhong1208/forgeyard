// Usage over time (1.4): the numbers behind the Usage page's line charts (GET /api/usage/series) and the
// helpers that turn them into axes, labels and colours.

import { num } from "./gateway";
import { bytes } from "./machines";
import { usd } from "./prices";

export type Step = "day" | "week" | "month";
export type Split = "none" | "key" | "model" | "provider" | "user";

export type SeriesLine = { id: string; name: string; values: number[]; total: number; other?: boolean; cloud?: boolean };
export type Period = { start: string; end: string; partial: boolean };

export type SeriesResponse = {
  step: Step; split: Split; metric: string; from: string; to: string; periods: Period[]; series: SeriesLine[];
  total: number[]; totals: Record<string, number>; omitted: number; everyone: boolean; is_admin: boolean;
};

// How far back each view looks: 30 days, 13 weeks (a quarter), 12 months.
export const VIEWS: { step: Step; label: string; periods: number; range: string }[] = [
  { step: "day", label: "Daily", periods: 30, range: "the last 30 days" },
  { step: "week", label: "Weekly", periods: 13, range: "the last 13 weeks" },
  { step: "month", label: "Monthly", periods: 12, range: "the last 12 months" },
];

export const SPLITS: { id: Split; label: string; admin?: boolean }[] = [
  { id: "none", label: "Total" },
  { id: "key", label: "Key" },
  { id: "model", label: "Model" },
  { id: "provider", label: "Provider" },
  { id: "user", label: "Person", admin: true },
];

type Kind = "count" | "usd" | "duration" | "bytes";

// What a chart can show. `always`: offered even when nothing was counted yet; the others once the range has some.
export const METRICS: { id: string; label: string; kind: Kind; always?: boolean }[] = [
  { id: "requests", label: "Requests", kind: "count", always: true },
  { id: "tokens", label: "Tokens (in + out)", kind: "count", always: true },
  { id: "prompt_tokens", label: "Tokens in", kind: "count", always: true },
  { id: "completion_tokens", label: "Tokens out", kind: "count", always: true },
  { id: "errors", label: "Errors", kind: "count", always: true },
  { id: "media_items", label: "Media items sent", kind: "count" },
  { id: "characters", label: "Characters spoken", kind: "count" },
  { id: "images", label: "Images made", kind: "count" },
  { id: "media_ms", label: "Audio and video made", kind: "duration" },
  { id: "busy_ms", label: "Busy time", kind: "duration" },
  { id: "bytes_in", label: "App bytes in", kind: "bytes" },
  { id: "bytes_out", label: "App bytes out", kind: "bytes" },
  { id: "cost_micros", label: "Cost (USD)", kind: "usd" },
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

// A period on the axis: "Nov 12" (a day, or the Monday a week starts on), "Nov 2026" (a month).
export function periodLabel(p: Period, step: Step): string {
  const [y, m, d] = parts(p.start);
  return step === "month" ? `${MONTHS[m - 1]} ${y}` : `${MONTHS[m - 1]} ${d}`;
}

// The same in full, for tooltips and the table: "Thu, Nov 12, 2026", "Nov 9 – 15, 2026", "November 2026";
// the period still going on says so.
export function periodTitle(p: Period, step: Step): string {
  const [y, m, d] = parts(p.start);
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
  return p.partial ? `${text} (so far)` : text;
}

function duration(ms: number): string {
  if (!ms) return "0 s";
  const s = ms / 1000;
  if (s < 60) return `${Number(s.toFixed(s < 10 ? 1 : 0))} s`;
  if (s < 3600) return `${Number((s / 60).toFixed(s < 600 ? 1 : 0))} min`;
  return `${Number((s / 3600).toFixed(s < 36000 ? 1 : 0))} h`;
}

const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

// A value in full, for tooltips, the legend and the table.
export function formatValue(v: number, kind: Kind): string {
  if (kind === "usd") return usd(v);
  if (kind === "duration") return duration(v);
  if (kind === "bytes") return v ? bytes(v) : "0";
  return num(v);
}

// A value on the axis, short: "1.2K", "$0.25", "3 min".
export function formatTick(v: number, kind: Kind): string {
  if (kind === "usd") {
    const dollars = v / 1_000_000;
    if (v === 0) return "$0";
    if (dollars >= 1000) return `$${COMPACT.format(dollars)}`;
    if (dollars >= 1) return `$${Number(dollars.toFixed(2))}`;
    return `$${Number(dollars.toPrecision(2))}`;
  }
  if (kind === "duration") return duration(v);
  if (kind === "bytes") return v ? bytes(v) : "0";
  return COMPACT.format(v);
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
