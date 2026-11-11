// The setup advisor (GET /api/advisor, admins only): findings about the setup, with where to fix them.

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

export const SEVERITY_LABEL: Record<Severity, string> = { error: "Error", warning: "Warning", info: "Note" };

// The findings a page shows as its banner: those tagged with the page ("keys") or with one of its
// objects ("route:4").
export function forPage(findings: Finding[], pages: string[]): Finding[] {
  return findings.filter((f) => f.pages.some((p) => pages.includes(p)));
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
  if (counts.error) parts.push(`${counts.error} error${counts.error === 1 ? "" : "s"}`);
  if (counts.warning) parts.push(`${counts.warning} warning${counts.warning === 1 ? "" : "s"}`);
  if (counts.info) parts.push(`${counts.info} note${counts.info === 1 ? "" : "s"}`);
  return parts.length ? parts.join(", ") : "Nothing to look at";
}
