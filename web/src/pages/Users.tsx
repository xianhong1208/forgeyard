import { IconPlus } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";

import { AdvisorBanner } from "../components/Advisor";
import { MfaPolicyPanel } from "../components/MfaRequired";
import { SsoProviderPanel } from "../components/Sso";
import { formatDate } from "../i18n/format";
import { useAuth } from "../auth";
import { CopyBox, Field, Notice, PageHeader, StatusChip } from "../components/ui";
import { api, errorText } from "../lib/api";
import { useT } from "../i18n/react";
import { exact, when } from "../lib/format";
import type { Role, User } from "../lib/types";
import { useApi } from "../lib/useApi";

type Link = { username: string; url: string; hours: number };

export function UsersPage() {
  const t = useT();
  const { user: me } = useAuth();
  const { data, error: loadError, reload: load } = useApi<{ users: User[] }>("/users");
  const users = data?.users ?? null;
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [link, setLink] = useState<Link | null>(null);
  const [adding, setAdding] = useState(false);
  // No email field (1.3.1, D11): nothing sends mail. The API still takes one, and the list shows it.
  const [form, setForm] = useState({ username: "", role: "user" as Role });
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      load();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const r = await api<{ user: User; set_password_url: string; expires_hours: number }>("/users", {
        method: "POST",
        body: { username: form.username, role: form.role },
      });
      setLink({ username: r.user.username, url: r.set_password_url, hours: r.expires_hours });
      setForm({ username: "", role: "user" });
      setAdding(false);
      load();
    } catch (err) {
      setMessage({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const resetLink = async (u: User) => {
    try {
      const r = await api<{ set_password_url: string; expires_hours: number }>(`/users/${u.id}/password-link`, { method: "POST" });
      setLink({ username: u.username, url: r.set_password_url, hours: r.expires_hours });
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const remove = (u: User) => {
    if (!window.confirm(t("admin.users.deleteConfirm", { name: u.username }))) return;
    void run(() => api(`/users/${u.id}`, { method: "DELETE" }), t("admin.users.deleted", { name: u.username }));
  };

  const resetMfa = (u: User) => {
    if (!window.confirm(t("admin.users.resetMfaConfirm", { name: u.username }))) return;
    void run(() => api(`/users/${u.id}/mfa-reset`, { method: "POST" }), t("admin.users.mfaReset", { name: u.username }));
  };

  const pending = users?.filter((u) => u.status === "pending") ?? [];

  return (
    <>
      <PageHeader
        index={t("admin.users.index")}
        title={t("admin.users.title")}
        lead={t("admin.users.lead")}
        actions={
          <button type="button" className="btn btn-primary" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
            <IconPlus size={16} aria-hidden="true" /> {t("admin.users.add")}
          </button>
        }
      />
      <AdvisorBanner pages={["users"]} />
      {loadError && <Notice tone="error">{loadError}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {link && (
        <section className="panel panel-link">
          <h2>{t("admin.users.linkFor", { name: link.username })}</h2>
          <p>{t("admin.users.linkBody", { name: link.username, hours: String(link.hours) })}</p>
          <CopyBox value={link.url} label={t("admin.users.linkLabel", { name: link.username })} />
          <button type="button" className="btn btn-quiet" onClick={() => setLink(null)}>{t("common.done")}</button>
        </section>
      )}
      {adding && (
        <form className="panel add-user" onSubmit={create}>
          <h2>{t("admin.users.new")}</h2>
          <div className="add-user-grid">
            <Field label={t("admin.users.username")} name="new-username" required autoCapitalize="none" autoComplete="off"
              value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <div className="field">
              <label htmlFor="new-role">{t("admin.users.role")}</label>
              <select id="new-role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                <option value="user">{t("common.role.user")}</option>
                <option value="operator">{t("common.role.operator")}</option>
                <option value="admin">{t("common.role.admin")}</option>
              </select>
            </div>
          </div>
          <p className="muted small">{t("admin.users.newNote")}</p>
          <div className="row-gap">
            <button className="btn btn-primary" disabled={busy}>{busy ? t("admin.users.creating") : t("admin.users.create")}</button>
            <button type="button" className="btn btn-quiet" onClick={() => setAdding(false)}>{t("common.cancel")}</button>
          </div>
        </form>
      )}
      {pending.length > 0 && (
        <Notice>{t.plural("admin.users.pending", pending.length)}</Notice>
      )}
      <div className="table-wrap">
        <table className="table users-table">
          <thead>
            <tr>
              <th>{t("admin.users.col.user")}</th>
              <th>{t("admin.users.col.role")}</th>
              <th>{t("admin.users.col.status")}</th>
              <th>{t("admin.users.col.lastSignIn")}</th>
              <th className="th-actions"><span className="sr-only">{t("admin.users.col.actions")}</span></th>
            </tr>
          </thead>
          <tbody>
            {users?.map((u) => {
              const self = u.id === me?.id;
              return (
                <tr key={u.id} className={u.status === "pending" ? "row-pending" : ""}>
                  <td data-label={t("admin.users.col.user")}>
                    <div>
                    <div className="cell-user">
                      <span className="mono strong">{u.username}</span>
                      {u.is_owner && <span className="chip chip-owner">{t("admin.users.owner")}</span>}
                      {self && <span className="chip chip-here">{t("admin.users.you")}</span>}
                      {u.has_mfa && <span className="chip chip-status-active" title={t("admin.users.twoFactorTitle")}>{t("admin.users.twoFactor")}</span>}
                      {u.mfa_enrolment && !u.has_mfa && (u.mfa_enrolment.due
                        ? <span className="chip chip-sev-warning" title={t("admin.users.mfaOverdueTitle")}>{t("admin.users.mfaOverdue")}</span>
                        : <span className="chip chip-sev-info" title={t("admin.users.mfaDueTitle", { date: formatDate(u.mfa_enrolment.by) })}>
                            {t("admin.users.mfaDue", { date: formatDate(u.mfa_enrolment.by) })}
                          </span>)}
                    </div>
                    {u.email && <div className="muted small">{u.email}</div>}
                    {u.sso_linked && <div className="muted small">{t("sso.users.linked")}</div>}
                    {!u.has_password && !u.sso_linked && u.status !== "pending" && <div className="muted small">{t("admin.users.noPassword")}</div>}
                    </div>
                  </td>
                  <td data-label={t("admin.users.col.role")}>
                    <select
                      aria-label={t("admin.users.roleOf", { name: u.username })}
                      value={u.role}
                      disabled={u.is_owner}
                      title={u.is_owner ? t("admin.users.ownerAdmin") : undefined}
                      onChange={(e) => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } }),
                        t("admin.users.nowRole", { name: u.username, role: t.maybe(`common.role.${e.target.value}`, e.target.value) }))}
                    >
                      <option value="admin">{t("common.role.admin")}</option>
                      <option value="operator">{t("common.role.operator")}</option>
                      <option value="user">{t("common.role.user")}</option>
                    </select>
                  </td>
                  <td data-label={t("admin.users.col.status")}><StatusChip status={u.status} /></td>
                  <td data-label={t("admin.users.col.lastSignIn")} title={exact(u.last_login_at)}>{when(u.last_login_at)}</td>
                  <td className="cell-actions">
                    {u.status === "pending" ? (
                      <>
                        <button type="button" className="btn btn-primary btn-small"
                          onClick={() => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { status: "active" } }), t("admin.users.approved", { name: u.username }))}>
                          {t("admin.users.approve")}
                        </button>
                        <button type="button" className="btn btn-quiet btn-small" onClick={() => remove(u)}>{t("admin.users.reject")}</button>
                      </>
                    ) : (
                      <>
                        <button type="button" className="btn btn-quiet btn-small" onClick={() => void resetLink(u)}>{t("admin.users.passwordLink")}</button>
                        <button type="button" className="btn btn-quiet btn-small"
                          onClick={() => void run(() => api(`/users/${u.id}/sign-out`, { method: "POST" }), t("admin.users.signedOut", { name: u.username }))}>
                          {t("admin.users.signOut")}
                        </button>
                        {!u.is_owner && !self && (
                          <>
                            {u.has_mfa && (
                              // The owner's two-factor sign-in is the owner's own (My account); nobody resets their own here.
                              <button type="button" className="btn btn-quiet btn-small" onClick={() => resetMfa(u)}>{t("admin.users.resetMfa")}</button>
                            )}
                            <button type="button" className="btn btn-quiet btn-small"
                              onClick={() => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { status: u.status === "disabled" ? "active" : "disabled" } }),
                                u.status === "disabled" ? t("admin.users.enabled", { name: u.username }) : t("admin.users.disabled", { name: u.username }))}>
                              {u.status === "disabled" ? t("admin.users.enable") : t("admin.users.disable")}
                            </button>
                            <button type="button" className="btn btn-danger btn-small" onClick={() => remove(u)}>{t("admin.users.delete")}</button>
                          </>
                        )}
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <MfaPolicyPanel say={(m) => { setMessage(m); load(); }} />
      <SsoProviderPanel say={(m) => { setMessage(m); load(); }} />
    </>
  );
}
