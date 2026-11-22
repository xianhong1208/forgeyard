// Sleep when idle in the console (1.5.0, #37): the "When not in use" setting of the deploy and edit forms (off by
// default; the last start says how long the first request after a sleep waits) and the Asleep / Waking badge.
import { IconMoon, IconSunrise } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { when } from "../lib/format";
import { bytes } from "../lib/machines";
import { IDLE_DEFAULT, IDLE_MAX, IDLE_MIN, elapsed, idleProblem, sleepState, startHint, type AsleepHere } from "../lib/sleep";
import type { Deployment, StartTime } from "../lib/types";

export function SleepSetting({ prefix, on, minutes, onChange, last, other }: {
  prefix: string;                     // the ids' prefix ("d" on the deploy form, "e" on the edit form)
  on: boolean;
  minutes: string;
  onChange: (on: boolean, minutes: string) => void;
  last?: StartTime | null;            // its own last start (edit), or another deployment's of the same model (deploy)
  other?: string;                     // that other deployment's name
}) {
  const problem = on ? idleProblem(minutes) : null;
  return (
    <section className="panel" aria-labelledby={`${prefix}-sleep-title`}>
      <h2 id={`${prefix}-sleep-title`}>When not in use</h2>
      <div className="sleep-set">
        <label className="check-line" htmlFor={`${prefix}-sleep`}>
          <input id={`${prefix}-sleep`} type="checkbox" checked={on}
            onChange={(e) => onChange(e.target.checked, e.target.checked && !minutes.trim() ? String(IDLE_DEFAULT) : minutes)} />
          <span><IconMoon size={15} aria-hidden="true" /> Sleep when idle</span>
        </label>
        <div className="field sleep-minutes">
          <label htmlFor={`${prefix}-idle`}>Idle minutes</label>
          <input id={`${prefix}-idle`} inputMode="numeric" value={minutes} disabled={!on} placeholder={String(IDLE_DEFAULT)}
            aria-invalid={problem ? true : undefined} aria-describedby={`${prefix}-sleep-hint`}
            onChange={(e) => onChange(on, e.target.value)} />
          {problem && <p className="field-error">{problem}</p>}
        </div>
      </div>
      <div id={`${prefix}-sleep-hint`} className="field-hint sleep-hint">
        <p>{on
          ? `After ${minutes.trim() && !problem ? minutes.trim() : IDLE_DEFAULT} min without a request its replicas stop and free their memory; the model stays listed, and Wake now or a request starts it again.`
          : `Off: it keeps its memory while nobody uses it. On, its replicas stop after the idle time (${IDLE_MIN} to ${IDLE_MAX.toLocaleString("en")} minutes) and start again when needed.`}</p>
        <p>{startHint(last, other)}</p>
        {on && <p>While it sleeps, its memory may be used by others; if none is free when it wakes, it cannot start until there is room.</p>}
      </div>
    </section>
  );
}

// 1.5.0 (#37): Asleep (moon) and Waking (with the time since it was woken) instead of the status chip.
export function SleepBadge({ d, fallback, big }: { d: Deployment; fallback?: ReactNode; big?: boolean }) {
  const s = sleepState(d);
  const cls = `chip deprow-state${big ? " chip-big" : ""}`;
  if (s === "asleep") {
    return <span className={`${cls} chip-asleep`} title={d.sleep?.asleep_since ? `Asleep since ${when(d.sleep.asleep_since)}` : undefined}>
      <IconMoon size={big ? 15 : 13} aria-hidden="true" /> Asleep</span>;
  }
  if (s === "waking") return <span className={`${cls} chip-waking`}><IconSunrise size={big ? 15 : 13} aria-hidden="true" /> Waking {elapsed(d.sleep?.woken_at)}</span>;
  return <>{fallback ?? null}</>;
}


// Machines (1.5.0, #37): the asleep deployments that would run on a machine. They hold nothing there (the memory
// map shows the card free), so this says the card may be claimed while they sleep.
export function AsleepHereLine({ list, compact }: { list: AsleepHere[]; compact?: boolean }) {
  if (!list.length) return null;
  return (
    <p className={`asleep-here${compact ? " asleep-here-compact" : ""}`} aria-label="Asleep here">
      <IconMoon size={14} aria-hidden="true" />
      <span>
        {list.map((a, i) => (
          <span key={a.deployment.id}>
            {i > 0 ? "; " : ""}
            <Link to={`/serving/${a.deployment.id}`} className="mono">{a.deployment.name}</Link> asleep here
            {a.bytes ? ` (${bytes(a.bytes)}${a.gpus.length ? ` on GPU ${a.gpus.join(", ")}` : a.deployment.estimate.device === "cpu" ? " of RAM" : ""} when awake)` : ""}
          </span>
        ))}
        {compact ? "" : ". It holds nothing now, so others may use that memory meanwhile; if none is free when it wakes, it waits for room or starts on another machine."}
      </span>
    </p>
  );
}
