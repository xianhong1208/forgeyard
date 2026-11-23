import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { AuthFrame } from "../components/AuthFrame";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, errorText } from "../lib/api";
import type { User } from "../lib/types";

export function SetupPage() {
  const { state, loadError, reload, setUser } = useAuth();
  const t = useT();
  const navigate = useNavigate();
  const [form, setForm] = useState({ setup_token: "", username: "", email: "", password: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!state) return <Splash error={loadError} onRetry={reload} />;
  if (!state.needs_setup) return <Navigate to="/login" replace />;

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const mismatch = form.confirm.length > 0 && form.confirm !== form.password;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError(null);
    try {
      const { confirm: _c, ...body } = form;
      void _c;
      const r = await api<{ user: User }>("/auth/setup", { method: "POST", body: { ...body, email: body.email || null }, expect401: true });
      setUser(r.user);
      navigate("/", { replace: true }); // the Get started list
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame
      title={t("auth.setup.title")}
      lead={t("auth.setup.lead")}
    >
      {error && <Notice tone="error">{error}</Notice>}
      <form onSubmit={submit} className="stack">
        <Field label={t("auth.setup.token")} name="setup_token" required autoComplete="off" spellCheck={false}
          value={form.setup_token} onChange={set("setup_token")}
          hint={t.rich("auth.setup.tokenHint", { file: <code>data/secrets.json</code> })} />
        <Field label={t("auth.username")} name="username" required autoComplete="username" autoCapitalize="none"
          value={form.username} onChange={set("username")} hint={t("auth.setup.usernameHint")} />
        <Field label={t("auth.setup.email")} name="email" type="email" autoComplete="email" value={form.email} onChange={set("email")} />
        <Field label={t("auth.password")} name="password" type="password" required minLength={12} autoComplete="new-password"
          value={form.password} onChange={set("password")} hint={t("auth.passwordHint")} />
        <Field label={t("auth.repeatPassword")} name="confirm" type="password" required autoComplete="new-password"
          value={form.confirm} onChange={set("confirm")} error={mismatch ? t("auth.mismatch") : null} />
        <button className="btn btn-primary btn-wide" disabled={busy || mismatch}>{busy ? t("auth.setup.busy") : t("auth.setup.submit")}</button>
      </form>
    </AuthFrame>
  );
}
