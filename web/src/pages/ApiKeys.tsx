import { IconKey, IconPlus } from "@tabler/icons-react";
import { useState, type ChangeEvent, type FormEvent } from "react";

import { CopyBox, Field, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { keyModels, limitText, num, snippets } from "../lib/gateway";
import type { ApiKey, KeyLimits, KeysResponse, NewKeyResponse } from "../lib/types";
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
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      {creating && data && (
        issued ? <IssuedKey issued={issued} models={data.models} onDone={close} />
          : <CreateKey data={data} onCancel={close} onCreated={(r) => { setIssued(r); reload(); }} />
      )}

      {data && (
        <section className="panel gw-base" aria-labelledby="base-title">
          <h2 id="base-title">Gateway address</h2>
          <CopyBox value={data.gateway_url} label="Gateway base URL" />
          <p className="muted small">
            Use it as the <code>base_url</code> of any OpenAI client, with <code>Authorization: Bearer &lt;key&gt;</code>. The <code>model</code> is a
            deployment's model name{data.models.length ? ":" : " (none deployed yet)."}
          </p>
          {data.models.length > 0 && (
            <p className="model-names">{data.models.map((m) => <code key={m.name}>{m.name}</code>)}</p>
          )}
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
              <p className="muted">No keys yet. Create one for each program or computer, so you can revoke one without the others.</p>
            </div>
          ) : (
            <div className="table-wrap">
              <table className="table keys-table">
                <thead>
                  <tr><th>Key</th>{everyone && <th>Owner</th>}<th>Models</th><th>Limits</th><th>Last used</th><th>Expires</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
                </thead>
                <tbody>
                  {data.keys.map((k) => (
                    <KeyRow key={k.id} k={k} everyone={everyone} isAdmin={data.is_admin} limits={data.limits}
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
    </>
  );
}

function KeyRow({ k, everyone, isAdmin, limits, editing, onEdit, onSaved, onError, onRevoke }: {
  k: ApiKey; everyone: boolean; isAdmin: boolean; limits: KeyLimits; editing: boolean; onEdit: () => void;
  onSaved: (text: string) => void; onError: (text: string) => void; onRevoke: () => void;
}) {
  const [rpm, setRpm] = useState(k.rpm ? String(k.rpm) : "");
  const [tpm, setTpm] = useState(k.tpm ? String(k.tpm) : "");
  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api(`/keys/${k.id}`, { method: "PATCH", body: { rpm: rpm ? Number(rpm) : null, tpm: tpm ? Number(tpm) : null } });
      onSaved(`Limits of ${k.name} saved.`);
    } catch (err) {
      onError(errorText(err));
    }
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
          <div>{limitText(k.effective_rpm, "requests")}</div>
          <div>{limitText(k.effective_tpm, "tokens")}</div>
          {active && (k.this_minute.requests > 0 || k.this_minute.tokens > 0) && (
            <div className="muted">now: {num(k.this_minute.requests)} req · {num(k.this_minute.tokens)} tok</div>
          )}
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
          <td colSpan={everyone ? 8 : 7}>
            <form className="limits-form" onSubmit={save} aria-label={`Limits of ${k.name}`}>
              <Field label="Requests per minute" name={`rpm-${k.id}`} type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)}
                placeholder={limits.default_rpm ? `default ${limits.default_rpm}` : "server default"}
                hint={isAdmin ? "Admins may set any value." : `Empty: the default; ${capText(limits.max_rpm)}.`} />
              <Field label="Tokens per minute" name={`tpm-${k.id}`} type="number" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)}
                placeholder={limits.default_tpm ? `default ${limits.default_tpm}` : "server default"}
                hint={isAdmin ? "Prompt and completion tokens together." : `Empty: the default; ${capText(limits.max_tpm)}.`} />
              <div className="row-gap limits-actions">
                <button type="submit" className="btn btn-primary btn-small">Save limits</button>
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { limits, is_admin: admin } = data;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api<NewKeyResponse>("/keys", {
        method: "POST",
        body: {
          name: name.trim(), models: all ? null : picked, rpm: rpm ? Number(rpm) : null, tpm: tpm ? Number(tpm) : null,
          expires_in_days: expiry ? Number(expiry) : null,
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
  const s = snippets(issued.gateway_url, issued.key, first, api_);
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

function CopyText({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="btn" onClick={() => {
      void navigator.clipboard?.writeText(value).then(() => { setDone(true); setTimeout(() => setDone(false), 1600); }, () => undefined);
    }}>{done ? "Copied" : "Copy example"}</button>
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
