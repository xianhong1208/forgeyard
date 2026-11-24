// The Playground's vision panel (1.4, #86; one picker since #100): vision models, the deployments of detection, SAM 2 and depth models,
// called through /api/playground/vision/<task> with the model's name (the /v1 gateway path) as the signed-in
// person. (The exposed vision apps of 1.3, made from the workload templates, left the console in 1.4 (#88) and were
// removed in 1.5.3 (#89).)

import { labels, t } from "../i18n/core";

// 1.4.4 (#141): image-classify, answered at /v1/vision/classify.
export type VisionTask = "detect" | "segment" | "depth" | "image-classify";

// A vision model (1.4): a route of a vision task, with how many replicas are ready.
export type VisionModel = {
  name: string; task: VisionTask; title: string; endpoint: string; deployment_id: number | null; model: string | null;
  licence: string | null; ready: number; running: boolean; reason: string | null;
  zero_shot?: boolean;     // 1.4.4: a detector told what to find (#142), or a classifier told the labels (#141)
};

export type VisionOptions = {
  models?: VisionModel[];
};

export type Detection = { label: string; score: number; box: [number, number, number, number] };
export type Mask = { score: number; area: number; box: [number, number, number, number] | null; png: string };
export type Scored = { label: string; score: number };

export type VisionAnswer = {
  model?: string; width: number; height: number; ms?: number;
  detections?: Detection[]; masks?: Mask[];
  depth_png?: string; min?: number; max?: number;
  labels?: Scored[]; multi_label?: boolean;     // image classification (1.4.4)
};

// The vision server reads at most 20 MiB per image (MAX_IMAGE_MB); a workload may take less.
export const VISION_MAX_MB = 20;
export const VISION_TYPES = ["image/png", "image/jpeg", "image/webp"];
// Detection asks for everything above this score once; the slider then filters in the browser.
export const DETECT_FLOOR = 0.1;
// Where the slider starts: a detector with fixed classes is sure of what it finds; a zero-shot one scores lower
// (OWLv2's matches are often 0.2 to 0.5).
export const START_THRESHOLD = 0.5;
export const START_THRESHOLD_ZERO_SHOT = 0.3;
// What a zero-shot detector may be told to find (the vision server's limits).
export const MAX_LABELS = 32;
export const MAX_LABEL_CHARS = 64;

// The names typed into "Find these" (comma-separated), trimmed, without repeats; null with why when they cannot be
// sent (none, too many, one too long).
export function parseLabels(text: string): { labels: string[]; error: string | null } {
  const seen = new Set<string>();
  const names: string[] = [];
  for (const raw of text.split(",")) {
    const x = raw.split(/\s+/).filter(Boolean).join(" ");
    if (!x || seen.has(x.toLowerCase())) continue;
    seen.add(x.toLowerCase());
    names.push(x);
  }
  if (!names.length) return { labels: names, error: t("playground.vision.nameOne") };
  if (names.length > MAX_LABELS) return { labels: names, error: t("playground.vision.tooMany", { max: String(MAX_LABELS) }) };
  const long = names.find((x) => x.length > MAX_LABEL_CHARS);
  if (long) return { labels: names, error: t("playground.vision.tooLong", { start: long.slice(0, 20), max: String(MAX_LABEL_CHARS) }) };
  return { labels: names, error: null };
}

export function imageLimitMb(w: { max_body_mb?: number } | undefined): number {
  return Math.min(VISION_MAX_MB, w?.max_body_mb ?? VISION_MAX_MB);
}

// What the panel sends an image to: a vision model.
export type VisionTarget = {
  kind: "model"; id: string; name: string; task: VisionTask; title: string; model: string; licence: string | null;
  ready: number; reason: string | null; max_body_mb?: number; zero_shot: boolean;
  url: string;            // where the console posts the image
  call: string;           // what a program calls, with an API key
  link: { to: string; label: string } | null;
};

// The path a task is answered at, under /v1 (and the console's /api/playground).
export function visionPath(task: VisionTask): string {
  return task === "image-classify" ? "vision/classify" : `vision/${task}`;
}

export function targets(o: VisionOptions | null | undefined): VisionTarget[] {
  const models: VisionTarget[] = (o?.models ?? []).map((m) => ({
    kind: "model", id: `model:${m.name}`, name: m.name, task: m.task, title: m.title, model: m.model ?? m.name,
    licence: m.licence, ready: m.ready, reason: m.reason,
    zero_shot: Boolean(m.zero_shot && (m.task === "detect" || m.task === "image-classify")),
    url: `/api/playground/${visionPath(m.task)}`,
    call: t("playground.vision.call", { path: visionPath(m.task), name: m.name }),
    link: m.deployment_id ? { to: `/serving/${m.deployment_id}`, label: t("playground.bar.deployment") } : null,
  }));
  return models;
}

// Why this file cannot be sent (null: it can). The limit leaves room for the form around the image.
export function checkImage(file: File, limitMb: number): string | null {
  if (!VISION_TYPES.includes(file.type)) return t("playground.vision.notType", { name: file.name || t("playground.vision.thisFile") });
  if (file.size > limitMb * 1024 * 1024 - 64 * 1024) {
    return t("playground.vision.tooBig", { name: file.name || t("playground.vision.theImage"), size: mib(file.size), mb: String(limitMb) });
  }
  return null;
}

function mib(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MiB` : `${Math.ceil(bytes / 1024)} KiB`;
}

// A box in pixels as percentages of the image, for an overlay that scales with it.
export function boxStyle(box: readonly number[], width: number, height: number): { left: string; top: string; width: string; height: string } {
  const clamp = (v: number, hi: number) => Math.max(0, Math.min(hi, v));
  const pct = (v: number, of: number) => `${(v / of * 100).toFixed(3)}%`;
  const [x0, y0, x1, y1] = [clamp(box[0], width), clamp(box[1], height), clamp(box[2], width), clamp(box[3], height)];
  return { left: pct(x0, width), top: pct(y0, height), width: pct(Math.max(0, x1 - x0), width), height: pct(Math.max(0, y1 - y0), height) };
}

export function shown(detections: Detection[], threshold: number): Detection[] {
  return detections.filter((d) => d.score >= threshold);
}

// One colour per label: in the order labels first appear (best score first), so the first few differ.
const PALETTE = ["#f0a020", "#2fb5a3", "#5b8de8", "#e0607a", "#a585f0", "#7cc24a", "#e8894a", "#3fb0de"];
export function labelColours(detections: Detection[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const d of detections) if (!out.has(d.label)) out.set(d.label, PALETTE[out.size % PALETTE.length]);
  return out;
}

// The model's name without its organisation, for a short label.
export function shortModel(model: string): string {
  return model.split("/").pop() || model;
}

// A point on the shown image (CSS pixels from its top left) in the image's own pixels.
export function toImage(x: number, y: number, rect: { width: number; height: number }, width: number, height: number): [number, number] {
  const clamp = (v: number, hi: number) => Math.round(Math.max(0, Math.min(hi, v)));
  return [clamp(x / rect.width * width, width - 1), clamp(y / rect.height * height, height - 1)];
}

// A press and release on the image: a click (a point) when it moved less than `slop` CSS pixels, else a box.
export type Prompt = { kind: "point"; point: [number, number] } | { kind: "box"; box: [number, number, number, number] };
export function promptFrom(a: [number, number], b: [number, number], moved: number, slop = 6): Prompt {
  if (moved < slop) return { kind: "point", point: a };
  return { kind: "box", box: [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])] };
}

// The depth map's colours: a perceptual map from far (dark) to near (bright), applied in the browser to the
// server's 16-bit grey PNG with an SVG filter (feComponentTransfer tables, one per channel).
export const DEPTH_STOPS: [number, number, number][] = [
  [13, 8, 135], [84, 2, 163], [139, 10, 165], [185, 50, 137], [219, 92, 104], [244, 136, 73], [254, 188, 43], [240, 249, 33],
];

export function depthTables(): { r: string; g: string; b: string } {
  const ch = (i: number) => DEPTH_STOPS.map((s) => (s[i] / 255).toFixed(3)).join(" ");
  return { r: ch(0), g: ch(1), b: ch(2) };
}

export function depthGradient(): string {
  return `linear-gradient(90deg, ${DEPTH_STOPS.map((s) => `rgb(${s.join(", ")})`).join(", ")})`;
}

export function pngUri(b64: string): string {
  return `data:image/png;base64,${b64}`;
}

export const ZERO_SHOT_HELP: Partial<Record<VisionTask, string>> = labels({
  detect: "playground.vision.zeroShot.detect",
  "image-classify": "playground.vision.zeroShot.image-classify",
});
export const TASK_HELP: Record<VisionTask, string> = labels({
  detect: "playground.vision.help.detect",
  segment: "playground.vision.help.segment",
  depth: "playground.vision.help.depth",
  "image-classify": "playground.vision.help.image-classify",
});

// How many labels a classification answer shows (the server's top_k: all of a zero-shot classifier's labels).
export const CLASSIFY_TOP_K = 5;
