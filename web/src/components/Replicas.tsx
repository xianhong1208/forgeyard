// 1.5.0 (#37): the Replicas setting of the deploy and edit forms (a fixed number, or automatic by load),
// and the deployment page's line about it.
import type { Deployment } from "../lib/types";
import { SCALE_DEFAULTS, loadText, scaleSummary, type ScaleForm } from "../lib/scale";

type Props = {
  id: string;                 // prefix of the fields' ids ("d" on the deploy form, "e" on the edit form)
  count: string;              // the fixed number
  onCount: (v: string) => void;
  scale: ScaleForm;
  onScale: (s: ScaleForm) => void;
  allowAuto: boolean;         // not for video deployments
  sleepOn?: boolean;          // with sleep when idle, the fewest may be 0
  now?: number | null;        // the number it runs now (edit form)
};

export function ReplicasField({ id, count, onCount, scale, onScale, allowAuto, sleepOn, now }: Props) {
  const set = <K extends keyof ScaleForm>(k: K, v: ScaleForm[K]) => onScale({ ...scale, [k]: v });
  const auto = allowAuto && scale.mode === "auto";
  return (
    <>
      {allowAuto && (
        <div className="field"><label htmlFor={`${id}-scale`}>Replicas</label>
          <select id={`${id}-scale`} value={scale.mode} onChange={(e) => set("mode", e.target.value as ScaleForm["mode"])}>
            <option value="fixed">Fixed number</option>
            <option value="auto">Automatic, by load</option>
          </select>
          <p className="field-hint">{auto
            ? `One more when its replicas are busy, one fewer after quiet minutes; only when it fits.${now ? ` ${now} now.` : ""}`
            : "Automatic adds and removes replicas by the requests the gateway sees."}</p></div>
      )}
      {!auto ? (
        <div className="field"><label htmlFor={`${id}-replicas`}>{allowAuto ? "Number of replicas" : "Replicas"}</label>
          <input id={`${id}-replicas`} inputMode="numeric" value={count} onChange={(e) => onCount(e.target.value)} /></div>
      ) : (
        <div className="field scale-bounds"><span className="label-like" id={`${id}-bounds`}>Between</span>
          <div className="row-gap" role="group" aria-labelledby={`${id}-bounds`}>
            <input id={`${id}-min`} aria-label="Fewest replicas" inputMode="numeric" value={scale.min} onChange={(e) => set("min", e.target.value)} placeholder={String(SCALE_DEFAULTS.min)} />
            <span className="muted">and</span>
            <input id={`${id}-max`} aria-label="Most replicas" inputMode="numeric" value={scale.max} onChange={(e) => set("max", e.target.value)} placeholder={String(SCALE_DEFAULTS.max)} />
          </div>
          <p className="field-hint">1 to 16.{sleepOn ? " 0 as the fewest: down to none only by sleeping when idle." : ""}</p></div>
      )}
    </>
  );
}

// Its target and windows, for the forms' collapsed Advanced section (only with automatic replicas).
export function ScaleAdvancedFields({ id, scale, onScale }: { id: string; scale: ScaleForm; onScale: (s: ScaleForm) => void }) {
  const set = <K extends keyof ScaleForm>(k: K, v: ScaleForm[K]) => onScale({ ...scale, [k]: v });
  return (
    <>
      <div className="field"><label htmlFor={`${id}-target`}>Target load per replica (%)</label>
        <input id={`${id}-target`} inputMode="numeric" value={scale.target} onChange={(e) => set("target", e.target.value)} placeholder={String(SCALE_DEFAULTS.target)} />
        <p className="field-hint">Automatic replicas: of its Concurrent requests, counting requests in flight and waiting in the gateway's line. 20 to 100.</p></div>
      <div className="field"><label htmlFor={`${id}-up`}>Busy for (seconds) before one more</label>
        <input id={`${id}-up`} inputMode="numeric" value={scale.upSeconds} onChange={(e) => set("upSeconds", e.target.value)} placeholder={String(SCALE_DEFAULTS.upSeconds)} />
        <p className="field-hint">10 to 600. At twice the target one is added after 10 s.</p></div>
      <div className="field"><label htmlFor={`${id}-down`}>Quiet for (minutes) before one fewer</label>
        <input id={`${id}-down`} inputMode="numeric" value={scale.downMinutes} onChange={(e) => set("downMinutes", e.target.value)} placeholder={String(SCALE_DEFAULTS.downMinutes)} />
        <p className="field-hint">1 to 120. The replica removed takes no new requests and finishes the ones it runs first.</p></div>
    </>
  );
}

// The deployment page: how many replicas it has and why, while automatic replicas are on.
export function AutoscaleLine({ d }: { d: Deployment }) {
  const a = d.autoscale;
  const summary = scaleSummary(d);
  if (!a || !summary) return null;
  const load = loadText(d);
  return (
    <div className="scale-line" aria-label="Automatic replicas">
      <p><span className="strong">{summary}</span>{a.wanted !== null && a.wanted !== a.replicas ? <span className="muted"> · the load asks for {a.wanted}</span> : null}</p>
      {load && <p className="small muted">{load}.</p>}
      {a.draining && <p className="small">Draining replica #{a.draining.replica}: it takes no new requests and is removed once {a.draining.in_flight === 0 ? "nothing is in flight on it" : `the ${a.draining.in_flight} in flight finish${a.draining.in_flight === 1 ? "es" : ""}`}.</p>}
      {a.room && <p className="small warn-text">{a.room}</p>}
      {a.paused && <p className="small muted">Not scaling now: {a.paused}.</p>}
      {a.last && !a.draining && <p className="small muted">Last: {a.last.text}</p>}
    </div>
  );
}
