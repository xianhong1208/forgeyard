import { useEffect } from "react";

// How often the pages that show running replicas ask again.
export const WORKLOADS_POLL_MS = 3000;

// Calls `reload` every `ms` while the page is visible.
export function usePoll(reload: () => void, ms: number) {
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState !== "hidden") reload();
    }, ms);
    return () => window.clearInterval(id);
  }, [reload, ms]);
}
