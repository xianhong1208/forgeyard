import { useCallback, useEffect, useState } from "react";

import { api, errorText } from "./api";

// GET a path, re-fetch on reload(). Errors are kept for display; a 401 still goes
// through api()'s sign-in redirect. A null path fetches nothing.
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (path === null) return;
    let alive = true;
    api<T>(path).then(
      (r) => {
        if (!alive) return;
        setData(r);
        setError(null);
      },
      (e) => {
        if (alive) setError(errorText(e));
      },
    );
    return () => {
      alive = false;
    };
  }, [path, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, reload };
}
