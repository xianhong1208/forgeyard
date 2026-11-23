// Numbers, sizes, durations, dates and plurals in the reader's language, through the browser's Intl.
//
// English keeps the console's compact units ("41 s ago", "5 min", "2 h"): Intl's English has no such short
// form ("41 sec. ago"), and the console has used these since 1.0. The number in them is still Intl's, and
// every other language gets Intl's own words ("41 秒前", "5 分鐘").
import { getLang, locale, type Lang } from "./lang";

const numberFormats = new Map<string, Intl.NumberFormat>();

function nf(lang: Lang, opts: Intl.NumberFormatOptions = {}): Intl.NumberFormat {
  const key = `${lang}|${JSON.stringify(opts)}`;
  let f = numberFormats.get(key);
  if (!f) {
    f = new Intl.NumberFormat(locale(lang), opts);
    numberFormats.set(key, f);
  }
  return f;
}

// Chinese numbers the Taiwan way: a space between the digits and 萬/億/兆 ("10 萬", Intl writes "10萬"), and none
// between 萬/億/兆 and the word after it ("10 萬個 token", not "10萬 個 token").
export function spaceCjkNumbers(s: string): string {
  return s.replace(/(\d)([萬億兆])/g, "$1 $2").replace(/([萬億兆]) (?=[\u4e00-\u9fff])/g, "$1");
}

export function formatNumber(n: number, lang: Lang = getLang(), opts: Intl.NumberFormatOptions = {}): string {
  return nf(lang, opts).format(n);
}

const KIB = 1024;
const MIB = KIB ** 2;
const GIB = KIB ** 3;
const TIB = KIB ** 4;

// 85899345920 -> "80 GiB", 4023779328 -> "3.7 GiB", 536870912 -> "512 MiB". The units are the same in every
// language; the number follows the reader's (digits, decimal mark).
export function formatBytes(n: number | null | undefined, lang: Lang = getLang()): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  // No grouping: a size stays one short number ("1000 MiB" just before 1 GiB).
  const one = (v: number) => nf(lang, { maximumFractionDigits: v >= 100 ? 0 : 1, useGrouping: false }).format(v);
  const whole = (v: number) => nf(lang, { maximumFractionDigits: 0, useGrouping: false }).format(Math.round(v));
  if (n >= TIB) return `${one(n / TIB)} TiB`;
  if (n >= GIB) return `${one(n / GIB)} GiB`;
  if (n >= MIB) return `${whole(n / MIB)} MiB`;
  return `${whole(n / KIB)} KiB`;
}

type Unit = "second" | "minute" | "hour" | "day";
const EN_UNIT: Record<Unit, string> = { second: "s", minute: "min", hour: "h", day: "d" };

// The largest unit that keeps the number readable: 41 s, 5 min, 3 h, 2 d.
function split(seconds: number): [number, Unit] {
  const abs = Math.abs(seconds);
  if (abs < 60) return [Math.round(abs), "second"];
  if (abs < 3600) return [Math.round(abs / 60), "minute"];
  if (abs < 86400) return [Math.round(abs / 3600), "hour"];
  return [Math.round(abs / 86400), "day"];
}

// A length of time: "41 s", "5 min" / "41 秒", "5 分鐘".
export function formatDuration(seconds: number, lang: Lang = getLang()): string {
  const [n, unit] = split(seconds);
  if (lang === "en") return `${nf(lang).format(n)} ${EN_UNIT[unit]}`;
  return nf(lang, { style: "unit", unit, unitDisplay: "short" }).format(n);
}

// How long ago (or how far ahead, for a negative age): "41 s ago", "in 5 min" / "41 秒前", "5 分鐘後".
export function formatAgo(seconds: number | null, lang: Lang = getLang()): string {
  if (seconds === null) return lang === "en" ? "never" : "從未";
  const [n, unit] = split(seconds);
  if (lang === "en") return seconds >= 0 ? `${nf(lang).format(n)} ${EN_UNIT[unit]} ago` : `in ${nf(lang).format(n)} ${EN_UNIT[unit]}`;
  return new Intl.RelativeTimeFormat(locale(lang), { numeric: "always" }).format(seconds >= 0 ? -n : n, unit);
}

export function parseServerTime(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
}

// A server timestamp relative to now, as the console shows them: "just now", "5 min ago", "3 d ago", and the
// date itself past two weeks. zh-TW: "剛剛", "5 分鐘前", "2026年11月9日".
export function formatRelative(value: string | null | undefined, lang: Lang = getLang(), now: number = Date.now()): string {
  if (!value) return "—";
  const d = parseServerTime(value);
  const seconds = Math.round((now - d.getTime()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) {
    if (lang === "en") return seconds >= 0 ? "just now" : "in a moment";
    return seconds >= 0 ? "剛剛" : "稍後";
  }
  if (abs >= 86400 * 14) return formatDate(value, lang);
  // Past 45 s it counts minutes (1.0's rule): "1 min ago", never "50 s ago".
  const [n, unit]: [number, Unit] = abs < 3600 ? [Math.max(1, Math.round(abs / 60)), "minute"]
    : abs < 86400 ? [Math.round(abs / 3600), "hour"] : [Math.round(abs / 86400), "day"];
  if (lang === "en") return `${nf(lang).format(n)} ${EN_UNIT[unit]} ${seconds >= 0 ? "ago" : "from now"}`;
  return new Intl.RelativeTimeFormat(locale(lang), { numeric: "always" }).format(seconds >= 0 ? -n : n, unit);
}

// "Nov 9, 2026" / "2026年11月9日".
export function formatDate(value: string | Date, lang: Lang = getLang()): string {
  const d = typeof value === "string" ? parseServerTime(value) : value;
  return d.toLocaleDateString(locale(lang), { year: "numeric", month: "short", day: "numeric" });
}

// The full date and time, for a tooltip.
export function formatDateTime(value: string | Date | null | undefined, lang: Lang = getLang()): string {
  if (!value) return "";
  const d = typeof value === "string" ? parseServerTime(value) : value;
  return d.toLocaleString(locale(lang));
}

// "14:05" / "下午02:05": a time of day in the reader's convention.
export function formatTime(value: string | Date, lang: Lang = getLang()): string {
  const d = typeof value === "string" ? new Date(value) : value;
  return d.toLocaleTimeString(locale(lang), { hour: "2-digit", minute: "2-digit" });
}

// Which plural form a count takes: "one" or "other" in English, always "other" in Chinese.
export function pluralForm(count: number, lang: Lang = getLang()): Intl.LDMLPluralRule {
  return new Intl.PluralRules(locale(lang)).select(count);
}
