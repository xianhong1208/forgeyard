import type { Accelerator, Machine } from "./types";

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

// 85899345920 -> "80 GiB", 4023779328 -> "3.7 GiB", 536870912 -> "512 MiB".
export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  if (n >= 1024 * GIB) return `${trim(n / (1024 * GIB))} TiB`;
  if (n >= GIB) return `${trim(n / GIB)} GiB`;
  if (n >= MIB) return `${Math.round(n / MIB)} MiB`;
  return `${Math.round(n / 1024)} KiB`;
}

function trim(v: number): string {
  if (v >= 100) return String(Math.round(v));
  return v.toFixed(1).replace(/\.0$/, "");
}

export function toMs(ts: string): number {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(ts) ? ts : `${ts}Z`).getTime();
}

// Seconds between two server timestamps (server_time - ts), so the browser's clock does not matter.
export function ageSeconds(serverTime: string, ts: string | null): number | null {
  if (!ts) return null;
  return Math.max(0, Math.round((toMs(serverTime) - toMs(ts)) / 1000));
}

export function ago(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function usedFraction(a: Accelerator): number | null {
  if (!a.vram_total || a.vram_free === null) return null;
  return Math.min(1, Math.max(0, 1 - a.vram_free / a.vram_total));
}

export function gpus(m: Machine): Accelerator[] {
  return m.accelerators.filter((a) => a.vendor !== "cpu");
}

// "8 × NVIDIA A100 80GB PCIe", or "AMD Radeon 8060S" — identical cards are counted once.
export function cardSummary(m: Machine): string {
  const counts = new Map<string, number>();
  for (const a of gpus(m)) counts.set(a.model, (counts.get(a.model) ?? 0) + 1);
  if (counts.size === 0) return "CPU only";
  return [...counts].map(([model, n]) => (n > 1 ? `${n} × ${model}` : model)).join(", ");
}

export function memoryTotals(list: Accelerator[]): { total: number; free: number } {
  let total = 0;
  let free = 0;
  for (const a of list) {
    total += a.vram_total ?? 0;
    free += a.vram_free ?? 0;
  }
  return { total, free };
}

// NVIDIA: "8.0" with "sm_80" beside it; AMD: the gfx target; CPU: the machine architecture.
export function archLabel(a: Accelerator): { main: string; sub?: string } {
  if (!a.arch) return { main: "?" };
  if (a.vendor === "nvidia") return { main: a.arch, sub: typeof a.detail.sm === "string" ? a.detail.sm : undefined };
  return { main: a.arch };
}

export const VENDOR_NAME: Record<string, string> = { nvidia: "NVIDIA", amd: "AMD", cpu: "CPU" };
