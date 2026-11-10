import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { AuthFrame } from "../components/AuthFrame";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, ApiError, errorText } from "../lib/api";
import { ENDED_TEXT, HOME, safeNext } from "../lib/nav";
import type { User } from "../lib/types";

export function LoginPage() {
  const { state, loadError, reload, user, setUser } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = safeNext(params.get("next"));
  const ended = params.get("ended");

  if (!state) return <Splash error={loadError} onRetry={reload} />;
  if (state.needs_setup) return <Navigate to="/setup" replace />;
  if (user) return <Navigate to={next} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ user: User }>("/auth/login", { method: "POST", body: { username, password }, expect401: true });
      setUser(r.user);
      navigate(next, { replace: true }); // back to the page that sent us here
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Wrong username or password." : errorText(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title="Sign in" lead={next !== HOME ? <>You'll go back to <code>{next}</code> afterwards.</> : undefined}>
      {ended && ENDED_TEXT[ended] && <Notice>{ENDED_TEXT[ended]}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      <form onSubmit={submit} className="stack">
        <Field label="Username" name="username" autoComplete="username" autoCapitalize="none" required
          value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        <Field label="Password" name="password" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} />
        <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
      <p className="auth-foot">
        {state.registration === "approval" ? (
          <>No account? <Link to="/register">Request one</Link>; an admin approves it.</>
        ) : (
          <>Accounts are created by an admin.</>
        )}
      </p>
    </AuthFrame>
  );
}
