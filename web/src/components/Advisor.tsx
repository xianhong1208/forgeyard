import { IconAlertTriangle, IconCircleCheck, IconInfoCircle, IconStethoscope } from "@tabler/icons-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router";

import { api, errorText } from "../lib/api";
import { SEVERITY_LABEL, backupKid, fixHref, forPage, summary, worst, type AdvisorReport, type Finding, type Severity } from "../lib/advisor";

// The setup advisor for admins: one report, fetched by the shell and shared by the nav badge, the
// Get started panel and the page banners. Plain users never ask for it (the API refuses them).
export const ADVISOR_POLL_MS = 60_000;

type AdvisorState = {
  report: AdvisorReport | null;
  error: string | null;
  reload: () => void;
  snooze: (key: string, snooze: boolean) => Promise<void>;
  confirmBackup: (kid: string) => Promise<void>;
};

const AdvisorContext = createContext<AdvisorState | null>(null);

export function useAdvisor(): AdvisorState | null {
  return useContext(AdvisorContext);
}

export function AdvisorProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const [report, setReport] = useState<AdvisorReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const { pathname } = useLocation();
  const reload = useCallback(() => setTick((t) => t + 1), []);

  // Again on every page change, so a fix made on one page clears its finding on the next.
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api<AdvisorReport>("/advisor").then(
      (r) => {
        if (!alive) return;
        setReport(r);
        setError(null);
      },
      (e) => {
        if (alive) setError(errorText(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [enabled, tick, pathname]);

  useEffect(() => {
    if (!enabled) return;
    const id = window.setInterval(() => {
      if (document.visibilityState !== "hidden") reload();
    }, ADVISOR_POLL_MS);
    return () => window.clearInterval(id);
  }, [enabled, reload]);

  const snooze = useCallback(async (key: string, on: boolean) => {
    setReport(await api<AdvisorReport>("/advisor/snooze", { method: "POST", body: { key, snooze: on } }));
  }, []);
  const confirmBackup = useCallback(async (kid: string) => {
    setReport(await api<AdvisorReport>("/advisor/secret-key-backup", { method: "POST", body: { kid } }));
  }, []);

  const value = useMemo(
    () => (enabled ? { report, error, reload, snooze, confirmBackup } : null),
    [enabled, report, error, reload, snooze, confirmBackup],
  );
  return <AdvisorContext.Provider value={value}>{children}</AdvisorContext.Provider>;
}

function SeverityIcon({ s, size = 16 }: { s: Severity; size?: number }) {
  const Icon = s === "info" ? IconInfoCircle : IconAlertTriangle;
  return <Icon size={size} stroke={1.9} aria-hidden="true" />;
}

// In the top bar: errors plus warnings that are not snoozed. Nothing when there are none.
export function HealthBadge({ onClick }: { onClick?: () => void }) {
  const a = useAdvisor();
  const r = a?.report;
  if (!r || r.badge === 0) return null;
  const tone = r.counts.error ? "error" : "warning";
  return (
    <Link to="/start#setup-health" className={`health-badge health-${tone}`} onClick={onClick}
      aria-label={`Setup health: ${summary({ ...r.counts, info: 0 })}`} title={`Setup health: ${summary(r.counts)}`}>
      <IconStethoscope size={17} stroke={1.8} aria-hidden="true" />
      <span className="health-count">{r.badge}</span>
    </Link>
  );
}

function FindingActions({ f, compact }: { f: Finding; compact?: boolean }) {
  const a = useAdvisor();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!a) return null;
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setProblem(null);
    try {
      await fn();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const kid = backupKid(f);
  const days = a.report?.snooze_days ?? 30;
  return (
    <div className="finding-actions">
      <Link to={fixHref(f.fix_url)} className={`btn btn-small${compact ? " btn-quiet" : ""}`}>{f.fix_label}</Link>
      {kid && (
        <button type="button" className="btn btn-small" disabled={busy} onClick={() => void run(() => a.confirmBackup(kid))}>
          <IconCircleCheck size={15} aria-hidden="true" /> I have a backup of key id {kid}
        </button>
      )}
      {f.snoozed_until ? (
        <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void run(() => a.snooze(f.key, false))}>
          Show again
        </button>
      ) : f.snoozable ? (
        <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void run(() => a.snooze(f.key, true))}
          title={`Hide it for you for ${days} days; it comes back if it still applies`}>
          Snooze {days} days
        </button>
      ) : (
        <span className="muted small">Errors stay until fixed</span>
      )}
      {problem && <span className="field-error">{problem}</span>}
    </div>
  );
}

function FindingRow({ f }: { f: Finding }) {
  return (
    <li className={`finding finding-${f.severity}`} data-key={f.key}>
      <span className="finding-icon"><SeverityIcon s={f.severity} /></span>
      <div className="finding-body">
        <p className="finding-title">
          <span className={`chip chip-sev-${f.severity}`}>{SEVERITY_LABEL[f.severity]}</span> <strong>{f.title}</strong>
        </p>
        <p className="finding-detail">{f.detail}</p>
        {f.snoozed_until && <p className="muted small">Snoozed until {f.snoozed_until.slice(0, 10)}.</p>}
        <FindingActions f={f} />
      </div>
    </li>
  );
}

// Get started: every finding, with the snoozed ones behind a toggle.
export function SetupHealth() {
  const a = useAdvisor();
  const [showSnoozed, setShowSnoozed] = useState(false);
  const { hash } = useLocation();
  const loaded = Boolean(a?.report);
  // The nav badge links to /start#setup-health: scroll there once the report is in.
  useEffect(() => {
    if (loaded && hash === "#setup-health") document.getElementById("setup-health")?.scrollIntoView?.({ block: "start" });
  }, [loaded, hash]);
  if (!a) return null;
  const r = a.report;
  return (
    <section className="panel health" id="setup-health" aria-labelledby="health-title">
      <div className="panel-head">
        <h2 id="health-title">Setup health</h2>
        {r && <span className={`health-summary health-summary-${worst(r.findings) ?? "ok"}`}>{summary(r.counts)}</span>}
      </div>
      <p className="muted small">
        Settings and combinations that are broken, risky or useless, checked from this server's settings, its machines, models,
        keys and providers. Only admins see this. Warnings and notes can be snoozed for you alone; errors stay until they are fixed.
      </p>
      {a.error && !r && <p className="field-error">{a.error}</p>}
      {!r && !a.error && <p className="muted">Checking…</p>}
      {r && r.findings.length === 0 && (
        <p className="health-ok"><IconCircleCheck size={18} aria-hidden="true" /> Nothing to look at.</p>
      )}
      {r && r.findings.length > 0 && <ul className="findings">{r.findings.map((f) => <FindingRow key={f.key} f={f} />)}</ul>}
      {r && r.snoozed.length > 0 && (
        <div className="health-snoozed">
          <button type="button" className="btn btn-quiet btn-small" aria-expanded={showSnoozed} onClick={() => setShowSnoozed((v) => !v)}>
            {showSnoozed ? "Hide" : "Show"} {r.snoozed.length} snoozed
          </button>
          {showSnoozed && <ul className="findings">{r.snoozed.map((f) => <FindingRow key={f.key} f={f} />)}</ul>}
        </div>
      )}
    </section>
  );
}

// A page's own findings, at its top: the first few, then a link to the rest.
export const BANNER_MAX = 3;

export function AdvisorBanner({ pages }: { pages: string[] }) {
  const a = useAdvisor();
  const found = a?.report ? forPage(a.report.findings, pages) : [];
  const tone = worst(found);
  if (!tone) return null;
  return (
    <section className={`advisor-banner advisor-${tone}`} aria-label="Setup health">
      <ul>
        {found.slice(0, BANNER_MAX).map((f) => (
          <li key={f.key} data-key={f.key}>
            <span className="finding-icon"><SeverityIcon s={f.severity} /></span>
            <div className="finding-body">
              <p className="finding-title"><strong>{f.title}</strong></p>
              <p className="finding-detail">{f.detail}</p>
              <FindingActions f={f} compact />
            </div>
          </li>
        ))}
      </ul>
      {found.length > BANNER_MAX && (
        <p className="small advisor-more">
          And {found.length - BANNER_MAX} more under <Link to="/start#setup-health">Get started → Setup health</Link>.
        </p>
      )}
    </section>
  );
}
