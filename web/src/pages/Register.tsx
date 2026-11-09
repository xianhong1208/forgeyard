import { useState, type FormEvent } from "react";
import { Link, Navigate } from "react-router";

import { useAuth } from "../auth";
import { AuthFrame } from "../components/AuthFrame";
import { Splash } from "../components/Splash";
import { Field, Notice } from "../components/ui";
import { api, errorText } from "../lib/api";

export function RegisterPage() {
  const { state, loadError, reload } = useAuth();
  const [form, setForm] = useState({ username: "", email: "", password: "" });
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
      await api("/auth/register", { method: "POST", body: { ...form, email: form.email || null } });
      setDone(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title="Request an account" lead="An admin has to approve new accounts before they can sign in.">
      {done ? (
        <Notice tone="ok">Request sent. You can sign in once an admin approves it. <Link to="/login">Back to sign in</Link></Notice>
      ) : (
        <>
          {error && <Notice tone="error">{error}</Notice>}
          <form onSubmit={submit} className="stack">
            <Field label="Username" name="username" required autoCapitalize="none" autoComplete="username" value={form.username} onChange={set("username")} />
            <Field label="Email (optional)" name="email" type="email" autoComplete="email" value={form.email} onChange={set("email")} />
            <Field label="Password" name="password" type="password" required minLength={12} autoComplete="new-password"
              value={form.password} onChange={set("password")} hint="At least 12 characters; common passwords are refused." />
            <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? "Sending…" : "Request account"}</button>
          </form>
          <p className="auth-foot"><Link to="/login">Back to sign in</Link></p>
        </>
      )}
    </AuthFrame>
  );
}
