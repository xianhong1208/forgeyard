// Adding a model: the recommended list, Hugging Face search (run by the server) and the
// inspection with its suggestion (recipe, estimate, where it fits).

import type { Estimate, Medium, Task } from "./types";

export type RecommendedGroup = { id: string; title: string; description: string };

export type RecommendedModel = {
  name: string; title: string; source: string; revision: string | null; file: string | null; group: string;
  task: Task; format: string; recipe: string; device: "gpu" | "cpu"; download_gib: number; memory_gib: number;
  gated: boolean; engine_args: Record<string, string[]>; note: string; in_catalog: string | null;
  license: string; inputs: Medium[]; mmproj: string | null;
  // Text to speech (1.3): the languages it speaks; the original model whose licence it is, for a conversion.
  languages?: string[]; license_from?: string | null;
  // Image models for stable-diffusion.cpp (1.3): the files beside the model file.
  components?: Record<string, string>;
};

export type RecommendedResponse = { groups: RecommendedGroup[]; models: RecommendedModel[] };

export type SearchResult = {
  id: string; source: string; downloads: number; likes: number; last_modified: string | null; gated: boolean;
  gated_mode: string | null; private: boolean; pipeline_tag: string | null; library: string | null; task: Task | null;
  format: string; params: number | null; weights_bytes: number | null; in_catalog: string | null; recipes: string[];
};

export type SearchResponse = { results: SearchResult[]; cached: boolean; endpoint: string };

export type FitMachine = { name: string; status: string; fits: boolean; reason: string; gpus: number[]; short_by: number | null };

export type Suggestion = {
  recipe: string | null; recipes: string[]; device?: "gpu" | "cpu"; devices?: ("gpu" | "cpu")[]; error?: string;
  params?: Record<string, unknown>; estimate?: Estimate; fits?: boolean; machines?: FitMachine[]; summary?: string;
};

export type InspectMeta = {
  format: string; revision?: string; ref?: string; task_hint?: Task | null; size_bytes: number; file?: string; file_count: number;
  gated?: boolean; gguf_files?: { path: string; size: number }[];
  license?: string | null; modalities?: Medium[] | null; mmproj?: { path: string; size: number } | null;
  mmproj_files?: { path: string; size: number }[];
  config?: { layers: number; kv_heads: number; max_context: number; dtype: string };
  weights?: { params: Record<string, number> }; gguf?: { layers: number; architecture: string; elements: number };
  // 1.3, image models: the native size and the files beside the model file (stable-diffusion.cpp), or a
  // diffusers pipeline's components.
  image?: { size: number; multiple: number; components?: Record<string, string>; single?: boolean };
  diffusers?: { pipeline: string | null; components: Record<string, { class: string; params: number; dtype: string | null }> };
  // 1.3, video models: the pipeline's own size, frame rate and the multiple its sizes take.
  video?: { size: [number, number]; multiple: number; fps: number };
};

export type InspectResponse = { meta: InspectMeta; recipes: string[]; suggestion: Suggestion };

// What is being added: picked from the list or the search, or typed in.
export type Pick = {
  source: string; revision: string; file: string; name: string; title: string; task: string; token: string;
  // GGUF vision models: the projector file; "" the default (F16, if the repository has one), "none" none.
  mmproj: string;
  recipe: string; device: "" | "gpu" | "cpu"; engine_args: Record<string, string[]>; gated: boolean; from: "list" | "search" | "source";
  // local:// models only: the server cannot look at them, so the format is given.
  format: string;
};

export function emptyPick(): Pick {
  return { source: "", revision: "", file: "", mmproj: "", name: "", title: "", task: "", token: "", recipe: "", device: "",
    engine_args: {}, gated: false, from: "source", format: "" };
}

export function pickFromList(m: RecommendedModel): Pick {
  return { source: m.source, revision: m.revision ?? "", file: m.file ?? "", mmproj: m.mmproj ?? "", name: m.name, title: m.title, task: m.task,
    token: "", recipe: m.recipe, device: m.device, engine_args: m.engine_args, gated: m.gated, from: "list", format: "" };
}

// A catalog name from a repository id: "Qwen/Qwen3-4B" -> "qwen3-4b".
export function nameFor(source: string): string {
  const last = source.replace(/^hf:\/\//, "").split("/").pop() ?? "";
  return last.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, "").slice(0, 64);
}

export function pickFromSearch(r: SearchResult): Pick {
  return { ...emptyPick(), source: r.source, name: nameFor(r.source), task: r.task ?? "", gated: r.gated, from: "search" };
}

// 1234567 -> "1.2 M"
export function compact(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)} k`;
  return String(n);
}

export function fitsLine(s: Suggestion): string | null {
  const yes = (s.machines ?? []).filter((m) => m.fits);
  if (!yes.length) return null;
  return yes.map((m) => `${m.name} (${m.gpus.length ? `GPU ${m.gpus.map((g) => `#${g}`).join(", ")}` : "CPU"})`).join(", ");
}

// Where to go to deploy a model just added, with the suggested recipe and device.
export function deployLink(name: string, s: Suggestion | null | undefined): string {
  const q = new URLSearchParams({ model: name });
  if (s?.recipe) q.set("recipe", s.recipe);
  if (s?.device) q.set("device", s.device);
  return `/models/deploy?${q.toString()}`;
}
