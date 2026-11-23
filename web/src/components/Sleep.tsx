// Sleep when idle in the console (1.5.0, #37): the "When not in use" setting of the deploy and edit forms (off by
// default; the last start says how long the first request after a sleep waits) and the Asleep / Waking badge.
import { IconMoon, IconSunrise } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { useT } from "../i18n/react";
import { when } from "../lib/format";
import { bytes } from "../lib/machines";
import { IDLE_DEFAULT, IDLE_MAX, IDLE_MIN, WAKE_WAIT_DEFAULT, WAKE_WAIT_MAX, WAKE_WAIT_MIN, elapsed, idleProblem, sleepState, startHint,
  wakeWaitProblem, type AsleepHere } from "../lib/sleep";
import type { Deployment, StartTime } from "../lib/types";

export function SleepSetting({ prefix, on, minutes, onChange, last, other }: {
  prefix: string;                     // the ids' prefix ("d" on the deploy form, "e" on the edit form)
  on: boolean;
  minutes: string;
  onChange: (on: boolean, minutes: string) => void;
  last?: StartTime | null;            // its own last start (edit), or another deployment's of the same model (deploy)
  other?: string;                     // that other deployment's name
}) {
  const t = useT();
  const problem = on ? idleProblem(minutes) : null;
  return (
    <section className="panel" aria-labelledby={`${prefix}-sleep-title`}>
      <h2 id={`${prefix}-sleep-title`}>{t("deployment.sleep.title")}</h2>
      <div className="sleep-set">
        <label className="check-line" htmlFor={`${prefix}-sleep`}>
          <input id={`${prefix}-sleep`} type="checkbox" checked={on}
            onChange={(e) => onChange(e.target.checked, e.target.checked && !minutes.trim() ? String(IDLE_DEFAULT) : minutes)} />
          <span><IconMoon size={15} aria-hidden="true" /> {t("deployment.sleep.label")}</span>
        </label>
        <div className="field sleep-minutes">
          <label htmlFor={`${prefix}-idle`}>{t("deploy.change.idle_minutes")}</label>
          <input id={`${prefix}-idle`} inputMode="numeric" value={minutes} disabled={!on} placeholder={String(IDLE_DEFAULT)}
            aria-invalid={problem ? true : undefined} aria-describedby={`${prefix}-sleep-hint`}
            onChange={(e) => onChange(on, e.target.value)} />
          {problem && <p className="field-error">{problem}</p>}
        </div>
      </div>
      <div id={`${prefix}-sleep-hint`} className="field-hint sleep-hint">
        <p>{on
          ? t("deploy.sleep.onHint", { minutes: minutes.trim() && !problem ? minutes.trim() : String(IDLE_DEFAULT) })
          : t("deploy.sleep.offHint", { min: IDLE_MIN, max: IDLE_MAX })}</p>
        <p>{startHint(last, other)}</p>
        {on && <p>{t("deploy.sleep.room")}</p>}
      </div>
    </section>
  );
}

// 1.5.0 (#37): Asleep (moon) and Waking (with the time since it was woken) instead of the status chip.
export function SleepBadge({ d, fallback, big }: { d: Deployment; fallback?: ReactNode; big?: boolean }) {
  const t = useT();
  const s = sleepState(d);
  const cls = `chip deprow-state${big ? " chip-big" : ""}`;
  if (s === "asleep") {
    return <span className={`${cls} chip-asleep`} title={d.sleep?.asleep_since ? t("fleet.sleep.asleepSince", { when: when(d.sleep.asleep_since) }) : undefined}>
      <IconMoon size={big ? 15 : 13} aria-hidden="true" /> {t("fleet.sleep.asleep")}</span>;
  }
  if (s === "waking") return <span className={`${cls} chip-waking`}><IconSunrise size={big ? 15 : 13} aria-hidden="true" /> {t("fleet.sleep.waking", { elapsed: elapsed(d.sleep?.waking?.since) })}</span>;
  return <>{fallback ?? null}</>;
}


// Machines (1.5.0, #37): the asleep deployments that would run on a machine. They hold nothing there (the memory
// map shows the card free), so this says the card may be claimed while they sleep.
export function AsleepHereLine({ list, compact }: { list: AsleepHere[]; compact?: boolean }) {
  const t = useT();
  if (!list.length) return null;
  return (
    <p className={`asleep-here${compact ? " asleep-here-compact" : ""}`} aria-label={t("fleet.sleep.hereLabel")}>
      <IconMoon size={14} aria-hidden="true" />
      <span>
        {list.map((a, i) => (
          <span key={a.deployment.id}>
            {i > 0 ? "; " : ""}
            {t.rich("fleet.sleep.here", { name: <Link to={`/serving/${a.deployment.id}`} className="mono">{a.deployment.name}</Link> })}
            {a.bytes ? (a.gpus.length ? t("fleet.sleep.hereSizeGpu", { size: bytes(a.bytes), gpus: a.gpus.join(", ") })
              : a.deployment.estimate.device === "cpu" ? t("fleet.sleep.hereSizeRam", { size: bytes(a.bytes) })
                : t("fleet.sleep.hereSize", { size: bytes(a.bytes) })) : ""}
          </span>
        ))}
        {compact ? "" : t("fleet.sleep.hereNote")}
      </span>
    </p>
  );
}

// Under Advanced on the deploy and edit forms (1.5.0 PR 3's parameter): how long a request waits for a wake.
export function WakeWaitField({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const t = useT();
  const problem = wakeWaitProblem(value);
  return (
    <div className="field"><label htmlFor={id}>{t("deploy.wake.label")}</label>
      <input id={id} inputMode="numeric" value={value} onChange={(e) => onChange(e.target.value)} placeholder={String(WAKE_WAIT_DEFAULT)}
        aria-invalid={problem ? true : undefined} />
      {problem && <p className="field-error">{problem}</p>}
      <p className="field-hint">{t.rich("deploy.wake.hint", { min: WAKE_WAIT_MIN, max: WAKE_WAIT_MAX, def: String(WAKE_WAIT_DEFAULT),
        code: <code>model_waking</code> })}</p></div>
  );
}
