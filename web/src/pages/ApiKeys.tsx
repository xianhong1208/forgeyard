import { IconKey, IconLock, IconPlus } from "@tabler/icons-react";
import { useState, type ChangeEvent, type FormEvent } from "react";

import { useAuth } from "../auth";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { BudgetMeter } from "../components/BudgetMeter";
import { CopyBox, Field, Notice, PageHeader, TrustHint } from "../components/ui";
import { api, errorText } from "../lib/api";
import { budgetInput, LEVEL_LABEL, parseBudget, resetText } from "../lib/budgets";
import { exact, when } from "../lib/format";
import { keyModels, num, snippets } from "../lib/gateway";
import { CLOUD_HELP, CLOUD_LABEL, CLOUD_LEVELS, cloudAbove } from "../lib/routing";
import type { ApiKey, CloudLevel, CloudSettings, KeyLimits, KeysResponse, NewKeyResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

const EXPIRY = [
  { value: "", label: "Never" },
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
];

const STATUS_TONE: Record<ApiKey["status"], string> = { active: "ok", revoked: "off", expired: "wait" };

function capText(n: number | null): string {
  return n ? `at most ${num(n)}` : "no cap";
}

type LimitName = "rpm" | "tpm";
const LIMIT_LABEL: Record<LimitName, string> = { rpm: "Requests", tpm: "Tokens" };
const CLOUD_TONE: Record<CloudLevel, string> = { off: "off", fallback: "run", on: "wait" };
const NO_CLOUD: CloudSettings = { default: "off", max: "off" };

function CloudChip({ level }: { level: CloudLevel }) {
  return <span className={`chip chip-tone-${CLOUD_TONE[level]}`} title={CLOUD_HELP[level]}>{CLOUD_LABEL[level]}</span>;
}

// A limit an admin set on someone else's key: its owner cannot change it.
function LockChip({ by }: { by: string }) {
  return (
    <span className="chip chip-lock" title={`Set by the admin ${by}; only an admin can change it.`}>
      <IconLock size={12} stroke={2} aria-hidden="true" /> set by {by}
    </span>
  );
}

export function ApiKeysPage() {
  const [everyone, setEveryone] = useState(false);
  const { data, error, reload } = useApi<KeysResponse>(`/keys${everyone ? "?all=true" : ""}`);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<NewKeyResponse | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  const revoke = async (k: ApiKey) => {
    const whose = everyone && k.user.username ? ` of ${k.user.username}` : "";
    if (!window.confirm(`Revoke ${k.name} (${k.prefix}…)${whose}? The very next request with it is refused. This cannot be undone.`)) return;
    try {
      await api(`/keys/${k.id}`, { method: "DELETE" });
      setMessage({ tone: "ok", text: `${k.name} is revoked.` });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const close = () => {
    setCreating(false);
    setIssued(null);
  };

  return (
    <>
      <PageHeader
        index="05 · API keys"
        title="API keys"
        lead="Keys for programs that call the models through Forgeyard's OpenAI-compatible gateway. A key is shown once; Forgeyard keeps only a hash of it."
        actions={
          <button type="button" className="btn btn-primary" aria-expanded={creating} onClick={() => (creating ? close() : setCreating(true))}>
            <IconPlus size={16} aria-hidden="true" /> New key
          </button>
        }
      />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="8-call-it">How do I call /v1 with curl or the OpenAI SDK?</HelpLink>
        <HelpLink page="concepts" anchor="api-keys-limits-and-usage">Keys, limits and usage</HelpLink>
        <HelpLink page="troubleshooting" anchor="errors-from-v1-401-403-429-503">Errors from /v1</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {data && <BudgetWarnings data={data} />}

      {creating && data && (
        issued ? <IssuedKey issued={issued} models={data.models} onDone={close} />
          : <CreateKey data={data} onCancel={close} onCreated={(r) => { setIssued(r); reload(); }} />
      )}

      {data && (
        <section className="panel gw-base" aria-labelledby="base-title">
          <h2 id="base-title">Gateway address</h2>
          <CopyBox value={data.gateway_url} label="Gateway base URL" />
          <p className="muted small">
            Use it as the <code>base_url</code> of any OpenAI client, with <code>Authorization: Bearer &lt;key&gt;</code>. The <code>model</code> is one of
            its model names{data.models.length ? ":" : " (none yet: deploy a model first)."}
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
            <h2 id="keys-title">{everyone ? "Everyone's keys" : "Your keys"}</h2>
            {data.is_admin && (
              <div className="segmented" role="group" aria-label="Whose keys">
                <button type="button" className={everyone ? "" : "on"} aria-pressed={!everyone} onClick={() => setEveryone(false)}>Mine</button>
                <button type="button" className={everyone ? "on" : ""} aria-pressed={everyone} onClick={() => setEveryone(true)}>Everyone's</button>
              </div>
            )}
          </div>
          {data.keys.length === 0 ? (
            <div className="panel empty-keys">
              <IconKey size={22} aria-hidden="true" />
              {everyone ? (
                <p className="muted">Nobody has an API key yet.</p>
              ) : (
                <>
                  <p className="muted">
                    You have no API keys yet. A program needs one to call the models at the address above. Create one per
                    program or computer, so you can revoke one without the others.
                  </p>
                  {!creating && (
                    <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
                      <IconPlus size={16} aria-hidden="true" /> Create a key
                    </button>
                  )}
                </>
              )}
            </div>
          ) : (
            <div className="table-wrap">
              <table className="table keys-table">
                <thead>
                  <tr><th>Key</th>{everyone && <th>Owner</th>}<th>Models</th><th>Limits</th><th>Cloud</th><th>Budget</th><th>Last used</th><th>Expires</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
                </thead>
                <tbody>
                  {data.keys.map((k) => (
                    <KeyRow key={k.id} k={k} everyone={everyone} isAdmin={data.is_admin} limits={data.limits} cloud={data.cloud ?? NO_CLOUD}
                      editing={editing === k.id} onEdit={() => setEditing(editing === k.id ? null : k.id)}
                      onSaved={(text) => { setEditing(null); setMessage({ tone: "ok", text }); reload(); }}
                      onError={(text) => setMessage({ tone: "error", text })} onRevoke={() => void revoke(k)} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {data?.is_admin && <LimitsPanel limits={data.limits} onSaved={() => { setMessage({ tone: "ok", text: "Limits for keys saved." }); reload(); }} />}
      {data?.is_admin && <CloudPanel cloud={data.cloud ?? NO_CLOUD} onSaved={() => { setMessage({ tone: "ok", text: "Cloud setting for keys saved." }); reload(); }} />}
    </>
  );
}

function KeyRow({ k, everyone, isAdmin, limits, cloud, editing, onEdit, onSaved, onError, onRevoke }: {
  k: ApiKey; everyone: boolean; isAdmin: boolean; limits: KeyLimits; cloud: CloudSettings; editing: boolean; onEdit: () => void;
  onSaved: (text: string) => void; onError: (text: string) => void; onRevoke: () => void;
}) {
  const { user } = useAuth();
  const [rpm, setRpm] = useState(k.rpm ? String(k.rpm) : "");
  const [tpm, setTpm] = useState(k.tpm ? String(k.tpm) : "");
  const level: CloudLevel = k.cloud ?? "off";
  const [cloudLevel, setCloudLevel] = useState<CloudLevel>(level);
  const [budget, setBudget] = useState(budgetInput(k.budget?.budget_micros));
  const budgetLock = k.budget_locked_by ?? null;
  const own = user?.id === k.user.id;
  const lockedBy: Record<LimitName, string | null> = { rpm: k.rpm_locked_by ?? null, tpm: k.tpm_locked_by ?? null };
  const cloudLock = k.cloud_locked_by ?? null;
  // Only what changed is sent: an unchanged locked value is not "changed" by its owner saving the other one.
  const patch = (values: Record<LimitName, string>) => {
    const out: Record<string, number | string | null> = Object.fromEntries((["rpm", "tpm"] as LimitName[])
      .map((n) => [n, values[n] ? Number(values[n]) : null] as const)
      .filter(([n, v]) => v !== k[n]));
    if (cloudLevel !== level) out.cloud = cloudLevel;
    const b = parseBudget(budget);
    if (b !== "bad" && b !== (k.budget?.budget_micros ?? null)) out.budget_micros = b;
    return out;
  };
  const send = async (body: Record<string, number | string | null>, done: string) => {
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
      onError("Monthly budget: US dollars, like 25 or 2.50 (empty: no budget).");
      return;
    }
    const body = patch({ rpm, tpm });
    if (Object.keys(body).length === 0) {
      onEdit();
      return;
    }
    await send(body, `Limits, cloud setting and budget of ${k.name} saved.`);
  };
  const lockHint = (n: LimitName, base: string) => {
    const by = lockedBy[n];
    if (by && !isAdmin) return <>Set by the admin <strong>{by}</strong>; only an admin can change it.</>;
    if (by) return <>Locked for {k.user.username ?? "the owner"} (set by {by}). Clear it to unlock.</>;
    if (isAdmin && !own) return `${base} Saving a value locks it for ${k.user.username ?? "the owner"}.`;
    return base;
  };
  const active = k.status === "active";
  return (
    <>
      <tr className={active ? "" : "row-off"}>
        <td data-label="Key">
          <div className="strong">{k.name}</div>
          <div className="mono small muted">{k.prefix}…</div>
        </td>
        {everyone && <td data-label="Owner" className="mono small">{k.user.username ?? "—"}</td>}
        <td data-label="Models" className="small">{keyModels(k)}</td>
        <td data-label="Limits" className="small">
          <div><span className="muted">Requests</span> {k.effective_rpm ? `${num(k.effective_rpm)}/min` : "no limit"}
            {lockedBy.rpm && <> <LockChip by={lockedBy.rpm} /></>}</div>
          <div><span className="muted">Tokens</span> {k.effective_tpm ? `${num(k.effective_tpm)}/min` : "no limit"}
            {lockedBy.tpm && <> <LockChip by={lockedBy.tpm} /></>}</div>
          {active && (k.this_minute.requests > 0 || k.this_minute.tokens > 0) && (
            <div className="muted">now: {num(k.this_minute.requests)} req · {num(k.this_minute.tokens)} tok</div>
          )}
        </td>
        <td data-label="Cloud" className="small"><CloudChip level={level} />{cloudLock && <> <LockChip by={cloudLock} /></>}</td>
        <td data-label="Budget" className="small">
          <div>
            {k.budget ? <BudgetMeter b={k.budget} label={`Budget of ${k.name}`} compact /> : <span className="muted">none</span>}
            {budgetLock && <LockChip by={budgetLock} />}
          </div>
        </td>
        <td data-label="Last used" className="small" title={exact(k.last_used_at)}>{k.last_used_at ? when(k.last_used_at) : "never"}</td>
        <td data-label="Expires" className="small" title={exact(k.expires_at)}>{k.expires_at ? when(k.expires_at) : "never"}</td>
        <td data-label="Status">
          <span className={`chip chip-tone-${STATUS_TONE[k.status]}`}>{k.status}</span>
          {k.revoked_by && <div className="muted small">by {k.revoked_by}</div>}
        </td>
        <td className="cell-actions">
          {active && (isAdmin || !everyone) && (
            <button type="button" className="btn btn-quiet btn-small" aria-expanded={editing} onClick={onEdit}>Limits</button>
          )}
          {active && <button type="button" className="btn btn-danger btn-small" onClick={onRevoke}>Revoke</button>}
        </td>
      </tr>
      {editing && (
        <tr className="row-edit">
          <td colSpan={everyone ? 10 : 9}>
            <form className="limits-form" onSubmit={save} aria-label={`Limits of ${k.name}`}>
              <Field label="Requests per minute" name={`rpm-${k.id}`} type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)}
                placeholder={limits.default_rpm ? `default ${limits.default_rpm}` : "server default"} disabled={!isAdmin && !!lockedBy.rpm}
                hint={lockHint("rpm", isAdmin ? "Admins may set any value." : `Empty: the default; ${capText(limits.max_rpm)}.`)} />
              <Field label="Tokens per minute" name={`tpm-${k.id}`} type="number" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)}
                placeholder={limits.default_tpm ? `default ${limits.default_tpm}` : "server default"} disabled={!isAdmin && !!lockedBy.tpm}
                hint={lockHint("tpm", isAdmin ? "Prompt and completion tokens together." : `Empty: the default; ${capText(limits.max_tpm)}.`)} />
              <div className="field">
                <label htmlFor={`cloud-${k.id}`}>Cloud providers</label>
                <select id={`cloud-${k.id}`} value={cloudLevel} disabled={!isAdmin && !!cloudLock} onChange={(e) => setCloudLevel(e.target.value as CloudLevel)}>
                  {CLOUD_LEVELS.map((c) => <option key={c} value={c} disabled={!isAdmin && cloudAbove(c, cloud.max) && c !== level}>{CLOUD_LABEL[c]}</option>)}
                </select>
                <p className="field-hint">{cloudLock && !isAdmin ? <>Set by the admin <strong>{cloudLock}</strong>; only an admin can change it.</>
                  : cloudLock ? `Locked for ${k.user.username ?? "the owner"} (set by ${cloudLock}).`
                    : isAdmin && !own ? `${CLOUD_HELP[cloudLevel]} Saving it locks it for ${k.user.username ?? "the owner"}.` : CLOUD_HELP[cloudLevel]}</p>
              </div>
              <Field label="Monthly budget (USD)" name={`budget-${k.id}`} inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)}
                placeholder="no budget" disabled={!isAdmin && !!budgetLock}
                hint={budgetLock && !isAdmin ? <>Set by the admin <strong>{budgetLock}</strong>; only an admin can change it.</>
                  : budgetLock ? `Locked for ${k.user.username ?? "the owner"} (set by ${budgetLock}). Clear it to unlock.`
                    : isAdmin && !own ? `Cloud spend per calendar month (UTC). Saving it locks it for ${k.user.username ?? "the owner"}.`
                      : "Cloud spend per calendar month (UTC). When it is used up, this key stays local. Empty: no budget."} />
              <div className="row-gap limits-actions">
                <button type="submit" className="btn btn-primary btn-small" disabled={!isAdmin && !!lockedBy.rpm && !!lockedBy.tpm && !!cloudLock && !!budgetLock}>Save limits</button>
                {isAdmin && budgetLock && (
                  <button type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ budget_micros: null }, `Budget of ${k.name} removed and unlocked.`)}>
                    <IconLock size={14} aria-hidden="true" /> Unlock budget
                  </button>
                )}
                {isAdmin && cloudLock && (
                  <button type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ cloud: null }, `Cloud setting of ${k.name} unlocked; it is the default again.`)}>
                    <IconLock size={14} aria-hidden="true" /> Unlock cloud
                  </button>
                )}
                {isAdmin && (["rpm", "tpm"] as LimitName[]).filter((n) => lockedBy[n]).map((n) => (
                  <button key={n} type="button" className="btn btn-quiet btn-small"
                    onClick={() => void send({ [n]: null }, `${LIMIT_LABEL[n]} limit of ${k.name} unlocked; it is the default again.`)}>
                    <IconLock size={14} aria-hidden="true" /> Unlock {LIMIT_LABEL[n].toLowerCase()}
                  </button>
                ))}
                <button type="button" className="btn btn-quiet btn-small" onClick={onEdit}>Cancel</button>
              </div>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}

function CreateKey({ data, onCancel, onCreated }: { data: KeysResponse; onCancel: () => void; onCreated: (r: NewKeyResponse) => void }) {
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
      setError("Monthly budget: US dollars, like 25 or 2.50 (empty: no budget).");
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
      <h2 id="new-key-title">New API key</h2>
      <form onSubmit={submit} className="key-form">
        <div className="grid-2">
          <Field label="Name" name="key-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} required
            placeholder="laptop, ci, folio…" hint="What uses it, so you know which one to revoke." />
          <div className="field">
            <label htmlFor="key-expiry">Expires</label>
            <select id="key-expiry" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {EXPIRY.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
            </select>
          </div>
        </div>
        <fieldset className="allowlist key-models">
          <legend>Models</legend>
          <label className="card-pick">
            <input type="radio" name="key-scope" checked={all} onChange={() => setAll(true)} /> Every deployment, also ones added later
          </label>
          <label className="card-pick">
            <input type="radio" name="key-scope" checked={!all} onChange={() => setAll(false)} /> Only these:
          </label>
          {!all && (
            <div className="allowlist-cards" role="group" aria-label="Allowed models">
              {data.models.length === 0 && <span className="muted small">No deployments yet.</span>}
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
          <Field label="Requests per minute" name="key-rpm" type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)}
            placeholder={limits.default_rpm ? `default ${limits.default_rpm}` : "no limit"}
            hint={admin ? "Empty: the default for keys." : `Empty: the default; ${capText(limits.max_rpm)}.`} />
          <Field label="Tokens per minute" name="key-tpm" type="number" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)}
            placeholder={limits.default_tpm ? `default ${limits.default_tpm}` : "no limit"}
            hint={admin ? "Prompt and completion tokens together." : `Empty: the default; ${capText(limits.max_tpm)}.`} />
        </div>
        <fieldset className="allowlist key-cloud">
          <legend>Cloud providers</legend>
          <div className="cloud-picks" role="radiogroup" aria-label="May this key use cloud providers?">
            {CLOUD_LEVELS.map((c) => {
              const over = !admin && cloudAbove(c, cloudSettings.max);
              return (
                <label key={c} className={`card-pick${over ? " row-off" : ""}`} title={over ? "An admin has not allowed this for your own keys" : undefined}>
                  <input type="radio" name="key-cloud" checked={cloud === c} disabled={over} onChange={() => setCloud(c)} />
                  <span className="strong">{CLOUD_LABEL[c]}</span> <span className="muted small">{CLOUD_HELP[c]}</span>
                </label>
              );
            })}
          </div>
        </fieldset>
        {cloud !== "off" && (
          <div className="grid-2">
            <Field label="Monthly budget (USD)" name="key-budget" inputMode="decimal" value={budget} onChange={(e) => setBudget(e.target.value)}
              placeholder="no budget" hint="What this key may spend at cloud providers per calendar month (UTC). When it is used up, the key stays local." />
          </div>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 16 }}>
          <button type="submit" className="btn btn-primary" disabled={busy || !name.trim() || (!all && picked.length === 0)}>
            {busy ? "Creating…" : "Create key"}
          </button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </section>
  );
}

function IssuedKey({ issued, models, onDone }: { issued: NewKeyResponse; models: { name: string; api: string }[]; onDone: () => void }) {
  const k = issued.api_key;
  const first = k.models?.[0] ?? models.find((m) => m.api === "openai-chat")?.name ?? models[0]?.name ?? "my-model";
  const api_ = models.find((m) => m.name === first)?.api ?? "openai-chat";
  const s = snippets(issued.gateway_url, issued.key, first, api_, issued.trust_url);
  const [tab, setTab] = useState<"curl" | "python">("python");
  return (
    <section className="panel panel-link" aria-labelledby="issued-title">
      <h2 id="issued-title">Copy your key now</h2>
      <p>
        <strong>{k.name}</strong> is shown only this once; Forgeyard keeps a hash of it and the prefix <code>{k.prefix}</code>.
        {k.expires_at ? ` It expires ${when(k.expires_at)}.` : ""}
      </p>
      <CopyBox value={issued.key} label="The new API key" />
      <div className="tabs snippet-tabs" role="tablist" aria-label="Example">
        <button type="button" role="tab" aria-selected={tab === "python"} className={tab === "python" ? "on" : ""} onClick={() => setTab("python")}>Python (openai)</button>
        <button type="button" role="tab" aria-selected={tab === "curl"} className={tab === "curl" ? "on" : ""} onClick={() => setTab("curl")}>curl</button>
      </div>
      <pre className="snippet" aria-label={`${tab} example`}>{tab === "python" ? s.python : s.curl}</pre>
      <div className="row-gap">
        <CopyText value={tab === "python" ? s.python : s.curl} />
        <button type="button" className="btn btn-quiet" onClick={onDone}>Done</button>
      </div>
    </section>
  );
}

// Budgets past 80% or used up: the person's own, and their keys'.
function BudgetWarnings({ data }: { data: KeysResponse }) {
  const mine = data.person_budget;
  const keys = data.keys.filter((k) => k.status === "active" && (k.budget?.level === "warning" || k.budget?.level === "exhausted"));
  const reset = resetText(data.resets_at);
  return (
    <>
      {mine && mine.budget_micros !== null && (
        <section className="panel budget-panel" aria-labelledby="my-budget-title">
          <h2 id="my-budget-title">Your monthly budget</h2>
          <p className="muted small">Cloud spend of all your keys and the playground together this month. It resets on {reset}.</p>
          <BudgetMeter b={mine} label="Your monthly budget" />
        </section>
      )}
      {mine?.level === "exhausted" && (
        <Notice tone="error">Your monthly cloud budget is used up: until {reset}, requests with your keys stay local, and models that only the cloud serves are refused.</Notice>
      )}
      {mine?.level === "warning" && <Notice tone="info">You have used over 80% of your monthly cloud budget.</Notice>}
      {keys.map((k) => (
        <Notice key={k.id} tone={k.budget?.level === "exhausted" ? "error" : "info"}>
          {k.name} ({k.prefix}…){data.keys.some((x) => x.user.id !== k.user.id) && k.user.username ? ` of ${k.user.username}` : ""}: budget {LEVEL_LABEL[k.budget!.level!]}
          {k.budget?.level === "exhausted" ? `; its requests stay local until ${reset}.` : "."}
        </Notice>
      ))}
    </>
  );
}

function CopyText({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="btn" onClick={() => {
      void navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1600); }, () => undefined);
    }}>{done ? "Copied" : "Copy example"}</button>
  );
}

function CloudPanel({ cloud, onSaved }: { cloud: CloudSettings; onSaved: () => void }) {
  const [f, setF] = useState<CloudSettings>(cloud);
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
  return (
    <section className="panel" aria-labelledby="cloud-title">
      <h2 id="cloud-title">Cloud for keys</h2>
      <p className="muted small">
        Whether requests with a key may reach cloud providers. Keys made before 1.2 are “Never”, and changing the default here changes no existing
        key. People may set their own keys up to the most allowed; admins may set any key, which locks it for its owner. In the playground, admins
        may use the cloud and others get the most allowed.
      </p>
      <form onSubmit={save}>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="cloud-default">New keys get</label>
            <select id="cloud-default" value={f.default} onChange={(e) => setF({ ...f, default: e.target.value as CloudLevel })}>
              {CLOUD_LEVELS.map((c) => <option key={c} value={c}>{CLOUD_LABEL[c]}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="cloud-max">The most people may give their own keys</label>
            <select id="cloud-max" value={f.max} onChange={(e) => setF({ ...f, max: e.target.value as CloudLevel })}>
              {CLOUD_LEVELS.map((c) => <option key={c} value={c}>{CLOUD_LABEL[c]}</option>)}
            </select>
          </div>
        </div>
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <button type="submit" className="btn" style={{ marginTop: 14 }}>Save cloud setting</button>
      </form>
    </section>
  );
}

function LimitsPanel({ limits, onSaved }: { limits: KeyLimits; onSaved: () => void }) {
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
      <h2 id="limits-title">Limits for keys</h2>
      <p className="muted small">
        A key without its own limit gets the default. People may give their own keys lower limits, never more than the cap; admins may set any
        key to anything. Empty means no limit. Counted per key over a sliding minute on this server.
      </p>
      <form onSubmit={save}>
        <div className="grid-4">
          <Field label="Default requests/min" name="default_rpm" type="number" min={0} value={f.default_rpm} onChange={set("default_rpm")} placeholder="no limit" />
          <Field label="Default tokens/min" name="default_tpm" type="number" min={0} value={f.default_tpm} onChange={set("default_tpm")} placeholder="no limit" />
          <Field label="Cap requests/min" name="max_rpm" type="number" min={0} value={f.max_rpm} onChange={set("max_rpm")} placeholder="no cap" />
          <Field label="Cap tokens/min" name="max_tpm" type="number" min={0} value={f.max_tpm} onChange={set("max_tpm")} placeholder="no cap" />
        </div>
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <button type="submit" className="btn" style={{ marginTop: 14 }}>Save limits</button>
      </form>
    </section>
  );
}
