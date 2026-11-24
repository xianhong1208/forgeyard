import { IconArrowLeft, IconCloud, IconCopy, IconCurrencyDollar, IconDownload, IconPlugConnected, IconPlus, IconTrash } from "@tabler/icons-react";
import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { useAuth } from "../auth";

import { AdvisorBanner } from "../components/Advisor";
import { BudgetMeter } from "../components/BudgetMeter";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { PriceRefresh } from "../components/PriceRefresh";
import { Field, Notice, PageHeader } from "../components/ui";
import { labels, t as tNow, tPlural } from "../i18n/core";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { budgetInput, LEVEL_LABEL, LEVEL_TONE, parseBudget, type Budget } from "../lib/budgets";
import { exact, when } from "../lib/format";
import { num } from "../lib/gateway";
import { PRICE_FIELDS, PRICE_LABEL, parsePrice, perMillion, priceText, SOURCE_LABEL } from "../lib/prices";
import type { Egress, Provider, ProviderKind, ProviderModel, ProviderResponse, ProvidersResponse, ProviderWhere, TestResult, UpstreamModel } from "../lib/types";
import { useApi } from "../lib/useApi";
import { CloudModels } from "./CloudModels";

type Msg = { tone: "ok" | "error"; text: string } | null;

const API_NAME: Record<string, string> = labels({ "openai-chat": "cloud.api.openai-chat", "openai-embeddings": "cloud.api.openai-embeddings" });

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
  if (!r.ok) return tNow("cloud.test.failed", { status: String(r.status), message: r.message ?? tNow("cloud.test.refused") });
  const vars = { ms: String(r.ms), n: String(r.models) };
  return r.sample.length
    ? tPlural("cloud.test.okSample", r.models, { ...vars, sample: `${r.sample.join(", ")}${r.models > r.sample.length ? ", …" : ""}` })
    : tPlural("cloud.test.ok", r.models, vars);
}

function WhereChip({ p }: { p: Pick<Provider, "external" | "enabled"> }) {
  const t = useT();
  return (
    <>
      <span className={`chip ${p.external ? "chip-tone-wait" : "chip-tone-ok"}`}>{p.external ? t("cloud.where.cloud") : t("cloud.where.inside")}</span>
      {!p.enabled && <> <span className="chip chip-tone-off">{t("cloud.where.disabled")}</span></>}
    </>
  );
}

function EgressPanel({ egress }: { egress: Egress }) {
  const t = useT();
  const how = egress.proxy ? t("cloud.egress.viaProxy") : t("cloud.egress.direct");
  return (
    <section className="panel" aria-labelledby="egress-title">
      <h2 id="egress-title">{t("cloud.egress.title")}</h2>
      <dl className="facts">
        <dt>{t("cloud.egress.connection")}</dt><dd>{egress.ca_file ? t("cloud.egress.tlsCa", { how }) : t("cloud.egress.tls", { how })}</dd>
        <dt>{t("cloud.egress.hosts")}</dt><dd>{egress.allowed_hosts.length ? egress.allowed_hosts.map((h) => <code key={h} className="egress-host">{h}</code>) : t("cloud.egress.anyHost")}</dd>
        <dt>{t("cloud.egress.refused")}</dt><dd>{egress.private_hosts ? t("cloud.egress.refusedPrivate") : t("cloud.egress.refusedText")}</dd>
        <dt>{t("cloud.egress.failing")}</dt><dd>{t("cloud.egress.failingText", { cooldown: String(egress.cooldown_s), timeout: String(egress.read_timeout_s) })}</dd>
      </dl>
    </section>
  );
}

// 1.4 (#81): Cloud, two tabs. Providers (the accounts at model APIs, and what leaves this server) and
// Cloud models (names of their own that an admin made: a provider's model under Forgeyard's keys and
// limits). A deployment's routing and fallback lives on the deployment's page. Before 1.4 this was the
// Providers page at /providers, which still opens it.
export function CloudPage() {
  const t = useT();
  const [search, setSearch] = useSearchParams();
  const tab = search.get("tab") === "models" ? "models" : "providers";
  const { data, error, reload } = useApi<ProvidersResponse>(tab === "providers" ? "/providers" : null);
  const [adding, setAdding] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const [testing, setTesting] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const { user } = useAuth();
  const admin = user?.role === "admin";
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
      <PageHeader index={t("cloud.index")} title={t("cloud.title")}
        lead={t("cloud.lead")}
        actions={admin && (tab === "providers" ? data && (
          <>
            {priced && (
              <button type="button" className="btn" aria-expanded={refreshing} onClick={() => setRefreshing((v) => !v)}>
                <IconCurrencyDollar size={16} aria-hidden="true" /> {t("cloud.refreshPrices")}
              </button>
            )}
            <button type="button" className="btn btn-primary" aria-expanded={adding} onClick={() => setAdding((v) => !v)}>
              <IconPlus size={16} aria-hidden="true" /> {t("cloud.addProvider")}
            </button>
          </>
        ) : (
          <Link to="/cloud/models/new" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> {t("cloud.newCloudModel")}</Link>
        ))} />
      <AdvisorBanner pages={[tab === "providers" ? "providers" : "cloud-models"]} />
      <HelpLinks>
        {tab === "providers" ? (
          <>
            <HelpLink page="cloud-providers" anchor="add-a-provider">{t("cloud.help.add")}</HelpLink>
            <HelpLink page="cloud-providers" anchor="when-a-request-spills">{t("cloud.help.spill")}</HelpLink>
            <HelpLink page="cloud-providers" anchor="what-each-providers-endpoint-cannot-do">{t("cloud.help.cannot")}</HelpLink>
            <HelpLink page="cloud-providers" anchor="cost-and-prices">{t("cloud.help.cost")}</HelpLink>
          </>
        ) : (
          <>
            <HelpLink page="cloud-providers" anchor="serve-a-cloud-model-under-a-name-of-its-own">{t("cloud.help.ownName")}</HelpLink>
            <HelpLink page="cloud-providers" anchor="let-a-deployment-spill-to-a-cloud-model">{t("cloud.help.fallback")}</HelpLink>
            <HelpLink page="cloud-providers" anchor="who-may-use-the-cloud">{t("cloud.help.keys")}</HelpLink>
          </>
        )}
      </HelpLinks>
      <div className="tabs" role="tablist" aria-label={t("cloud.tabs")}>
        <button type="button" role="tab" aria-selected={tab === "providers"} className={tab === "providers" ? "on" : ""}
          onClick={() => setSearch({})}>{t("cloud.tab.providers")}</button>
        <button type="button" role="tab" aria-selected={tab === "models"} className={tab === "models" ? "on" : ""}
          onClick={() => setSearch({ tab: "models" })}>{t("cloud.tab.models")}</button>
      </div>
      {tab === "models" ? <CloudModels /> : (
      <>
      {error && <Notice tone="error">{error}</Notice>}
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {refreshing && <PriceRefresh onClose={() => setRefreshing(false)}
        onApplied={(text) => { setRefreshing(false); setMsg({ tone: "ok", text }); reload(); }} />}
      {adding && data && <AddProvider kinds={data.kinds} onCancel={() => setAdding(false)}
        onAdded={(p) => { setAdding(false); setMsg({ tone: "ok", text: t("cloud.added", { name: p.name }) }); reload(); }} />}
      {data && data.providers.length === 0 && !adding && (
        <section className="panel empty-keys">
          <IconCloud size={22} aria-hidden="true" />
          <p className="muted">{admin ? t("cloud.empty.admin") : t("cloud.empty.user")}</p>
        </section>
      )}
      {data && data.providers.length > 0 && (
        <div className="table-wrap">
          <table className="table providers-table">
            <thead><tr><th>{t("cloud.col.provider")}</th><th>{t("cloud.col.baseUrl")}</th><th>{t("cloud.col.key")}</th><th>{t("cloud.col.where")}</th><th>{t("cloud.col.models")}</th>{admin && <th><span className="sr-only">{t("cloud.col.actions")}</span></th>}</tr></thead>
            <tbody>
              {data.providers.map((p) => (
                <tr key={p.id} className={p.enabled ? "" : "row-off"}>
                  <td data-label={t("cloud.col.provider")}><div className="strong"><Link to={`/cloud/providers/${p.id}`}>{p.name}</Link></div><div className="muted small">{p.kind_label}</div></td>
                  <td data-label={t("cloud.col.baseUrl")} className="mono small cell-url">{p.base_url}</td>
                  <td data-label={t("cloud.col.key")} className="mono small">{p.key_hint ?? <span className="muted">{t("cloud.none")}</span>}</td>
                  <td data-label={t("cloud.col.where")}><WhereChip p={p} />
                    {p.budget?.level && p.budget.level !== "ok" && <> <span className={`chip chip-tone-${LEVEL_TONE[p.budget.level]}`}>{t("cloud.budgetChip", { level: LEVEL_LABEL[p.budget.level] })}</span></>}</td>
                  <td data-label={t("cloud.col.models")} className="small">
                    <div>
                    {p.models.length ? p.models.map((m) => (
                      <div key={m.id}><span className="mono">{m.upstream_model}</span>
                        {p.external && <span className="muted"> · {m.price_input === null && m.price_output === null ? t("cloud.noPrice") : `${perMillion(m.price_input)} / ${perMillion(m.price_output)}`}</span>}
                        {m.cooldown_s > 0 && <> <span className="chip chip-tone-bad" title={t("cloud.coolingTitle")}>{t("cloud.cooling", { s: String(Math.ceil(m.cooldown_s)) })}</span></>}</div>
                    )) : <span className="muted">{t("cloud.noneYet")}</span>}
                    </div>
                  </td>
                  {admin && (
                    <td className="cell-actions">
                      <button type="button" className="btn btn-small" disabled={testing === p.id} onClick={() => void test(p)}>
                        <IconPlugConnected size={14} aria-hidden="true" /> {testing === p.id ? t("cloud.testing") : t("cloud.test")}
                      </button>
                      <Link className="btn btn-quiet btn-small" to={`/cloud/providers/${p.id}`}>{t("cloud.open")}</Link>
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
      )}
    </>
  );
}

export const WHERE_DELAY_MS = 400;

function AddProvider({ kinds, onCancel, onAdded }: { kinds: ProviderKind[]; onCancel: () => void; onAdded: (p: Provider) => void }) {
  const t = useT();
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
  // 1.3.1 (D4): what the kind and the base URL's address suggest, asked of the server (it resolves
  // names from where requests are sent). Untouched, the box follows it; what is shown is what is saved.
  const [where, setWhere] = useState<ProviderWhere | null>(null);
  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      api<ProviderWhere>("/providers/where", { method: "POST", body: { kind, base_url: baseUrl.trim() || null } }).then(
        (r) => { if (alive) setWhere(r); }, () => { if (alive) setWhere(null); });
    }, WHERE_DELAY_MS);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [kind, baseUrl]);
  const suggested = where?.external ?? k?.default_external ?? true;
  const ext = external ?? suggested;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ provider: Provider }>("/providers", {
        method: "POST",
        body: { name: name.trim() || k.label, kind, base_url: baseUrl.trim() || null, api_key: key.trim() || null,
          external: ext, allow_http: !ext && allowHttp, headers: parseHeaders(headers) },
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
      <h2 id="add-provider-title">{t("cloud.add.title")}</h2>
      <form onSubmit={submit}>
        <fieldset className="allowlist">
          <legend>{t("cloud.add.kind")}</legend>
          <div className="kind-picks" role="radiogroup" aria-label={t("cloud.add.kindAria")}>
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
          <Field label={t("cloud.add.name")} name="p-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder={k?.label}
            hint={t("cloud.add.nameHint")} />
          <Field label={t("cloud.add.baseUrl")} name="p-base" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={k?.base_url || k?.base_url_hint}
            hint={k?.base_url ? t("cloud.add.baseEmpty") : t("cloud.add.baseRequired")} required={!k?.base_url} />
          <Field label={t("cloud.add.key")} name="p-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
            hint={t("cloud.add.keyHint")} required={k?.key_required} />
          <div className="field">
            <label htmlFor="p-headers">{t("cloud.add.headers")}</label>
            <textarea id="p-headers" className="mono" rows={2} value={headers} onChange={(e) => setHeaders(e.target.value)}
              placeholder={kind === "anthropic" ? "anthropic-workspace-id: wrkspc_…" : kind === "openrouter" ? "X-Title: Forgeyard" : t("cloud.add.headersPlaceholder")} />
            <p className="field-hint">{t("cloud.add.headersHint")}</p>
          </div>
        </div>
        <label className="check provider-check">
          <input type="checkbox" checked={ext} onChange={(e) => setExternal(e.target.checked)} />
          <span>{t.rich("cloud.add.outside", { strong: (s) => <strong>{s}</strong> })}</span>
        </label>
        {where && (
          <p className="where-hint small muted" role="status" aria-label={t("cloud.add.whyAria")}>
            <span className={`chip ${where.external ? "chip-tone-wait" : where.basis === "unknown" ? "" : "chip-tone-ok"}`}>
              {where.basis === "unknown" ? t("cloud.add.cannotTell") : where.external ? t("cloud.add.outsideChip") : t("cloud.add.insideChip")}
            </span>
            {where.reason}
            {external !== null && external !== where.external && (external ? t("cloud.add.changedTicked") : t("cloud.add.changedUnticked"))}
          </p>
        )}
        {!ext && (
          <label className="check provider-check">
            <input type="checkbox" checked={allowHttp} onChange={(e) => setAllowHttp(e.target.checked)} />
            <span>{t("cloud.add.allowHttp")}</span>
          </label>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 16 }}>
          <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? t("cloud.add.adding") : t("cloud.add.submit")}</button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
        </div>
      </form>
    </section>
  );
}

export function ProviderDetailPage() {
  const t = useT();
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
    setMsg({ tone: "ok", text: p?.enabled ? t("cloud.detail.disabled") : t("cloud.detail.enabled") });
    reload();
  });
  const clone = () => run(async () => {
    const name = window.prompt(t("cloud.detail.clonePrompt"), t("cloud.detail.cloneName", { name: String(p?.name) }));
    if (!name) return;
    const r = await api<{ provider: Provider }>(`/providers/${id}/clone`, { method: "POST", body: { name } });
    navigate(`/cloud/providers/${r.provider.id}`);
  });
  const remove = () => run(async () => {
    if (!window.confirm(t("cloud.detail.deleteConfirm", { name: String(p?.name) }))) return;
    await api(`/providers/${id}`, { method: "DELETE" });
    navigate("/cloud", { replace: true });
  });

  return (
    <>
      <p className="crumb"><Link to="/cloud"><IconArrowLeft size={14} aria-hidden="true" /> {t("cloud.detail.crumb")}</Link></p>
      <PageHeader index={`04 · ${p?.kind_label ?? t("cloud.detail.provider")}`} title={p?.name ?? t("cloud.detail.provider")}
        lead={p ? t("cloud.detail.lead", { url: p.base_url, who: p.created_by ?? "?", when: when(p.created_at) }) : undefined}
        actions={p && admin && (
          <>
            <button type="button" className="btn" disabled={busy} onClick={() => void test()}><IconPlugConnected size={16} aria-hidden="true" /> {t("cloud.detail.test")}</button>
            <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void clone()}><IconCopy size={16} aria-hidden="true" /> {t("cloud.detail.clone")}</button>
            <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void toggle()}>{p.enabled ? t("cloud.detail.disable") : t("cloud.detail.enable")}</button>
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void remove()}><IconTrash size={16} aria-hidden="true" /> {t("cloud.detail.delete")}</button>
          </>
        )} />
      <AdvisorBanner pages={[`provider:${id}`]} />
      {error && <Notice tone="error">{error}</Notice>}
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {p && (
        <>
          <div className="wl-status"><WhereChip p={p} /><span className="mono small">{p.key_hint ? t("cloud.detail.keyHint", { hint: p.key_hint }) : t("cloud.detail.noKey")}</span></div>
          {p.budget && <ProviderBudget p={p} budget={p.budget} admin={admin} onSaved={(text) => { setMsg({ tone: "ok", text }); reload(); }} />}
          <Models p={p} admin={admin} kinds={data.kinds} onChanged={reload} />
          {admin && <Settings p={p} defaultTimeout={data?.egress?.read_timeout_s} onSaved={(text) => { setMsg({ tone: "ok", text }); reload(); }} />}
        </>
      )}
    </>
  );
}

function ProviderBudget({ p, budget, admin, onSaved }: { p: Provider; budget: Budget; admin: boolean; onSaved: (text: string) => void }) {
  const t = useT();
  const [value, setValue] = useState(budgetInput(budget.budget_micros));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const micros = parseBudget(value);
    if (micros === "bad") {
      setError(t("cloud.budget.bad"));
      return;
    }
    setError(null);
    try {
      await api(`/budgets/providers/${p.id}`, { method: "PUT", body: { budget_micros: micros } });
      onSaved(micros === null ? t("cloud.budget.removed") : t("cloud.budget.saved"));
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <section className="panel budget-panel" aria-labelledby="budget-title">
      <h2 id="budget-title">{t("cloud.budget.title")}</h2>
      <p className="muted small">{t("cloud.budget.lead", { name: p.name })}</p>
      {budget.level === "exhausted" && <Notice tone="error">{t("cloud.budget.exhausted", { name: p.name })}</Notice>}
      {budget.level === "warning" && <Notice tone="info">{t("cloud.budget.warning")}</Notice>}
      <BudgetMeter b={budget} label={t("cloud.budget.of", { name: p.name })} />
      {admin && (
        <form onSubmit={(e) => void save(e)} className="row-gap" style={{ marginTop: 12, alignItems: "flex-end" }}>
          <Field label={t("cloud.budget.field")} name="pb-budget" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)}
            placeholder={t("cloud.budget.noBudget")} error={error} hint={t("cloud.budget.hint")} />
          <button type="submit" className="btn">{t("cloud.budget.save")}</button>
        </form>
      )}
    </section>
  );
}

function Settings({ p, defaultTimeout, onSaved }: { p: Provider; defaultTimeout?: number; onSaved: (text: string) => void }) {
  const t = useT();
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
      await api(`/cloud/providers/${p.id}`, { method: "PATCH", body });
      setKey("");
      onSaved(clearKey ? t("cloud.settings.keyRemoved") : key.trim() ? t("cloud.settings.savedKey") : t("cloud.settings.saved"));
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <section className="panel" aria-labelledby="settings-title">
      <h2 id="settings-title">{t("cloud.settings.title")}</h2>
      <form onSubmit={(e) => void save(e)}>
        <div className="grid-2">
          <Field label={t("cloud.add.baseUrl")} name="ps-base" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} required />
          <Field label={p.has_key ? t("cloud.settings.replaceKey", { hint: String(p.key_hint) }) : t("cloud.add.key")} name="ps-key" type="password" autoComplete="off" value={key}
            onChange={(e) => setKey(e.target.value)} placeholder={t("cloud.settings.keepKey")} hint={t("cloud.settings.writeOnly")} />
          <div className="field">
            <label htmlFor="ps-headers">{t("cloud.settings.headers")}</label>
            <textarea id="ps-headers" className="mono" rows={2} value={headers} onChange={(e) => setHeaders(e.target.value)} />
          </div>
          <Field label={t("cloud.settings.timeout")} name="ps-timeout" type="number" min={1} max={3600} value={timeout}
            onChange={(e) => setTimeoutS(e.target.value)} placeholder={defaultTimeout ? t("cloud.settings.timeoutDefault", { s: String(defaultTimeout) }) : t("cloud.settings.serverDefault")}
            hint={t("cloud.settings.timeoutHint")} />
        </div>
        <label className="check provider-check">
          <input type="checkbox" checked={external} onChange={(e) => setExternal(e.target.checked)} />
          <span>{t.rich("cloud.settings.outside", { strong: (s) => <strong>{s}</strong> })}</span>
        </label>
        {!external && (
          <label className="check provider-check">
            <input type="checkbox" checked={allowHttp} onChange={(e) => setAllowHttp(e.target.checked)} />
            <span>{t("cloud.add.allowHttp")}</span>
          </label>
        )}
        {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
        <div className="row-gap" style={{ marginTop: 14 }}>
          <button type="submit" className="btn">{t("cloud.settings.save")}</button>
          {p.has_key && <button type="button" className="btn btn-quiet" onClick={(e) => void save(e as unknown as FormEvent, true)}>{t("cloud.settings.removeKey")}</button>}
        </div>
      </form>
    </section>
  );
}

function Models({ p, admin, kinds, onChanged }: { p: Provider; admin: boolean; kinds: ProviderKind[]; onChanged: () => void }) {
  const t = useT();
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
    if (!window.confirm(t("cloud.models.removeConfirm", { name: m.upstream_model }))) return;
    try { await api(`/provider-models/${m.id}`, { method: "DELETE" }); onChanged(); } catch (err) { setError(errorText(err)); }
  };
  const shown = (listing ?? []).filter((m) => !filter || m.id.toLowerCase().includes(filter.toLowerCase())).slice(0, 200);

  return (
    <section className="panel" aria-labelledby="models-title">
      <div className="panel-head">
        <h2 id="models-title">{t("cloud.models.title")}</h2>
        {admin && p.external && p.models.length > 0 ? (
          <button type="button" className="btn btn-small" aria-expanded={refreshing} onClick={() => setRefreshing((v) => !v)}>
            <IconCurrencyDollar size={14} aria-hidden="true" /> {t("cloud.refreshPrices")}
          </button>
        ) : <span className="muted small">{t("cloud.models.what", { name: p.name })}</span>}
      </div>
      {!p.external && p.models.length > 0 && <p className="muted small">{t("cloud.models.free")}</p>}
      {note && <Notice tone="ok">{note}</Notice>}
      {refreshing && <PriceRefresh providerId={p.id} onClose={() => setRefreshing(false)}
        onApplied={(text) => { setRefreshing(false); setNote(text); onChanged(); }} />}
      {p.models.length === 0 ? <p className="muted small">{admin ? t("cloud.models.noneAdmin") : t("cloud.models.none")}</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>{t("cloud.models.col.model")}</th><th>{t("cloud.models.col.serves")}</th>{p.external && <th>{t("cloud.models.col.price")}</th>}
                <th>{t("cloud.models.col.context")}</th><th>{t("cloud.models.col.usedBy")}</th><th>{t("cloud.models.col.since")}</th>
                {admin && <th><span className="sr-only">{t("cloud.col.actions")}</span></th>}
              </tr>
            </thead>
            <tbody>
              {p.models.map((m) => (
                <Fragment key={m.id}>
                <tr>
                  <td data-label={t("cloud.models.col.model")}><div><span className="mono">{m.upstream_model}</span>{m.display_name && <div className="muted small">{m.display_name}</div>}</div></td>
                  <td data-label={t("cloud.models.col.serves")} className="small">{API_NAME[m.api] ?? m.api}</td>
                  {p.external && <td data-label={t("cloud.models.col.priceShort")} className="small"><Prices m={m} /></td>}
                  <td data-label={t("cloud.models.col.context")} className="small">{m.context_length ? num(m.context_length) : "—"}</td>
                  <td data-label={t("cloud.models.col.usedBy")} className="small">{m.routes.length ? m.routes.map((r) => <code key={r} className="egress-host">{r}</code>) : <span className="muted">{t("cloud.models.noRoute")}</span>}</td>
                  <td data-label={t("cloud.models.col.sinceShort")} className="small">
                    {t("cloud.models.sent", { n: num(m.sent) })}{m.in_flight ? t("cloud.models.inFlight", { n: String(m.in_flight) }) : ""}
                    {m.cooldown_s > 0 ? t("cloud.models.coolingDown", { s: String(Math.ceil(m.cooldown_s)) }) : ""}
                  </td>
                  {admin && <td className="cell-actions">
                    {p.external && <button type="button" className="btn btn-quiet btn-small" aria-expanded={editing === m.id}
                      onClick={() => setEditing(editing === m.id ? null : m.id)}>{t("cloud.models.prices")}</button>}
                    <button type="button" className="btn btn-quiet btn-small" disabled={m.routes.length > 0}
                    title={m.routes.length ? t("cloud.models.removeFirst") : undefined} onClick={() => void remove(m)}>{t("cloud.models.remove")}</button></td>}
                </tr>
                {editing === m.id && (
                  <tr className="price-edit-row"><td colSpan={7}>
                    <PriceEditor m={m} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); setNote(t("cloud.models.pricesSaved", { name: m.upstream_model })); onChanged(); }} />
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
            <Field label={p.kind === "azure" ? t("cloud.models.deploymentName") : t("cloud.models.modelName")} name="pm-name" value={model} onChange={(e) => setModel(e.target.value)}
              placeholder={p.kind === "anthropic" ? "claude-sonnet-4-5" : p.kind === "gemini" ? "gemini-2.5-flash" : "gpt-4o-mini"} required />
            <div className="field">
              <label htmlFor="pm-api">{t("cloud.models.serves")}</label>
              <select id="pm-api" value={apiName} onChange={(e) => setApiName(e.target.value)}>
                <option value="openai-chat">{t("cloud.models.servesChat")}</option>
                {k?.embeddings !== false && <option value="openai-embeddings">{t("cloud.models.servesEmbeddings")}</option>}
              </select>
            </div>
            <Field label={t("cloud.models.context")} name="pm-ctx" type="number" min={1} value={context} onChange={(e) => setContext(e.target.value)} />
            <div className="model-add-actions">
              <button type="submit" className="btn" disabled={!model.trim()}><IconPlus size={15} aria-hidden="true" /> {t("cloud.models.add")}</button>
              {k?.models_endpoint && <button type="button" className="btn btn-quiet" disabled={loading} onClick={() => void load()}>
                <IconDownload size={15} aria-hidden="true" /> {loading ? t("cloud.models.asking") : t("cloud.models.import")}</button>}
            </div>
          </form>
          {p.kind === "azure" && <p className="muted small">{t("cloud.models.azure")}</p>}
          {listing && (
            <div className="import-list">
              <div className="row-gap">
                <input aria-label={t("cloud.models.filterAria")} placeholder={t("cloud.models.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} />
                <span className="muted small">{t("cloud.models.listed", { n: String(listing.length) })}</span>
              </div>
              <ul>
                {shown.map((m) => (
                  <li key={`${m.id}-${m.api}`}>
                    <label className="card-pick">
                      <input type="checkbox" disabled={m.added} checked={m.added || m.id in picked}
                        onChange={() => setPicked((x) => { const y = { ...x }; if (m.id in y) delete y[m.id]; else y[m.id] = m.api; return y; })} />
                      <span className="mono">{m.id}</span>
                      <span className="muted small">{API_NAME[m.api]}{m.context_length ? t("cloud.models.ctx", { n: num(m.context_length) }) : ""}{m.added ? t("cloud.models.added") : ""}</span>
                    </label>
                  </li>
                ))}
              </ul>
              <div className="row-gap">
                <button type="button" className="btn btn-primary" disabled={!Object.keys(picked).length}
                  onClick={() => void add(Object.entries(picked).map(([id, a]) => ({ upstream_model: id, api: a,
                    context_length: listing.find((m) => m.id === id)?.context_length ?? null })))}>
                  {Object.keys(picked).length ? t("cloud.models.addSelected", { n: String(Object.keys(picked).length) }) : t("cloud.models.addSelectedNone")}
                </button>
                <button type="button" className="btn btn-quiet" onClick={() => { setListing(null); setPicked({}); }}>{t("common.close")}</button>
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
  const t = useT();
  if (PRICE_FIELDS.every((f) => m[f] === null)) return <span className="chip chip-tone-wait" title={t("cloud.price.noPriceTitle")}>{t("cloud.noPrice")}</span>;
  return (
    <div className="price-cell">
      <div><span className="muted">{t("cloud.price.in")}</span> <span className="mono">{perMillion(m.price_input)}</span> · <span className="muted">{t("cloud.price.out")}</span> <span className="mono">{perMillion(m.price_output)}</span></div>
      {(m.price_cache_read !== null || m.price_cache_write !== null) && (
        <div><span className="muted">{t("cloud.price.cached")}</span> <span className="mono">{perMillion(m.price_cache_read)}</span>{m.price_cache_write !== null && <> · <span className="muted">{t("cloud.price.write")}</span> <span className="mono">{perMillion(m.price_cache_write)}</span></>}</div>
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
  const t = useT();
  const [text, setText] = useState<Record<string, string>>(() => Object.fromEntries(PRICE_FIELDS.map((f) => [f, priceText(m[f])])));
  const [error, setError] = useState<string | null>(null);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const body: Record<string, number | null> = {};
    for (const f of PRICE_FIELDS) {
      const v = parsePrice(text[f]);
      if (v === "bad") { setError(t("cloud.price.bad", { label: PRICE_LABEL[f] })); return; }
      body[f] = v;
    }
    try {
      await api(`/provider-models/${m.id}`, { method: "PATCH", body });
      onSaved();
    } catch (err) { setError(errorText(err)); }
  };
  return (
    <form className="price-edit" onSubmit={(e) => void save(e)} aria-label={t("cloud.price.editAria", { name: m.upstream_model })}>
      <p className="muted small">{t("cloud.price.editLead")}</p>
      <div className="grid-4">
        {PRICE_FIELDS.map((f) => (
          <Field key={f} label={t("cloud.price.field", { label: PRICE_LABEL[f] })} name={`${f}-${m.id}`} inputMode="decimal" value={text[f]}
            onChange={(e) => setText((x) => ({ ...x, [f]: e.target.value }))} placeholder={t("cloud.price.unknown")} />
        ))}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="row-gap"><button type="submit" className="btn">{t("cloud.price.save")}</button><button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button></div>
    </form>
  );
}
