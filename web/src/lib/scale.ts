// 1.5.0 (#37): automatic replicas on the deploy and edit forms. The server checks the values
// (forgeyard/workloads/autoscale.py); these are the defaults it uses when one is left empty.
import { t, tPlural } from "../i18n/core";
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

// A setting not stored stays empty (the field shows the default as its placeholder), so opening the form and saving
// changes nothing.
const num = (v: unknown) => (typeof v === "number" ? String(v) : "");

export function scaleForm(params: Record<string, unknown>): ScaleForm {
  return {
    mode: params.autoscale === true ? "auto" : "fixed", was: params.autoscale === true ? "auto" : "fixed",
    min: num(params.min_replicas), max: num(params.max_replicas), target: num(params.autoscale_target),
    upSeconds: num(params.scale_up_seconds), downMinutes: num(params.scale_down_minutes),
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
  const low = a.min === 0 ? t("deployment.scale.zeroAsleep") : String(a.min);
  return t("deployment.scale.summary", { low, max: a.max, now: a.replicas });
}

export function loadText(d: Deployment): string | null {
  const a = d.autoscale;
  if (!a?.on || a.load === null) return null;
  const goal = Math.round(a.capacity * a.target) / 100;
  const per = a.per_replica === null ? "" : t("deployment.scale.perReplica", { per: String(a.per_replica) });
  return tPlural("deployment.scale.load", a.capacity, { load: String(a.load), seconds: a.scale_up_seconds, per, goal: String(goal) });
}
