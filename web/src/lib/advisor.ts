// The setup advisor (GET /api/advisor, admins only): findings about the setup, with where to fix them.

import { hasKey, labels, lookup, t, tMaybe, tPlural, translateMaybe } from "../i18n/core";
import { formatDate, formatTime, pluralForm } from "../i18n/format";
import { getLang, type Lang } from "../i18n/lang";
import { guideHref, type GuideLang } from "./guide";

export type Severity = "error" | "warning" | "info";

export type Finding = {
  id: string;
  key: string;
  severity: Severity;
  title: string;
  detail: string;
  fix_url: string;
  fix_label: string;
  scope: string;
  pages: string[];
  snoozable: boolean;
  snoozed_until?: string;
  // 1.6.0 (#39): what the detail is made of, for findings whose detail the console writes in another language.
  facts?: Record<string, unknown>;
};

export type AdvisorReport = {
  findings: Finding[];
  snoozed: Finding[];
  counts: Record<Severity, number>;
  badge: number;
  snooze_days: number;
  checked_at: string;
};

/** A report as the advisor API sends it, or null: anything else (an older server, a proxy's error page
 * answered with 200, a stand-in in tests) never reaches the badge or the panels. */
export function asReport(value: unknown): AdvisorReport | null {
  const r = value as Partial<AdvisorReport> | null;
  if (!r || typeof r !== "object" || !Array.isArray(r.findings) || typeof r.badge !== "number") return null;
  const c = r.counts as Partial<Record<Severity, unknown>> | undefined;
  if (!c || typeof c !== "object" || !["error", "warning", "info"].every((k) => typeof c[k as Severity] === "number")) return null;
  return { ...r, snoozed: Array.isArray(r.snoozed) ? r.snoozed : [] } as AdvisorReport;
}

export const SEVERITY_LABEL: Record<Severity, string> = labels({ error: "advisor.severity.error", warning: "advisor.severity.warning", info: "advisor.severity.info" });

// The findings a page shows as its banner: those tagged with the page ("keys") or with one of its
// objects ("route:4").
// A machine's own page ("machine:3", 1.4) shows the findings about that machine and its cards.
export function forPage(findings: Finding[], pages: string[]): Finding[] {
  const machines = pages.filter((p) => p.startsWith("machine:")).map((p) => p.slice("machine:".length));
  return findings.filter((f) => f.pages.some((p) => pages.includes(p))
    || machines.some((id) => f.scope === `machine:${id}` || f.scope.startsWith(`gpu:${id}:`)));
}

// The worst severity among them, for the banner's tone.
export function worst(findings: Finding[]): Severity | null {
  for (const s of ["error", "warning", "info"] as Severity[]) if (findings.some((f) => f.severity === s)) return s;
  return null;
}

// The server writes guide links in English; open them in the reader's chosen language.
export function fixHref(url: string, lang?: GuideLang): string {
  const m = /^\/guide\/en\/([a-z-]+)(?:#(.+))?$/.exec(url);
  if (!m) return url;
  return lang ? guideHref(m[1], m[2], lang) : guideHref(m[1], m[2]);
}

// The key id of the secret key a backup reminder is about (scope "secret-key:<id>").
export function backupKid(f: Finding): string | null {
  return f.id === "secret-key-backup" && f.scope.startsWith("secret-key:") ? f.scope.slice("secret-key:".length) : null;
}

export function summary(counts: Record<Severity, number>): string {
  const parts: string[] = [];
  if (counts.error) parts.push(tPlural("advisor.count.error", counts.error));
  if (counts.warning) parts.push(tPlural("advisor.count.warning", counts.warning));
  if (counts.info) parts.push(tPlural("advisor.count.info", counts.info));
  return parts.length ? parts.join(t("advisor.listJoin")) : t("advisor.nothingShort");
}

// A finding in the reader's language (#189): the server writes them in English and the API does not change, so
// another language names what the finding is about by its id, and keeps the server's own words (with its
// machine and model names) as the detail. English: the server's title as sent.
export function findingTitle(f: Pick<Finding, "id" | "title">, lang: Lang = getLang()): { title: string; translated: boolean } {
  const key = `advisor.finding.${f.id}`;
  if (lang === "en" || !hasKey(key)) return { title: f.title, translated: false };
  return { title: lookup(lang, key), translated: true };
}

// "tony, uma, vera and 2 more" / "tony、uma、vera 和另外 2 人", as the server names people (at most three).
function names(list: string[], lang: Lang): string {
  const shown = list.slice(0, 3).join(lang === "en" ? ", " : "、");
  if (list.length <= 3) return shown;
  return interpolateIn(lang, "advisor.names.more", { names: shown, more: list.length - 3 });
}

function interpolateIn(lang: Lang, key: string, vars: Record<string, string | number>): string {
  return translateMaybe(lang, key, key, vars);
}

function plural(lang: Lang, base: string, count: number, vars: Record<string, string | number>): string {
  const key = `${base}.${pluralForm(count, lang)}`;
  return interpolateIn(lang, hasKey(key) ? key : `${base}.other`, { count, ...vars });
}

const strings = (v: unknown): string[] | null => (Array.isArray(v) && v.every((x) => typeof x === "string") ? v : null);

// A finding's detail in the reader's language, for the findings that send what it is made of (`facts`); else the
// server's English words, as sent (translated: false).
export function findingDetail(f: Pick<Finding, "id" | "detail" | "facts">, lang: Lang = getLang()): { detail: string; translated: boolean } {
  const facts = f.facts ?? {};
  if (lang !== "en" && f.id === "admins-without-mfa") {
    const admins = strings(facts.admins);
    if (admins?.length) {
      const text = plural(lang, "advisor.detail.adminsWithoutMfa", admins.length, { names: names(admins, lang) });
      return { detail: facts.required ? `${text}${lookup(lang, "advisor.detail.adminsWithoutMfa.required")}` : text, translated: true };
    }
  }
  if (lang !== "en" && f.id === "mfa-setup-pending") {
    const waiting = strings(facts.waiting);
    const overdue = strings(facts.overdue);
    if (waiting && overdue) {
      const first = typeof facts.first_ends === "string" ? facts.first_ends : null;
      const parts = [
        waiting.length && first ? plural(lang, "advisor.detail.mfaPending.waiting", waiting.length,
          { names: names(waiting, lang), date: `${formatDate(first, lang)} ${formatTime(first, lang)}` }) : "",
        overdue.length ? plural(lang, "advisor.detail.mfaPending.overdue", overdue.length, { names: names(overdue, lang) }) : "",
        lookup(lang, "advisor.detail.mfaPending.tail"),
      ];
      return { detail: parts.filter(Boolean).join(""), translated: true };
    }
  }
  return { detail: f.detail, translated: false };
}

// The fix button's words: the server's labels are a small fixed set.
export function fixLabel(label: string): string {
  return tMaybe(`advisor.fix.${label}`, label);
}

