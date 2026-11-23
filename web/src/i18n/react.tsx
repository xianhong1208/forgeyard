// The language in React: useLang() follows the stored choice (no provider: any component can ask), and
// useT() gives t() for that language, plus plurals, rich text and the Intl helpers.
import { Fragment, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { interpolate, lookup, translate, translateMaybe, translatePlural, type Key, type PluralBase, type PluralVars, type Placeholders, type Tags,
  type VarValue, type VarsArg } from "./core";
import type { en } from "./dictionaries";
import { formatAgo, formatBytes, formatDate, formatDateTime, formatDuration, formatNumber, formatRelative, formatTime } from "./format";
import { applyHtmlLang, getLang, subscribeLang, type Lang } from "./lang";

export function useLang(): Lang {
  return useSyncExternalStore(subscribeLang, () => getLang(), () => "en");
}

// Rich text: "Did your browser warn? <link>Trust this server</link> once." with link: (text) => <Link…>.
// A {name} may be any node (a <code>, a <strong>); a <tag> gets its text and returns the node to show.
export type RichParts<K extends Key> = Record<Placeholders<(typeof en)[K]>, ReactNode> &
  Record<Tags<(typeof en)[K]>, (text: string) => ReactNode>;

export function richText(lang: Lang, key: Key, parts: Record<string, unknown>): ReactNode {
  const s = lookup(lang, key);
  const out: ReactNode[] = [];
  const re = /<(\w+)>(.*?)<\/\1>|\{(\w+)\}/g;
  let last = 0;
  let i = 0;
  for (let m = re.exec(s); m; m = re.exec(s)) {
    if (m.index > last) out.push(s.slice(last, m.index));
    if (m[1]) {
      const fn = parts[m[1]];
      const text = interpolate(m[2], parts, lang);
      out.push(<Fragment key={i++}>{typeof fn === "function" ? (fn as (t: string) => ReactNode)(text) : text}</Fragment>);
    } else {
      const v = parts[m[3]];
      out.push(<Fragment key={i++}>{v === undefined ? m[0] : typeof v === "number" ? formatNumber(v, lang) : (v as ReactNode)}</Fragment>);
    }
    last = re.lastIndex;
  }
  if (last < s.length) out.push(s.slice(last));
  return <>{out}</>;
}

export type Translator = {
  <K extends Key>(key: K, ...vars: VarsArg<K>): string;
  lang: Lang;
  plural<B extends PluralBase>(base: B, count: number, vars?: PluralVars<B>): string;
  rich<K extends Key>(key: K, parts: RichParts<K>): ReactNode;
  // A key built from a code ("common.status." + status): its text, or the fallback when there is no such key.
  maybe(key: string, fallback: string, vars?: Record<string, VarValue>): string;
  num(n: number, opts?: Intl.NumberFormatOptions): string;
  bytes(n: number | null | undefined): string;
  duration(seconds: number): string;
  ago(seconds: number | null): string;
  relative(value: string | null | undefined): string;
  date(value: string | Date): string;
  dateTime(value: string | Date | null | undefined): string;
  time(value: string | Date): string;
};

export function makeTranslator(lang: Lang): Translator {
  const t = (<K extends Key>(key: K, ...vars: VarsArg<K>) => translate(lang, key, ...vars)) as Translator;
  t.lang = lang;
  t.plural = (base, count, vars) => translatePlural(lang, base, count, vars);
  t.rich = (key, parts) => richText(lang, key, parts as Record<string, unknown>);
  t.maybe = (key, fallback, vars) => translateMaybe(lang, key, fallback, vars);
  t.num = (n, opts) => formatNumber(n, lang, opts);
  t.bytes = (n) => formatBytes(n, lang);
  t.duration = (s) => formatDuration(s, lang);
  t.ago = (s) => formatAgo(s, lang);
  t.relative = (v) => formatRelative(v, lang);
  t.date = (v) => formatDate(v, lang);
  t.dateTime = (v) => formatDateTime(v, lang);
  t.time = (v) => formatTime(v, lang);
  return t;
}

export function useT(): Translator {
  const lang = useLang();
  return useMemo(() => makeTranslator(lang), [lang]);
}

// Once, at the root: <html lang> follows the language (also on a change in another tab).
export function HtmlLang() {
  const lang = useLang();
  useEffect(() => applyHtmlLang(lang), [lang]);
  return null;
}
