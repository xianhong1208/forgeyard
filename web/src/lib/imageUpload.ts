// Uploading an image file to a machine (1.4, issue #35), and copying one from another machine (1.4.1, #104): the
// words of the dialogs.
import { bytes } from "./machines";
import type { Machine } from "./types";

export type RecipeMatch = { recipe: string; variant: string; image: string; how: "checked" | "tag" };
export type LoadedImage = { id: string; tags: string[]; content: string | null; os: string | null; architecture: string | null;
  matches: RecipeMatch[] };
export type UploadResult = { ok: true; bytes: number; images: LoadedImage[] };

export const UPLOAD_ACCEPT = ".tar,.tar.gz,.tgz,application/x-tar,application/gzip";

// Why this machine cannot take an upload now (null: it can).
export function uploadBlocked(m: Machine): string | null {
  if (m.status === "revoked") return `${m.name} was revoked.`;
  if (!(m.features ?? []).includes("image_load")) {
    return `${m.name}'s agent cannot load images: it has no container engine, or it is too old (update the agent).`;
  }
  if (m.status !== "online") return `${m.name} is offline: an image can be uploaded once its agent is connected.`;
  return null;
}

export function uploadLine(phase: string, sent: number, total: number, machine: string): string {
  if (phase === "loading") return `Loading it into ${machine}'s container engine…`;
  const pct = total ? Math.floor((100 * Math.min(sent, total)) / total) : 0;
  return `Sending ${bytes(sent)} of ${bytes(total)} (${pct}%)`;
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
      ? ` Its tag is not the recipe's: run docker tag ${shortId(i.id)} ${want[0]} on the machine to use it.`
      : " Deployments here use it without a pull.";
    return `The pinned image of ${checked.map(name).join(", ")}, checked by its recorded ids.${then}`;
  }
  if (tagged.length) return `Named as the image of ${tagged.map(name).join(", ")}. That recipe names it by a tag only, so it is used unchecked.`;
  return "Not the image of any recipe. A custom recipe can name it by its tag.";
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
    return "No other machine can send an image now: it needs a connected agent of 1.4.1 or later with a container engine.";
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
  if (i.same_as_source === true) return `The same image as on ${source} (same content id).`;
  if (i.same_as_source === false) return `Not the same image as on ${source}: what was loaded differs from what it has.`;
  return "";
}
