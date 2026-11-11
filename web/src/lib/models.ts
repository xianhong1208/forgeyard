import { bytes } from "./machines";
import type { CatalogModel, Medium, Recipe, Replica, Task } from "./types";

export const TASK_LABEL: Record<Task, string> = { chat: "Chat", embed: "Embeddings", rerank: "Rerank", asr: "Speech to text" };

export const API_LABEL: Record<string, string> = {
  "openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", rerank: "/v1/rerank",
  "openai-audio": "/v1/audio/transcriptions",
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

// "downloading the model: 1.2 of 7.5 GiB (16%)" as a fraction for a bar.
export function downloadFraction(r: Replica): number | null {
  const d = r.download;
  if (!d || !d.total) return null;
  return Math.min(1, d.done / d.total);
}

export function downloadText(r: Replica): string | null {
  const d = r.download;
  if (!d) return null;
  const what = d.phase === "engine" ? "engine" : "model";
  return d.total ? `Downloading the ${what}: ${bytes(d.done)} of ${bytes(d.total)}` : `Downloading the ${what}: ${bytes(d.done)}`;
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
