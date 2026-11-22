// 1.5.0 (#37): automatic replicas on the deploy and edit forms. The server checks the values
// (forgeyard/workloads/autoscale.py); these are the defaults it uses when one is left empty.
import type { Deployment, Recipe } from "./types";

export const SCALE_DEFAULTS = { min: 1, max: 2, target: 80, upSeconds: 60, downMinutes: 10 };
export const SCALE_PARAMS = ["autoscale", "min_replicas", "max_replicas", "autoscale_target", "scale_up_seconds", "scale_down_minutes"];

export type ScaleForm = {
  mode: "fixed" | "auto";
  was: "fixed" | "auto";  // what the deployment had when the form opened (a change back to fixed turns it off)
  min: string;
  max: string;
  target: string;        // "" = 80
  upSeconds: string;     // "" = 60
  downMinutes: string;   // "" = 10
};

export function emptyScale(): ScaleForm {
  return { mode: "fixed", was: "fixed", min: String(SCALE_DEFAULTS.min), max: String(SCALE_DEFAULTS.max), target: "", upSeconds: "", downMinutes: "" };
}

// Video deployments are never scaled automatically: a video job belongs to the replica that makes it.
export function canAutoscale(recipe: Recipe | undefined): boolean {
  return !!recipe && !recipe.tasks.includes("video");
}

const num = (v: unknown, fallback: number) => (typeof v === "number" ? String(v) : String(fallback));

export function scaleForm(params: Record<string, unknown>): ScaleForm {
  return {
    mode: params.autoscale === true ? "auto" : "fixed", was: params.autoscale === true ? "auto" : "fixed",
    min: num(params.min_replicas, SCALE_DEFAULTS.min), max: num(params.max_replicas, SCALE_DEFAULTS.max),
    target: typeof params.autoscale_target === "number" ? String(params.autoscale_target) : "",
    upSeconds: typeof params.scale_up_seconds === "number" ? String(params.scale_up_seconds) : "",
    downMinutes: typeof params.scale_down_minutes === "number" ? String(params.scale_down_minutes) : "",
  };
}

const n = (v: string) => (v.trim() === "" ? null : Number(v.trim()));

// For a new deployment: the parameters to send (nothing for a fixed number), and the replicas it starts with.
export function scaleDeployParams(s: ScaleForm): { params: Record<string, unknown>; replicas: number | null } {
  if (s.mode !== "auto") return { params: {}, replicas: null };
  const p: Record<string, unknown> = { autoscale: true };
  const low = n(s.min); const high = n(s.max);
  if (low !== null) p.min_replicas = low;
  if (high !== null) p.max_replicas = high;
  for (const [k, v] of [["autoscale_target", s.target], ["scale_up_seconds", s.upSeconds], ["scale_down_minutes", s.downMinutes]] as const) {
    if (n(v) !== null) p[k] = n(v);
  }
  return { params: p, replicas: Math.max(1, low ?? SCALE_DEFAULTS.min) };
}

// For a change: every setting (null goes back to its default); back to a fixed number, automatic off.
export function scaleEditParams(s: ScaleForm): Record<string, unknown> {
  if (s.mode !== "auto") return s.was === "auto" ? { autoscale: null } : {};
  return { autoscale: true, min_replicas: n(s.min), max_replicas: n(s.max), autoscale_target: n(s.target),
    scale_up_seconds: n(s.upSeconds), scale_down_minutes: n(s.downMinutes) };
}

// "Automatic, 1 to 4 · 2 now" for the deployment page; null for a fixed number.
export function scaleSummary(d: Deployment): string | null {
  const a = d.autoscale;
  if (!a?.on) return null;
  const low = a.min === 0 ? "0 (asleep)" : String(a.min);
  return `Automatic, ${low} to ${a.max} · ${a.replicas} now`;
}

export function loadText(d: Deployment): string | null {
  const a = d.autoscale;
  if (!a?.on || a.load === null) return null;
  const goal = Math.round(a.capacity * a.target) / 100;
  const per = a.per_replica === null ? "" : `, ${a.per_replica} per ready replica`;
  return `Load ${a.load} over the last ${a.scale_up_seconds} s${per} (target ${goal} of ${a.capacity} concurrent request${a.capacity === 1 ? "" : "s"} each)`;
}
