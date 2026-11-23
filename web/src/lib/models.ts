import { hasKey, labels, lookup, t, tMaybe } from "../i18n/core";
import { formatNumber } from "../i18n/format";
import { getLang } from "../i18n/lang";
import { IDLE_DEFAULT } from "./sleep";
import { bytes } from "./machines";
import type { CatalogModel, Deployment, DownloadProgress, Medium, Recipe, Task } from "./types";

// Anything with a download in progress: a replica (M3, 1.3.1) or a download ahead of a deployment (1.4).
type WithDownload = { download?: DownloadProgress | null };

export const TASK_LABEL: Record<Task, string> = labels({ chat: "fleet.task.chat", embed: "fleet.task.embed", rerank: "fleet.task.rerank",
  asr: "fleet.task.asr", tts: "fleet.task.tts", image: "fleet.task.image", video: "fleet.task.video", detect: "fleet.task.detect",
  segment: "fleet.task.segment", depth: "fleet.task.depth", classify: "fleet.task.classify", "image-classify": "fleet.task.image-classify" });

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

export const MEDIUM_LABEL: Record<Medium, string> = labels({ image: "library.medium.image", audio: "library.medium.audio", video: "library.medium.video" });

// "Images, video" (圖片、影片) for what a model takes besides text; null: unknown (a catalog entry from before 1.3).
// English lowercases all but the first; Chinese has no case.
export function mediaText(m: Medium[] | null | undefined): string | null {
  if (m === null || m === undefined) return null;
  return m.length ? m.map((x, i) => (i ? MEDIUM_LABEL[x].toLowerCase() : MEDIUM_LABEL[x])).join(t("library.listSep")) : t("library.textOnly");
}

// A catalog model's task in the reader's language: English shows the server's own label, as before.
export function taskLabel(m: { task: Task; task_label: string }): string {
  return getLang() === "en" ? m.task_label : TASK_LABEL[m.task] ?? m.task_label;
}

// Text the server sends that the console knows by an id (a built-in engine's description, a group of the recommended
// list). The translation is used only while the server still sends the English it was made from, so a changed text
// is never covered by an old translation (and English readers always see what was sent).
export function sentText(key: string, sent: string): string {
  if (!hasKey(key) || lookup("en", key) !== sent) return sent;
  return lookup(getLang(), key);
}

export function gib(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(n / 1024 ** 3).toFixed(2)} GiB`;
}

// A parameter count: "4.02 B", "596 M"; in Chinese the 萬/億 of Intl's compact notation ("40.2億").
export function params(n: number | undefined): string {
  if (!n) return "—";
  const lang = getLang();
  if (lang !== "en") return formatNumber(n, lang, { notation: "compact", maximumSignificantDigits: 3 });
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} B` : `${Math.round(n / 1e6)} M`;
}

// Billions of parameters, always: "4.02 B" (a model's or a pipeline component's); in Chinese as params() says it.
export function billions(n: number): string {
  return getLang() === "en" ? `${(n / 1e9).toFixed(2)} B` : params(n);
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
  const layers = d.layers ? t("fleet.layers", { done: d.layers_done ?? 0, total: d.layers }) : "";
  if (d.phase === "image" && d.via === "server") {
    // 1.4: a pinned image through the server's relay, layer by layer, then loaded into the engine.
    if (d.unpacking) return d.layers ? t("fleet.pull.relayLoadingLayers", { layers: d.layers }) : t("fleet.pull.relayLoading");
    if (d.total) return layers ? t("fleet.pull.relayBytesLayers", { done: bytes(done), total: bytes(d.total), layers })
      : t("fleet.pull.relayBytes", { done: bytes(done), total: bytes(d.total) });
    return layers ? t("fleet.pull.relayLayers", { layers }) : t("fleet.pull.relay");
  }
  if (d.phase === "image") {
    if (d.unpacking) return t("fleet.pull.unpacking", { layers });
    if (d.total) return t("fleet.pull.bytes", { done: bytes(done), total: bytes(d.total) });
    if (done && layers) return t("fleet.pull.bytesLayers", { done: bytes(done), layers });
    return layers ? t("fleet.pull.layers", { layers }) : t("fleet.pull.plain");
  }
  // 1.4: through the server's relay, for a machine that cannot reach the source itself; 1.4.1: from another
  // machine's cache, through the server.
  const what = d.phase === "engine" ? "engine" : "model";
  const how = d.via === "server" ? (d.source ? "From" : "Relay") : "";
  const sizes = { done: bytes(done), total: d.total ? bytes(d.total) : "", source: d.source ?? "" };
  return tMaybe(`fleet.download.${what}${how}${d.total ? "Of" : ""}`, "", sizes);
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
  // 1.5.0 (#37): sleep when idle (off by default) and its idle time in minutes ("" = 30).
  sleepOn: boolean;
  idleMinutes: string;
  wakeWait: string;      // seconds a request waits for a wake ("" = 300)
  params: Record<string, string>;
};

export function emptyDeploy(): DeployForm {
  return { model: "", recipe: "", name: "", servedName: "", device: "gpu", gpus: "1", share: "", replicas: "1", machine: "",
    vram: "", token: "", extraArgs: "", firstAnswer: "", sleepOn: false, idleMinutes: String(IDLE_DEFAULT), wakeWait: "", params: {} };
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
  if (f.sleepOn) {
    p.sleep_when_idle = true;
    if (f.idleMinutes.trim()) p.idle_minutes = Number(f.idleMinutes.trim());
    if (f.wakeWait.trim()) p.wake_wait = Number(f.wakeWait.trim());
  }
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
  sleepOn: boolean;
  idleMinutes: string;   // "" = not set (30 when on)
  wakeWait: string;      // "" = not set (300)
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
    sleepOn: p.sleep_when_idle === true, idleMinutes: typeof p.idle_minutes === "number" ? String(p.idle_minutes) : "",
    wakeWait: typeof p.wake_wait === "number" ? String(p.wake_wait) : "",
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
  // 1.5.0: off is no parameter (null drops it); the idle time is kept as set, also while off.
  p.sleep_when_idle = f.sleepOn ? true : null;
  p.idle_minutes = f.idleMinutes.trim() ? Number(f.idleMinutes.trim()) : null;
  p.wake_wait = f.wakeWait.trim() ? Number(f.wakeWait.trim()) : null;
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
