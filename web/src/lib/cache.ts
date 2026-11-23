// A machine's model cache (1.4), as rows for its page: each Hugging Face revision, each GGUF file of
// a revision that has several, single-file models and engine archives; and why a row cannot be removed.
import { t, tPlural } from "../i18n/core";
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
      ? [m.revision ? t("machine.cache.revision", { revision: m.revision.slice(0, 12) }) : null,
        m.files !== undefined ? tPlural("machine.cache.files", m.files) : null,
        m.missing ? t("machine.cache.missing", { count: m.missing }) : null].filter(Boolean).join(" · ")
      : t("machine.cache.urlFile");
    rows.push({ key: m.key, title, sub, bytes: m.bytes, catalog: m.catalog, usedBy: m.used_by, part: false, kind: "model" });
    if (parts.length > 1) {
      for (const p of parts) {
        rows.push({ key: p.key, title: p.file, sub: t("machine.cache.part"), bytes: p.bytes, catalog: p.catalog,
          usedBy: p.used_by, part: true, kind: "model" });
      }
    }
  }
  for (const e of c.engines) {
    rows.push({ key: e.key, title: e.name || t("machine.cache.engine", { id: e.id }), sub: t("machine.cache.engineArchive", { id: e.id }), bytes: e.bytes, catalog: [],
      usedBy: e.used_by, part: false, kind: "engine" });
  }
  return rows;
}

// Why Remove is off for this row (null: it can be removed).
export function removeBlocked(c: MachineCache, row: CacheRow): string | null {
  if (c.remove_note) return c.remove_note;
  if (row.usedBy.length) {
    const names = row.usedBy.map((u) => u.name).join(", ");
    return tPlural("machine.cache.inUseBy", row.usedBy.length, { names });
  }
  return null;
}

export function removeQuestion(machine: string, row: CacheRow): string {
  const size = row.bytes ? t("machine.cache.size", { size: bytes(row.bytes) }) : "";
  const vars = { title: row.title, size, machine };
  if (row.part) return t("machine.cache.removePart", vars);
  if (row.kind === "engine") return t("machine.cache.removeEngine", vars);
  if (row.key.startsWith("hub/")) return t("machine.cache.removeHub", vars);
  return t("machine.cache.remove", vars);
}

// "7.5 GiB in the cache · 50 GiB free of 100 GiB".
export function diskLine(c: MachineCache): string {
  const parts = [];
  if (c.used_bytes !== null && c.used_bytes !== undefined) parts.push(t("machine.cache.inCache", { size: bytes(c.used_bytes) }));
  if (c.free_bytes !== null && c.free_bytes !== undefined) {
    parts.push(c.total_bytes ? t("machine.freeOf", { free: bytes(c.free_bytes), total: bytes(c.total_bytes) }) : t("machine.free", { free: bytes(c.free_bytes) }));
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

// A deployment's page; an app has none in the console since 1.4 (#88): it is only named.
export function userHref(u: CacheUser): string | null {
  return u.kind === "deployment" ? `/serving/${u.id}` : null;
}
