import { IconChevronDown, IconChevronUp, IconKey, IconLock, IconPlus } from "@tabler/icons-react";
import { useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { useAuth } from "../auth";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { BudgetMeter } from "../components/BudgetMeter";
import { CopyBox, Field, Notice, PageHeader, TrustHint } from "../components/ui";
import type { Key } from "../i18n/core";
import { useT, type Translator } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { budgetInput, LEVEL_LABEL, parseBudget, resetText } from "../lib/budgets";
import { exact, when } from "../lib/format";
import { foldLabel, keyLimits, keyModels, num, snippets, splitKeys } from "../lib/gateway";
import { CLOUD_POLICIES, customHelp, policyOf, policyValue, type CloudPolicy } from "../lib/cloudPolicy";
import { CLOUD_HELP, CLOUD_LABEL, CLOUD_LEVELS, cloudAbove } from "../lib/routing";
import type { ApiKey, CloudLevel, CloudSettings, KeyLimits, KeysResponse, NewKeyResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

const EXPIRY: { value: string; label: Key }[] = [
  { value: "", label: "keys.expiry.never" },
  { value: "7", label: "keys.expiry.7" },
  { value: "30", label: "keys.expiry.30" },
  { value: "90", label: "keys.expiry.90" },
  { value: "365", label: "keys.expiry.365" },
];

const STATUS_TONE: Record<ApiKey["status"], string> = { active: "ok", revoked: "off", expired: "wait" };

// The exact limits, for the tooltip of the short ones.
function limitsTitle(t: Translator, k: ApiKey): string {
  const per = (n: number | null) => (n ? t("keys.perMin", { n: num(n) }) : t("keys.noLimit"));
  return t("keys.limitsTitle", { rpm: per(k.effective_rpm), tpm: per(k.effective_tpm) });
}

function capText(t: Translator, n: number | null): string {
  return n ? t("keys.atMost", { n: num(n) }) : t("keys.noCap");
}

type LimitName = "rpm" | "tpm";
const CLOUD_TONE: Record<CloudLevel, string> = { off: "off", fallback: "run", on: "wait" };
const NO_CLOUD: CloudSettings = { default: "off", max: "off" };

function CloudChip({ level }: { level: CloudLevel }) {
  return <span className={`chip chip-tone-${CLOUD_TONE[level]}`} title={CLOUD_HELP[level]}>{CLOUD_LABEL[level]}</span>;
}

// A limit an admin set on someone else's key: its owner cannot change it.
function LockChip({ by }: { by: string }) {
  const t = useT();
  return (
    <span className="chip chip-lock" title={t("keys.lock.title", { by })}>
      <IconLock size={12} stroke={2} aria-hidden="true" /> {t("keys.lock.chip", { by })}
    </span>
  );
}

// "Set by the admin <strong>x</strong>; only an admin can change it."
function ByAdmin({ by }: { by: string }) {
  const t = useT();
  return <>{t.rich("keys.lock.byAdmin", { by, strong: (s) => <strong>{s}</strong> })}</>;
}

export function ApiKeysPage() {
  const t = useT();
  const [everyone, setEveryone] = useState(false);
  const { data, error, reload } = useApi<KeysResponse>(`/keys${everyone ? "?all=true" : ""}`);
  // 1.6.1 (#190): "Make a team key" on a team's page opens the form with that team chosen.
  const [search] = useSearchParams();
  const forTeam = search.get("team");
  const [creating, setCreating] = useState(forTeam !== null);
  const [issued, setIssued] = useState<NewKeyResponse | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  // Revoked and expired keys are folded away under one row, in Mine and in Everyone's (1.4).
  const [showOld, setShowOld] = useState(false);
  const scope = (all: boolean) => {
    setEveryone(all);
    setShowOld(false);
  };

  const revoke = async (k: ApiKey) => {
    const owner = k.team ? t("teams.field.keyTeam", { name: k.team.title }) : everyone ? k.user.username : null;
    const question = owner
      ? t("keys.revoke.confirmOf", { name: k.name, prefix: k.prefix, owner })
      : t("keys.revoke.confirm", { name: k.name, prefix: k.prefix });
    if (!window.confirm(question)) return;
    try {
      await api(`/keys/${k.id}`, { method: "DELETE" });
      setMessage({ tone: "ok", text: t("keys.revoke.done", { name: k.name }) });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const close = () => {
    setCreating(false);
    setIssued(null);
  };

  const code = (s: string) => <code>{s}</code>;
  return (
    <>
      <PageHeader
        index={t("keys.index")}
        title={t("keys.title")}
        lead={t("keys.lead")}
        actions={
          <button type="button" className="btn btn-primary" aria-expanded={creating} onClick={() => (creating ? close() : setCreating(true))}>
            <IconPlus size={16} aria-hidden="true" /> {t("keys.new")}
          </button>
        }
      />
      <AdvisorBanner pages={["keys"]} />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="8-call-it">{t("keys.help.call")}</HelpLink>
        <HelpLink page="concepts" anchor="api-keys-limits-and-usage">{t("keys.help.limits")}</HelpLink>
        <HelpLink page="troubleshooting" anchor="errors-from-v1-401-403-429-503">{t("keys.help.errors")}</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {data && <BudgetWarnings data={data} />}

      {creating && data && (
        issued ? <IssuedKey issued={issued} models={data.models} onDone={close} />
          : <CreateKey data={data} team={forTeam} onCancel={close} onCreated={(r) => { setIssued(r); reload(); }} />
      )}

      {data && (
        <section className="panel gw-base" aria-labelledby="base-title">
          <h2 id="base-title">{t("keys.gateway.title")}</h2>
          <CopyBox value={data.gateway_url} label={t("keys.gateway.copyLabel")} />
          <p className="muted small">
            {t.rich(data.models.length ? "keys.gateway.useNames" : "keys.gateway.useNone",
              { baseUrl: code("base_url"), auth: code("Authorization: Bearer <key>"), model: code("model") })}
          </p>
          {data.models.length > 0 && (
            <p className="model-names">{data.models.map((m) => <code key={m.name}>{m.name}</code>)}</p>
          )}
          {data.trust_url && <TrustHint url={data.trust_url} />}
        </section>
      )}

      {data && (
        <section aria-labelledby="keys-title">
          <div className="panel-head list-head">
            <h2 id="keys-title">{everyone ? t("keys.list.everyone") : t("keys.list.yours")}</h2>
            {data.is_admin && (
              <div className="segmented" role="group" aria-label={t("keys.list.whose")}>
                <button type="button" className={everyone ? "" : "on"} aria-pressed={!everyone} onClick={() => scope(false)}>{t("keys.list.mine")}</button>
                <button type="button" className={everyone ? "on" : ""} aria-pressed={everyone} onClick={() => scope(true)}>{t("keys.list.all")}</button>
              </div>
            )}
          </div>
          {data.keys.length === 0 ? (
            <div className="panel empty-keys">
              <IconKey size={22} aria-hidden="true" />
              {everyone ? (
                <p className="muted">{t("keys.empty.nobody")}</p>
              ) : (
                <>
                  <p className="muted">{t("keys.empty.you")}</p>
                  {!creating && (
                    <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
                      <IconPlus size={16} aria-hidden="true" /> {t("keys.empty.create")}
                    </button>
                  )}
                </>
              )}
            </div>
          ) : (
            <KeysList keys={data.keys} everyone={everyone} showOld={showOld} onToggle={() => setShowOld(!showOld)} empty={(
              <div className="panel empty-keys">
                <IconKey size={22} aria-hidden="true" />
                <p className="muted">
                  {everyone ? t("keys.empty.noActiveAll") : t("keys.empty.noActiveMine")}
                </p>
                {!everyone && !creating && (
                  <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
                    <IconPlus size={16} aria-hidden="true" /> {t("keys.empty.create")}
                  </button>
                )}
              </div>
            )}>
              {(k) => (
                <KeyRow key={k.id} k={k} everyone={everyone} isAdmin={data.is_admin} limits={data.limits} cloud={data.cloud ?? NO_CLOUD}
                  editing={editing === k.id} onEdit={() => setEditing(editing === k.id ? null : k.id)}
                  onSaved={(text) => { setEditing(null); setMessage({ tone: "ok", text }); reload(); }}
                  onError={(text) => setMessage({ tone: "error", text })} onRevoke={() => void revoke(k)} />
              )}
            </KeysList>
          )}
        </section>
      )}

      {data?.is_admin && <LimitsPanel limits={data.limits} envSeeds={data.env_seeds?.limits} onSaved={() => { setMessage({ tone: "ok", text: t("keys.saved.limits") }); reload(); }} />}
      {data?.is_admin && <CloudPanel cloud={data.cloud ?? NO_CLOUD} envSeeds={data.env_seeds?.cloud} onSaved={() => { setMessage({ tone: "ok", text: t("keys.saved.cloud") }); reload(); }} />}
    </>
  );
}

// The active keys in a table, then one row that opens the revoked and expired ones below them.
function KeysList({ keys, everyone, showOld, onToggle, empty, children }: {
  keys: ApiKey[]; everyone: boolean; showOld: boolean; onToggle: () => void; empty: ReactNode; children: (k: ApiKey) => ReactNode;
}) {
  const t = useT();
  const { active, inactive } = splitKeys(keys);
  const shown = showOld ? [...active, ...inactive] : active;
  return (
    <>
      {active.length === 0 && empty}
      {shown.length > 0 && (
        <div className="table-wrap">
          <table className="table keys-table">
            <thead>
              <tr>
                <th>{t("keys.col.key")}</th>{everyone && <th>{t("keys.col.owner")}</th>}<th>{t("keys.col.models")}</th><th>{t("keys.col.limits")}</th>
                <th>{t("keys.col.cloud")}</th><th>{t("keys.col.budget")}</th><th>{t("keys.col.lastUsed")}</th><th>{t("keys.col.expires")}</th>
                <th>{t("keys.col.status")}</th><th><span className="sr-only">{t("keys.col.actions")}</span></th>
              </tr>
            </thead>
            <tbody>{shown.map(children)}</tbody>
          </table>
        </div>
      )}
      {inactive.length > 0 && (
        <button type="button" className="keys-fold" aria-expanded={showOld} onClick={onToggle}>
          {showOld ? <IconChevronUp size={16} aria-hidden="true" /> : <IconChevronDown size={16} aria-hidden="true" />}
          {foldLabel(inactive, showOld)}
        </button>
      )}
    </>
  );
}

function KeyRow({ k, everyone, isAdmin, limits, cloud, editing, onEdit, onSaved, onError, onRevoke }: {
  k: ApiKey; everyone: boolean; isAdmin: boolean; limits: KeyLimits; cloud: CloudSettings; editing: boolean; onEdit: () => void;
  onSaved: (text: string) => void; onError: (text: string) => void; onRevoke: () => void;
}) {
  const t = useT();
  const { user } = useAuth();
  const [rpm, setRpm] = useState(k.rpm ? String(k.rpm) : "");
  const [tpm, setTpm] = useState(k.tpm ? String(k.tpm) : "");
  const level: CloudLevel = k.cloud ?? "off";
  const [cloudLevel, setCloudLevel] = useState<CloudLevel>(level);
  const [budget, setBudget] = useState(budgetInput(k.budget?.budget_micros));
  const budgetLock = k.budget_locked_by ?? null;
  // 1.6.1 (#190): a team key's managers act on it as its owner would; an admin's value on it is locked for them.
  const own = k.team ? !isAdmin : user?.id === k.user.id;
  const owner = k.team ? t("teams.field.keyTeam", { name: k.team.title }) : k.user.username ?? t("keys.theOwner");
  const lockedBy: Record<LimitName, string | null> = { rpm: k.rpm_locked_by ?? null, tpm: k.tpm_locked_by ?? null };
  const cloudLock = k.cloud_locked_by ?? null;
  // Only what changed is sent: an unchanged locked value is not "changed" by its owner saving the other one.
  const patch = (values: Record<LimitName, string>) => {
    const out: Record<string, number | string | string[] | null> = Object.fromEntries((["rpm", "tpm"] as LimitName[])
      .map((n) => [n, values[n] ? Number(values[n]) : null] as const)
      .filter(([n, v]) => v !== k[n]));
    if (cloudLevel !== level) out.cloud = cloudLevel;
    const b = parseBudget(budget);
    if (b !== "bad" && b !== (k.budget?.budget_micros ?? null)) out.budget_micros = b;
    return out;
  };
  const send = async (body: Record<string, number | string | string[] | null>, done: string) => {
    try {
      await api(`/keys/${k.id}`, { method: "PATCH", body });
      onSaved(done);
    } catch (err) {
      onError(errorText(err));
    }
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (parseBudget(budget) === "bad") {
      onError(t("keys.form.badBudget"));
      return;
    }
    const body = patch({ rpm, tpm });
    if (Object.keys(body).length === 0) {
      onEdit();
      return;
    }
    await send(body, t("keys.form.saved", { name: k.name }));
  };
  const lockHint = (n: LimitName, base: string) => {
    const by = lockedBy[n];
    if (by && !isAdmin) return <ByAdmin by={by} />;
    if (by) return <>{t("keys.lock.lockedClear", { owner, by })}</>;
    if (isAdmin && !own) return t("keys.lock.savingValue", { base, owner });
    return base;
  };
  const active = k.status === "active";
  const limitLines = keyLimits(k);
  return (
    <>
      <tr className={active ? "" : "row-off"}>
        {/* Every value cell holds one element: on a phone the cell is a label | value grid, and a second
            child would wrap into the label column. */}
        <td data-label={t("keys.col.key")}>
          <div>
            <div className="strong">{k.name}</div>
            <div className="mono small muted">{k.prefix}…</div>
            {k.team && !everyone && <TeamChip name={k.team.title} />}
          </div>
        </td>
        {everyone && <td data-label={t("keys.col.owner")} className="mono small"><div>{k.team ? <TeamChip name={k.team.title} /> : k.user.username ?? "—"}</div></td>}
        <td data-label={t("keys.col.models")} className="small"><div>{keyModels(k)}</div></td>
        <td data-label={t("keys.col.limits")} className="small">
          <div className="key-limits" title={limitsTitle(t, k)}>
            {limitLines.map((l) => (
              <div key={l.text} className={l.none ? "muted" : undefined}>
                <span className="nowrap">{l.text}</span>{l.locks.map((by, i) => <span key={i}> <LockChip by={by} /></span>)}
              </div>
            ))}
            {active && (k.this_minute.requests > 0 || k.this_minute.tokens > 0) && (
              <div className="muted">{t("keys.now", { requests: num(k.this_minute.requests), tokens: num(k.this_minute.tokens) })}</div>
            )}
          </div>
        </td>
        <td data-label={t("keys.col.cloud")} className="small">
          <div><CloudChip level={level} />{cloudLock && <> <LockChip by={cloudLock} /></>}
            {k.effective_cloud && k.effective_cloud !== level && (
              <> <span className="chip chip-tone-off" title={t("keys.effectiveTitle")}>
                {t("keys.effective", { level: CLOUD_LABEL[k.effective_cloud] })}
              </span></>
            )}</div>
        </td>
        <td data-label={t("keys.col.budget")} className="small">
          <div>
            {k.status === "revoked" ? <span className="muted">—</span>
              : k.budget ? <BudgetMeter b={k.budget} label={t("keys.budgetOf", { name: k.name })} compact /> : <span className="muted">{t("keys.none")}</span>}
            {budgetLock && <LockChip by={budgetLock} />}
          </div>
        </td>
        <td data-label={t("keys.col.lastUsed")} className="small" title={exact(k.last_used_at)}><div>{k.last_used_at ? when(k.last_used_at) : t("keys.never")}</div></td>
        <td data-label={t("keys.col.expires")} className="small" title={exact(k.expires_at)}><div>{k.expires_at ? when(k.expires_at) : t("keys.never")}</div></td>
        <td data-label={t("keys.col.status")}>
          <div>
            <span className={`chip chip-tone-${STATUS_TONE[k.status]}`}>{t.maybe(`common.status.${k.status}`, k.status)}</span>
            {k.revoked_by && <div className="muted small">{t("keys.revokedBy", { name: k.revoked_by })}</div>}
          </div>
        </td>
        <td className="cell-actions">
          {active && (isAdmin || !everyone) && (
            <button type="button" className="btn btn-quiet btn-small" aria-expanded={editing} onClick={onEdit}>{t("keys.limitsButton")}</button>
          )}
          {active && <button type="button" className="btn btn-danger btn-small" onClick={onRevoke}>{t("keys.revoke")}</button>}
        </td>
      </tr>
      {editing && (
        <tr className="row-edit">
          <td colSpan={everyone ? 10 : 9}>
            <form className="limits-form" onSubmit={save} aria-label={t("keys.form.limitsOf", { name: k.name })}>
              <Field label={t("keys.form.rpm")} name={`rpm-${k.id}`} type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)}
                placeholder={limits.default_rpm ? t("keys.form.default", { n: String(limits.default_rpm) }) : t("keys.form.serverDefault")} disabled={!isAdmin && !!lockedBy.rpm}
                hint={lockHint("rpm", isAdmin ? t("keys.form.adminAny") : t("keys.form.emptyDefaultCap", { cap: capText(t, limits.max_rpm) }))} />
              <Field label={t("keys.form.tpm")} name={`tpm-${k.id}`} type="number" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)}
                placeholder={limits.default_tpm ? t("keys.form.default", { n: String(limits.default_tpm) }) : t("keys.form.serverDefault")} disabled={!isAdmin && !!lockedBy.tpm}
                hint={lockHint("tpm", isAdmin ? t("keys.form.tokensTogether") : t("keys.form.emptyDefaultCap", { cap: capText(t, limits.max_tpm) }))} />
              <div className="field">
                <label htmlFor={`cloud-${k.id}`}>{t("keys.form.cloud")}</label>
                <select id={`cloud-${k.id}`} value={cloudLevel} disabled={!isAdmin && !!cloudLock} onChange={(e) => setCloudLevel(e.target.value as CloudLevel)}>
                  {CLOUD_LEVELS.map((c) => <option key={c} value={c} disabled={!isAdmin && cloudAbove(c, cloud.max) && c !== level}>{CLOUD_LABEL[c]}</option>)}
                </select>
                <p className="field-hint">{cloudLock && !isAdmin ? <ByAdmin by={cloudLock} />
                  : cloudLock ? t("keys.lock.locked", { owner, by: cloudLock })
                    : isAdmin && !own ? t("keys.lock.savingIt", { base: CLOUD_HELP[cloudLevel], owner }) : CLOUD_HELP[cloudLevel]}</p>
              </div>
              <Field label={t("keys.form.budget")} name={`budget-${k.id}`} inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)}
                placeholder={t("keys.form.noBudget")} disabled={!isAdmin && !!budgetLock}
                hint={budgetLock && !isAdmin ? <ByAdmin by={budgetLock} />
                  : budgetLock ? t("keys.lock.lockedClear", { owner, by: budgetLock })
                    : isAdmin && !own ? t("keys.form.budgetAdmin", { owner })
                      : t("keys.form.budgetHint")} />
              <div className="row-gap limits-actions">
                <button type="submit" className="btn btn-primary btn-small"
                  disabled={!isAdmin && !!lockedBy.rpm && !!lockedBy.tpm && !!cloudLock && !!budgetLock}>{t("keys.form.save")}</button>
                {isAdmin && budgetLock && (
                  <button type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ budget_micros: null }, t("keys.form.budgetRemoved", { name: k.name }))}>
                    <IconLock size={14} aria-hidden="true" /> {t("keys.form.unlockBudget")}
                  </button>
                )}
                {isAdmin && cloudLock && (
                  <button type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ cloud: null }, t("keys.form.cloudUnlocked", { name: k.name }))}>
                    <IconLock size={14} aria-hidden="true" /> {t("keys.form.unlockCloud")}
                  </button>
                )}
                {isAdmin && (["rpm", "tpm"] as LimitName[]).filter((n) => lockedBy[n]).map((n) => (
                  <button key={n} type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ [n]: null }, t(`keys.form.unlocked.${n}`, { name: k.name }))}>
                    <IconLock size={14} aria-hidden="true" /> {t(`keys.form.unlock.${n}`)}
                  </button>
                ))}
                <button type="button" className="btn btn-quiet btn-small" onClick={onEdit}>{t("common.cancel")}</button>
              </div>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}

// 1.6.1 (#190): a key that belongs to a team.
function TeamChip({ name }: { name: string }) {
  const t = useT();
  return <span className="chip chip-tone-run" title={t("teams.chipTitle", { name })}>{t("teams.chip", { name })}</span>;
}

function CreateKey({ data, team: forTeam, onCancel, onCreated }: { data: KeysResponse; team?: string | null; onCancel: () => void; onCreated: (r: NewKeyResponse) => void }) {
  const t = useT();
  const teams = data.teams ?? [];
  // "" is a personal key; a team's name makes the key the team's (1.6.1).
  const [team, setTeam] = useState(forTeam && teams.some((x) => x.name === forTeam) ? forTeam : "");
  const [name, setName] = useState("");
  const [all, setAll] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [rpm, setRpm] = useState("");
  const [tpm, setTpm] = useState("");
  const [expiry, setExpiry] = useState("");
  const cloudSettings = data.cloud ?? NO_CLOUD;
  const [cloud, setCloud] = useState<CloudLevel>(cloudSettings.default);
  const [budget, setBudget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { limits, is_admin: admin } = data;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const budgetMicros = parseBudget(budget);
    if (budgetMicros === "bad") {
      setError(t("keys.form.badBudget"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(await api<NewKeyResponse>("/keys", {
        method: "POST",
        body: {
          name: name.trim(), models: all ? null : picked, rpm: rpm ? Number(rpm) : null, tpm: tpm ? Number(tpm) : null,
          expires_in_days: expiry ? Number(expiry) : null, cloud, budget_micros: budgetMicros,
          ...(team ? { team } : {}),
        },
      }));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (m: string) => setPicked((p) => (p.includes(m) ? p.filter((x) => x !== m) : [...p, m]));
  return (
    <section className="panel panel-link" aria-labelledby="new-key-title">
      <h2 id="new-key-title">{t("keys.create.title")}</h2>
      <form onSubmit={submit} className="key-form">
        {teams.length > 0 && (
          <div className="grid-2">
            <div className="field">
              <label htmlFor="key-team">{t("teams.field.keyOwner")}</label>
              <select id="key-team" value={team} onChange={(e) => setTeam(e.target.value)}>
                <option value="">{t("teams.field.keyYou")}</option>
                {teams.map((x) => <option key={x.id} value={x.name}>{t("teams.field.keyTeam", { name: x.title })}</option>)}
              </select>
              <p className="field-hint">{t("teams.field.keyHint")}</p>
            </div>
          </div>
        )}
        <div className="grid-2">
          <Field label={t("keys.create.name")} name="key-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} required
            placeholder={t("keys.create.namePlaceholder")} hint={t("keys.create.nameHint")} />
          <div className="field">
            <label htmlFor="key-expiry">{t("keys.create.expires")}</label>
            <select id="key-expiry" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {EXPIRY.map((x) => <option key={x.value} value={x.value}>{t(x.label as "keys.expiry.never")}</option>)}
            </select>
          </div>
        </div>
        <fieldset className="allowlist key-models">
          <legend>{t("keys.create.models")}</legend>
          <label className="card-pick">
            <input type="radio" name="key-scope" checked={all} onChange={() => setAll(true)} /> {t("keys.create.every")}
          </label>
          <label className="card-pick">
            <input type="radio" name="key-scope" checked={!all} onChange={() => setAll(false)} /> {t("keys.create.only")}
          </label>
          {!all && (
            <div className="allowlist-cards" role="group" aria-label={t("keys.create.allowed")}>
              {data.models.length === 0 && <span className="muted small">{t("keys.create.noDeployments")}</span>}
              {data.models.map((m) => (
                <label key={m.name} className="card-pick">
                  <input type="checkbox" checked={picked.includes(m.name)} onChange={() => toggle(m.name)} />
                  <span className="mono">{m.name}</span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
        <div className="grid-2">
          <Field label={t("keys.form.rpm")} name="key-rpm" type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)}
            placeholder={limits.default_rpm ? t("keys.form.default", { n: String(limits.default_rpm) }) : t("keys.noLimit")}
            hint={admin ? t("keys.form.emptyDefault") : t("keys.form.emptyDefaultCap", { cap: capText(t, limits.max_rpm) })} />
          <Field label={t("keys.form.tpm")} name="key-tpm" type="number" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)}
            placeholder={limits.default_tpm ? t("keys.form.default", { n: String(limits.default_tpm) }) : t("keys.noLimit")}
            hint={admin ? t("keys.form.tokensTogether") : t("keys.form.emptyDefaultCap", { cap: capText(t, limits.max_tpm) })} />
        </div>
        <fieldset className="allowlist key-cloud">
          <legend>{t("keys.form.cloud")}</legend>
          <div className="cloud-picks" role="radiogroup" aria-label={t("keys.create.cloudAria")}>
            {CLOUD_LEVELS.map((c) => {
              const over = !admin && cloudAbove(c, cloudSettings.max);
              return (
                <label key={c} className={`card-pick${over ? " row-off" : ""}`} title={over ? t("keys.create.notAllowed") : undefined}>
                  <input type="radio" name="key-cloud" checked={cloud === c} disabled={over} onChange={() => setCloud(c)} />
                  <span className="strong">{CLOUD_LABEL[c]}</span> <span className="muted small">{CLOUD_HELP[c]}</span>
                </label>
              );
            })}
          </div>
        </fieldset>
        {cloud !== "off" && (
          <div className="grid-2">
            <Field label={t("keys.form.budget")} name="key-budget" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)}
              placeholder={t("keys.form.noBudget")} hint={t("keys.create.budgetHint")} />
          </div>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 16 }}>
          <button type="submit" className="btn btn-primary" disabled={busy || !name.trim() || (!all && picked.length === 0)}>
            {busy ? t("keys.create.creating") : t("keys.create.submit")}
          </button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
        </div>
      </form>
    </section>
  );
}

function IssuedKey({ issued, models, onDone }: { issued: NewKeyResponse; models: { name: string; api: string }[]; onDone: () => void }) {
  const t = useT();
  const k = issued.api_key;
  const first = k.models?.[0] ?? models.find((m) => m.api === "openai-chat")?.name ?? models[0]?.name ?? "my-model";
  const api_ = models.find((m) => m.name === first)?.api ?? "openai-chat";
  const s = snippets(issued.gateway_url, issued.key, first, api_, issued.trust_url);
  const [tab, setTab] = useState<"curl" | "python">("python");
  return (
    <section className="panel panel-link" aria-labelledby="issued-title">
      <h2 id="issued-title">{t("keys.issued.title")}</h2>
      <p>
        {t.rich("keys.issued.body", { name: <strong>{k.name}</strong>, prefix: <code>{k.prefix}</code> })}
        {k.expires_at ? t("keys.issued.expires", { when: when(k.expires_at) }) : ""}
      </p>
      <CopyBox value={issued.key} label={t("keys.issued.copyLabel")} />
      <div className="tabs snippet-tabs" role="tablist" aria-label={t("keys.issued.example")}>
        <button type="button" role="tab" aria-selected={tab === "python"} className={tab === "python" ? "on" : ""} onClick={() => setTab("python")}>Python (openai)</button>
        <button type="button" role="tab" aria-selected={tab === "curl"} className={tab === "curl" ? "on" : ""} onClick={() => setTab("curl")}>curl</button>
      </div>
      <pre className="snippet" aria-label={t("keys.issued.exampleAria", { tab })}>{tab === "python" ? s.python : s.curl}</pre>
      <div className="row-gap">
        <CopyText value={tab === "python" ? s.python : s.curl} />
        <button type="button" className="btn btn-quiet" onClick={onDone}>{t("common.done")}</button>
      </div>
    </section>
  );
}

// Budgets past 80% or used up: the person's own, and their keys'.
function BudgetWarnings({ data }: { data: KeysResponse }) {
  const t = useT();
  const mine = data.person_budget;
  const keys = data.keys.filter((k) => k.status === "active" && (k.budget?.level === "warning" || k.budget?.level === "exhausted"));
  const reset = resetText(data.resets_at);
  const others = (k: ApiKey) => data.keys.some((x) => x.user.id !== k.user.id) && k.user.username;
  return (
    <>
      {mine && mine.budget_micros !== null && (
        <section className="panel budget-panel" aria-labelledby="my-budget-title">
          <h2 id="my-budget-title">{t("keys.budget.mine")}</h2>
          <p className="muted small">{t("keys.budget.mineNote", { reset })}</p>
          <BudgetMeter b={mine} label={t("keys.budget.mine")} />
        </section>
      )}
      {mine?.level === "exhausted" && (
        <Notice tone="error">{t("keys.budget.mineExhausted", { reset })}</Notice>
      )}
      {mine?.level === "warning" && <Notice tone="info">{t("keys.budget.mineWarning")}</Notice>}
      {keys.map((k) => {
        const key = others(k)
          ? t("keys.budget.keyNameOf", { name: k.name, prefix: k.prefix, owner: k.user.username ?? "" })
          : t("keys.budget.keyName", { name: k.name, prefix: k.prefix });
        const level = LEVEL_LABEL[k.budget!.level!];
        return (
          <Notice key={k.id} tone={k.budget?.level === "exhausted" ? "error" : "info"}>
            {k.budget?.level === "exhausted" ? t("keys.budget.keyExhausted", { key, level, reset }) : t("keys.budget.keyWarning", { key, level })}
          </Notice>
        );
      })}
    </>
  );
}

function CopyText({ value }: { value: string }) {
  const t = useT();
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="btn" onClick={() => {
      void navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1600); }, () => undefined);
    }}>{done ? t("common.copied") : t("keys.issued.copyExample")}</button>
  );
}

function FromEnv({ names }: { names?: string[] }) {
  const t = useT();
  if (!names?.length) return null;
  return (
    <p className="from-env small" role="note">
      <span className="chip chip-tone-wait">{t("keys.env.chip")}</span>
      <span>{t.rich("keys.env.note", { names: names.map((n, i) => <span key={n}>{i ? ", " : ""}<code>{n}</code></span>) })}</span>
    </p>
  );
}

function CloudPanel({ cloud, envSeeds, onSaved }: { cloud: CloudSettings; envSeeds?: string[]; onSaved: () => void }) {
  const t = useT();
  const [f, setF] = useState<CloudSettings>(cloud);
  const [policy, setPolicy] = useState<CloudPolicy>(() => policyOf(cloud));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api("/keys/cloud", { method: "PUT", body: f });
      onSaved();
    } catch (err) {
      setError(errorText(err));
    }
  };
  const choose = (id: CloudPolicy) => {
    setPolicy(id);
    setF((cur) => policyValue(id, cur));
  };
  const preset = CLOUD_POLICIES.find((p) => p.id === policy);
  return (
    <section className="panel" aria-labelledby="cloud-title">
      <h2 id="cloud-title">{t("keys.policy.title")}</h2>
      <p className="muted small">{t("keys.policy.lead")}</p>
      <FromEnv names={envSeeds} />
      <form onSubmit={save}>
        <div className="field cloud-policy">
          <label htmlFor="cloud-policy">{t("keys.policy.label")}</label>
          <select id="cloud-policy" value={policy} onChange={(e) => choose(e.target.value as CloudPolicy)}>
            {CLOUD_POLICIES.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            <option value="custom">{t("keys.policy.custom")}</option>
          </select>
          <p className="field-hint">
            {preset ? preset.help : customHelp()}
          </p>
        </div>
        {policy === "custom" && (
          <div className="grid-2">
            <div className="field">
              <label htmlFor="cloud-default">{t("keys.policy.default")}</label>
              <select id="cloud-default" value={f.default} onChange={(e) => setF({ ...f, default: e.target.value as CloudLevel })}>
                {CLOUD_LEVELS.map((c) => <option key={c} value={c}>{CLOUD_LABEL[c]}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="cloud-max">{t("keys.policy.max")}</label>
              <select id="cloud-max" value={f.max} onChange={(e) => setF({ ...f, max: e.target.value as CloudLevel })}>
                {CLOUD_LEVELS.map((c) => <option key={c} value={c}>{CLOUD_LABEL[c]}</option>)}
              </select>
              {cloudAbove(f.default, f.max) && <p className="field-hint">{t("keys.policy.atMost", { level: CLOUD_LABEL[f.max] })}</p>}
            </div>
          </div>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <button type="submit" className="btn" style={{ marginTop: 14 }}>{t("keys.policy.save")}</button>
      </form>
    </section>
  );
}

function LimitsPanel({ limits, envSeeds, onSaved }: { limits: KeyLimits; envSeeds?: string[]; onSaved: () => void }) {
  const t = useT();
  const show = (n: number | null) => (n ? String(n) : "");
  const [f, setF] = useState({ default_rpm: show(limits.default_rpm), default_tpm: show(limits.default_tpm),
    max_rpm: show(limits.max_rpm), max_tpm: show(limits.max_tpm) });
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const body = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v ? Number(v) : 0]));
      await api("/keys/limits", { method: "PUT", body });
      onSaved();
    } catch (err) {
      setError(errorText(err));
    }
  };
  const set = (k: keyof typeof f) => (e: ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <section className="panel panel-gap" aria-labelledby="limits-title">
      <h2 id="limits-title">{t("keys.limits.title")}</h2>
      <p className="muted small">{t("keys.limits.lead")}</p>
      <FromEnv names={envSeeds} />
      <form onSubmit={save}>
        <div className="grid-4">
          <Field label={t("keys.limits.defaultRpm")} name="default_rpm" type="number" min={0} value={f.default_rpm} onChange={set("default_rpm")} placeholder={t("keys.noLimit")} />
          <Field label={t("keys.limits.defaultTpm")} name="default_tpm" type="number" min={0} value={f.default_tpm} onChange={set("default_tpm")} placeholder={t("keys.noLimit")} />
          <Field label={t("keys.limits.capRpm")} name="max_rpm" type="number" min={0} value={f.max_rpm} onChange={set("max_rpm")} placeholder={t("keys.noCap")} />
          <Field label={t("keys.limits.capTpm")} name="max_tpm" type="number" min={0} value={f.max_tpm} onChange={set("max_tpm")} placeholder={t("keys.noCap")} />
        </div>
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <button type="submit" className="btn" style={{ marginTop: 14 }}>{t("keys.form.save")}</button>
      </form>
    </section>
  );
}
