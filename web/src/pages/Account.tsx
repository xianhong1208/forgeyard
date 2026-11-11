import { useState, type FormEvent } from "react";

import { useAuth } from "../auth";
import { Field, Notice, PageHeader, RoleChip } from "../components/ui";
import { api, errorText } from "../lib/api";
import { device, exact, when } from "../lib/format";
import { useApi } from "../lib/useApi";

type SessionRow = {
  id: string;
  current: boolean;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  ip: string | null;
  user_agent: string | null;
};

/** "2 hours", "90 minutes", "1 hour": the idle limit as people say it. */
export function sessionLength(minutes: number): string {
  if (minutes % 60 === 0) return `${minutes / 60} hour${minutes === 60 ? "" : "s"}`;
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

export function AccountPage() {
  const { user, signOut } = useAuth();
  const { data, error: listError, reload: load } = useApi<{ sessions: SessionRow[]; idle_minutes?: number; max_days?: number }>("/auth/sessions");
  const sessions = data?.sessions ?? null;
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [pw, setPw] = useState({ current_password: "", new_password: "", confirm: "" });
  const [busy, setBusy] = useState(false);

  const end = async (s: SessionRow) => {
    if (s.current) return signOut();
    try {
      await api(`/auth/sessions/${s.id}`, { method: "DELETE" });
      setMessage({ tone: "ok", text: `Signed out ${device(s.user_agent)}.` });
      load();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const endOthers = async () => {
    try {
      const r = await api<{ ended: number }>("/auth/sessions/end-others", { method: "POST" });
      setMessage({ tone: "ok", text: r.ended ? `Signed out ${r.ended} other session${r.ended > 1 ? "s" : ""}.` : "No other sessions." });
      load();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const mismatch = pw.confirm.length > 0 && pw.confirm !== pw.new_password;
  const changePassword = async (e: FormEvent) => {
    e.preventDefault();
    if (mismatch) return;
    setBusy(true);
    try {
      const r = await api<{ other_sessions_ended: number }>("/auth/password", {
        method: "POST",
        body: { current_password: pw.current_password, new_password: pw.new_password },
      });
      setPw({ current_password: "", new_password: "", confirm: "" });
      setMessage({
        tone: "ok",
        text: `Password changed.${r.other_sessions_ended ? ` ${r.other_sessions_ended} other session${r.other_sessions_ended > 1 ? "s were" : " was"} signed out.` : ""}`,
      });
      load();
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const others = sessions?.filter((s) => !s.current).length ?? 0;

  return (
    <>
      <PageHeader index="My account" title={user?.username ?? ""} lead="Your password and the browsers signed in as you." />
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <div className="columns">
        <section className="panel">
          <h2>Account</h2>
          <dl className="facts">
            <dt>Username</dt><dd className="mono">{user?.username}</dd>
            <dt>Role</dt><dd>{user && <RoleChip role={user.role} />}{user?.is_owner && <span className="chip chip-owner">owner</span>}</dd>
            <dt>Email</dt><dd>{user?.email || <span className="muted">none</span>}</dd>
            <dt>Last sign-in</dt><dd title={exact(user?.last_login_at)}>{when(user?.last_login_at)}</dd>
          </dl>
          <h2 className="panel-gap">Change password</h2>
          <form className="stack" onSubmit={changePassword}>
            <input type="text" name="username" autoComplete="username" value={user?.username ?? ""} readOnly hidden />
            <Field label="Current password" name="current_password" type="password" autoComplete="current-password" required
              value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} />
            <Field label="New password" name="new_password" type="password" autoComplete="new-password" required minLength={12}
              value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })}
              hint="At least 12 characters. Your other sessions are signed out." />
            <Field label="Repeat new password" name="confirm" type="password" autoComplete="new-password" required
              value={pw.confirm} onChange={(e) => setPw({ ...pw, confirm: e.target.value })} error={mismatch ? "The passwords do not match." : null} />
            <div><button className="btn btn-primary" disabled={busy || mismatch}>{busy ? "Saving…" : "Change password"}</button></div>
          </form>
        </section>
        <section className="panel">
          <div className="panel-head">
            <h2>Sessions</h2>
            {others > 0 && <button type="button" className="btn btn-quiet" onClick={endOthers}>Sign out all others</button>}
          </div>
          {data?.idle_minutes && data?.max_days && (
            <p className="muted small">Sessions end after {sessionLength(data.idle_minutes)} without activity, and after {data.max_days} day{data.max_days === 1 ? "" : "s"} at most.</p>
          )}
          {listError && <Notice tone="error">{listError}</Notice>}
          <ul className="sessions">
            {sessions?.map((s) => (
              <li key={s.id} className={s.current ? "current" : ""}>
                <div className="session-main">
                  <span className="session-device">{device(s.user_agent)}</span>
                  {s.current && <span className="chip chip-here">this browser</span>}
                </div>
                <div className="session-meta">
                  <span className="mono">{s.ip ?? "unknown IP"}</span>
                  <span title={exact(s.created_at)}>signed in {when(s.created_at)}</span>
                  <span title={exact(s.last_seen_at)}>active {when(s.last_seen_at)}</span>
                </div>
                <button type="button" className="btn btn-quiet btn-small" onClick={() => void end(s)}>
                  Sign out
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
