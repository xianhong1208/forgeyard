import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";

import { api, setUnauthorizedHandler } from "./lib/api";
import type { AuthState, User } from "./lib/types";

type Auth = {
  state: AuthState | null;
  loadError: string | null;
  user: User | null;
  // Why the last session ended (from a 401), shown on the sign-in page.
  ended: string | null;
  reload: () => Promise<void>;
  setUser: (u: User | null) => void;
  signOut: () => Promise<void>;
};

const Ctx = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ended, setEnded] = useState<string | null>(null);
  const navigate = useNavigate();

  const reload = useCallback(async () => {
    try {
      setState(await api<AuthState>("/auth/state"));
      setLoadError(null);
    } catch {
      // Not reachable or a 5xx: say so, keep whatever we knew. Never treat it as signed out.
      setLoadError("Forgeyard is not answering right now.");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    api<AuthState>("/auth/state").then(
      (s) => {
        if (!alive) return;
        setState(s);
        if (s.ended) setEnded(s.ended);
      },
      () => alive && setLoadError("Forgeyard is not answering right now."),
    );
    return () => {
      alive = false;
    };
  }, []);

  // A 401 anywhere: forget the user. RequireAuth then sends the browser to the sign-in
  // page with the current page as ?next and this reason as ?ended.
  useEffect(() => {
    setUnauthorizedHandler((reason) => {
      setEnded(reason ?? null);
      setState((s) => (s ? { ...s, user: null } : s));
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const setUser = useCallback((u: User | null) => {
    setEnded(null);
    setState((s) => (s ? { ...s, user: u, needs_setup: false, ended: null } : { needs_setup: false, registration: "off", user: u }));
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      setState((s) => (s ? { ...s, user: null } : s));
      navigate("/login?ended=signed_out", { replace: true });
    }
  }, [navigate]);

  const value = useMemo(
    () => ({ state, loadError, user: state?.user ?? null, ended, reload, setUser, signOut }),
    [state, loadError, ended, reload, setUser, signOut],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): Auth {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
