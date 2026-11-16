// The Engines tab (1.4, #92): what a card says at a glance, and the YAML helpers of "Add an engine".
import type { Recipe } from "./types";

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
