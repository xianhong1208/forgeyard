// 1.5.2 (#95 PR 4): trying out an engine before switching to it. On an engine's page (operators and admins): Try it out
// opens a dialog (a catalog model this engine serves, a machine, the parameters, the estimate and where it fits, the
// time to start); the server then deploys it for a few minutes, isolated (never in /v1/models, no key, no usage),
// checks it and removes it. The Trials section follows a running trial (with Cancel) and lists the history, each
// trial's checks and how it compares with the built-in engine. Small lines for other pages: Serving's trials in
// progress, an engine card's last result, Edit's engine choice.
import { IconChevronDown, IconChevronRight, IconFlask, IconPlayerStop, IconX } from "@tabler/icons-react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { useT, type Translator } from "../i18n/react";
import { ApiError, api, errorText } from "../lib/api";
import { formatNumber } from "../i18n/format";
import { engineHref } from "../lib/pages";
import type { EnginePage, EngineTrial, Recipe, TrialBrief, TrialCheck, TrialCode, TrialDryRun, TrialsResponse } from "../lib/types";
import { Notice } from "./ui";

const POLL_MS = 3000;
const MINUTES = { min: 5, max: 180, default: 30 };
// What the dialog shows as fields: the engine's own parameters that are not advanced (the rest keep their values).
const COMMON = new Set(["gpus", "share", "vram", "extra_args"]);

type Code = TrialCode;

// Memory to two decimals (5.17 GiB against 5.54 GiB reserved: the differences a trial is about), MiB below 1 GiB.
function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  const gib = Math.abs(n) / 1024 ** 3;
  if (gib >= 1) return `${formatNumber(n / 1024 ** 3, undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false })} GiB`;
  return `${formatNumber(Math.round(n / 1024 ** 2), undefined, { useGrouping: false })} MiB`;
}

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

function secs(v: unknown): string {
  const n = Number(v);
  return Number.isFinite(n) ? (n >= 10 ? n.toFixed(0) : n.toFixed(1)) : "—";
}

// A refusal in the reader's language: English as the server wrote it; Chinese by its code, with the server's text.
function trialError(t: Translator, e: unknown): string {
  if (!(e instanceof ApiError)) return errorText(e);
  const sent = e.detail.message ?? "";
  if (t.lang === "en" && sent) return sent;
  return t.maybe(`enginetrial.err.${e.code}`, sent || errorText(e), { message: sent });
}

function punct(t: Translator) {
  return t.lang === "en" ? { colon: ": ", sep: ", ", list: ", ", end: ".", open: " (", close: ")" }
    : { colon: "：", sep: "，", list: "、", end: "。", open: "（", close: "）" };
}

// ------------------------------------------------------------------ the button and the dialog
export function TrialButton({ r, page, onStarted }: { r: Recipe; page: EnginePage; onStarted: (text: string) => void }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const can = page.models.length > 0 && page.machines.some((m) => m.variant) && !page.trial_running;
  return (
    <>
      <button type="button" className="btn" onClick={() => setOpen(true)} disabled={!can}
        title={page.trial_running ? t("enginetrial.runningLine", { model: page.trial_running.model, machine: page.trial_running.machine,
          phase: t.maybe(`enginetrial.phase.${page.trial_running.phase}`, str(page.trial_running.phase)) }) : t("enginetrial.buttonTitle")}>
        <IconFlask size={15} aria-hidden="true" /> {t("enginetrial.button")}
      </button>
      {open && <TrialDialog r={r} page={page} onClose={() => setOpen(false)}
        onStarted={() => { setOpen(false); onStarted(t("enginetrial.started", { name: r.name })); }} />}
    </>
  );
}

export function TrialDialog({ r, page, onClose, onStarted }: { r: Recipe; page: EnginePage; onClose: () => void; onStarted: () => void }) {
  const t = useT();
  const machines = page.machines.filter((m) => m.variant);
  const [model, setModel] = useState(page.models[0]?.name ?? "");
  const [machine, setMachine] = useState<number>(() => (machines.find((m) => m.status === "online") ?? machines[0])?.machine_id ?? 0);
  const [params, setParams] = useState<Record<string, unknown> | null>(null);
  const [edited, setEdited] = useState(false);
  const [minutes, setMinutes] = useState(String(MINUTES.default));
  const [dry, setDry] = useState<TrialDryRun | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("select, button")?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);

  // The estimate and where it fits, for this model and machine (and the parameters once someone changed them).
  useEffect(() => {
    if (!model || !machine) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      setChecking(true);
      api<TrialDryRun>(`/recipes/${encodeURIComponent(r.name)}/trials`, { method: "POST",
        body: { model, machine_id: machine, dry_run: true, ...(edited && params ? { params } : {}) } }).then(
        (d) => { if (!alive) return; setDry(d); setError(null); setChecking(false); if (!edited) setParams(d.params); },
        (e) => { if (!alive) return; setDry(null); setError(trialError(t, e)); setChecking(false); });
    }, edited ? 400 : 0);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [r.name, model, machine, params, edited, t]);

  const choose = (fn: () => void) => { fn(); setEdited(false); setParams(null); };
  const setParam = (k: string, v: unknown) => { setEdited(true); setParams((p) => ({ ...(p ?? {}), [k]: v })); };
  const gpu = Number(params?.gpus ?? 0) > 0;
  // As on the deploy form: layers on the GPU mean nothing on the CPU, CPU threads nothing on a GPU.
  const fields = Object.entries(r.params).filter(([k, p]) => !p.advanced && !COMMON.has(k) && params && k in params
    && !(!gpu && k === "gpu_layers") && !(gpu && k === "threads"));
  const m = machines.find((x) => x.machine_id === machine);
  const mins = Number(minutes);
  const minutesOk = Number.isInteger(mins) && mins >= MINUTES.min && mins <= MINUTES.max;
  const fits = Boolean(dry?.placement.fits);
  const placedGpus = dry?.placement.placed?.[0]?.gpus ?? [];
  const amount = dry ? bytes(dry.estimate.amount) : "";

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/recipes/${encodeURIComponent(r.name)}/trials`, { method: "POST",
        body: { model, machine_id: machine, params, timeout_minutes: mins } });
      onStarted();
    } catch (e) { setError(trialError(t, e)); }
    setBusy(false);
  };

  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="dlg panel et-dlg tr-dlg" role="dialog" aria-modal="true" aria-labelledby="tr-title" ref={box}>
        <div className="dlg-head">
          <h2 id="tr-title"><IconFlask size={18} aria-hidden="true" /> {t("enginetrial.dlgTitle", { name: r.name })}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label={t("common.close")} onClick={onClose} disabled={busy}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small dlg-lead">{r.based_on ? t("enginetrial.lead", { name: r.name, builtin: r.based_on }) : t("enginetrial.leadBuiltin", { name: r.name })}</p>
        <p className="small muted">{t("enginetrial.isolated")}</p>
        {page.models.length === 0 ? <Notice tone="warn">{t("enginetrial.noModel")}</Notice> : (
          <div className="field">
            <label htmlFor="tr-model">{t("enginetrial.model")}</label>
            <select id="tr-model" value={model} onChange={(e) => choose(() => setModel(e.target.value))} aria-describedby="tr-model-hint">
              {page.models.map((x) => <option key={x.id} value={x.name}>{x.title ? `${x.name} · ${x.title}` : x.name}</option>)}
            </select>
            <p className="field-hint" id="tr-model-hint">{t("enginetrial.modelHint")}</p>
          </div>
        )}
        {machines.length === 0 ? <Notice tone="warn">{t("enginetrial.noMachine")}</Notice> : (
          <div className="field">
            <label htmlFor="tr-machine">{t("enginetrial.machine")}</label>
            <select id="tr-machine" value={machine} onChange={(e) => choose(() => setMachine(Number(e.target.value)))} aria-describedby="tr-machine-hint">
              {machines.map((x) => <option key={x.machine_id} value={x.machine_id} disabled={x.status !== "online"}>
                {`${x.name} · ${x.variant}`}{x.status !== "online" ? ` · ${t("enginetrial.offline")}` : ""}</option>)}
            </select>
            <p className="field-hint" id="tr-machine-hint">{t("enginetrial.machineHint")}</p>
          </div>
        )}
        {params && (
          <fieldset className="tr-params">
            <legend className="small">{t("enginetrial.params")}</legend>
            <p className="small muted">{dry?.params_from && !edited ? t("enginetrial.paramsFrom", { name: dry.params_from }) : !edited ? t("enginetrial.paramsDefault") : ""}</p>
            <div className="tr-param-grid">
              {fields.map(([k, p]) => (
                <div className="field" key={k}>
                  <label htmlFor={`tr-p-${k}`}>{p.label ?? k}</label>
                  {p.type === "choice" && p.choices
                    ? <select id={`tr-p-${k}`} value={str(params[k])} onChange={(e) => setParam(k, e.target.value)}>
                        {p.choices.map((c) => <option key={c} value={c}>{c}</option>)}</select>
                    : <input id={`tr-p-${k}`} className="mono" inputMode={p.type === "size" ? "text" : "numeric"} value={str(params[k])}
                        onChange={(e) => setParam(k, p.type === "int" && /^\d+$/.test(e.target.value) ? Number(e.target.value) : e.target.value)} />}
                </div>
              ))}
              {gpu && (
                <div className="field">
                  <label htmlFor="tr-share">{t("enginetrial.gpu")}</label>
                  <select id="tr-share" value={str(params.share)} onChange={(e) => setParam("share", e.target.value)}>
                    <option value="exclusive">{t("enginetrial.shareExclusive")}</option>
                    <option value="vram">{t("enginetrial.shareVram")}</option>
                  </select>
                </div>
              )}
              <div className="field">
                <label htmlFor="tr-minutes">{t("enginetrial.minutes")}</label>
                <input id="tr-minutes" className="mono" inputMode="numeric" value={minutes} onChange={(e) => setMinutes(e.target.value)}
                  aria-invalid={!minutesOk} aria-describedby="tr-minutes-hint" />
              </div>
            </div>
            <p className="field-hint" id="tr-minutes-hint">{t("enginetrial.minutesHint", { min: String(MINUTES.min), max: String(MINUTES.max) })}</p>
          </fieldset>
        )}
        <div className="tr-verdict" aria-live="polite">
          {checking && !dry ? <p className="small muted">{t("enginetrial.checking")}</p> : dry && (
            <>
              <p className="small"><strong>{t("enginetrial.estimate", { amount: dry.estimate.device === "gpu" ? t("enginetrial.perGpu", { amount }) : t("enginetrial.ram", { amount }) })}</strong></p>
              {fits
                ? <p className="small tr-fits">{placedGpus.length ? t("enginetrial.fitsGpu", { machine: m?.name ?? "", gpus: placedGpus.join(", ") }) : t("enginetrial.fits", { machine: m?.name ?? "" })}</p>
                : <Notice tone="warn">{t("enginetrial.noFit", { why: dry.placement.summary ?? "" })}</Notice>}
              {dry.engine_on.kind === "image" && dry.engine_on.present === false && <Notice tone="info">{t("enginetrial.imageMissing", { machine: m?.name ?? "" })}</Notice>}
              {dry.running > 0 && <p className="small muted">{t("enginetrial.limit", { running: String(dry.running), limit: String(dry.limit) })}</p>}
            </>
          )}
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button type="button" className="btn btn-primary" onClick={() => void start()} aria-busy={busy}
            disabled={busy || !model || !machine || !fits || !minutesOk || checking || !params}>
            <IconFlask size={16} aria-hidden="true" /> {busy ? t("enginetrial.starting") : t("enginetrial.start")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ the results
export function stateChip(t: Translator, state: string) {
  const tone = state === "passed" ? "chip-tone-ok" : state === "failed" ? "chip-tone-bad" : state === "running" ? "chip-tone-run" : "";
  return <span className={`chip ${tone}`}>{t.maybe(`enginetrial.state.${state}`, state)}</span>;
}

function codeText(t: Translator, c: Code): string {
  const vars: Record<string, string> = {};
  for (const [k, v] of Object.entries(c)) if (k !== "code") vars[k] = k === "amount" ? bytes(Number(v)) : str(v);
  const key = c.code === "not_asked" ? `enginetrial.code.not_asked.${str(c.why) || "none"}` : `enginetrial.code.${c.code}`;
  return t.maybe(key, "", vars);
}

function noteOf(t: Translator, c: TrialCheck): string {
  // English: the server's own sentence. Chinese: the console's, from its codes (each with the server's specifics).
  if (t.lang === "en" || !c.codes?.length) return c.note ?? "";
  const parts = c.codes.map((x) => codeText(t, x)).filter(Boolean);
  return parts.length ? parts.join("") : c.note ?? "";
}

function valueOf(t: Translator, tr: EngineTrial, c: TrialCheck): string {
  if (c.value === null || c.value === undefined) return t("enginetrial.unknown");
  if (c.id === "started") return t("enginetrial.value.seconds", { seconds: secs(c.value) });
  if (c.id === "memory") return t("enginetrial.value.memory", { amount: bytes(Number(c.value)) });
  if (c.id === "latency" && typeof c.value === "object") {
    const v = c.value as { p50_ms: number | null; p95_ms: number | null; warmup_ms: number | null };
    if (v.p50_ms === null) return t("enginetrial.unknown");
    return t("enginetrial.value.latency", { p50: String(v.p50_ms), p95: String(v.p95_ms) })
      + (v.warmup_ms !== null ? ` · ${t("enginetrial.value.warmup", { ms: String(v.warmup_ms) })}` : "");
  }
  void tr;
  return str(c.value);
}

function expectedOf(t: Translator, tr: EngineTrial, c: TrialCheck): string {
  if (c.id === "started") {
    const base = tr.comparison?.deployment?.start_seconds ?? tr.comparison?.trial?.start_seconds;
    return base ? t("enginetrial.expected.builtinStart", { builtin: tr.builtin ?? "", seconds: secs(base) }) : t("enginetrial.expected.ready");
  }
  if (c.id === "memory") {
    const reserved = tr.reserved ?? tr.estimate;
    return reserved ? t("enginetrial.expected.memory", { reserved: bytes(reserved) }) : t("enginetrial.unknown");
  }
  if (c.id === "answers" && typeof c.expected === "string" && c.expected.includes("/")) {
    return t("enginetrial.expected.answers", { count: c.expected.split("/")[1] });
  }
  return str(c.expected) || t("enginetrial.unknown");
}

// The one-line result, in the reader's language: why it failed or stopped, else what it measured.
export function trialSummary(t: Translator, tr: EngineTrial | TrialBrief): string {
  const state = t.maybe(`enginetrial.state.${tr.state}`, tr.state);
  if (t.lang === "en" && tr.summary) return tr.summary;
  const why = tr.why;
  if (why && why.code !== "checks") {
    const key = why.code === "timeout" ? `enginetrial.why.timeout.${str(why.phase) || "starting"}` : `enginetrial.why.${why.code}`;
    const vars: Record<string, string> = Object.fromEntries(Object.entries(why).filter(([k]) => k !== "code").map(([k, v]) => [k, str(v)]));
    const text = t.maybe(key, "", vars);
    if (text) return `${state}${punct(t).colon}${text}`;
  }
  if (!("checks" in tr)) return tr.summary ?? state;
  const parts: string[] = [];
  if (tr.start) parts.push(t("enginetrial.sum.start", { seconds: secs(tr.start.seconds) }));
  const ans = tr.checks.find((c) => c.id === "answers");
  if (ans?.value) parts.push(t("enginetrial.sum.answers", { value: str(ans.value) }));
  if (tr.p50_ms !== null && tr.p50_ms !== undefined) parts.push(t("enginetrial.sum.p50", { ms: String(tr.p50_ms) }));
  if (tr.measured !== null && tr.measured !== undefined) parts.push(t("enginetrial.sum.memory", { measured: bytes(tr.measured), reserved: bytes(tr.reserved ?? tr.estimate ?? 0) }));
  const p = punct(t);
  const failed = tr.checks.filter((c) => c.ok === false).map((c) => t.maybe(`enginetrial.check.${c.id}`, c.label));
  const head = failed.length ? `${state}${p.open}${t("enginetrial.why.checks", { checks: failed.join(p.list) })}${p.close}` : state;
  return parts.length ? `${head}${p.colon}${parts.join(p.sep)}${p.end}` : `${head}${p.end}`;
}

export function TrialChecks({ tr }: { tr: EngineTrial }) {
  const t = useT();
  return (
    <div className="table-wrap">
      <table className="table tr-checks">
        <thead><tr><th>{t("enginetrial.col.check")}</th><th>{t("enginetrial.col.result")}</th><th>{t("enginetrial.col.value")}</th>
          <th>{t("enginetrial.col.expected")}</th><th>{t("enginetrial.col.note")}</th></tr></thead>
        <tbody>{tr.checks.map((c) => (
          <tr key={c.id}>
            <td data-label={t("enginetrial.col.check")} className="strong">{t.maybe(`enginetrial.check.${c.id}`, c.label)}</td>
            <td data-label={t("enginetrial.col.result")}>
              {/* Latency and the answers compared are shown, never judged: "for information", not "not checked". */}
              <span className={`chip ${c.ok === true ? "chip-tone-ok" : c.ok === false ? "chip-tone-bad" : ""}`}>
                {c.ok === null && c.value !== null && c.value !== undefined ? t("enginetrial.ok.info") : t(`enginetrial.ok.${String(c.ok)}` as "enginetrial.ok.true")}</span>
            </td>
            <td data-label={t("enginetrial.col.value")} className="mono small">{valueOf(t, tr, c)}
              {c.id === "started" && tr.start?.phases && Object.keys(tr.start.phases).length > 0 && (
                <div className="muted">{Object.entries(tr.start.phases).map(([k, v]) => `${t.maybe(`enginetrial.startPhase.${k}`, k)} ${t("enginetrial.value.seconds", { seconds: secs(v) })}`).join(" · ")}</div>
              )}
            </td>
            <td data-label={t("enginetrial.col.expected")} className="small">{expectedOf(t, tr, c)}</td>
            <td data-label={t("enginetrial.col.note")} className="small muted tr-note">{noteOf(t, c) || "—"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Comparison({ tr }: { tr: EngineTrial }) {
  const t = useT();
  const c = tr.comparison;
  if (!tr.builtin || !c) return null;
  const d = c.deployment;
  const p = c.trial;
  return (
    <div className="et-block tr-compare">
      <h4 className="small">{t("enginetrial.compare", { builtin: tr.builtin })}</h4>
      {!d && !p && <p className="small muted">{t("enginetrial.compareNone", { model: tr.model.name, builtin: tr.builtin })}</p>}
      {d && <p className="small">{t("enginetrial.compareDeployment", { name: d.name, where: d.same_machine ? t("enginetrial.compareSameMachine") : "",
        start: d.start_seconds ? t("enginetrial.value.seconds", { seconds: secs(d.start_seconds) }) : t("enginetrial.unknown"),
        reserved: d.reserved ? bytes(d.reserved) : t("enginetrial.unknown") })}</p>}
      {p && <p className="small">{t("enginetrial.compareTrial", { machine: p.machine, date: p.finished_at ? t.date(p.finished_at) : "—",
        start: p.start_seconds ? t("enginetrial.value.seconds", { seconds: secs(p.start_seconds) }) : t("enginetrial.unknown"),
        measured: p.measured !== null ? bytes(p.measured) : t("enginetrial.unknown"), reserved: p.reserved ? bytes(p.reserved) : t("enginetrial.unknown"),
        p50: p.p50_ms !== null ? String(p.p50_ms) : "—" })}</p>}
    </div>
  );
}

function TrialDetail({ id }: { id: number }) {
  const t = useT();
  const [tr, setTr] = useState<EngineTrial | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api<{ trial: EngineTrial }>(`/engine-trials/${id}`).then((r) => { if (alive) setTr(r.trial); }, (e) => { if (alive) setError(errorText(e)); });
    return () => { alive = false; };
  }, [id]);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!tr) return <p className="small muted">{t("common.loading")}</p>;
  const params = Object.entries(tr.params).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(" ") : String(v)}`);
  return (
    <div className="tr-detail">
      <TrialChecks tr={tr} />
      <Comparison tr={tr} />
      <p className="small muted mono lp-break">{t("enginetrial.params.line", { list: params.join(" · ") })}{tr.params_from ? ` (${t("enginetrial.from", { name: tr.params_from })})` : ""}</p>
      {tr.log_tail && (
        <details className="tr-more"><summary className="small">{t("enginetrial.output")}</summary><pre className="mono small tr-log">{tr.log_tail}</pre></details>
      )}
      {(tr.events ?? []).length > 0 && (
        <details className="tr-more"><summary className="small">{t("enginetrial.events")}</summary>
          <ul className="small tr-events">{(tr.events ?? []).map((e, i) => <li key={i}><span className="muted">{e.at ? t.dateTime(e.at) : ""}</span> {e.message}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

export function TrialsSection({ r, page, onChanged, canRun }: { r: Recipe; page: EnginePage; onChanged: () => void; canRun: boolean }) {
  const t = useT();
  const [list, setList] = useState<TrialsResponse | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const load = useCallback(() => api<TrialsResponse>(`/recipes/${encodeURIComponent(r.name)}/trials`).then(setList, () => undefined), [r.name]);
  const running = (list?.trials ?? []).find((x) => x.state === "running") ?? null;
  const wasRunning = useRef(false);
  useEffect(() => { void load(); }, [load, page.trial_running?.id]);
  useEffect(() => {
    if (!running) {
      if (wasRunning.current) { wasRunning.current = false; onChanged(); }
      return;
    }
    wasRunning.current = true;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, load, onChanged]);

  const cancel = async (tr: EngineTrial) => {
    if (!window.confirm(t("enginetrial.cancelConfirm", { name: r.name }))) return;
    try {
      await api(`/engine-trials/${tr.id}/cancel`, { method: "POST" });
      setNote({ tone: "ok", text: t("enginetrial.cancelled") });
      setOpen(tr.id);
      void load();
      onChanged();
    } catch (e) { setNote({ tone: "error", text: errorText(e) }); }
  };

  const done = (list?.trials ?? []).filter((x) => x.state !== "running");
  return (
    <>
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {running && (
        <div className="tr-running" role="status">
          <p className="tr-running-line"><span className="pulse" aria-hidden="true" /> <strong>{t("enginetrial.runningLine", { model: running.model.name, machine: running.machine.name,
            phase: t.maybe(`enginetrial.phase.${running.phase}`, str(running.phase)) })}</strong>
            <span className="muted small"> · {t("enginetrial.since", { time: t.dateTime(running.created_at) })} · {running.requested_by}</span></p>
          {running.checks.length > 0 && <TrialChecks tr={running} />}
          {canRun && <button type="button" className="btn btn-quiet btn-small" onClick={() => void cancel(running)}>
            <IconPlayerStop size={14} aria-hidden="true" /> {t("enginetrial.cancel")}</button>}
        </div>
      )}
      {list && done.length === 0 && !running && <p className="empty-line">{r.based_on ? t("enginetrial.noneCopy", { name: r.name }) : t("enginetrial.none")}</p>}
      {done.length > 0 && (
        <div className="table-wrap">
          <table className="table tr-history">
            <thead><tr><th>{t("enginetrial.col.when")}</th><th>{t("enginetrial.col.model")}</th><th>{t("enginetrial.col.machine")}</th>
              <th>{t("enginetrial.col.result")}</th><th><span className="sr-only">{t("enginetrial.show")}</span></th></tr></thead>
            <tbody>{done.map((x) => (
              <Fragment key={x.id}>
                <tr>
                  <td data-label={t("enginetrial.col.when")} className="small">{t.dateTime(x.finished_at ?? x.created_at)}<div className="muted">{x.requested_by}</div></td>
                  <td data-label={t("enginetrial.col.model")} className="mono small">{x.model.name}</td>
                  <td data-label={t("enginetrial.col.machine")} className="mono small">{x.machine.id ? <Link to={`/machines/${x.machine.id}`}>{x.machine.name}</Link> : x.machine.name}</td>
                  <td data-label={t("enginetrial.col.result")} className="small"><div className="cell-stack"><span>{stateChip(t, x.state)}</span><span className="tr-sum">{trialSummary(t, x)}</span></div></td>
                  <td className="cell-actions">
                    <button type="button" className="btn btn-quiet btn-small" aria-expanded={open === x.id} onClick={() => setOpen(open === x.id ? null : x.id)}>
                      {open === x.id ? <IconChevronDown size={14} aria-hidden="true" /> : <IconChevronRight size={14} aria-hidden="true" />} {open === x.id ? t("enginetrial.hide") : t("enginetrial.show")}
                    </button>
                  </td>
                </tr>
                {open === x.id && <tr className="tr-open"><td colSpan={5}><TrialDetail id={x.id} /></td></tr>}
              </Fragment>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ------------------------------------------------------------------ lines for other pages
export function TrialCardChip({ last, running, recipe }: { last?: TrialBrief | null; running?: boolean; recipe: string }) {
  const t = useT();
  if (!last && !running) return null;
  const state = running ? "running" : last!.state;
  const label = running ? t("enginetrial.card.running") : state === "passed" ? t("enginetrial.card.passed") : t("enginetrial.card.failed");
  const title = last ? t("enginetrial.card.title", { state: t.maybe(`enginetrial.state.${last.state}`, last.state), model: last.model, machine: last.machine,
    date: t.date(last.finished_at ?? last.created_at) }) : label;
  return <Link to={`${engineHref(recipe)}#trials`} className={`chip tr-chip ${state === "passed" ? "chip-tone-ok" : state === "failed" ? "chip-tone-bad" : "chip-tone-run"}`} title={title}>
    <IconFlask size={12} aria-hidden="true" /> {label}</Link>;
}

export function LastTrialLine({ name, builtin, last }: { name: string; builtin: boolean; last: TrialBrief | null | undefined }) {
  const t = useT();
  if (last) {
    return <p className="small tr-last">{t.rich("enginetrial.lastTried", { machine: last.machine, model: last.model, date: t.date(last.finished_at ?? last.created_at),
      state: <Link to={`${engineHref(name)}#trials`}>{t.maybe(`enginetrial.state.${last.state}`, last.state)}</Link> })}</p>;
  }
  if (builtin) return null;
  return <p className="small muted tr-last">{t("enginetrial.neverTried", { name })}</p>;
}

export function ServingTrials({ list }: { list: TrialBrief[] | undefined }) {
  const t = useT();
  if (!list?.length) return null;
  return (
    <section className="panel tr-serving" aria-labelledby="tr-serving-h">
      <h2 id="tr-serving-h" className="small"><IconFlask size={15} aria-hidden="true" /> {t("enginetrial.serving")}</h2>
      <ul className="tr-serving-list">{list.map((x) => (
        <li key={x.id}><Link to={`${engineHref(x.recipe)}#trials`}>{t("enginetrial.servingLine", { recipe: x.recipe, model: x.model, machine: x.machine,
          phase: t.maybe(`enginetrial.phase.${x.phase}`, str(x.phase)) })}</Link></li>
      ))}</ul>
      <p className="small muted">{t("enginetrial.servingNote")}</p>
    </section>
  );
}
