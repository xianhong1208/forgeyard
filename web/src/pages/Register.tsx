import { useState, type FormEvent } from "react";
import { Link, Navigate } from "react-router";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { AuthFrame } from "../components/AuthFrame";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, errorText } from "../lib/api";

export function RegisterPage() {
  const { state, loadError, reload } = useAuth();
  const t = useT();
  const [form, setForm] = useState({ username: "", password: "" });
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!state) return <Splash error={loadError} onRetry={reload} />;
  if (state.registration !== "approval") return <Navigate to="/login" replace />;

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/register", { method: "POST", body: form });
      setDone(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title={t("auth.register.title")} lead={t("auth.register.lead")}>
      {done ? (
        <Notice tone="ok">{t("auth.register.sent")} <Link to="/login">{t("auth.backToSignIn")}</Link></Notice>
      ) : (
        <>
          {error && <Notice tone="error">{error}</Notice>}
          <form onSubmit={submit} className="stack">
            <Field label={t("auth.username")} name="username" required autoCapitalize="none" autoComplete="username" value={form.username} onChange={set("username")} />
            <Field label={t("auth.password")} name="password" type="password" required minLength={12} autoComplete="new-password"
              value={form.password} onChange={set("password")} hint={t("auth.passwordHint")} />
            <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? t("auth.register.busy") : t("auth.register.submit")}</button>
          </form>
          <p className="auth-foot"><Link to="/login">{t("auth.backToSignIn")}</Link></p>
        </>
      )}
    </AuthFrame>
  );
}
