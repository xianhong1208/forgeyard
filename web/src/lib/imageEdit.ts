// 1.5.5 (#143): images given to a model. An image model that edits (/v1/images/edits: a prompt and 1..N input
// images) and a video model that takes a start image (/v1/videos input_reference: optional or required). What the
// Playground sends, what the model page and the catalog say about it.
import { t } from "../i18n/core";
import { MAX_IMAGE_BYTES, type ImageOptions } from "./gateway";
import type { CatalogModel, StartImage } from "./types";

// An image picked in the browser: shown from its data: URI, sent as the file itself.
export type Picked = { name: string; uri: string; file: File };

// What both engines read (the gateway refuses the rest: HEIC, GIF, BMP…).
export const INPUT_TYPES = ["image/png", "image/jpeg", "image/webp"];
export const INPUT_ACCEPT = INPUT_TYPES.join(",");

export type ImageMode = "generate" | "edit";

// What the Image panel offers for a model: text to image, edits, or both (generate first).
export function imageModes(o: Pick<ImageOptions, "generate" | "edit"> | null): ImageMode[] {
  if (!o) return ["generate"];
  const modes: ImageMode[] = [];
  if (o.generate !== false) modes.push("generate");
  if ((o.edit ?? 0) > 0) modes.push("edit");
  return modes.length ? modes : ["generate"];
}

// Why these files cannot be added to `have` images when at most `max` are taken (null: they can), before any is read.
export function pickProblem(files: File[], have: number, max: number): string | null {
  if (have + files.length > max) {
    return max === 1 ? t("playground.edit.onlyOne") : t("playground.edit.tooMany", { max: String(max) });
  }
  for (const f of files) {
    if (!INPUT_TYPES.includes(f.type)) return t("playground.edit.type", { name: f.name });
    if (f.size > MAX_IMAGE_BYTES) return t("playground.image.tooLarge", { name: f.name, mb: String(MAX_IMAGE_BYTES / 1024 / 1024) });
  }
  return null;
}

// The edit as OpenAI's SDK sends it: multipart, the images as `image[]` files in order. No size: the first input's
// shape at the deployment's default number of pixels (the gateway picks it). No format: PNG.
export function editForm(model: string, prompt: string, n: number, size: string, format: string, images: Picked[]): FormData {
  const form = new FormData();
  form.append("model", model);
  form.append("prompt", prompt);
  form.append("n", String(n));
  if (size) form.append("size", size);
  if (format) form.append("output_format", format);
  for (const p of images) form.append("image[]", p.file, p.name);
  return form;
}

// A video with a start image: multipart, the image as the file `input_reference` (what the SDK sends).
export function videoForm(model: string, prompt: string, size: string, seconds: string, image: Picked): FormData {
  const form = new FormData();
  form.append("model", model);
  form.append("prompt", prompt);
  if (size) form.append("size", size);
  form.append("seconds", seconds);
  form.append("input_reference", image.file, image.name);
  return form;
}

// ------------------------------------------------------------------ what a catalog model does with images
export type Ability = { key: "edits" | "editsOnly" | "i2v"; text: string; title: string };

// The chips beside a model's task: "Edits images" (up to N input images; "only" when it makes nothing from text) and
// "Image to video" (a start image optional or required). None for a model that takes no image.
export function abilities(m: Pick<CatalogModel, "task" | "image" | "video">): Ability[] {
  const out: Ability[] = [];
  const edit = m.task === "image" ? m.image?.edit ?? 0 : 0;
  if (edit > 0) {
    const only = m.image?.generate === false;
    out.push({ key: only ? "editsOnly" : "edits", text: t(only ? "modelpage.can.editsOnly" : "modelpage.can.edits"),
      title: t(edit === 1 ? "modelpage.can.editsTitle.one" : "modelpage.can.editsTitle.other", { count: String(edit) }) });
  }
  const start = m.task === "video" ? startImage(m.video?.image_input) : "none";
  if (start !== "none") {
    out.push({ key: "i2v", text: t(start === "required" ? "modelpage.can.i2vRequired" : "modelpage.can.i2vOptional"),
      title: t(start === "required" ? "modelpage.can.i2vRequiredTitle" : "modelpage.can.i2vOptionalTitle") });
  }
  return out;
}

export function startImage(v: StartImage | null | undefined): StartImage {
  return v === "optional" || v === "required" ? v : "none";
}

// The deployment parameters for images given to a model apply only to a model that takes them: Input images and
// Largest input for an image model that edits, Largest start image for a video model that takes one. A model whose
// catalog entry does not say (unknown) shows them.
export function inputParamApplies(name: string, m: Pick<CatalogModel, "task" | "image" | "video"> | undefined): boolean {
  if (!m || (name !== "max_input_images" && name !== "max_input_side")) return true;
  if (m.task === "image") return m.image?.edit === undefined || (m.image?.edit ?? 0) > 0;
  if (m.task === "video") return m.video?.image_input === undefined || startImage(m.video.image_input) !== "none";
  return true;
}
