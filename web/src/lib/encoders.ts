// 1.5.4 (#141, #144): the words and the arithmetic of the transformers engine's models in the console: what an
// embedding model takes (texts, images or both), the Playground's similarity table, a zero-shot classifier's
// labels and template, and the languages a model card names.
import { t } from "../i18n/core";
import { getLang, type Lang } from "../i18n/lang";
import type { EmbedModel } from "./types";

export type Modality = "text" | "image";

// What an embedding model takes; one recorded before 1.5.4 (or a cloud model) embeds texts.
export function modalitiesOf(e: EmbedModel | null | undefined): Modality[] {
  const m = (e?.modalities ?? []).filter((x): x is Modality => x === "text" || x === "image");
  return m.length ? m : ["text"];
}

export const embedsImages = (e: EmbedModel | null | undefined) => modalitiesOf(e).includes("image");
export const embedsText = (e: EmbedModel | null | undefined) => modalitiesOf(e).includes("text");

// "Texts and images · 512 dimensions · texts up to 77 tokens"; "Images only · 384 dimensions".
export function embedSummary(e: EmbedModel | null | undefined): string {
  const m = modalitiesOf(e);
  const parts = [m.length === 2 ? t("modelpage.embed.both") : m[0] === "image" ? t("modelpage.embed.images") : t("modelpage.embed.texts")];
  if (e?.dimensions) parts.push(t("modelpage.embed.dims", { count: e.dimensions }));
  if (e?.text_max_tokens && m.includes("text")) parts.push(t("modelpage.embed.textMax", { count: e.text_max_tokens }));
  return parts.join(" · ");
}

// A CLIP-like model (texts and images in one space): its repository can also be a zero-shot image classifier.
export function clipLike(e: { modalities?: string[] } | null | undefined): boolean {
  const m = e?.modalities ?? [];
  return m.includes("text") && m.includes("image");
}

// ------------------------------------------------------------------ vectors
export function dot(a: readonly number[], b: readonly number[]): number {
  let s = 0;
  for (let i = 0; i < Math.min(a.length, b.length); i++) s += a[i] * b[i];
  return s;
}

export function norm(v: readonly number[]): number {
  return Math.sqrt(dot(v, v));
}

// Cosine similarity (the vectors the transformers server sends are normalised already: then it is their dot
// product). 0 for an empty or zero vector.
export function cosine(a: readonly number[], b: readonly number[]): number {
  const n = norm(a) * norm(b);
  return n > 0 ? dot(a, b) / n : 0;
}

// One item of the Playground's Embeddings request: a text, or an image read in the browser (a data: URI) or
// named by an http(s) address the gateway fetches.
export type EmbedItem = { kind: "text"; text: string } | { kind: "image"; name: string; uri: string; url?: boolean };

// The request's `input`: plain strings for texts (a texts-only request stays OpenAI's), {"image": …} for images.
export function requestInput(items: readonly EmbedItem[]): (string | { image: string })[] {
  return items.map((x) => (x.kind === "text" ? x.text : { image: x.uri }));
}

// The similarity table: texts down the side and images across when there are both (text -> image search, the
// point of a CLIP-like model), else every item against every other (images for DINOv2, texts for a text model).
// A cell is null for an item against itself; `best` is each row's most similar column (-1: none).
export type Grid = { rows: number[]; cols: number[]; cells: (number | null)[][]; best: number[]; min: number; max: number };

export function similarityGrid(items: readonly EmbedItem[], vectors: readonly (readonly number[])[]): Grid | null {
  if (items.length !== vectors.length || items.length < 2) return null;
  const texts = items.flatMap((x, i) => (x.kind === "text" ? [i] : []));
  const images = items.flatMap((x, i) => (x.kind === "image" ? [i] : []));
  const rows = texts.length ? texts : images;
  const cols = images.length ? images : texts;
  const cells = rows.map((r) => cols.map((c) => (r === c ? null : cosine(vectors[r], vectors[c]))));
  const values = cells.flat().filter((v): v is number => v !== null);
  if (!values.length) return null;
  const best = cells.map((row) => {
    let at = -1;
    row.forEach((v, j) => { if (v !== null && (at < 0 || v > (row[at] as number))) at = j; });
    return at;
  });
  return { rows, cols, cells, best, min: Math.min(...values), max: Math.max(...values) };
}

// How strongly a cell is shaded, 0 to 1, relative to the table's own range: a CLIP-like model's text-image
// scores sit between about 0.1 and 0.4, so a fixed scale would shade them all alike.
export function shade(v: number | null, g: Pick<Grid, "min" | "max">): number {
  if (v === null) return 0;
  if (g.max - g.min < 1e-9) return 1;
  return (v - g.min) / (g.max - g.min);
}

// An item's short name in the table: "T1 a photo of two c…", "I2 cats.jpg".
export function itemLabel(items: readonly EmbedItem[], i: number, chars = 28): string {
  const x = items[i];
  const n = items.slice(0, i + 1).filter((y) => y.kind === x.kind).length;
  const text = x.kind === "text" ? x.text : x.name;
  return `${x.kind === "text" ? "T" : "I"}${n} ${text.length > chars ? `${text.slice(0, chars - 1).trimEnd()}…` : text}`;
}

// An image's address typed by the person: http(s) only (the gateway refuses anything else); null when it is not one.
export function imageUrl(text: string): string | null {
  try {
    const u = new URL(text.trim());
    return (u.protocol === "http:" || u.protocol === "https:") && u.host ? u.href : null;
  } catch {
    return null;
  }
}

// The name an image by address is shown under: its last path part, else its host.
export function urlName(url: string): string {
  try {
    const u = new URL(url);
    const last = u.pathname.split("/").filter(Boolean).pop();
    return last ? decodeURIComponent(last) : u.host;
  } catch {
    return url;
  }
}

// ------------------------------------------------------------------ zero-shot classification
export const MAX_LABELS = 32;          // the gateway's (forgeyard/gateway/classify.py)
export const MAX_LABEL_CHARS = 64;
export const MAX_TEMPLATE_CHARS = 200;
export const DEFAULT_TEMPLATE = "This text is about {}.";   // the transformers server's
export const ZH_TEMPLATE = "這段文字是關於{}。";
export const DEFAULT_LABELS = ["technology", "food", "complaint", "praise"];

// Labels typed into the chips field: split at commas (also Chinese 、，) and new lines, trimmed, each once
// (case-insensitive, as the gateway counts them), added after the ones already chosen. The error says why some were
// not added.
export function addLabels(current: readonly string[], typed: string): { labels: string[]; error: string | null } {
  const out = [...current];
  const seen = new Set(out.map((x) => x.toLowerCase()));
  let error: string | null = null;
  for (const raw of typed.split(/[,，、\n]/)) {
    const s = raw.trim();
    if (!s) continue;
    if (s.length > MAX_LABEL_CHARS) { error = t("playground.cls.zs.tooLong", { max: MAX_LABEL_CHARS }); continue; }
    if (seen.has(s.toLowerCase())) continue;
    if (out.length >= MAX_LABELS) { error = t("playground.cls.zs.tooMany", { max: MAX_LABELS }); break; }
    out.push(s);
    seen.add(s.toLowerCase());
  }
  return { labels: out, error };
}

// Why a hypothesis template cannot be sent (null: it can; empty means the model's default).
export function templateProblem(s: string): string | null {
  if (!s) return null;
  if (s.length > MAX_TEMPLATE_CHARS || [...s].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127) || s.split("{}").length !== 2) {
    return t("playground.cls.zs.badTemplate", { max: MAX_TEMPLATE_CHARS, braces: "{}" });
  }
  return null;
}

// ------------------------------------------------------------------ languages
// Chinese, Japanese or Korean characters: a text an English-only model reads badly.
export function hasCjk(text: string): boolean {
  return /[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]/.test(text);
}

// A model card that names English alone.
export function englishOnly(langs: readonly string[] | null | undefined): boolean {
  return !!langs && langs.length > 0 && langs.every((x) => x === "en");
}

// A language code in the reader's words ("en" -> English / 英文); "multilingual" as such; an unknown code as it is.
export function languageName(code: string, lang: Lang = getLang()): string {
  if (code === "multilingual") return t("modelpage.lang.multilingual");
  try {
    const names = new Intl.DisplayNames([lang], { type: "language", fallback: "code" });
    return names.of(code) ?? code;
  } catch {
    return code;
  }
}

// "English", "English, French, German and 9 more": at most `max` names, the rest counted (all in `full`).
export function languagesLine(codes: readonly string[], max = 4, lang: Lang = getLang()): { text: string; full: string } {
  const names = codes.map((c) => languageName(c, lang));
  const sep = t("playground.cls.join");
  const full = names.join(sep);
  if (names.length <= max) return { text: full, full };
  return { text: t("modelpage.lang.more", { names: names.slice(0, max).join(sep), count: names.length - max }), full };
}
