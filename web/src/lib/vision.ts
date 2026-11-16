// The Playground's Vision tab (1.4, #86): vision models, the deployments of detection, SAM 2 and depth models,
// called through /api/playground/vision/<task> with the model's name (the /v1 gateway path) as the signed-in
// person. The exposed vision apps of 1.3 (workload templates) are not offered: apps left the console in 1.4
// (#88; deprecated, they keep running at /x/ until 1.5), and the options no longer list them.

export type VisionTask = "detect" | "segment" | "depth";

// A vision model (1.4): a route of a vision task, with how many replicas are ready.
export type VisionModel = {
  name: string; task: VisionTask; title: string; endpoint: string; deployment_id: number | null; model: string | null;
  licence: string | null; ready: number; running: boolean; reason: string | null;
};

export type VisionOptions = {
  models?: VisionModel[];
  tasks: { task: VisionTask; title: string; template: string; model: string }[];
};

export type Detection = { label: string; score: number; box: [number, number, number, number] };
export type Mask = { score: number; area: number; box: [number, number, number, number] | null; png: string };

export type VisionAnswer = {
  model?: string; width: number; height: number; ms?: number;
  detections?: Detection[]; masks?: Mask[];
  depth_png?: string; min?: number; max?: number;
};

// The vision server reads at most 20 MiB per image (MAX_IMAGE_MB); a workload may take less.
export const VISION_MAX_MB = 20;
export const VISION_TYPES = ["image/png", "image/jpeg", "image/webp"];
// Detection asks for everything above this score once; the slider then filters in the browser.
export const DETECT_FLOOR = 0.1;

export function imageLimitMb(w: { max_body_mb?: number } | undefined): number {
  return Math.min(VISION_MAX_MB, w?.max_body_mb ?? VISION_MAX_MB);
}

// What the tab sends an image to: a vision model.
export type VisionTarget = {
  kind: "model"; id: string; name: string; task: VisionTask; title: string; model: string; licence: string | null;
  ready: number; reason: string | null; max_body_mb?: number;
  url: string;            // where the console posts the image
  call: string;           // what a program calls, with an API key
  link: { to: string; label: string } | null;
};

export function targets(o: VisionOptions | null | undefined): VisionTarget[] {
  const models: VisionTarget[] = (o?.models ?? []).map((m) => ({
    kind: "model", id: `model:${m.name}`, name: m.name, task: m.task, title: m.title, model: m.model ?? m.name,
    licence: m.licence, ready: m.ready, reason: m.reason, url: `/api/playground/vision/${m.task}`,
    call: `POST /v1/vision/${m.task} with model=${m.name}`,
    link: m.deployment_id ? { to: `/serving/${m.deployment_id}`, label: "Deployment" } : null,
  }));
  return models;
}

export function targetLabel(t: VisionTarget): string {
  return `${t.name} · ${t.title} · ${shortModel(t.model)}${t.ready ? "" : " (not ready)"}`;
}

// Why this file cannot be sent (null: it can). The limit leaves room for the form around the image.
export function checkImage(file: File, limitMb: number): string | null {
  if (!VISION_TYPES.includes(file.type)) return `${file.name || "This file"} is not a PNG, JPEG or WebP image.`;
  if (file.size > limitMb * 1024 * 1024 - 64 * 1024) return `${file.name || "The image"} is ${mib(file.size)}: the most it takes is ${limitMb} MiB.`;
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

export const TASK_HELP: Record<VisionTask, string> = {
  detect: "Finds objects and draws a box, label and score for each.",
  segment: "Click a point on the image, or drag a box around something: the model outlines it.",
  depth: "Estimates how near each pixel is and shows it as a depth map beside the photo.",
};
