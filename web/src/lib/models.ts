import { bytes } from "./machines";
import type { CatalogModel, Deployment, DownloadProgress, Medium, Recipe, Task } from "./types";

// Anything with a download in progress: a replica (M3, 1.3.1) or a download ahead of a deployment (1.4).
type WithDownload = { download?: DownloadProgress | null };

export const TASK_LABEL: Record<Task, string> = { chat: "Chat", embed: "Embeddings", rerank: "Rerank", asr: "Speech to text", tts: "Text to speech", image: "Images", video: "Video",
  detect: "Object detection", segment: "Segmentation", depth: "Depth estimation", classify: "Text classification",
  "image-classify": "Image classification" };

// 1.4: the tasks of vision models, and the licences they are added with (as the server checks).
export const VISION_TASKS: Task[] = ["detect", "segment", "depth", "image-classify"];
export type VisionTaskName = "detect" | "segment" | "depth" | "image-classify";
export function isVisionTask(t: Task): t is VisionTaskName {
  return VISION_TASKS.includes(t);
}
export const PERMISSIVE = ["apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause"];

export const API_LABEL: Record<string, string> = {
  "openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", rerank: "/v1/rerank",
  "openai-audio": "/v1/audio/transcriptions", "openai-speech": "/v1/audio/speech",
  "openai-images": "/v1/images/generations", "openai-videos": "/v1/videos",
  "vision-detect": "/v1/vision/detect", "vision-segment": "/v1/vision/segment", "vision-depth": "/v1/vision/depth",
  "vision-classify": "/v1/vision/classify",
  classify: "/v1/classify",
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
  // 1.4: through the server's relay, for a machine that cannot reach the source itself; 1.4.1: from another
  // machine's cache, through the server.
  const from = d.via === "server" && d.source ? `, from ${d.source}` : "";
  const what = `${d.phase === "engine" ? "engine" : "model"}${d.via === "server" ? ` via the server${from}` : ""}`;
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
  firstAnswer: string;   // 1.4.5: seconds a replica has to start answering ("" = the gateway's default, 120)
  params: Record<string, string>;
};

export function emptyDeploy(): DeployForm {
  return { model: "", recipe: "", name: "", servedName: "", device: "gpu", gpus: "1", share: "", replicas: "1", machine: "",
    vram: "", token: "", extraArgs: "", firstAnswer: "", params: {} };
}

// 1.4.5: the gateway's wait for a first answer applies to every deployment but image and video ones (their own timeout).
export const FIRST_ANSWER_DEFAULT = 120;
export function takesFirstAnswer(recipe: Recipe | undefined): boolean {
  return !!recipe && recipe.tasks.some((t) => t !== "image" && t !== "video");
}

// The recipe's defaults; with the model's own longest context (1.4.4), a context default longer than that starts at
// it instead (a 512-token classifier on a recipe whose default is 8192), as the server does.
export function recipeDefaults(recipe: Recipe | undefined, modelMax?: number | null): Record<string, string> {
  const out: Record<string, string> = {};
  if (!recipe) return out;
  for (const [k, p] of Object.entries(recipe.params)) out[k] = String(p.default);
  const ctx = recipe.params.context;
  if (ctx && modelMax && typeof ctx.default === "number" && ctx.default > modelMax) out.context = String(Math.max(ctx.min ?? 1, modelMax));
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
  if (f.firstAnswer.trim() && takesFirstAnswer(recipe)) p.first_answer_timeout = Number(f.firstAnswer.trim());
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
  firstAnswer: string;
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
    firstAnswer: typeof p.first_answer_timeout === "number" ? String(p.first_answer_timeout) : "",
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
  if (takesFirstAnswer(recipe)) p.first_answer_timeout = f.firstAnswer.trim() ? Number(f.firstAnswer.trim()) : null;
  return { params: p, replicas: Number(f.replicas || "1"), machine_id: f.machine ? Number(f.machine) : null };
}

// ---------------------------------------------------------------- the catalog's source line (1.4, #92)
const QUANT = /(?:^|[-_.])(i?q\d+(?:_[a-z0-9]+)*|bf16|fp16|f16|f32|fp8|mxfp4(?:_moe)?)$/i;
const MAX_STEM = 28;

// The quantisation a GGUF file name ends with (Q4_K_M, IQ4_XS, Q8_0, F16, BF16…), split parts
// (-00001-of-00003) ignored; else the file's stem, shortened.
export function quantTag(file: string): string {
  const base = file.split("/").pop() ?? file;
  const stem = base.replace(/\.gguf$/i, "").replace(/-\d{5}-of-\d{5}$/i, "");
  const m = QUANT.exec(stem);
  if (m) return m[1].toUpperCase();
  return stem.length > MAX_STEM ? `${stem.slice(0, MAX_STEM - 1)}…` : stem;
}

// The short source under a catalog model's name: the repository (and the quantisation of a single
// file), a URL's host and file, a local directory's name and file. `full` is everything, for a tooltip.
export function shortSource(m: Pick<CatalogModel, "kind" | "source" | "file" | "revision" | "mmproj">): { text: string; full: string } {
  const full = [m.source, m.file, m.mmproj ? `+ ${m.mmproj.path}` : null].filter(Boolean).join(" · ") + (m.revision ? ` @ ${m.revision}` : "");
  if (m.kind === "url") {
    let host = m.source;
    let name = m.file ?? "";
    try {
      const u = new URL(m.source);
      host = u.hostname;
      name = name || decodeURIComponent(u.pathname.split("/").pop() ?? "");
    } catch { /* keep the source as it is */ }
    return { text: name ? `${host} · ${name}` : host, full };
  }
  if (m.kind === "local") {
    const dir = m.source.replace(/^local:\/\//, "").replace(/\/+$/, "").split("/").pop() ?? m.source;
    return { text: m.file ? `${dir} · ${m.file.split("/").pop()}` : dir, full };
  }
  const repo = m.source.replace(/^hf:\/\//, "");
  return { text: m.file ? `${repo} · ${quantTag(m.file)}` : repo, full };
}
