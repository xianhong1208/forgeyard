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

// "The host has 9 NVIDIA GPUs; its agent sees 8", or null when it sees them all (or its agent does not count).
export function hiddenText(census: GpuCensus | undefined): string | null {
  if (!census || typeof census !== "object" || !census.vendors) return null;
  const short = Object.entries(census.vendors).filter(([, v]) => v && v.host > v.visible);
  if (short.length === 0) return null;
  return short
    .map(([vendor, v]) => t("machine.gpuChanges.hidden", { host: String(v.host), vendor: VENDOR[vendor] ?? vendor, seen: String(v.visible) }))
    .join(t("machine.gpuChanges.join"));
}
