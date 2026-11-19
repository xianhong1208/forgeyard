// 1.4.3 (#140): a page per catalog model (/models/<name>) and per engine (/models/engines/<name>). The words the
// console uses beside them under /models (deploy, deployments, routes, engines…) are refused as model names by the
// API, so a model's page never hides another page.
import type { ModelKind } from "./fleet";
import { isVisionTask } from "./models";
import type { CatalogModel, DeviceFit, EnginePage, EngineOn, MachineVerdict, ModelFile, ModelPage, RecipeVariant } from "./types";

export const modelHref = (name: string) => `/models/${encodeURIComponent(name)}`;
export const engineHref = (name: string) => `/models/engines/${encodeURIComponent(name)}`;

export function catalogKind(m: Pick<CatalogModel, "task" | "modalities">): ModelKind {
  if (m.task === "chat") return (m.modalities ?? []).length > 0 ? "multimodal" : "chat";
  if (isVisionTask(m.task)) return "vision";
  return m.task;
}

// The Hugging Face repository of a model from there (for its link), else null.
export function hfRepo(m: Pick<CatalogModel, "kind" | "source">): string | null {
  return m.kind === "hf" ? m.source.replace(/^hf:\/\//, "") : null;
}

// How a variant's image is pinned: by digest (the exact image), by tag only, or not an image (a program).
export function pinned(v: Pick<RecipeVariant, "runtime" | "image">): "digest" | "tag" | null {
  if (v.runtime !== "container" || !v.image) return null;
  return v.image.includes("@sha256:") ? "digest" : "tag";
}

// The start of an image's digest, for a short label: "sha256:0123abcd".
export function digestOf(image: string | null): string | null {
  if (!image) return null;
  const at = image.indexOf("@sha256:");
  return at > 0 ? image.slice(at + 1, at + 1 + 7 + 8) : null;
}

// One machine's verdict for an engine: on a GPU when one fits, else on the CPU when that fits, else the GPU verdict
// (it says why, and whether sharing would make it fit), else the CPU one.
export type MachineFit = { verdict: MachineVerdict; device: "gpu" | "cpu" } | null;

export function machineFit(fit: Partial<Record<"gpu" | "cpu", DeviceFit>> | undefined, machineId: number): MachineFit {
  const of = (d: "gpu" | "cpu") => fit?.[d]?.machines?.find((v) => v.machine_id === machineId) ?? null;
  const gpu = of("gpu");
  const cpu = of("cpu");
  if (gpu?.fits) return { verdict: gpu, device: "gpu" };
  if (cpu?.fits) return { verdict: cpu, device: "cpu" };
  if (gpu) return { verdict: gpu, device: "gpu" };
  if (cpu) return { verdict: cpu, device: "cpu" };
  return null;
}

// The estimate the facts strip shows for an engine: the GPU one when it has one, else the CPU one.
export function headlineEstimate(fit: Partial<Record<"gpu" | "cpu", DeviceFit>> | undefined) {
  return fit?.gpu?.estimate ?? fit?.cpu?.estimate ?? null;
}

// The one line a folded Files section shows.
export function filesLine(files: ModelPage["files"], used: string | null): { count: number; others: number; used: ModelFile | null } {
  const down = files.files.filter((f) => f.downloaded);
  return {
    count: files.downloaded_count,
    others: files.files.length - down.length,
    used: down.find((f) => f.path === used) ?? (down.length === 1 ? down[0] : null),
  };
}

// What a machine has of an engine, in words: its image or program is there, is not (it is fetched when a deployment
// starts there), or is not known yet (never listed, no cache report).
export function presenceText(kind: EngineOn["kind"], present: boolean | null): string {
  if (kind === "command") return "installed command";
  const what = kind === "program" ? "program" : "image";
  return present === true ? `${what} here` : present === false ? `${what} not here: fetched on deploy` : `${what}: not known yet`;
}

// The same on an engine's page, under its chip (here / not here): where it is, or when it comes.
export function whereText(kind: EngineOn["kind"], present: boolean | null): string {
  if (kind === "command") return "installed command";
  if (kind === "program") return present === true ? "in its model cache" : present === false ? "downloaded when a deployment starts there" : "no cache report yet";
  return present === true ? "in its container engine" : present === false ? "pulled when a deployment starts there" : "not listed yet";
}

// A machine's image listing older than this is listed again when an engine's page opens (connected machines only).
export const STALE_MS = 10 * 60 * 1000;

function parseTime(s: string): number {
  return Date.parse(/Z$|[+-]\d\d:\d\d$/.test(s) ? s : `${s}Z`);
}

export function stale(p: Pick<EnginePage, "machines">, now: number): boolean {
  return p.machines.some((x) => x.can_list && x.kind === "image" && (!x.listed_at || now - parseTime(x.listed_at) > STALE_MS));
}
