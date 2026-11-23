import { useState, type FormEvent } from "react";

import { useAuth } from "../auth";
import { Field, Notice, PageHeader, RoleChip } from "../components/ui";
import { api, errorText } from "../lib/api";
import { tPlural } from "../i18n/core";
import { useT } from "../i18n/react";
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
  if (minutes % 60 === 0) return tPlural("admin.account.hours", minutes / 60);
  return tPlural("admin.account.minutes", minutes);
}

export function AccountPage() {
  const t = useT();
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
      setMessage({ tone: "ok", text: t("admin.account.signedOut", { device: device(s.user_agent) }) });
      load();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const endOthers = async () => {
    try {
      const r = await api<{ ended: number }>("/auth/sessions/end-others", { method: "POST" });
      setMessage({ tone: "ok", text: r.ended ? t.plural("admin.account.endedOthers", r.ended) : t("admin.account.noOthers") });
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
        text: r.other_sessions_ended ? t.plural("admin.account.changedOthers", r.other_sessions_ended) : t("admin.account.changed"),
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
      <PageHeader index={t("admin.account.index")} title={user?.username ?? ""} lead={t("admin.account.lead")} />
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <div className="columns">
        <section className="panel">
          <h2>{t("admin.account.title")}</h2>
          <dl className="facts">
            <dt>{t("admin.account.username")}</dt><dd className="mono">{user?.username}</dd>
            <dt>{t("admin.account.role")}</dt><dd>{user && <RoleChip role={user.role} />}{user?.is_owner && <span className="chip chip-owner">{t("admin.account.owner")}</span>}</dd>
            {user?.email && <><dt>{t("admin.account.email")}</dt><dd>{user.email}</dd></>}
            <dt>{t("admin.account.lastSignIn")}</dt><dd title={exact(user?.last_login_at)}>{when(user?.last_login_at)}</dd>
          </dl>
          <h2 className="panel-gap">{t("admin.account.changePassword")}</h2>
          <form className="stack" onSubmit={changePassword}>
            <input type="text" name="username" autoComplete="username" value={user?.username ?? ""} readOnly hidden />
            <Field label={t("admin.account.current")} name="current_password" type="password" autoComplete="current-password" required
              value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} />
            <Field label={t("admin.account.newPassword")} name="new_password" type="password" autoComplete="new-password" required minLength={12}
              value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })}
              hint={t("admin.account.newHint")} />
            <Field label={t("admin.account.repeat")} name="confirm" type="password" autoComplete="new-password" required
              value={pw.confirm} onChange={(e) => setPw({ ...pw, confirm: e.target.value })} error={mismatch ? t("admin.account.mismatch") : null} />
            <div><button className="btn btn-primary" disabled={busy || mismatch}>{busy ? t("admin.account.saving") : t("admin.account.changePassword")}</button></div>
          </form>
        </section>
        <section className="panel">
          <div className="panel-head">
            <h2>{t("admin.account.sessions")}</h2>
            {others > 0 && <button type="button" className="btn btn-quiet" onClick={endOthers}>{t("admin.account.signOutOthers")}</button>}
          </div>
          {data?.idle_minutes && data?.max_days && (
            <p className="muted small">{t.plural("admin.account.sessionsNote", data.max_days, { idle: sessionLength(data.idle_minutes) })}</p>
          )}
          {listError && <Notice tone="error">{listError}</Notice>}
          <ul className="sessions">
            {sessions?.map((s) => (
              <li key={s.id} className={s.current ? "current" : ""}>
                <div className="session-main">
                  <span className="session-device">{device(s.user_agent)}</span>
                  {s.current && <span className="chip chip-here">{t("admin.account.thisBrowser")}</span>}
                </div>
                <div className="session-meta">
                  <span className="mono">{s.ip ?? t("admin.account.unknownIp")}</span>
                  <span title={exact(s.created_at)}>{t("admin.account.signedIn", { when: when(s.created_at) })}</span>
                  <span title={exact(s.last_seen_at)}>{t("admin.account.active", { when: when(s.last_seen_at) })}</span>
                </div>
                <button type="button" className="btn btn-quiet btn-small" onClick={() => void end(s)}>
                  {t("admin.account.signOut")}
                </button>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}
