import { useState, type FormEvent } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { AuthFrame } from "../components/AuthFrame";
import { SignInWith } from "../components/Sso";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, ApiError, errorText } from "../lib/api";
import { ENDED_TEXT, HOME, safeNext } from "../lib/nav";
import type { User } from "../lib/types";

// 1.6.0 (#39): an account with an authenticator app answers a challenge after the right password; the code (or a
// recovery code) then signs in.
type LoginAnswer = { user: User } | { mfa_required: true; challenge: string; expires_in: number };

export function LoginPage() {
  const { state, loadError, reload, user, setUser } = useAuth();
  const t = useT();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState(false);
  const next = safeNext(params.get("next"));
  const ended = params.get("ended");

  if (!state) return <Splash error={loadError} onRetry={reload} />;
  if (state.needs_setup) return <Navigate to="/setup" replace />;
  if (user) return <Navigate to={next} replace />;

  const signedIn = (u: User) => {
    setUser(u);
    navigate(next, { replace: true }); // back to the page that sent us here
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<LoginAnswer>("/auth/login", { method: "POST", body: { username, password }, expect401: true });
      if ("mfa_required" in r) {
        setChallenge(r.challenge);
        setCode("");
        setRecovery(false);
      } else {
        signedIn(r.user);
      }
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? t("auth.login.wrong") : errorText(err));
    } finally {
      setPassword("");
      setBusy(false);
    }
  };

  const back = (message: string | null = null) => {
    setChallenge(null);
    setCode("");
    setError(message);
  };

  const submitCode = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ user: User }>("/auth/mfa", { method: "POST", body: { challenge, code } });
      signedIn(r.user);
    } catch (err) {
      setCode("");
      if (err instanceof ApiError && err.code === "mfa_challenge_expired") back(t("auth.mfa.expired"));
      else if (err instanceof ApiError && err.code === "mfa_code_invalid" && typeof err.detail.attempts_left === "number") {
        if (err.detail.attempts_left > 0) setError(t.plural("auth.mfa.wrong", err.detail.attempts_left));
        else back(t("auth.mfa.expired"));
      } else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (challenge) {
    return (
      <AuthFrame title={t("auth.mfa.title")} lead={recovery ? t("auth.mfa.leadRecovery") : t("auth.mfa.lead")}>
        {error && <Notice tone="error">{error}</Notice>}
        <form onSubmit={submitCode} className="stack">
          {recovery ? (
            <Field key="recovery" label={t("auth.mfa.recoveryCode")} name="recovery-code" autoComplete="off" autoCapitalize="none"
              spellCheck={false} required maxLength={40} value={code} onChange={(e) => setCode(e.target.value)} autoFocus />
          ) : (
            <Field key="code" label={t("auth.mfa.code")} name="code" inputMode="numeric" autoComplete="one-time-code" required
              pattern="[0-9 ]{6,7}" maxLength={7} value={code} onChange={(e) => setCode(e.target.value)} autoFocus />
          )}
          <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? t("auth.mfa.busy") : t("auth.mfa.submit")}</button>
        </form>
        <p className="auth-foot auth-switch">
          <button type="button" className="linkish" onClick={() => { setRecovery((v) => !v); setCode(""); setError(null); }}>
            {recovery ? t("auth.mfa.useApp") : t("auth.mfa.useRecovery")}
          </button>
        </p>
        <p className="auth-foot">
          <button type="button" className="linkish" onClick={() => back()}>{t("auth.backToSignIn")}</button>
        </p>
      </AuthFrame>
    );
  }

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
      {state.sso && <SignInWith name={state.sso.name} next={next} />}
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
