import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { AuthFrame } from "../components/AuthFrame";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, ApiError, errorText } from "../lib/api";
import { ENDED_TEXT, HOME, safeNext } from "../lib/nav";
import type { User } from "../lib/types";

export function LoginPage() {
  const { state, loadError, reload, user, setUser } = useAuth();
  const t = useT();
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
      setError(err instanceof ApiError && err.status === 401 ? t("auth.login.wrong") : errorText(err));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title={t("auth.login.title")} lead={next !== HOME ? t.rich("auth.login.lead", { next: <code>{next}</code> }) : undefined}>
      {ended && ENDED_TEXT[ended] && <Notice>{ENDED_TEXT[ended]}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      <form onSubmit={submit} className="stack">
        <Field label={t("auth.username")} name="username" autoComplete="username" autoCapitalize="none" required
          value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        <Field label={t("auth.password")} name="password" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} />
        <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? t("auth.login.busy") : t("auth.login.submit")}</button>
      </form>
      <p className="auth-foot">
        {t.rich("auth.login.guide", { link: (text) => <Link to="/guide">{text}</Link> })}
      </p>
      {state.tls?.mode === "forgeyard-ca" && (
        <p className="auth-foot auth-trust">
          {t.rich("auth.login.trust", { link: (text) => <Link to="/trust">{text}</Link> })}
        </p>
      )}
      <p className="auth-foot">
        {state.registration === "approval" ? (
          t.rich("auth.login.request", { link: (text) => <Link to="/register">{text}</Link> })
        ) : (
          t("auth.login.byAdmin")
        )}
      </p>
    </AuthFrame>
  );
}
