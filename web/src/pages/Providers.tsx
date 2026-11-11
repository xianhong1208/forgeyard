import { IconArrowLeft, IconCloud, IconCopy, IconCurrencyDollar, IconDownload, IconPlugConnected, IconPlus, IconTrash } from "@tabler/icons-react";
import { Fragment, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { BudgetMeter } from "../components/BudgetMeter";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { PriceRefresh } from "../components/PriceRefresh";
import { Field, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { budgetInput, LEVEL_LABEL, LEVEL_TONE, parseBudget, type Budget } from "../lib/budgets";
import { exact, when } from "../lib/format";
import { num } from "../lib/gateway";
import { PRICE_FIELDS, PRICE_LABEL, parsePrice, perMillion, priceText, SOURCE_LABEL } from "../lib/prices";
import type { Egress, Provider, ProviderKind, ProviderModel, ProviderResponse, ProvidersResponse, TestResult, UpstreamModel } from "../lib/types";
import { useApi } from "../lib/useApi";

type Msg = { tone: "ok" | "error"; text: string } | null;

const API_NAME: Record<string, string> = { "openai-chat": "chat", "openai-embeddings": "embeddings" };

function headersText(h: Record<string, string> | undefined): string {
  return Object.entries(h ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n");
}

function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

function testText(r: TestResult): string {
  return r.ok ? `Connected in ${r.ms} ms: ${r.models} model${r.models === 1 ? "" : "s"} listed${r.sample.length ? ` (${r.sample.join(", ")}${r.models > r.sample.length ? ", …" : ""})` : ""}.`
    : `HTTP ${r.status}: ${r.message ?? "the provider refused"}`;
}

function WhereChip({ p }: { p: Pick<Provider, "external" | "enabled"> }) {
  return (
    <>
      <span className={`chip ${p.external ? "chip-tone-wait" : "chip-tone-ok"}`}>{p.external ? "cloud" : "inside your network"}</span>
      {!p.enabled && <> <span className="chip chip-tone-off">disabled</span></>}
    </>
  );
}

function EgressPanel({ egress }: { egress: Egress }) {
  return (
    <section className="panel" aria-labelledby="egress-title">
      <h2 id="egress-title">What leaves this server</h2>
      <dl className="facts">
        <dt>Connection</dt><dd>{egress.proxy ? "through EGRESS_PROXY" : "direct (no proxy)"}; TLS verified{egress.ca_file ? ", plus EGRESS_CA_FILE" : ""}; redirects never followed</dd>
        <dt>Hosts</dt><dd>{egress.allowed_hosts.length ? egress.allowed_hosts.map((h) => <code key={h} className="egress-host">{h}</code>) : "any host (EGRESS_ALLOWED_HOSTS is empty)"}</dd>
        <dt>Refused</dt><dd>plain http and private addresses for cloud providers{egress.private_hosts ? " (allowed on this server: PROVIDER_ALLOW_PRIVATE_HOSTS, for testing)" : ""}; link-local and cloud metadata addresses always</dd>
        <dt>A failing target</dt><dd>is tried last for {egress.cooldown_s} s; answers may take {egress.read_timeout_s} s</dd>
      </dl>
    </section>
  );
}

export function ProvidersPage() {
  const { data, error, reload } = useApi<ProvidersResponse>("/providers");
  const [adding, setAdding] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const [testing, setTesting] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const admin = data?.is_admin ?? false;
  const priced = (data?.providers ?? []).some((p) => p.external && p.models.length > 0);

  const test = async (p: Provider) => {
    setTesting(p.id);
    setMsg(null);
    try {
      const r = await api<TestResult>(`/providers/${p.id}/test`, { method: "POST" });
      setMsg({ tone: r.ok ? "ok" : "error", text: `${p.name}: ${testText(r)}` });
    } catch (e) {
      setMsg({ tone: "error", text: `${p.name}: ${errorText(e)}` });
    } finally {
      setTesting(null);
    }
  };

  return (
    <>
      <PageHeader index="07 · Providers" title="Cloud providers"
        lead="Model APIs outside Forgeyard: OpenAI, Anthropic, Gemini, Azure OpenAI, OpenRouter, or any OpenAI-compatible server. A route can send requests there when local replicas are busy or down, or serve a cloud model under a name of its own."
        actions={admin && (
          <>
            {priced && (
              <button type="button" className="btn" aria-expanded={refreshing} onClick={() => setRefreshing((v) => !v)}>
                <IconCurrencyDollar size={16} aria-hidden="true" /> Refresh prices from OpenRouter
              </button>
            )}
            <button type="button" className="btn btn-primary" aria-expanded={adding} onClick={() => setAdding((v) => !v)}>
              <IconPlus size={16} aria-hidden="true" /> Add a provider
            </button>
          </>
        )} />
      <HelpLinks>
        <HelpLink page="cloud-providers" anchor="add-a-provider">How do I add OpenAI, Anthropic, Gemini or Azure?</HelpLink>
        <HelpLink page="cloud-providers" anchor="when-a-request-spills">When does a request go to the cloud?</HelpLink>
        <HelpLink page="cloud-providers" anchor="what-each-providers-endpoint-cannot-do">What each provider's endpoint cannot do</HelpLink>
        <HelpLink page="cloud-providers" anchor="cost-and-prices">How cost is counted</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {refreshing && <PriceRefresh onClose={() => setRefreshing(false)}
        onApplied={(text) => { setRefreshing(false); setMsg({ tone: "ok", text }); reload(); }} />}
      {adding && data && <AddProvider kinds={data.kinds} onCancel={() => setAdding(false)}
        onAdded={(p) => { setAdding(false); setMsg({ tone: "ok", text: `${p.name} added. Test it, then add its models.` }); reload(); }} />}
      {data && data.providers.length === 0 && !adding && (
        <section className="panel empty-keys">
          <IconCloud size={22} aria-hidden="true" />
          <p className="muted">
            No providers yet. {admin ? "Add one to give a local model somewhere to go when it is busy, or to serve a cloud model through Forgeyard's keys and limits."
              : "Admins add providers."} API keys decide whether their requests may leave your network: every key is “never” until someone allows it.
          </p>
        </section>
      )}
      {data && data.providers.length > 0 && (
        <div className="table-wrap">
          <table className="table providers-table">
            <thead><tr><th>Provider</th><th>Base URL</th><th>Key</th><th>Where</th><th>Models</th>{admin && <th><span className="sr-only">Actions</span></th>}</tr></thead>
            <tbody>
              {data.providers.map((p) => (
                <tr key={p.id} className={p.enabled ? "" : "row-off"}>
                  <td data-label="Provider"><div className="strong"><Link to={`/providers/${p.id}`}>{p.name}</Link></div><div className="muted small">{p.kind_label}</div></td>
                  <td data-label="Base URL" className="mono small cell-url">{p.base_url}</td>
                  <td data-label="Key" className="mono small">{p.key_hint ?? <span className="muted">none</span>}</td>
                  <td data-label="Where"><WhereChip p={p} />
                    {p.budget?.level && p.budget.level !== "ok" && <> <span className={`chip chip-tone-${LEVEL_TONE[p.budget.level]}`}>budget {LEVEL_LABEL[p.budget.level]}</span></>}</td>
                  <td data-label="Models" className="small">
                    <div>
                    {p.models.length ? p.models.map((m) => (
                      <div key={m.id}><span className="mono">{m.upstream_model}</span>
                        {p.external && <span className="muted"> · {m.price_input === null && m.price_output === null ? "no price" : `${perMillion(m.price_input)} / ${perMillion(m.price_output)}`}</span>}
                        {m.cooldown_s > 0 && <> <span className="chip chip-tone-bad" title="It failed recently and is tried last">cooling {Math.ceil(m.cooldown_s)} s</span></>}</div>
                    )) : <span className="muted">none yet</span>}
                    </div>
                  </td>
                  {admin && (
                    <td className="cell-actions">
                      <button type="button" className="btn btn-small" disabled={testing === p.id} onClick={() => void test(p)}>
                        <IconPlugConnected size={14} aria-hidden="true" /> {testing === p.id ? "Testing…" : "Test"}
                      </button>
                      <Link className="btn btn-quiet btn-small" to={`/providers/${p.id}`}>Open</Link>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && <EgressPanel egress={data.egress} />}
    </>
  );
}

function AddProvider({ kinds, onCancel, onAdded }: { kinds: ProviderKind[]; onCancel: () => void; onAdded: (p: Provider) => void }) {
  const [kind, setKind] = useState(kinds[0]?.kind ?? "openai");
  const k = kinds.find((x) => x.kind === kind) ?? kinds[0];
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [key, setKey] = useState("");
  const [external, setExternal] = useState<boolean | null>(null);
  const [allowHttp, setAllowHttp] = useState(false);
  const [headers, setHeaders] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ext = external ?? k?.default_external ?? true;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ provider: Provider }>("/providers", {
        method: "POST",
        body: { name: name.trim() || k.label, kind, base_url: baseUrl.trim() || null, api_key: key.trim() || null, external: ext,
          allow_http: !ext && allowHttp, headers: parseHeaders(headers) },
      });
      onAdded(r.provider);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel panel-link" aria-labelledby="add-provider-title">
      <h2 id="add-provider-title">Add a provider</h2>
      <form onSubmit={submit}>
        <fieldset className="allowlist">
          <legend>Kind</legend>
          <div className="kind-picks" role="radiogroup" aria-label="Kind of provider">
            {kinds.map((x) => (
              <label key={x.kind} className={`card-pick kind-pick${x.kind === kind ? " on" : ""}`}>
                <input type="radio" name="provider-kind" checked={x.kind === kind} onChange={() => { setKind(x.kind); setExternal(null); }} />
                <span className="strong">{x.label}</span>
              </label>
            ))}
          </div>
          {k && <p className="muted small kind-note">{k.note}</p>}
        </fieldset>
        <div className="grid-2">
          <Field label="Name" name="p-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder={k?.label}
            hint="Shown in routes and in X-Forgeyard-Target." />
          <Field label="Base URL" name="p-base" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={k?.base_url || k?.base_url_hint}
            hint={k?.base_url ? "Empty: the address above." : "Required for this kind."} required={!k?.base_url} />
          <Field label="API key" name="p-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
            hint="Stored encrypted. It is never shown again, only its last four characters." required={k?.key_required} />
          <div className="field">
            <label htmlFor="p-headers">Extra headers (optional, not secret)</label>
            <textarea id="p-headers" className="mono" rows={2} value={headers} onChange={(e) => setHeaders(e.target.value)}
              placeholder={kind === "anthropic" ? "anthropic-workspace-id: wrkspc_…" : kind === "openrouter" ? "X-Title: Forgeyard" : "Name: value"} />
            <p className="field-hint">One per line. The key never goes here.</p>
          </div>
        </div>
        <label className="check provider-check">
          <input type="checkbox" checked={ext} onChange={(e) => setExternal(e.target.checked)} />
          <span><strong>Outside your network (cloud).</strong> Only keys allowed to use the cloud reach it. Untick for a server of your own, e.g. a vLLM on your VPN.</span>
        </label>
        {!ext && (
          <label className="check provider-check">
            <input type="checkbox" checked={allowHttp} onChange={(e) => setAllowHttp(e.target.checked)} />
            <span>Allow plain HTTP (only to a private address).</span>
          </label>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 16 }}>
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "Adding…" : "Add provider"}</button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </section>
  );
}

export function ProviderDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, reload } = useApi<ProviderResponse>(`/providers/${id}`);
  const [msg, setMsg] = useState<Msg>(null);
  const [busy, setBusy] = useState(false);
  const p = data?.provider;
  const admin = data?.is_admin ?? false;

  const run = async (what: () => Promise<void>) => {
    setBusy(true);
    setMsg(null);
    try { await what(); } catch (e) { setMsg({ tone: "error", text: errorText(e) }); } finally { setBusy(false); }
  };
  const test = () => run(async () => {
    const r = await api<TestResult>(`/providers/${id}/test`, { method: "POST" });
    setMsg({ tone: r.ok ? "ok" : "error", text: testText(r) });
  });
  const toggle = () => run(async () => {
    await api(`/providers/${id}`, { method: "PATCH", body: { enabled: !p?.enabled } });
    setMsg({ tone: "ok", text: p?.enabled ? "Disabled: routes skip its models." : "Enabled." });
    reload();
  });
  const clone = () => run(async () => {
    const name = window.prompt("Name of the copy (its key is not copied; enter one for it):", `${p?.name} copy`);
    if (!name) return;
    const r = await api<{ provider: Provider }>(`/providers/${id}/clone`, { method: "POST", body: { name } });
    navigate(`/providers/${r.provider.id}`);
  });
  const remove = () => run(async () => {
    if (!window.confirm(`Delete ${p?.name} and its models? Its key is deleted with it.`)) return;
    await api(`/providers/${id}`, { method: "DELETE" });
    navigate("/providers", { replace: true });
  });

  return (
    <>
      <p className="crumb"><Link to="/providers"><IconArrowLeft size={14} aria-hidden="true" /> Providers</Link></p>
      <PageHeader index={`07 · ${p?.kind_label ?? "Provider"}`} title={p?.name ?? "Provider"}
        lead={p ? `${p.base_url} · added by ${p.created_by ?? "?"} ${when(p.created_at)}` : undefined}
        actions={p && admin && (
          <>
            <button type="button" className="btn" disabled={busy} onClick={() => void test()}><IconPlugConnected size={16} aria-hidden="true" /> Test connection</button>
            <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void clone()}><IconCopy size={16} aria-hidden="true" /> Clone</button>
            <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void toggle()}>{p.enabled ? "Disable" : "Enable"}</button>
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void remove()}><IconTrash size={16} aria-hidden="true" /> Delete</button>
          </>
        )} />
      {error && <Notice tone="error">{error}</Notice>}
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {p && (
        <>
          <div className="wl-status"><WhereChip p={p} /><span className="mono small">{p.key_hint ? `key ${p.key_hint}` : "no key"}</span></div>
          {p.budget && <ProviderBudget p={p} budget={p.budget} admin={admin} onSaved={(text) => { setMsg({ tone: "ok", text }); reload(); }} />}
          <Models p={p} admin={admin} kinds={data.kinds} onChanged={reload} />
          {admin && <Settings p={p} onSaved={(text) => { setMsg({ tone: "ok", text }); reload(); }} />}
        </>
      )}
    </>
  );
}

function ProviderBudget({ p, budget, admin, onSaved }: { p: Provider; budget: Budget; admin: boolean; onSaved: (text: string) => void }) {
  const [value, setValue] = useState(budgetInput(budget.budget_micros));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const micros = parseBudget(value);
    if (micros === "bad") {
      setError("US dollars, like 100 or 12.50; empty: no budget.");
      return;
    }
    setError(null);
    try {
      await api(`/budgets/providers/${p.id}`, { method: "PUT", body: { budget_micros: micros } });
      onSaved(micros === null ? "Budget removed." : "Budget saved.");
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <section className="panel budget-panel" aria-labelledby="budget-title">
      <h2 id="budget-title">Monthly budget</h2>
      <p className="muted small">
        What requests to {p.name} may cost per calendar month (UTC), reserved before each one is sent. When it is used up, routes skip {p.name}: other
        providers or local replicas serve the requests they can.
      </p>
      {budget.level === "exhausted" && <Notice tone="error">This month's budget is used up: {p.name} is skipped until the month ends.</Notice>}
      {budget.level === "warning" && <Notice tone="info">Over 80% of this month's budget is spent.</Notice>}
      <BudgetMeter b={budget} label={`Budget of ${p.name}`} />
      {admin && (
        <form onSubmit={(e) => void save(e)} className="row-gap" style={{ marginTop: 12, alignItems: "flex-end" }}>
          <Field label="Monthly budget (USD)" name="pb-budget" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)}
            placeholder="no budget" error={error} />
          <button type="submit" className="btn">Save budget</button>
        </form>
      )}
    </section>
  );
}

function Settings({ p, onSaved }: { p: Provider; onSaved: (text: string) => void }) {
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [key, setKey] = useState("");
  const [external, setExternal] = useState(p.external);
  const [allowHttp, setAllowHttp] = useState(p.allow_http);
  const [headers, setHeaders] = useState(headersText(p.headers));
  const [timeout, setTimeoutS] = useState(p.timeout_s ? String(p.timeout_s) : "");
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent, clearKey = false) => {
    e.preventDefault();
    setError(null);
    try {
      const body: Record<string, unknown> = { base_url: baseUrl.trim(), external, allow_http: !external && allowHttp,
        headers: parseHeaders(headers), timeout_s: timeout ? Number(timeout) : null };
      if (clearKey) body.clear_api_key = true;
      else if (key.trim()) body.api_key = key.trim();
      await api(`/providers/${p.id}`, { method: "PATCH", body });
      setKey("");
      onSaved(clearKey ? "Key removed." : key.trim() ? "Saved, with the new key." : "Saved.");
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <section className="panel" aria-labelledby="settings-title">
      <h2 id="settings-title">Settings</h2>
      <form onSubmit={(e) => void save(e)}>
        <div className="grid-2">
          <Field label="Base URL" name="ps-base" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required />
          <Field label={p.has_key ? `Replace the key (now ${p.key_hint})` : "API key"} name="ps-key" type="password" autoComplete="off" value={key}
            onChange={(e) => setKey(e.target.value)} placeholder="leave empty to keep it" hint="Write-only: the key is never shown." />
          <div className="field">
            <label htmlFor="ps-headers">Extra headers (not secret)</label>
            <textarea id="ps-headers" className="mono" rows={2} value={headers} onChange={(e) => setHeaders(e.target.value)} />
          </div>
          <Field label="Answer timeout (seconds)" name="ps-timeout" type="number" min={1} max={3600} value={timeout}
            onChange={(e) => setTimeoutS(e.target.value)} placeholder="server default" />
        </div>
        <label className="check provider-check">
          <input type="checkbox" checked={external} onChange={(e) => setExternal(e.target.checked)} />
          <span><strong>Outside your network (cloud).</strong> Only keys allowed to use the cloud reach it.</span>
        </label>
        {!external && (
          <label className="check provider-check">
            <input type="checkbox" checked={allowHttp} onChange={(e) => setAllowHttp(e.target.checked)} />
            <span>Allow plain HTTP (only to a private address).</span>
          </label>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 14 }}>
          <button type="submit" className="btn">Save settings</button>
          {p.has_key && <button type="button" className="btn btn-quiet" onClick={(e) => void save(e as unknown as FormEvent, true)}>Remove the key</button>}
        </div>
      </form>
    </section>
  );
}

function Models({ p, admin, kinds, onChanged }: { p: Provider; admin: boolean; kinds: ProviderKind[]; onChanged: () => void }) {
  const k = kinds.find((x) => x.kind === p.kind);
  const [model, setModel] = useState("");
  const [apiName, setApiName] = useState("openai-chat");
  const [context, setContext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [listing, setListing] = useState<UpstreamModel[] | null>(null);
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const add = async (models: { upstream_model: string; api: string; context_length?: number | null }[]) => {
    setError(null);
    try {
      await api(`/providers/${p.id}/models`, { method: "POST", body: { models } });
      setModel(""); setContext(""); setListing(null); setPicked({});
      onChanged();
    } catch (err) { setError(errorText(err)); }
  };
  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setListing((await api<{ models: UpstreamModel[] }>(`/providers/${p.id}/upstream-models`)).models);
    } catch (err) { setError(errorText(err)); } finally { setLoading(false); }
  };
  const remove = async (m: ProviderModel) => {
    if (!window.confirm(`Remove ${m.upstream_model}?`)) return;
    try { await api(`/provider-models/${m.id}`, { method: "DELETE" }); onChanged(); } catch (err) { setError(errorText(err)); }
  };
  const shown = (listing ?? []).filter((m) => !filter || m.id.toLowerCase().includes(filter.toLowerCase())).slice(0, 200);

  return (
    <section className="panel" aria-labelledby="models-title">
      <div className="panel-head">
        <h2 id="models-title">Models</h2>
        {admin && p.external && p.models.length > 0 ? (
          <button type="button" className="btn btn-small" aria-expanded={refreshing} onClick={() => setRefreshing((v) => !v)}>
            <IconCurrencyDollar size={14} aria-hidden="true" /> Refresh prices from OpenRouter
          </button>
        ) : <span className="muted small">what routes may send to {p.name}</span>}
      </div>
      {!p.external && p.models.length > 0 && <p className="muted small">Inside your network: requests here cost nothing, so its models have no prices.</p>}
      {note && <Notice tone="ok">{note}</Notice>}
      {refreshing && <PriceRefresh providerId={p.id} onClose={() => setRefreshing(false)}
        onApplied={(text) => { setRefreshing(false); setNote(text); onChanged(); }} />}
      {p.models.length === 0 ? <p className="muted small">No models yet. {admin ? "Add them by name, or import the provider's list." : ""}</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Model</th><th>Serves</th>{p.external && <th>Price per 1M tokens</th>}<th>Context</th><th>Used by</th><th>Since the server started</th>{admin && <th><span className="sr-only">Actions</span></th>}</tr></thead>
            <tbody>
              {p.models.map((m) => (
                <Fragment key={m.id}>
                <tr>
                  <td data-label="Model"><div><span className="mono">{m.upstream_model}</span>{m.display_name && <div className="muted small">{m.display_name}</div>}</div></td>
                  <td data-label="Serves" className="small">{API_NAME[m.api] ?? m.api}</td>
                  {p.external && <td data-label="Price per 1M" className="small"><Prices m={m} /></td>}
                  <td data-label="Context" className="small">{m.context_length ? num(m.context_length) : "—"}</td>
                  <td data-label="Used by" className="small">{m.routes.length ? m.routes.map((r) => <code key={r} className="egress-host">{r}</code>) : <span className="muted">no route</span>}</td>
                  <td data-label="Since start" className="small">{num(m.sent)} sent{m.in_flight ? ` · ${m.in_flight} in flight` : ""}{m.cooldown_s > 0 ? ` · cooling down ${Math.ceil(m.cooldown_s)} s` : ""}</td>
                  {admin && <td className="cell-actions">
                    {p.external && <button type="button" className="btn btn-quiet btn-small" aria-expanded={editing === m.id}
                      onClick={() => setEditing(editing === m.id ? null : m.id)}>Prices</button>}
                    <button type="button" className="btn btn-quiet btn-small" disabled={m.routes.length > 0}
                    title={m.routes.length ? "Remove it from its routes first" : undefined} onClick={() => void remove(m)}>Remove</button></td>}
                </tr>
                {editing === m.id && (
                  <tr className="price-edit-row"><td colSpan={7}>
                    <PriceEditor m={m} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); setNote(`Prices of ${m.upstream_model} saved.`); onChanged(); }} />
                  </td></tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {admin && (
        <>
          <form className="model-add" onSubmit={(e) => { e.preventDefault(); void add([{ upstream_model: model.trim(), api: apiName, context_length: context ? Number(context) : null }]); }}>
            <Field label={p.kind === "azure" ? "Deployment name" : "Model name"} name="pm-name" value={model} onChange={(e) => setModel(e.target.value)}
              placeholder={p.kind === "anthropic" ? "claude-sonnet-4-5" : p.kind === "gemini" ? "gemini-2.5-flash" : "gpt-4o-mini"} required />
            <div className="field">
              <label htmlFor="pm-api">Serves</label>
              <select id="pm-api" value={apiName} onChange={(e) => setApiName(e.target.value)}>
                <option value="openai-chat">chat and completions</option>
                {k?.embeddings !== false && <option value="openai-embeddings">embeddings</option>}
              </select>
            </div>
            <Field label="Context (tokens, optional)" name="pm-ctx" type="number" min={1} value={context} onChange={(e) => setContext(e.target.value)} />
            <div className="model-add-actions">
              <button type="submit" className="btn" disabled={!model.trim()}><IconPlus size={15} aria-hidden="true" /> Add model</button>
              {k?.models_endpoint && <button type="button" className="btn btn-quiet" disabled={loading} onClick={() => void load()}>
                <IconDownload size={15} aria-hidden="true" /> {loading ? "Asking…" : "Import from /models"}</button>}
            </div>
          </form>
          {p.kind === "azure" && <p className="muted small">Azure lists base models at /models, not your deployments: add your deployment names by hand.</p>}
          {listing && (
            <div className="import-list">
              <div className="row-gap">
                <input aria-label="Filter models" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
                <span className="muted small">{listing.length} listed</span>
              </div>
              <ul>
                {shown.map((m) => (
                  <li key={`${m.id}-${m.api}`}>
                    <label className="card-pick">
                      <input type="checkbox" disabled={m.added} checked={m.added || m.id in picked}
                        onChange={() => setPicked((x) => { const y = { ...x }; if (m.id in y) delete y[m.id]; else y[m.id] = m.api; return y; })} />
                      <span className="mono">{m.id}</span>
                      <span className="muted small">{API_NAME[m.api]}{m.context_length ? ` · ${num(m.context_length)} ctx` : ""}{m.added ? " · added" : ""}</span>
                    </label>
                  </li>
                ))}
              </ul>
              <div className="row-gap">
                <button type="button" className="btn btn-primary" disabled={!Object.keys(picked).length}
                  onClick={() => void add(Object.entries(picked).map(([id, a]) => ({ upstream_model: id, api: a,
                    context_length: listing.find((m) => m.id === id)?.context_length ?? null })))}>
                  Add {Object.keys(picked).length || ""} selected
                </button>
                <button type="button" className="btn btn-quiet" onClick={() => { setListing(null); setPicked({}); }}>Close</button>
              </div>
            </div>
          )}
        </>
      )}
      {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
    </section>
  );
}

function Prices({ m }: { m: ProviderModel }) {
  if (PRICE_FIELDS.every((f) => m[f] === null)) return <span className="chip chip-tone-wait" title="Requests to it are counted as unpriced until it has prices">no price</span>;
  return (
    <div className="price-cell">
      <div><span className="muted">in</span> <span className="mono">{perMillion(m.price_input)}</span> · <span className="muted">out</span> <span className="mono">{perMillion(m.price_output)}</span></div>
      {(m.price_cache_read !== null || m.price_cache_write !== null) && (
        <div><span className="muted">cached</span> <span className="mono">{perMillion(m.price_cache_read)}</span>{m.price_cache_write !== null && <> · <span className="muted">write</span> <span className="mono">{perMillion(m.price_cache_write)}</span></>}</div>
      )}
      {m.price_source && (
        <div className="muted" title={m.price_checked_at ? exact(m.price_checked_at) : undefined}>
          {SOURCE_LABEL[m.price_source] ?? m.price_source}{m.price_ref && m.price_source === "openrouter" ? ` (${m.price_ref})` : ""}{m.price_checked_at ? `, ${when(m.price_checked_at)}` : ""}
        </div>
      )}
    </div>
  );
}

function PriceEditor({ m, onCancel, onSaved }: { m: ProviderModel; onCancel: () => void; onSaved: () => void }) {
  const [text, setText] = useState<Record<string, string>>(() => Object.fromEntries(PRICE_FIELDS.map((f) => [f, priceText(m[f])])));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const body: Record<string, number | null> = {};
    for (const f of PRICE_FIELDS) {
      const v = parsePrice(text[f]);
      if (v === "bad") { setError(`${PRICE_LABEL[f]}: a number of US dollars per million tokens, like 2.50.`); return; }
      body[f] = v;
    }
    try {
      await api(`/provider-models/${m.id}`, { method: "PATCH", body });
      onSaved();
    } catch (err) { setError(errorText(err)); }
  };
  return (
    <form className="price-edit" onSubmit={(e) => void save(e)} aria-label={`Prices of ${m.upstream_model}`}>
      <p className="muted small">US dollars per million tokens, as on the provider's pricing page. Leave a field empty when it does not apply; without an input or output price, requests are counted as unpriced. Cached input and cache writes fall back to the input price.</p>
      <div className="grid-4">
        {PRICE_FIELDS.map((f) => (
          <Field key={f} label={`${PRICE_LABEL[f]} ($)`} name={`${f}-${m.id}`} inputMode="decimal" value={text[f]}
            onChange={(e) => setText((x) => ({ ...x, [f]: e.target.value }))} placeholder="unknown" />
        ))}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="row-gap"><button type="submit" className="btn">Save prices</button><button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button></div>
    </form>
  );
}
