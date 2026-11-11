import { IconPlus } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";

import { useAuth } from "../auth";
import { CopyBox, Field, Notice, PageHeader, StatusChip } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import type { Role, User } from "../lib/types";
import { useApi } from "../lib/useApi";

type Link = { username: string; url: string; hours: number };

export function UsersPage() {
  const { user: me } = useAuth();
  const { data, error: loadError, reload: load } = useApi<{ users: User[] }>("/users");
  const users = data?.users ?? null;
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [link, setLink] = useState<Link | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ username: "", email: "", role: "user" as Role });
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
        body: { username: form.username, email: form.email || null, role: form.role },
      });
      setLink({ username: r.user.username, url: r.set_password_url, hours: r.expires_hours });
      setForm({ username: "", email: "", role: "user" });
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
    if (!window.confirm(`Delete ${u.username}? Their sessions end now. This cannot be undone.`)) return;
    void run(() => api(`/users/${u.id}`, { method: "DELETE" }), `Deleted ${u.username}.`);
  };

  const pending = users?.filter((u) => u.status === "pending") ?? [];

  return (
    <>
      <PageHeader
        index="03 · Users"
        title="Users"
        lead="Who can sign in, and with which role. Admins manage users, machines and the model catalog; operators run workloads and deploy models; users call the models with their own API keys and see their own usage."
        actions={
          <button type="button" className="btn btn-primary" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
            <IconPlus size={16} aria-hidden="true" /> Add user
          </button>
        }
      />
      {loadError && <Notice tone="error">{loadError}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {link && (
        <section className="panel panel-link">
          <h2>One-time link for {link.username}</h2>
          <p>
            Send this to {link.username}. It sets their password, works once and expires in {link.hours} hours. It is
            shown only now.
          </p>
          <CopyBox value={link.url} label={`Set-password link for ${link.username}`} />
          <button type="button" className="btn btn-quiet" onClick={() => setLink(null)}>Done</button>
        </section>
      )}
      {adding && (
        <form className="panel add-user" onSubmit={create}>
          <h2>New user</h2>
          <div className="add-user-grid">
            <Field label="Username" name="new-username" required autoCapitalize="none" autoComplete="off"
              value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <Field label="Email (optional)" name="new-email" type="email" autoComplete="off"
              value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
            <div className="field">
              <label htmlFor="new-role">Role</label>
              <select id="new-role" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
                <option value="user">user</option>
                <option value="operator">operator</option>
                <option value="admin">admin</option>
              </select>
            </div>
          </div>
          <p className="muted small">They get a one-time link to set their own password; you never see it.</p>
          <div className="row-gap">
            <button className="btn btn-primary" disabled={busy}>{busy ? "Creating…" : "Create and get link"}</button>
            <button type="button" className="btn btn-quiet" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </form>
      )}
      {pending.length > 0 && (
        <Notice>
          {pending.length} account{pending.length > 1 ? "s are" : " is"} waiting for approval.
        </Notice>
      )}
      <div className="table-wrap">
        <table className="table users-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Status</th>
              <th>Last sign-in</th>
              <th className="th-actions"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {users?.map((u) => {
              const self = u.id === me?.id;
              return (
                <tr key={u.id} className={u.status === "pending" ? "row-pending" : ""}>
                  <td data-label="User">
                    <div>
                    <div className="cell-user">
                      <span className="mono strong">{u.username}</span>
                      {u.is_owner && <span className="chip chip-owner">owner</span>}
                      {self && <span className="chip chip-here">you</span>}
                    </div>
                    {u.email && <div className="muted small">{u.email}</div>}
                    {!u.has_password && u.status !== "pending" && <div className="muted small">Has not set a password yet</div>}
                    </div>
                  </td>
                  <td data-label="Role">
                    <select
                      aria-label={`Role of ${u.username}`}
                      value={u.role}
                      disabled={u.is_owner}
                      title={u.is_owner ? "The owner is always an admin" : undefined}
                      onChange={(e) => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { role: e.target.value } }), `${u.username} is now ${e.target.value}.`)}
                    >
                      <option value="admin">admin</option>
                      <option value="operator">operator</option>
                      <option value="user">user</option>
                    </select>
                  </td>
                  <td data-label="Status"><StatusChip status={u.status} /></td>
                  <td data-label="Last sign-in" title={exact(u.last_login_at)}>{when(u.last_login_at)}</td>
                  <td className="cell-actions">
                    {u.status === "pending" ? (
                      <>
                        <button type="button" className="btn btn-primary btn-small"
                          onClick={() => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { status: "active" } }), `Approved ${u.username}.`)}>
                          Approve
                        </button>
                        <button type="button" className="btn btn-quiet btn-small" onClick={() => remove(u)}>Reject</button>
                      </>
                    ) : (
                      <>
                        <button type="button" className="btn btn-quiet btn-small" onClick={() => void resetLink(u)}>Password link</button>
                        <button type="button" className="btn btn-quiet btn-small"
                          onClick={() => void run(() => api(`/users/${u.id}/sign-out`, { method: "POST" }), `Signed ${u.username} out everywhere.`)}>
                          Sign out
                        </button>
                        {!u.is_owner && !self && (
                          <>
                            <button type="button" className="btn btn-quiet btn-small"
                              onClick={() => void run(() => api(`/users/${u.id}`, { method: "PATCH", body: { status: u.status === "disabled" ? "active" : "disabled" } }),
                                u.status === "disabled" ? `Enabled ${u.username}.` : `Disabled ${u.username}; their sessions ended.`)}>
                              {u.status === "disabled" ? "Enable" : "Disable"}
                            </button>
                            <button type="button" className="btn btn-danger btn-small" onClick={() => remove(u)}>Delete</button>
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
    </>
  );
}
