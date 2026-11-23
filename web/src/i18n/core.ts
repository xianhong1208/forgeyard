// t(): one string of the console in the reader's language. The keys are the English dictionary's, so
// TypeScript refuses a key that does not exist, a missing {placeholder} value and an unknown one.
import { en, zhTW, type Key } from "./dictionaries";
import { formatNumber, pluralForm, spaceCjkNumbers } from "./format";
import { getLang, type Lang } from "./lang";

type En = typeof en;
export type { Key };

// The {names} in a string, and the <tags> of rich text.
export type Placeholders<S> = S extends `${string}{${infer P}}${infer R}` ? P | Placeholders<R> : never;
export type Tags<S> = S extends `${string}<${infer T}>${infer R}` ? (T extends `/${string}` ? Tags<R> : T | Tags<R>) : never;

export type VarValue = string | number;
export type Vars<K extends Key> = Record<Placeholders<En[K]>, VarValue>;
// No second argument for a string without placeholders; the exact set for one with them.
export type VarsArg<K extends Key> = [Placeholders<En[K]>] extends [never] ? [vars?: undefined] : [vars: Vars<K>];

// The base of a plural key: "machines.unused" for "machines.unused.one" / "machines.unused.other".
export type PluralBase = Key extends infer K ? (K extends `${infer B}.other` ? B : never) : never;
type PluralKey<B extends PluralBase> = Extract<`${B}.one` | `${B}.other`, Key>;
export type PluralVars<B extends PluralBase> = Omit<Record<Placeholders<En[PluralKey<B>]>, VarValue>, "count">;

const DICTS: Record<Lang, Record<string, string>> = { en, "zh-TW": zhTW };

export function hasKey(key: string): key is Key {
  return Object.prototype.hasOwnProperty.call(en, key);
}

// {name} -> its value; numbers in the reader's digits. A name with no value stays as it is, so a mistake
// shows on screen instead of an empty gap.
export function interpolate(s: string, vars: Record<string, unknown> | undefined, lang: Lang): string {
  if (!vars) return s;
  const out = s.replace(/\{(\w+)\}/g, (whole, name: string) => {
    const v = vars[name];
    if (v === undefined || v === null) return whole;
    return typeof v === "number" ? formatNumber(v, lang) : String(v);
  });
  return lang === "zh-TW" ? spaceCjkNumbers(out) : out;
}

// The raw string for a key: the language's, else the English one (never expected: the parity test and the
// types keep both complete), else the key itself.
export function lookup(lang: Lang, key: string): string {
  return DICTS[lang][key] ?? DICTS.en[key] ?? key;
}

export function translate<K extends Key>(lang: Lang, key: K, ...args: VarsArg<K>): string {
  return interpolate(lookup(lang, key), args[0] as Record<string, unknown> | undefined, lang);
}

// "1 unused join token" / "2 unused join tokens": the form Intl.PluralRules picks, {count} filled in.
export function translatePlural<B extends PluralBase>(lang: Lang, base: B, count: number, vars?: PluralVars<B>): string {
  const form = `${base}.${pluralForm(count, lang)}`;
  const key = hasKey(form) ? form : `${base}.other`;
  return interpolate(lookup(lang, key), { ...(vars ?? {}), count }, lang);
}

// A key made from a code the server sent: its text, or the fallback (what the server said) without one.
export function translateMaybe(lang: Lang, key: string, fallback: string, vars?: Record<string, VarValue>): string {
  return hasKey(key) ? interpolate(lookup(lang, key), vars, lang) : fallback;
}

export function tMaybe(key: string, fallback: string, vars?: Record<string, VarValue>): string {
  return translateMaybe(getLang(), key, fallback, vars);
}

// Outside React (lib functions called while a page renders): the language in use now.
export function t<K extends Key>(key: K, ...args: VarsArg<K>): string {
  return translate(getLang(), key, ...args);
}

export function tPlural<B extends PluralBase>(base: B, count: number, vars?: PluralVars<B>): string {
  return translatePlural(getLang(), base, count, vars);
}

// A record whose values are looked up when read, for tables of labels that pages index and iterate
// (MACHINE_KIND[kind].label, Object.entries(TASK_LABEL)): they follow the language without a re-import.
export function labels<R extends string>(keys: Record<R, Key>): Record<R, string> {
  const out = {} as Record<R, string>;
  for (const k of Object.keys(keys) as R[]) {
    Object.defineProperty(out, k, { enumerable: true, get: () => lookup(getLang(), keys[k]) });
  }
  return out;
}
