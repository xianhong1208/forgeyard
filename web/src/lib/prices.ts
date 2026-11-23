// Cost and prices (1.2): the server keeps micro-USD (costs) and micro-USD per million tokens (prices).
// Money stays in US dollars in every language, written the reader's way by Intl ("$1.20", "US$1.20").
import { labels, t } from "../i18n/core";
import { formatNumber } from "../i18n/format";
import { getLang } from "../i18n/lang";

export const PRICE_FIELDS = ["price_input", "price_output", "price_cache_read", "price_cache_write"] as const;
export type PriceField = (typeof PRICE_FIELDS)[number];
export type PriceSet = Record<PriceField, number | null>;

export const PRICE_LABEL: Record<PriceField, string> = labels({
  price_input: "cloud.price.label.price_input",
  price_output: "cloud.price.label.price_output",
  price_cache_read: "cloud.price.label.price_cache_read",
  price_cache_write: "cloud.price.label.price_cache_write",
});

function trim(text: string, min: number): string {
  const [whole, frac = ""] = text.split(".");
  let f = frac.replace(/0+$/, "");
  if (f.length < min) f = f.padEnd(min, "0");
  return f ? `${whole}.${f}` : whole;
}

// US dollars with at least 2 and at most `digits` decimals, as Intl writes them in the reader's language.
function dollarsText(dollars: number, digits: number, grouping = true): string {
  return formatNumber(dollars, getLang(), {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: digits, useGrouping: grouping,
  });
}

// A cost in micro-USD: "$0.000185" below a cent, "$0.1234" below a dollar, "$1,234.56" above.
export function usd(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  const dollars = micros / 1_000_000;
  if (micros === 0) return dollarsText(0, 2);
  if (micros < 10_000) return dollarsText(dollars, 6);
  if (micros < 1_000_000) return dollarsText(dollars, 4);
  return dollarsText(dollars, 2);
}

// A price in micro-USD per million tokens, as USD per million: "$2.50", "$0.075", "free" (no grouping: "$1000.00").
export function perMillion(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  if (micros === 0) return t("cloud.price.free");
  return dollarsText(micros / 1_000_000, 6, false);
}

// What an admin typed (USD per million tokens) to micro-USD per million; "" is unknown (null).
export function parsePrice(text: string): number | null | "bad" {
  const t = text.trim().replace(/^\$/, "");
  if (!t) return null;
  if (!/^\d+(\.\d{0,6})?$/.test(t)) return "bad";
  const [whole, frac = ""] = t.split(".");
  return Number(whole) * 1_000_000 + Number(frac.padEnd(6, "0"));
}

// The other way, for an input field.
export function priceText(micros: number | null | undefined): string {
  return micros === null || micros === undefined ? "" : trim((micros / 1_000_000).toFixed(6), 0);
}

export function share(part: number, whole: number): string {
  if (!whole) return "—";
  const pct = (part / whole) * 100;
  return `${pct > 0 && pct < 1 ? "<1" : Math.round(pct)}%`;
}

export const SOURCE_LABEL: Record<string, string> = labels({ manual: "cloud.price.source.manual", openrouter: "cloud.price.source.openrouter" });
