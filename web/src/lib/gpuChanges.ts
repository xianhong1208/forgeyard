// 1.5.4: a machine's GPU changes (runtime_info.gpu_changes: GPUs added or removed since its agent last reported them)
// and whether the host has GPUs the agent does not see (runtime_info.gpu_census). Pure, so each case is tested on
// its own; the machine page's GPU access section shows what these return.
import { t } from "../i18n/core";
import type { GpuCensus, GpuChange, GpuChangeCard, Machine } from "./types";

const VENDOR: Record<string, string> = { nvidia: "NVIDIA", amd: "AMD" };

// "GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB)", as the server's Setup health says it.
export function cardLabel(g: GpuChangeCard): string {
  const size = typeof g.vram_total === "number" && g.vram_total > 0 ? `, ${Math.round(g.vram_total / 1024 ** 3)} GiB` : "";
  return `GPU #${g.index} (${g.model || "Unknown"}${size})`;
}

// The newest changes first, at most `most`.
export function recentChanges(m: Pick<Machine, "runtime_info">, most = 5): GpuChange[] {
  const list = m.runtime_info?.gpu_changes;
  return Array.isArray(list) ? [...list].reverse().slice(0, most) : [];
}

// "added GPU #8 (…); removed GPU #3 (…)" in the reader's language.
export function changeText(c: GpuChange): string {
  const parts: string[] = [];
  if (c.added?.length) parts.push(t("machine.gpuChanges.added", { gpus: c.added.map(cardLabel).join(", ") }));
  if (c.removed?.length) parts.push(t("machine.gpuChanges.removed", { gpus: c.removed.map(cardLabel).join(", ") }));
  return parts.join(t("machine.gpuChanges.join"));
}

// Per vendor, as the server classifies it (machines/gpu_changes.census_state): the agent sees every GPU (nothing to
// say), none ("unused": a CPU-only agent on a GPU host, on purpose), some it was limited to on purpose ("limited"), or
// some but not all with nothing limiting it ("hidden": usually a GPU added after the container was made; Setup health
// warns about that one only).
export type CensusState = "unused" | "limited" | "hidden";

export function censusState(v: { host: number; visible: number; missing?: string[]; limited_by?: string }): CensusState | null {
  if (!v || typeof v.host !== "number" || typeof v.visible !== "number" || v.host <= v.visible) return null;
  if (v.visible === 0) return "unused";
  return v.limited_by ? "limited" : "hidden";
}

// What the machine page says, per vendor: "warn" for a hidden GPU, "info" for the deliberate cases.
export function censusLines(census: GpuCensus | undefined): { tone: "warn" | "info"; text: string }[] {
  if (!census || typeof census !== "object" || !census.vendors) return [];
  const out: { tone: "warn" | "info"; text: string }[] = [];
  for (const [vendor, v] of Object.entries(census.vendors)) {
    const state = censusState(v);
    const vars = { host: String(v.host), vendor: VENDOR[vendor] ?? vendor, seen: String(v.visible), how: v.limited_by ?? "" };
    if (state === "hidden") out.push({ tone: "warn", text: t("machine.gpuChanges.hiddenWhat", { hidden: t("machine.gpuChanges.hidden", vars) }) });
    else if (state === "unused") out.push({ tone: "info", text: t("machine.gpuChanges.unused", vars) });
    else if (state === "limited") out.push({ tone: "info", text: t("machine.gpuChanges.limited", vars) });
  }
  return out;
}
