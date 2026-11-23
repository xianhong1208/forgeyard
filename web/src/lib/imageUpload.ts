// Uploading an image file to a machine (1.4, issue #35), and copying one from another machine (1.4.1, #104): the
// words of the dialogs.
import { t as tr } from "../i18n/core";
import { bytes } from "./machines";
import type { Machine } from "./types";

export type RecipeMatch = { recipe: string; variant: string; image: string; how: "checked" | "tag" };
export type LoadedImage = { id: string; tags: string[]; content: string | null; os: string | null; architecture: string | null;
  matches: RecipeMatch[] };
export type UploadResult = { ok: true; bytes: number; images: LoadedImage[] };

export const UPLOAD_ACCEPT = ".tar,.tar.gz,.tgz,application/x-tar,application/gzip";

// Why this machine cannot take an upload now (null: it can).
export function uploadBlocked(m: Machine): string | null {
  if (m.status === "revoked") return tr("machine.img.revoked", { name: m.name });
  if (!(m.features ?? []).includes("image_load")) {
    return tr("machine.img.cannotLoad", { name: m.name });
  }
  if (m.status !== "online") return tr("machine.img.offline", { name: m.name });
  return null;
}

export function uploadLine(phase: string, sent: number, total: number, machine: string): string {
  if (phase === "loading") return tr("machine.img.loadingInto", { machine });
  const pct = total ? Math.floor((100 * Math.min(sent, total)) / total) : 0;
  return tr("machine.img.sending", { sent: bytes(sent), total: bytes(total), pct: String(pct) });
}

export function shortId(id: string): string {
  return id.replace(/^sha256:/, "").slice(0, 12);
}

// Which recipe's image it is: by its recorded ids (a pinned image), or only named like a tag-only one.
export function matchLine(i: LoadedImage): string {
  const checked = i.matches.filter((x) => x.how === "checked");
  const tagged = i.matches.filter((x) => x.how === "tag");
  const name = (x: RecipeMatch) => `${x.recipe} (${x.variant})`;
  if (checked.length) {
    const want = [...new Set(checked.map((x) => x.image.split("@")[0]))].filter((t) => !i.tags.includes(t));
    const then = want.length
      ? tr("machine.img.match.retag", { id: shortId(i.id), tag: want[0] })
      : tr("machine.img.match.noPull");
    return tr("machine.img.match.pinned", { recipes: checked.map(name).join(", "), then });
  }
  if (tagged.length) return tr("machine.img.match.tag", { recipes: tagged.map(name).join(", ") });
  return tr("machine.img.match.none");
}

// 1.4.1 (#104): copying an image from another machine. An image as a machine's agent lists it.
export type ListedImage = { id: string; tags: string[]; content: string | null; size: number | null; created: string | null;
  os: string | null; architecture: string | null; matches: RecipeMatch[] };
export type CopiedImage = LoadedImage & { same_as_source: boolean | null };
export type CopyResult = { ok: true; bytes: number; images: CopiedImage[]; source: { id: number; name: string } };

// The machines an image can be copied from: the others whose agent can send images (1.4.1, a container engine),
// connected now.
export function copySources(m: Machine, all: Machine[]): Machine[] {
  return all.filter((x) => x.id !== m.id && x.status === "online" && (x.features ?? []).includes("image_save"));
}

// Why no image can be copied to this machine now (null: it can).
export function copyBlocked(m: Machine, all: Machine[]): string | null {
  const blocked = uploadBlocked(m);
  if (blocked) return blocked;
  if (!copySources(m, all).length) {
    return tr("machine.img.noSource");
  }
  return null;
}

// "llama.cpp:server · 1a2b3c4d5e6f · 1.9 GiB · linux/amd64".
export function listedLine(i: ListedImage): string {
  const parts = [shortId(i.id)];
  if (i.size) parts.push(bytes(i.size));
  if (i.architecture) parts.push(`${i.os || "linux"}/${i.architecture}`);
  return parts.join(" · ");
}

// Whether what was loaded is the image the source has.
export function sameLine(i: CopiedImage, source: string): string {
  if (i.same_as_source === true) return tr("machine.img.same", { source });
  if (i.same_as_source === false) return tr("machine.img.notSame", { source });
  return "";
}

// 1.4.2 (#124): the images a recipe names first, and only those unless the admin asks for all of them (on a shared
// host the other machine's engine holds other people's images too).
export function copyChoices(list: ListedImage[], showAll: boolean): ListedImage[] {
  const named = list.filter((i) => i.matches.length > 0);
  return showAll ? [...named, ...list.filter((i) => i.matches.length === 0)] : named;
}

// 1.4.2 (#123): a machine's engine images on its page: the ones a recipe names, and removing one it no longer uses.
export type EngineImageUser = { name: string; kind: "deployment" | "workload"; id: number };
export type EngineImage = ListedImage & { used_by: EngineImageUser[]; agent: boolean; remove_note: string | null };
export type EngineImagesView = {
  machine_id: number; listed: boolean; note: string | null; images: EngineImage[]; others: number;
  free_bytes: number | null; total_bytes: number | null; can_remove: boolean; remove_note: string | null;
};
export type ImageRemoved = { ok: true; image: ListedImage; freed_bytes: number;
  free_bytes: number | null; total_bytes: number | null };

// "llama.cpp (cpu), vision (cuda)": the recipes that name it.
export function recipesLine(i: EngineImage): string {
  const seen = [...new Set(i.matches.map((x) => `${x.recipe} (${x.variant})`))];
  const how = i.matches.some((x) => x.how === "checked") ? tr("machine.img.howPinned") : tr("machine.img.howTag");
  return `${seen.join(", ")} · ${how}`;
}

// "40 GiB free of 100 GiB" where the engine keeps its images, when the agent can see it.
export function engineDiskLine(v: { free_bytes: number | null; total_bytes: number | null }): string | null {
  if (v.free_bytes === null || v.free_bytes === undefined) return null;
  return v.total_bytes ? tr("machine.freeOf", { free: bytes(v.free_bytes), total: bytes(v.total_bytes) }) : tr("machine.free", { free: bytes(v.free_bytes) });
}

export function engineRemoveQuestion(machine: string, i: EngineImage): string {
  const name = i.tags[0] ?? shortId(i.id);
  const size = i.size ? tr("machine.cache.size", { size: bytes(i.size) }) : "";
  return tr("machine.img.removeQuestion", { name, size, machine });
}

// What the console says once an image is gone: up to its size came back (layers another image shares stay), and the
// space where the engine keeps its images now, when the agent can see it.
export function removedLine(machine: string, r: ImageRemoved): string {
  const name = r.image.tags[0] ?? shortId(r.image.id);
  const disk = engineDiskLine(r);
  return tr("machine.img.removed", { name, machine, freed: bytes(r.freed_bytes), now: disk ? tr("machine.img.removedNow", { disk }) : "" });
}
