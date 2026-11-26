import { IconArrowLeft, IconPencil, IconPlus, IconTrash, IconUsersGroup } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth";
import { BudgetMeter } from "../components/BudgetMeter";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Field, Notice, PageHeader, StatusChip } from "../components/ui";
import { teamPageTabs as extTabs } from "../ext";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { budgetInput, parseBudget, resetText } from "../lib/budgets";
import { exact, when } from "../lib/format";
import { shown, useInfo } from "../lib/info";
import type { Team, TeamResponse, TeamRole, TeamsResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

// Teams (1.6.1, #190): people who share API keys, deployments and a cloud budget. Admins make, rename and delete
// teams and choose their managers; managers add and remove members and make the team's keys; everyone sees the teams
// they are in. An install without teams shows admins an empty list and nobody else this page.

type Message = { tone: "ok" | "error"; text: string } | null;

export function RoleChip({ role }: { role: TeamRole | null }) {
  const t = useT();
  if (!role) return <span className="muted">—</span>;
  return <span className={`chip ${role === "manager" ? "chip-tone-run" : "chip-tone-off"}`}>{t(`teams.role.${role}`)}</span>;
}

export function TeamsPage() {
  const t = useT();
  const { data, error, reload } = useApi<TeamsResponse>("/teams");
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const admin = data?.is_admin ?? false;
  return (
    <>
      <PageHeader index={t("teams.index")} title={t("teams.title")} lead={admin ? t("teams.lead.admin") : t("teams.lead.mine")}
        actions={admin && (
          <button type="button" className="btn btn-primary" aria-expanded={adding} onClick={() => setAdding((v) => !v)}>
            <IconPlus size={16} aria-hidden="true" /> {t("teams.new")}
          </button>
        )} />
      <HelpLinks>
        <HelpLink page="how-to" anchor="share-models-with-a-team">{t("teams.help.share")}</HelpLink>
        <HelpLink page="concepts" anchor="teams">{t("teams.help.concepts")}</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {adding && <NewTeam onCancel={() => setAdding(false)} onMade={(name) => { setAdding(false); setMessage({ tone: "ok", text: t("teams.made", { name }) }); reload(); }} />}
      {data && (data.teams.length === 0 ? (
        <div className="panel teams-empty">
          <IconUsersGroup size={22} aria-hidden="true" />
          <p className="muted">{admin ? t("teams.empty.admin") : t("teams.empty.mine")}</p>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table teams-table">
            <thead>
              <tr>
                <th>{t("teams.col.team")}</th><th>{t("teams.col.yourRole")}</th><th>{t("teams.col.members")}</th>
                <th>{t("teams.col.keys")}</th><th>{t("teams.col.deployments")}</th><th>{t("teams.col.budget")}</th>
              </tr>
            </thead>
            <tbody>
              {data.teams.map((x) => (
                <tr key={x.id}>
                  <td data-label={t("teams.col.team")}>
                    <div>
                      {x.my_role || admin ? <Link to={`/teams/${x.name}`} className="strong">{x.title}</Link> : <span className="strong">{x.title}</span>}
                      <div className="mono small muted">{x.name}</div>
                    </div>
                  </td>
                  <td data-label={t("teams.col.yourRole")}><div><RoleChip role={x.my_role} /></div></td>
                  <td data-label={t("teams.col.members")}><div>{t("teams.membersCount", { members: x.members, managers: x.managers })}</div></td>
                  <td data-label={t("teams.col.keys")}><div>{x.keys}</div></td>
                  <td data-label={t("teams.col.deployments")}><div>{x.deployments}</div></td>
                  <td data-label={t("teams.col.budget")} className="small">
                    <div><BudgetMeter b={x.budget} label={t("teams.budgetOf", { name: x.title })} compact /></div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}

function NewTeam({ onCancel, onMade }: { onCancel: () => void; onMade: (name: string) => void }) {
  const t = useT();
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ team: Team }>("/teams", { method: "POST", body: { name: name.trim(), title: title.trim() || null } });
      onMade(r.team.title);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="panel panel-link" onSubmit={submit} aria-label={t("teams.new")}>
      <h2>{t("teams.new")}</h2>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="grid-2">
        <Field label={t("teams.form.name")} name="team-name" required autoCapitalize="none" autoComplete="off" maxLength={64}
          value={name} onChange={(e) => setName(e.target.value)} hint={t("teams.form.nameHint")} />
        <Field label={t("teams.form.title")} name="team-title" maxLength={128} value={title} onChange={(e) => setTitle(e.target.value)}
          placeholder={t("teams.form.titlePlaceholder")} hint={t("teams.form.titleHint")} />
      </div>
      <div className="row-gap">
        <button className="btn btn-primary" disabled={busy || !name.trim()}>{busy ? t("teams.form.making") : t("teams.form.make")}</button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </form>
  );
}

export function TeamPage() {
  const t = useT();
  const { name } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data, error, reload } = useApi<TeamResponse>(`/teams/${name}`);
  const [message, setMessage] = useState<Message>(null);
  const [editing, setEditing] = useState(false);
  const info = useInfo(extTabs.length > 0);
  const team = data?.team;
  const admin = user?.role === "admin";

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };
  const remove = async () => {
    if (!team || !window.confirm(t("teams.delete.confirm", { name: team.title }))) return;
    try {
      await api(`/teams/${team.name}`, { method: "DELETE" });
      navigate("/teams", { replace: true });
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  return (
    <>
      <p className="crumb"><Link to="/teams"><IconArrowLeft size={14} aria-hidden="true" /> {t("teams.title")}</Link></p>
      <PageHeader index={t("teams.index")} title={team?.title ?? t("teams.team")}
        lead={team ? t("teams.page.lead", { name: team.name, by: team.created_by ?? "?", when: when(team.created_at) }) : undefined}
        actions={team && admin && (
          <>
            <button type="button" className="btn" aria-expanded={editing} onClick={() => setEditing((v) => !v)}><IconPencil size={16} aria-hidden="true" /> {t("teams.edit")}</button>
            <button type="button" className="btn btn-danger" onClick={() => void remove()}><IconTrash size={16} aria-hidden="true" /> {t("teams.delete")}</button>
          </>
        )} />
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {team && editing && admin && (
        <EditTeam team={team} onCancel={() => setEditing(false)}
          onSaved={(next) => { setEditing(false); setMessage({ tone: "ok", text: t("teams.saved") }); if (next !== team.name) navigate(`/teams/${next}`, { replace: true }); else reload(); }} />
      )}
      {team && (
        <>
          <div className="wl-status">
            <span>{t("teams.page.yourRole")} {admin && !team.my_role ? <span className="chip chip-tone-wait">{t("common.role.admin")}</span> : <RoleChip role={team.my_role} />}</span>
            <span className="muted small">{t("teams.membersCount", { members: team.members, managers: team.managers })}</span>
          </div>
          <section className="panel" aria-labelledby="team-budget-title">
            <h2 id="team-budget-title">{t("teams.budget.title")}</h2>
            <BudgetMeter b={team.budget} label={t("teams.budgetOf", { name: team.title })} />
            <p className="muted small">{t("teams.budget.lead", { reset: resetText(data?.resets_at) })}{" "}
              {admin ? t("teams.budget.admin") : t("teams.budget.others")}</p>
          </section>
          <Members team={team} admin={admin} run={run} />
          {team.key_list && <TeamKeys team={team} />}
          <section className="panel" aria-labelledby="team-deps-title">
            <h2 id="team-deps-title">{t("teams.deployments.title")}</h2>
            {(team.deployment_list ?? []).length === 0 ? <p className="muted small">{t("teams.deployments.none")}</p> : (
              <ul className="team-list">
                {(team.deployment_list ?? []).map((d) => <li key={d.id}><Link to={`/serving/${d.id}`}>{d.name}</Link> <code className="small">{d.served_name}</code></li>)}
              </ul>
            )}
            <p className="muted small">{team.can_manage ? t("teams.deployments.manage") : t("teams.deployments.member")}</p>
          </section>
          {/* 1.6.1 (#190): an extension's sections for a team (none in the community edition). */}
          {extTabs.filter((x) => shown(info, x, admin)).map((x) => (
            <section key={x.id} className="panel" aria-label={x.label(t)}>
              <h2>{x.label(t)}</h2>
              {x.render(team)}
            </section>
          ))}
        </>
      )}
    </>
  );
}

function EditTeam({ team, onCancel, onSaved }: { team: Team; onCancel: () => void; onSaved: (name: string) => void }) {
  const t = useT();
  const [name, setName] = useState(team.name);
  const [title, setTitle] = useState(team.title);
  const [budget, setBudget] = useState(budgetInput(team.budget.budget_micros));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const micros = parseBudget(budget);
    if (micros === "bad") {
      setError(t("keys.form.badBudget"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ team: Team }>(`/teams/${team.name}`, { method: "PATCH", body: { name: name.trim(), title: title.trim(), budget_micros: micros } });
      onSaved(r.team.name);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="panel panel-link" onSubmit={submit} aria-label={t("teams.edit")}>
      <h2>{t("teams.edit")}</h2>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="grid-3">
        <Field label={t("teams.form.name")} name="team-edit-name" required autoCapitalize="none" maxLength={64} value={name}
          onChange={(e) => setName(e.target.value)} hint={t("teams.form.renameHint")} />
        <Field label={t("teams.form.title")} name="team-edit-title" required maxLength={128} value={title} onChange={(e) => setTitle(e.target.value)} />
        <Field label={t("teams.form.budget")} name="team-edit-budget" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)}
          placeholder={t("keys.form.noBudget")} hint={t("teams.form.budgetHint")} />
      </div>
      <div className="row-gap">
        <button className="btn btn-primary" disabled={busy}>{t("keys.form.save")}</button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </form>
  );
}

function Members({ team, admin, run }: { team: Team; admin: boolean; run: (fn: () => Promise<unknown>, ok: string) => Promise<void> }) {
  const t = useT();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<TeamRole>("member");
  const list = team.member_list ?? [];
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const who = username.trim().toLowerCase();
    if (!who) return;
    await run(() => api(`/teams/${team.name}/members/${encodeURIComponent(who)}`, { method: "PUT", body: { role } }),
      t("teams.members.added", { name: who, role: t(`teams.role.${role}`) }));
    setUsername("");
  };
  const removeOne = (name: string) => {
    if (!window.confirm(t("teams.members.removeConfirm", { name, team: team.title }))) return;
    void run(() => api(`/teams/${team.name}/members/${encodeURIComponent(name)}`, { method: "DELETE" }), t("teams.members.removed", { name }));
  };
  return (
    <section className="panel" aria-labelledby="team-members-title">
      <h2 id="team-members-title">{t("teams.members.title")}</h2>
      {list.length === 0 ? <p className="muted small">{t("teams.members.none")}</p> : (
        <div className="table-wrap">
          <table className="table team-members">
            <thead><tr><th>{t("teams.members.col.person")}</th><th>{t("teams.members.col.role")}</th><th>{t("teams.members.col.account")}</th>
              <th>{t("teams.members.col.added")}</th><th><span className="sr-only">{t("teams.members.col.actions")}</span></th></tr></thead>
            <tbody>
              {list.map((m) => (
                <tr key={m.username}>
                  <td data-label={t("teams.members.col.person")} className="mono"><div>{m.username}</div></td>
                  <td data-label={t("teams.members.col.role")}>
                    <div>
                      {admin ? (
                        <select aria-label={t("teams.members.roleOf", { name: m.username })} value={m.role}
                          onChange={(e) => void run(() => api(`/teams/${team.name}/members/${encodeURIComponent(m.username)}`, { method: "PUT", body: { role: e.target.value } }),
                            t("teams.members.nowRole", { name: m.username, role: t(`teams.role.${e.target.value as TeamRole}`) }))}>
                          <option value="member">{t("teams.role.member")}</option>
                          <option value="manager">{t("teams.role.manager")}</option>
                        </select>
                      ) : <RoleChip role={m.role} />}
                    </div>
                  </td>
                  <td data-label={t("teams.members.col.account")}><div><StatusChip status={m.status} /></div></td>
                  <td data-label={t("teams.members.col.added")} className="small" title={exact(m.added_at)}>
                    <div>{m.added_by ? t("teams.members.addedBy", { by: m.added_by, when: when(m.added_at) }) : when(m.added_at)}</div>
                  </td>
                  <td className="cell-actions">
                    {(admin || (team.can_manage && m.role === "member")) && (
                      <button type="button" className="btn btn-quiet btn-small" onClick={() => removeOne(m.username)}>{t("teams.members.remove")}</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {team.can_manage && (
        <form className="team-add" onSubmit={add} aria-label={t("teams.members.add")}>
          <Field label={t("teams.members.username")} name="team-add-user" autoCapitalize="none" autoComplete="off" value={username}
            onChange={(e) => setUsername(e.target.value)} hint={admin ? t("teams.members.adminHint") : t("teams.members.managerHint")} />
          {admin && (
            <div className="field">
              <label htmlFor="team-add-role">{t("teams.members.col.role")}</label>
              <select id="team-add-role" value={role} onChange={(e) => setRole(e.target.value as TeamRole)}>
                <option value="member">{t("teams.role.member")}</option>
                <option value="manager">{t("teams.role.manager")}</option>
              </select>
            </div>
          )}
          <div className="field team-add-button">
            <button className="btn btn-primary" disabled={!username.trim()}><IconPlus size={16} aria-hidden="true" /> {t("teams.members.add")}</button>
          </div>
        </form>
      )}
    </section>
  );
}

function TeamKeys({ team }: { team: Team }) {
  const t = useT();
  const keys = team.key_list ?? [];
  const live = keys.filter((k) => k.status === "active");
  return (
    <section className="panel" aria-labelledby="team-keys-title">
      <div className="panel-head">
        <h2 id="team-keys-title">{t("teams.keys.title")}</h2>
        <Link to={`/keys?team=${encodeURIComponent(team.name)}`} className="btn btn-small"><IconPlus size={14} aria-hidden="true" /> {t("teams.keys.make")}</Link>
      </div>
      <p className="muted small">{t("teams.keys.lead")}</p>
      {live.length === 0 ? <p className="muted small">{t("teams.keys.none")}</p> : (
        <div className="table-wrap">
          <table className="table team-keys">
            <thead><tr><th>{t("keys.col.key")}</th><th>{t("teams.keys.madeBy")}</th><th>{t("keys.col.lastUsed")}</th></tr></thead>
            <tbody>
              {live.map((k) => (
                <tr key={k.id}>
                  <td data-label={t("keys.col.key")}><div><div className="strong">{k.name}</div><div className="mono small muted">{k.prefix}…</div></div></td>
                  <td data-label={t("teams.keys.madeBy")} className="mono small"><div>{k.created_by ?? "—"}</div></td>
                  <td data-label={t("keys.col.lastUsed")} className="small" title={exact(k.last_used_at)}><div>{k.last_used_at ? when(k.last_used_at) : t("keys.never")}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {keys.length > live.length && <p className="muted small">{t.plural("teams.keys.old", keys.length - live.length)}</p>}
    </section>
  );
}
