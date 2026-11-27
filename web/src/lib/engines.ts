// The Engines tab (1.4, #92): what a card says at a glance, and the YAML helpers of "Add a custom engine".
import { imageTag } from "./format";
import { sentText } from "./models";
import type { Recipe, RecipeVariant } from "./types";

export type Hardware = "nvidia" | "amd" | "cpu";
export const HARDWARE_LABEL: Record<Hardware, string> = { nvidia: "NVIDIA", amd: "AMD", cpu: "CPU" };

// The hardware an engine runs on, from its variants, NVIDIA first, each once.
export function engineHardware(r: Recipe): Hardware[] {
  const seen = new Set(r.variants.map((v) => v.vendor));
  return (["nvidia", "amd", "cpu"] as Hardware[]).filter((h) => seen.has(h));
}

// The recipe YAML under another name: its top-level `name:` line is replaced (or added first).
export function renameRecipe(yaml: string, name: string): string {
  const line = /^name:[^\n]*$/m;
  return line.test(yaml) ? yaml.replace(line, `name: ${name}`) : `name: ${name}\n${yaml}`;
}

// A name not taken yet: base, then base-2, base-3…
export function freeName(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base;
  for (let n = 2; ; n += 1) if (!taken.includes(`${base}-${n}`)) return `${base}-${n}`;
}

// A built-in engine's description in the reader's language, while the server sends the English it was translated
// from; a custom engine's (or a changed one) as sent.
export function engineDescription(r: Pick<Recipe, "name" | "builtin" | "description">): string {
  return r.builtin ? sentText(`library.engineDesc.${r.name}`, r.description) : r.description;
}

// 1.5.2 (#189): what an engine runs, shown next to its name: each container image once (by its tag; the full
// reference, digest included, goes in the tooltip) or, for a process variant, its program (the archive's file name,
// or the installed command), in variant order, with the hardware each one is for.
export type EngineRun = { label: string; full: string; vendors: Hardware[] };

export function engineRuns(variants: RecipeVariant[], device?: "gpu" | "cpu"): EngineRun[] {
  const out: EngineRun[] = [];
  for (const v of variants) {
    if (device && (v.vendor === "cpu") !== (device === "cpu")) continue;
    const full = v.runtime === "container" ? v.image : v.engine;
    if (!full) continue;
    const seen = out.find((x) => x.full === full);
    if (seen) {
      if (!seen.vendors.includes(v.vendor)) seen.vendors.push(v.vendor);
      continue;
    }
    out.push({ label: v.runtime === "container" ? imageTag(full) : full.split("/").pop() || full, full, vendors: [v.vendor] });
  }
  return out;
}

// The tooltip of an engine's image line: every image or program, each on its own line with its hardware.
export function engineRunsTitle(runs: EngineRun[]): string {
  return runs.map((r) => (r.vendors.length ? `${r.vendors.map((h) => HARDWARE_LABEL[h]).join(", ")}: ${r.full}` : r.full)).join("\n");
}

// A deployment's: the images its replicas run now, each once (a container replica says which); with none (stopped,
// not placed yet, or process replicas), the engine's own for the deployment's device.
export function deploymentRuns(replicaImages: (string | null | undefined)[], recipe: Pick<Recipe, "variants"> | undefined,
  device: "gpu" | "cpu"): EngineRun[] {
  const images = [...new Set(replicaImages.filter((x): x is string => Boolean(x)))];
  if (images.length) return images.map((full) => ({ label: imageTag(full), full, vendors: [] }));
  return recipe ? engineRuns(recipe.variants, device) : [];
}

