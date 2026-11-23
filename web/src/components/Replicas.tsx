// 1.5.0 (#37): the Replicas setting of the deploy and edit forms (a fixed number, or automatic by load),
// and the deployment page's line about it.
import type { Deployment } from "../lib/types";
import { SCALE_DEFAULTS, loadText, scaleSummary, type ScaleForm } from "../lib/scale";
import { useT } from "../i18n/react";

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
  const t = useT();
  const set = <K extends keyof ScaleForm>(k: K, v: ScaleForm[K]) => onScale({ ...scale, [k]: v });
  const auto = allowAuto && scale.mode === "auto";
  return (
    <>
      {allowAuto && (
        <div className="field"><label htmlFor={`${id}-scale`}>{t("deployment.replicas")}</label>
          <select id={`${id}-scale`} value={scale.mode} onChange={(e) => set("mode", e.target.value as ScaleForm["mode"])}>
            <option value="fixed">{t("deploy.scale.fixed")}</option>
            <option value="auto">{t("deploy.scale.auto")}</option>
          </select>
          <p className="field-hint">{auto
            ? t("deploy.scale.autoHint", { now: now ? t("deploy.scale.now", { count: now }) : "" })
            : t("deploy.scale.fixedHint")}</p></div>
      )}
      {!auto ? (
        <div className="field"><label htmlFor={`${id}-replicas`}>{allowAuto ? t("deploy.scale.number") : t("deployment.replicas")}</label>
          <input id={`${id}-replicas`} inputMode="numeric" value={count} onChange={(e) => onCount(e.target.value)} /></div>
      ) : (
        <div className="field scale-bounds"><span className="label-like" id={`${id}-bounds`}>{t("deploy.scale.between")}</span>
          <div className="row-gap" role="group" aria-labelledby={`${id}-bounds`}>
            <input id={`${id}-min`} aria-label={t("deploy.change.min_replicas")} inputMode="numeric" value={scale.min} onChange={(e) => set("min", e.target.value)} placeholder={String(SCALE_DEFAULTS.min)} />
            <span className="muted">{t("deploy.scale.and")}</span>
            <input id={`${id}-max`} aria-label={t("deploy.change.max_replicas")} inputMode="numeric" value={scale.max} onChange={(e) => set("max", e.target.value)} placeholder={String(SCALE_DEFAULTS.max)} />
          </div>
          <p className="field-hint">{t("deploy.scale.bounds")}{sleepOn ? t("deploy.scale.zeroHint") : ""}</p></div>
      )}
    </>
  );
}

// Its target and windows, for the forms' collapsed Advanced section (only with automatic replicas).
export function ScaleAdvancedFields({ id, scale, onScale }: { id: string; scale: ScaleForm; onScale: (s: ScaleForm) => void }) {
  const t = useT();
  const set = <K extends keyof ScaleForm>(k: K, v: ScaleForm[K]) => onScale({ ...scale, [k]: v });
  return (
    <>
      <div className="field"><label htmlFor={`${id}-target`}>{t("deploy.change.autoscale_target")}</label>
        <input id={`${id}-target`} inputMode="numeric" value={scale.target} onChange={(e) => set("target", e.target.value)} placeholder={String(SCALE_DEFAULTS.target)} />
        <p className="field-hint">{t("deploy.scale.targetHint")}</p></div>
      <div className="field"><label htmlFor={`${id}-up`}>{t("deploy.scale.up")}</label>
        <input id={`${id}-up`} inputMode="numeric" value={scale.upSeconds} onChange={(e) => set("upSeconds", e.target.value)} placeholder={String(SCALE_DEFAULTS.upSeconds)} />
        <p className="field-hint">{t("deploy.scale.upHint")}</p></div>
      <div className="field"><label htmlFor={`${id}-down`}>{t("deploy.scale.down")}</label>
        <input id={`${id}-down`} inputMode="numeric" value={scale.downMinutes} onChange={(e) => set("downMinutes", e.target.value)} placeholder={String(SCALE_DEFAULTS.downMinutes)} />
        <p className="field-hint">{t("deploy.scale.downHint")}</p></div>
    </>
  );
}

// The deployment page: how many replicas it has and why, while automatic replicas are on.
export function AutoscaleLine({ d }: { d: Deployment }) {
  const t = useT();
  const a = d.autoscale;
  const summary = scaleSummary(d);
  if (!a || !summary) return null;
  const load = loadText(d);
  return (
    <div className="scale-line" aria-label={t("deploy.change.autoscale")}>
      <p><span className="strong">{summary}</span>{a.wanted !== null && a.wanted !== a.replicas ? <span className="muted">{t("deployment.scale.wanted", { wanted: a.wanted })}</span> : null}</p>
      {load && <p className="small muted">{load}.</p>}
      {a.draining && <p className="small">{t("deployment.scale.draining", { replica: a.draining.replica, when: a.draining.in_flight === 0
        ? t("deployment.scale.drainNothing") : t.plural("deployment.scale.drainFinish", a.draining.in_flight) })}</p>}
      {a.room && <p className="small warn-text">{a.room}</p>}
      {a.paused && <p className="small muted">{t("deployment.scale.paused", { reason: a.paused })}</p>}
      {a.last && !a.draining && <p className="small muted">{t("deployment.scale.last", { text: a.last.text })}</p>}
    </div>
  );
}
