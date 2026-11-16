import { useSyncExternalStore } from "react";

// The console's phone layout: the same width as the stylesheet's `@media (max-width: 760px)`.
export const NARROW = "(max-width: 760px)";

function query(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(NARROW) : null;
}

function subscribe(changed: () => void): () => void {
  const q = query();
  if (!q) return () => undefined;
  q.addEventListener("change", changed);
  return () => q.removeEventListener("change", changed);
}

// True on a narrow screen, and again false when the window grows past it.
export function useNarrow(): boolean {
  return useSyncExternalStore(subscribe, () => query()?.matches ?? false, () => false);
}
