// Cost and prices (1.2): the server keeps micro-USD (costs) and micro-USD per million tokens (prices).

export const PRICE_FIELDS = ["price_input", "price_output", "price_cache_read", "price_cache_write"] as const;
export type PriceField = (typeof PRICE_FIELDS)[number];
export type PriceSet = Record<PriceField, number | null>;

export const PRICE_LABEL: Record<PriceField, string> = {
  price_input: "Input",
  price_output: "Output",
  price_cache_read: "Cached input",
  price_cache_write: "Cache write",
};

function trim(text: string, min: number): string {
  const [whole, frac = ""] = text.split(".");
  let f = frac.replace(/0+$/, "");
  if (f.length < min) f = f.padEnd(min, "0");
  return f ? `${whole}.${f}` : whole;
}

// A cost in micro-USD: "$0.000185" below a cent, "$0.1234" below a dollar, "$1,234.56" above.
export function usd(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  if (micros === 0) return "$0.00";
  const dollars = micros / 1_000_000;
  if (micros < 10_000) return `$${trim(dollars.toFixed(6), 2)}`;
  if (micros < 1_000_000) return `$${trim(dollars.toFixed(4), 2)}`;
  return `$${dollars.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// A price in micro-USD per million tokens, as USD per million: "$2.50", "$0.075", "free".
export function perMillion(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "—";
  if (micros === 0) return "free";
  return `$${trim((micros / 1_000_000).toFixed(6), 2)}`;
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

export const SOURCE_LABEL: Record<string, string> = { manual: "entered by hand", openrouter: "from OpenRouter" };
