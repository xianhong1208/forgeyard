// Sleep when idle in the console (1.5.0, #37): the state a deployment is in, what its last start says about a
// wake, and where an asleep one would run. The server decides everything; this only words it.

import type { Deployment, StartTime } from "./types";

export const IDLE_DEFAULT = 30;
export const IDLE_MIN = 1;
export const IDLE_MAX = 1440;
// 1.5.0 PR 3: seconds a request waits for an asleep model to wake.
export const WAKE_WAIT_DEFAULT = 300;
export const WAKE_WAIT_MIN = 10;
export const WAKE_WAIT_MAX = 1800;

// Asleep (stopped because nobody used it), waking (a request or Wake now woke it, no replica ready yet), or neither.
export type SleepState = "asleep" | "waking" | null;

export function sleepState(d: Deployment): SleepState {
  const w = d.workload as Deployment["workload"] | undefined;   // a summary without its workload: neither
  if (w?.desired_state === "asleep" || d.sleep?.asleep) return "asleep";
  if (w?.desired_state === "running" && d.sleep?.waking && w.ready === 0) return "waking";
  return null;
}

function parse(value: string): number {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).getTime();
}

// "0:42", "12:05": the time since `from`, for a wake under way.
export function elapsed(from: string | null | undefined, now: number = Date.now()): string {
  if (!from) return "";
  const s = Math.max(0, Math.floor((now - parse(from)) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// "74 s", "3 min 20 s": a start time as people read it.
export function seconds(n: number): string {
  const s = Math.max(1, Math.round(n));
  if (s < 90) return `${s} s`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
}

// Next to the setting on the deploy and edit forms: what the first request after a sleep waits. On the deploy form
// `other` names the deployment of the same model and engine whose start it is.
export function startHint(last: StartTime | null | undefined, other?: string): string {
  if (!last) {
    return "The first request after a sleep waits while the model starts: seconds for a small llama.cpp model, a minute or more for vLLM.";
  }
  const took = other ? `${other} (the same model and engine) last started in ${seconds(last.seconds)}` : `The last start took ${seconds(last.seconds)}`;
  return `${took}: the first request after a sleep waits about that long.`;
}

// On the deploy form there is no deployment yet: the last start of another deployment of the same model with
// the same engine says the most.
export function sameModelStart(deployments: Deployment[], model: string, recipe: string): { name: string; last: StartTime } | null {
  const found = deployments
    .filter((d) => d.model.name === model && d.recipe === recipe && d.last_start)
    .sort((a, b) => parse(b.last_start!.at) - parse(a.last_start!.at))[0];
  return found ? { name: found.name, last: found.last_start! } : null;
}

// The idle-minutes field as the API takes it: a whole number in range, else why not.
export function idleProblem(text: string): string | null {
  return wholeProblem(text, IDLE_MIN, IDLE_MAX, "minutes");
}

// The wake-wait field (seconds), the same way.
export function wakeWaitProblem(text: string): string | null {
  return wholeProblem(text, WAKE_WAIT_MIN, WAKE_WAIT_MAX, "seconds");
}

function wholeProblem(text: string, min: number, max: number, unit: string): string | null {
  const t = text.trim();
  if (!t) return null;
  if (!/^\d+$/.test(t)) return `A whole number of ${unit}.`;
  const n = Number(t);
  return n < min || n > max ? `${min} to ${max.toLocaleString("en")} ${unit}.` : null;
}

// About how long a wake takes: what the server expects for the wake under way, else the last start.
export function wakeSeconds(d: Deployment): number | null {
  return d.sleep?.waking?.expected_seconds ?? d.last_start?.seconds ?? null;
}

// Asleep deployments that would run on a machine: they hold nothing there now, but the memory map would show
// their reservation once they wake, so people know the card may be claimed meanwhile.
export type AsleepHere = { deployment: Deployment; bytes: number | null; gpus: string[] };

export function asleepOn(deployments: Deployment[], machineId: number): AsleepHere[] {
  const out: AsleepHere[] = [];
  for (const d of deployments) {
    if (sleepState(d) !== "asleep") continue;
    const reps = d.workload.replica_list.filter((r) => !r.final && (r.machine?.id ?? d.pinned?.id) === machineId);
    if (!reps.length && d.pinned?.id !== machineId) continue;
    const per = d.estimate.device === "gpu" ? d.estimate.per_gpu : d.estimate.ram;
    // Per card (or of RAM), as the memory map counts a reservation.
    const bytes = Math.max(0, ...reps.map((r) => r.vram ?? per ?? 0)) || per || null;
    const gpus = [...new Set(reps.flatMap((r) => r.gpus.filter((g) => g.vendor !== "cpu").map((g) => `#${g.index}`)))];
    out.push({ deployment: d, bytes, gpus });
  }
  return out;
}
