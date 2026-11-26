import { useEffect, useState } from "react";

import { api } from "./api";

// GET /api/info (1.6.1, #190): the edition, and what extensions add. The community edition: no features.
export type Info = { version: string; edition: string; features: string[]; audit_actions: string[] };

/** The server's info, asked once per mount and only when `enabled` (only pages with extension entries need it). null
 * until it answers, and when it cannot (an older server): then no extension entry is shown. */
export function useInfo(enabled = true): Info | null {
  const [info, setInfo] = useState<Info | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api<Info>("/info").then(
      (r) => alive && setInfo(r),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [enabled]);
  return info;
}

/** Whether an extension entry is shown: its feature is listed, and an admin-only one to admins only. */
export function shown(info: Info | null, entry: { feature: string; admin?: boolean }, admin: boolean): boolean {
  return !!info && info.features.includes(entry.feature) && (!entry.admin || admin);
}
