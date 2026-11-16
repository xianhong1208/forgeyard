import { bytes } from "./machines";
import type { CatalogModel, Deployment, DownloadProgress, Medium, Recipe, Task } from "./types";

// Anything with a download in progress: a replica (M3, 1.3.1) or a download ahead of a deployment (1.4).
type WithDownload = { download?: DownloadProgress | null };

export const TASK_LABEL: Record<Task, string> = { chat: "Chat", embed: "Embeddings", rerank: "Rerank", asr: "Speech to text", tts: "Text to speech", image: "Images", video: "Video",
  detect: "Object detection", segment: "Segmentation", depth: "Depth estimation" };

// 1.4: the tasks of vision models, and the licences they are added with (as the server checks).
export const VISION_TASKS: Task[] = ["detect", "segment", "depth"];
export const PERMISSIVE = ["apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause"];

export const API_LABEL: Record<string, string> = {
  "openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", rerank: "/v1/rerank",
  "openai-audio": "/v1/audio/transcriptions", "openai-speech": "/v1/audio/speech",
  "openai-images": "/v1/images/generations", "openai-videos": "/v1/videos",
  "vision-detect": "/v1/vision/detect", "vision-segment": "/v1/vision/segment", "vision-depth": "/v1/vision/depth",
};

export const MEDIUM_LABEL: Record<Medium, string> = { image: "Images", audio: "Audio", video: "Video" };

// "Images, video" for what a model takes besides text; null: unknown (a catalog entry from before 1.3).
export function mediaText(m: Medium[] | null | undefined): string | null {
  if (m === null || m === undefined) return null;
  return m.length ? m.map((x, i) => (i ? MEDIUM_LABEL[x].toLowerCase() : MEDIUM_LABEL[x])).join(", ") : "Text only";
}

export function gib(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

export function params(n: number | undefined): string {
  if (!n) return "—";
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} B` : `${Math.round(n / 1e6)} M`;
}

// "downloading the model: 1.2 of 7.5 GiB (16%)" as a fraction for a bar (never past the end).
export function downloadFraction(r: WithDownload): number | null {
  const d = r.download;
  if (!d || !d.total) return null;
  return Math.max(0, Math.min(1, d.done / d.total));
}

// What the bar says: a model or engine download, or an image pull (1.3.1). Never more than the total.
export function downloadText(r: WithDownload): string | null {
  const d = r.download;
  if (!d) return null;
  const done = d.total ? Math.min(d.done, d.total) : d.done;
  if (d.phase === "image" && d.via === "server") {
    // 1.4: a pinned image through the server's relay, layer by layer, then loaded into the engine.
    const layers = d.layers ? `${d.layers_done ?? 0} of ${d.layers} layers` : "";
    if (d.unpacking) return `Pulling the image via the server: loading it${d.layers ? `, ${d.layers} layers` : ""}`;
    if (d.total) return `Pulling the image via the server: ${bytes(done)} of ${bytes(d.total)}${layers ? `, ${layers}` : ""}`;
    return layers ? `Pulling the image via the server: ${layers}` : "Pulling the image via the server";
  }
  if (d.phase === "image") {
    const layers = d.layers ? `${d.layers_done ?? 0} of ${d.layers} layers` : "";
    if (d.unpacking) return `Pulling the image: unpacking, ${layers}`;
    if (d.total) return `Pulling the image: ${bytes(done)} of ${bytes(d.total)}`;
    if (done && layers) return `Pulling the image: ${bytes(done)}, ${layers}`;
    return layers ? `Pulling the image: ${layers}` : "Pulling the image";
  }
  // 1.4: through the server's relay, for a machine that cannot reach the source itself.
  const what = `${d.phase === "engine" ? "engine" : "model"}${d.via === "server" ? " via the server" : ""}`;
  return d.total ? `Downloading the ${what}: ${bytes(done)} of ${bytes(d.total)}` : `Downloading the ${what}: ${bytes(done)}`;
}

export function compatibleRecipes(model: CatalogModel | undefined, recipes: Recipe[]): Recipe[] {
  if (!model) return [];
  return recipes.filter((r) => r.formats.includes(model.format) && r.tasks.includes(model.task));
}

export type DeployForm = {
  model: string;
  recipe: string;
  name: string;
  servedName: string;
  device: "gpu" | "cpu";
  gpus: string;
  share: "" | "exclusive" | "vram";
  replicas: string;
  machine: string;
  vram: string;
  token: string;
  extraArgs: string;
  params: Record<string, string>;
};

export function emptyDeploy(): DeployForm {
  return { model: "", recipe: "", name: "", servedName: "", device: "gpu", gpus: "1", share: "", replicas: "1", machine: "",
    vram: "", token: "", extraArgs: "", params: {} };
}

export function recipeDefaults(recipe: Recipe | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!recipe) return out;
  for (const [k, p] of Object.entries(recipe.params)) out[k] = String(p.default);
  return out;
}

export function slug(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
}

// The JSON the API takes (validation stays on the server).
export function deployBody(f: DeployForm, recipe: Recipe | undefined): Record<string, unknown> {
  const p: Record<string, unknown> = {};
  if (recipe) {
    for (const [k, def] of Object.entries(recipe.params)) {
      const v = f.params[k];
      if (v === undefined || v === "") continue;
      p[k] = def.type === "int" || def.type === "float" ? Number(v) : v;
    }
  }
  p.gpus = f.device === "cpu" ? 0 : Number(f.gpus || "1");
  if (f.share) p.share = f.share;
  if (f.vram.trim()) p.vram = f.vram.trim();
  const extra = f.extraArgs.split("\n").map((l) => l.trim()).filter(Boolean);
  if (extra.length) p.extra_args = extra;
  const body: Record<string, unknown> = { name: f.name.trim(), model: f.model, recipe: f.recipe, params: p,
    replicas: Number(f.replicas || "1") };
  if (f.servedName.trim()) body.served_name = f.servedName.trim();
  if (f.machine) body.machine_id = Number(f.machine);
  if (f.token.trim()) body.hf_token = f.token.trim();
  return body;
}

// 1.3.1: changing a deployment. The form starts from what it runs with now.
export type EditForm = {
  device: "gpu" | "cpu";
  gpus: string;
  share: "exclusive" | "vram";
  replicas: string;
  machine: string;   // "" = automatic, else the id of the machine it is pinned to
  vram: string;
  extraArgs: string;
  params: Record<string, string>;
};

function sizeText(n: number): string {
  const g = n / 1024 ** 3;
  return Number.isInteger(g) ? `${g}Gi` : String(n);
}

export function editForm(d: Deployment, recipe: Recipe): EditForm {
  const p = d.params;
  const params: Record<string, string> = recipeDefaults(recipe);
  for (const k of Object.keys(recipe.params)) if (p[k] !== undefined && p[k] !== null) params[k] = String(p[k]);
  const gpus = Number(p.gpus ?? 1);
  return {
    device: gpus === 0 ? "cpu" : "gpu", gpus: String(gpus || 1), share: p.share === "vram" ? "vram" : "exclusive",
    replicas: String(d.workload.replica_list.length || d.workload.spec.replicas || 1),
    machine: d.workload.spec.machine_id ? String(d.workload.spec.machine_id) : "",
    vram: typeof p.vram === "number" ? sizeText(p.vram) : "", extraArgs: Array.isArray(p.extra_args) ? p.extra_args.join("\n") : "",
    params,
  };
}

// The PATCH body: every parameter (the server compares), null to go back to a default.
export function editBody(f: EditForm, recipe: Recipe): Record<string, unknown> {
  const p: Record<string, unknown> = {};
  for (const [k, def] of Object.entries(recipe.params)) {
    const v = f.params[k];
    if (v === undefined || v === "") continue;
    p[k] = def.type === "int" || def.type === "float" ? Number(v) : v;
  }
  p.gpus = f.device === "cpu" ? 0 : Number(f.gpus || "1");
  p.share = f.share;
  p.vram = f.vram.trim() || null;
  const extra = f.extraArgs.split("\n").map((l) => l.trim()).filter(Boolean);
  p.extra_args = extra.length ? extra : null;
  return { params: p, replicas: Number(f.replicas || "1"), machine_id: f.machine ? Number(f.machine) : null };
}
