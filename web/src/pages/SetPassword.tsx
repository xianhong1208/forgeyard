import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { AuthFrame } from "../components/AuthFrame";
import { useT } from "../i18n/react";
import { Field, Notice } from "../components/ui";
import { api, errorText } from "../lib/api";

function tokenFromHash(): string {
  return new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token") ?? "";
}

export function SetPasswordPage() {
  const t = useT();
  const [token] = useState(tokenFromHash);
  const [username, setUsername] = useState<string | null>(null);
  const [invalid, setInvalid] = useState<string | null>(token ? null : "incomplete");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    api<{ username: string }>("/auth/password-link/check", { method: "POST", body: { token } })
      .then((r) => setUsername(r.username))
      .catch((e) => setInvalid(errorText(e)));
  }, [token]);

  const mismatch = confirm.length > 0 && confirm !== password;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password-link", { method: "POST", body: { token, password } });
      setDone(true);
      history.replaceState(null, "", "/set-password"); // drop the used token from the address bar
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame title={t("auth.setPassword.title")} lead={username ? t.rich("auth.setPassword.lead", { username: <strong>{username}</strong> }) : undefined}>
      {invalid ? (
        <Notice tone="error">{invalid === "incomplete" ? t("auth.setPassword.incomplete") : invalid}</Notice>
      ) : done ? (
        <Notice tone="ok">{t("auth.setPassword.done")} <Link to="/login">{t("auth.setPassword.signIn")}</Link></Notice>
      ) : username ? (
        <>
          {error && <Notice tone="error">{error}</Notice>}
          <form onSubmit={submit} className="stack">
            <input type="text" name="username" autoComplete="username" value={username} readOnly hidden />
            <Field label={t("auth.newPassword")} name="password" type="password" required minLength={12} autoComplete="new-password"
              value={password} onChange={(e) => setPassword(e.target.value)} hint={t("auth.passwordHint")} />
            <Field label={t("auth.repeatPassword")} name="confirm" type="password" required autoComplete="new-password"
              value={confirm} onChange={(e) => setConfirm(e.target.value)} error={mismatch ? t("auth.mismatch") : null} />
            <button className="btn btn-primary btn-wide" disabled={busy || mismatch}>{busy ? t("auth.setPassword.busy") : t("auth.setPassword.submit")}</button>
          </form>
        </>
      ) : (
        <p className="muted">{t("auth.setPassword.checking")}</p>
      )}
    </AuthFrame>
  );
}
