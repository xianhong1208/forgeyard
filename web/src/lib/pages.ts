// 1.4.3 (#140): a page per catalog model (/models/<name>) and per engine (/models/engines/<name>). The words the
// console uses beside them under /models (deploy, deployments, routes, engines…) are refused as model names by the
// API, so a model's page never hides another page.
import { t } from "../i18n/core";
import type { ModelKind } from "./fleet";
import { isVisionTask } from "./models";
import { bytes } from "./machines";
import type { Accelerator, CatalogModel, DeviceFit, EnginePage, EngineOn, EngineVia, MachineVerdict, ModelFile, ModelPage, RecipeVariant } from "./types";

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
  // 1.5.6 (#282): a machine with no GPU the engine can use (no card in the GPU verdict, nothing to share) is judged on
  // its CPU: "no allowed GPU that faster-whisper supports" next to a CPU variant that is there reads as a contradiction.
  if (gpu && cpu && !usableGpu(gpu)) return { verdict: cpu, device: "cpu" };
  if (gpu) return { verdict: gpu, device: "gpu" };
  if (cpu) return { verdict: cpu, device: "cpu" };
  return null;
}

function usableGpu(v: MachineVerdict): boolean {
  return (v.cards ?? []).length > 0 || Boolean(v.share_fits);
}

// 1.5.6 (#282): what a CPU replica uses is the machine's system memory (the CPU row: on an APU, not the part set aside
// for the GPU). {need, free, total} of it, and the GPU's own memory on an APU (null elsewhere), so the page can name both.
export function cpuMemory(v: MachineVerdict, need: number | undefined, accelerators: Accelerator[] | undefined):
  { need: number | null; free: number | null; total: number | null; apuGpu: number | null } {
  const row = (v.cards ?? [])[0] ?? null;   // a CPU verdict's one entry: the CPU row
  const apu = (accelerators ?? []).find((a) => a.vendor !== "cpu" && a.unified_memory);
  return { need: need ?? null, free: row ? row.available : null, total: row?.total ?? null, apuGpu: apu?.vram_total ?? null };
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

// What a machine has of an engine, in words: its image or program is there, is not (it comes when a deployment starts
// there: pulled or downloaded by the machine, or through the server; 1.5.6, #282), or is not known yet (never listed, no
// cache report).
export function presenceText(kind: EngineOn["kind"], present: boolean | null, via?: EngineVia | null, size?: number | null): string {
  if (kind === "command") return t("modelpage.presence.command");
  const what = kind === "program" ? "program" : "image";
  if (present === false) return t(`modelpage.presence.${what}Not${viaSuffix(via)}`, { size: sizeText("modelpage.presence.size", size) });
  return t(`modelpage.presence.${what}${present === true ? "Here" : "Unknown"}`);
}

// The same on an engine's page, under its chip (here / not here): where it is, or when it comes.
export function whereText(kind: EngineOn["kind"], present: boolean | null, via?: EngineVia | null, size?: number | null): string {
  if (kind === "command") return t("enginepage.where.command");
  const what = kind === "program" ? "program" : "image";
  if (present === false) return t(`enginepage.where.${what}Not${viaSuffix(via)}`, { size: sizeText("enginepage.where.size", size) });
  return t(`enginepage.where.${what}${present === true ? "Here" : "Unknown"}`);
}

// 1.5.6 (#282): the deploy form, once a machine is chosen (or the estimate placed a replica there): whether that machine
// has the engine's image or program now, and how it comes when not. Nothing for an installed command.
export function engineLine(machine: string, e: Pick<EngineOn, "kind" | "present" | "via" | "size">): string | null {
  if (e.kind !== "image" && e.kind !== "program") return null;
  const what = e.kind === "program" ? "program" : "image";
  const state = e.present === true ? "Here" : e.present === false ? (`Not${viaSuffix(e.via)}` as const) : "Unknown";
  return t(`deploy.engine.${what}${state}`, { machine, size: sizeText("deploy.engine.size", e.size) });
}

function viaSuffix(via: EngineVia | null | undefined): "" | "Server" | "Auto" {
  return via === "server" ? "Server" : via === "auto" ? "Auto" : "";
}

function sizeText(key: "modelpage.presence.size" | "enginepage.where.size" | "deploy.engine.size", size: number | null | undefined): string {
  return typeof size === "number" && size > 0 ? t(key, { size: bytes(size) }) : "";
}

// A machine's image listing older than this is listed again when an engine's page opens (connected machines only).
export const STALE_MS = 10 * 60 * 1000;

function parseTime(s: string): number {
  return Date.parse(/Z$|[+-]\d\d:\d\d$/.test(s) ? s : `${s}Z`);
}

export function stale(p: Pick<EnginePage, "machines">, now: number): boolean {
  return p.machines.some((x) => x.can_list && x.kind === "image" && (!x.listed_at || now - parseTime(x.listed_at) > STALE_MS));
}
