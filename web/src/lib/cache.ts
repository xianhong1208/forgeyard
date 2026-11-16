// A machine's model cache (1.4), as rows for its page: each Hugging Face revision, each GGUF file of
// a revision that has several, single-file models and engine archives; and why a row cannot be removed.
import { bytes } from "./machines";
import type { CacheCatalogRef, CacheUser, MachineCache } from "./types";

export type CacheRow = {
  key: string;
  title: string;
  sub: string;
  bytes: number | null;
  catalog: CacheCatalogRef[];
  usedBy: CacheUser[];
  // A GGUF file shown under its revision.
  part: boolean;
  kind: "model" | "engine";
};

export function cacheRows(c: MachineCache): CacheRow[] {
  const rows: CacheRow[] = [];
  for (const m of c.models) {
    const parts = m.parts ?? [];
    const title = m.kind === "hf" ? m.repo || m.key : m.file || m.catalog[0]?.name || m.key;
    const sub = m.kind === "hf"
      ? [m.revision ? `revision ${m.revision.slice(0, 12)}` : null, m.files !== undefined ? `${m.files} ${m.files === 1 ? "file" : "files"}` : null,
        m.missing ? `${m.missing} missing on disk` : null].filter(Boolean).join(" · ")
      : "single GGUF file from a URL";
    rows.push({ key: m.key, title, sub, bytes: m.bytes, catalog: m.catalog, usedBy: m.used_by, part: false, kind: "model" });
    if (parts.length > 1) {
      for (const p of parts) {
        rows.push({ key: p.key, title: p.file, sub: "one GGUF file of this revision", bytes: p.bytes, catalog: p.catalog,
          usedBy: p.used_by, part: true, kind: "model" });
      }
    }
  }
  for (const e of c.engines) {
    rows.push({ key: e.key, title: e.name || `engine ${e.id}`, sub: `engine archive ${e.id}`, bytes: e.bytes, catalog: [],
      usedBy: e.used_by, part: false, kind: "engine" });
  }
  return rows;
}

// Why Remove is off for this row (null: it can be removed).
export function removeBlocked(c: MachineCache, row: CacheRow): string | null {
  if (c.remove_note) return c.remove_note;
  if (row.usedBy.length) {
    const names = row.usedBy.map((u) => u.name).join(", ");
    return `In use by ${names}. Stop ${row.usedBy.length === 1 ? "it" : "them"} first.`;
  }
  return null;
}

export function removeQuestion(machine: string, row: CacheRow): string {
  const size = row.bytes ? ` (${bytes(row.bytes)})` : "";
  const again = "A deployment that needs it downloads it again.";
  if (row.part) return `Remove ${row.title}${size} from ${machine}'s cache? The other files of this revision stay. ${again}`;
  if (row.kind === "engine") return `Remove the engine ${row.title}${size} from ${machine}'s cache? ${again}`;
  if (row.key.startsWith("hub/")) {
    return `Remove ${row.title}${size} from ${machine}'s cache? Files another revision shares stay. ${again}`;
  }
  return `Remove ${row.title}${size} from ${machine}'s cache? ${again}`;
}

// "7.5 GiB in the cache · 50 GiB free of 100 GiB".
export function diskLine(c: MachineCache): string {
  const parts = [];
  if (c.used_bytes !== null && c.used_bytes !== undefined) parts.push(`${bytes(c.used_bytes)} in the cache`);
  if (c.free_bytes !== null && c.free_bytes !== undefined) {
    parts.push(c.total_bytes ? `${bytes(c.free_bytes)} free of ${bytes(c.total_bytes)}` : `${bytes(c.free_bytes)} free`);
  }
  return parts.join(" · ");
}

// Share of the disk the cache takes, and the share still free (0..1 each).
export function diskShares(c: MachineCache): { cache: number; other: number } | null {
  if (!c.total_bytes || c.free_bytes === null || c.free_bytes === undefined) return null;
  const used = Math.max(0, c.total_bytes - c.free_bytes);
  const cache = Math.min(used, c.used_bytes ?? 0);
  return { cache: cache / c.total_bytes, other: (used - cache) / c.total_bytes };
}

export function userHref(u: CacheUser): string {
  return u.kind === "deployment" ? `/models/deployments/${u.id}` : `/workloads/${u.id}`;
}
