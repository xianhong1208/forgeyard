import { IconArrowLeft, IconArrowDown, IconPlus, IconTrash, IconX } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Field, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { API_KIND } from "../lib/gateway";
import { ROUTE_KIND_LABEL, ROUTE_KIND_TONE, spillRules, targetName, targetWhere, tiers } from "../lib/routing";
import type { RouteInfo, RouteOptions, RouteTarget } from "../lib/types";
import { useApi } from "../lib/useApi";

// One target as the editor holds it: what it points at, and how it is shown.
type Draft = { key: string; kind: "local" | "provider"; ref: number; name: string; where: string; cloud: boolean; weight: number; enabled: boolean };

function fromTarget(t: RouteTarget): Draft {
  return {
    key: `${t.kind}:${t.deployment?.id ?? t.provider_model?.id}`, kind: t.kind, ref: (t.deployment?.id ?? t.provider_model?.id) as number,
    // The chip already says local, cloud or own server; the line adds what is not obvious.
    name: targetName(t), where: t.provider_model ? (t.provider_model.provider.enabled ? "" : "provider disabled") : targetWhere(t),
    cloud: t.cloud, weight: t.weight, enabled: t.enabled,
  };
}

const APIS = ["openai-chat", "openai-embeddings", "rerank", "openai-audio"];

export function RouteDetailPage() {
  const { id } = useParams();
  const isNew = id === "new";
  const route = useApi<{ route: RouteInfo; is_admin: boolean }>(isNew ? null : `/routes/${id}`);
  const admin = isNew || (route.data?.is_admin ?? false);
  const options = useApi<RouteOptions>(admin ? "/route-options" : null);
  const r = isNew ? null : route.data?.route ?? null;
  if (!isNew && !r) {
    return (
      <>
        <p className="crumb"><Link to="/models?tab=routes"><IconArrowLeft size={14} aria-hidden="true" /> Routes</Link></p>
        {route.error ? <Notice tone="error">{route.error}</Notice> : <p className="muted">Loading…</p>}
      </>
    );
  }
  return <RouteEditor key={r?.id ?? "new"} r={r} admin={admin} opts={options.data} reload={route.reload} />;
}

function RouteEditor({ r, admin, opts, reload }: { r: RouteInfo | null; admin: boolean; opts: RouteOptions | null; reload: () => void }) {
  const isNew = r === null;
  const navigate = useNavigate();
  const [served, setServed] = useState("");
  const [apiName, setApiName] = useState(r?.api ?? "openai-chat");
  const [localOnly, setLocalOnly] = useState(r?.local_only ?? false);
  const [longPrompt, setLongPrompt] = useState(r?.spill_on_long_prompt ?? false);
  const [waitMs, setWaitMs] = useState(String(r?.wait_ms ?? 0));
  const [draft, setDraft] = useState<Draft[][]>(r ? tiers(r).map((tier) => tier.map(fromTarget)) : [[]]);
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const used = new Set(draft.flat().map((d) => d.key));
  const choices = [
    ...(opts?.deployments ?? []).filter((d) => d.api === apiName).map((d) => ({ key: `local:${d.id}`, kind: "local" as const, ref: d.id, name: d.name,
      where: `local deployment (${d.served_name})`, cloud: false })),
    ...(["openai-chat", "openai-embeddings"].includes(apiName) ? opts?.provider_models ?? [] : []).filter((m) => m.api === apiName)
      .map((m) => ({ key: `provider:${m.id}`, kind: "provider" as const, ref: m.id, name: `${m.provider.name}/${m.upstream_model}`,
        where: m.provider.external ? "cloud" : "inside your network", cloud: m.provider.external })),
  ].filter((c) => !used.has(c.key));

  const addTo = (tier: number, key: string) => {
    const c = choices.find((x) => x.key === key);
    if (!c) return;
    setDraft((d) => d.map((t, i) => (i === tier ? [...t, { ...c, weight: 1, enabled: true }] : t)));
  };
  const change = (tier: number, key: string, patch: Partial<Draft>) =>
    setDraft((d) => d.map((t, i) => (i === tier ? t.map((x) => (x.key === key ? { ...x, ...patch } : x)) : t)));
  const drop = (tier: number, key: string) => setDraft((d) => d.map((t, i) => (i === tier ? t.filter((x) => x.key !== key) : t)));
  const removeTier = (tier: number) => setDraft((d) => (d.length > 1 ? d.filter((_, i) => i !== tier) : d));

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    const targets = draft.filter((t) => t.length).flatMap((t, tier) => t.map((x) => ({
      tier, kind: x.kind, weight: x.weight, enabled: x.enabled, [x.kind === "local" ? "deployment_id" : "provider_model_id"]: x.ref })));
    const body = { local_only: localOnly, spill_on_long_prompt: longPrompt, wait_ms: Number(waitMs) || 0, targets };
    try {
      if (isNew) {
        const out = await api<{ route: RouteInfo }>("/routes", { method: "POST", body: { ...body, served_name: served.trim(), api: apiName } });
        navigate(`/models/routes/${out.route.id}`, { replace: true });
      } else {
        await api(`/routes/${r.id}`, { method: "PUT", body });
        setMsg({ tone: "ok", text: "Routing saved. It applies to the next request." });
        reload();
      }
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!r || !window.confirm(`Delete the route ${r.served_name}? Requests for it get 404 from then on.`)) return;
    try {
      await api(`/routes/${r.id}`, { method: "DELETE" });
      navigate("/models?tab=routes", { replace: true });
    } catch (err) { setMsg({ tone: "error", text: errorText(err) }); }
  };

  const title = isNew ? "New route" : r?.served_name ?? "Route";
  return (
    <>
      <p className="crumb"><Link to="/models?tab=routes"><IconArrowLeft size={14} aria-hidden="true" /> Routes</Link></p>
      <PageHeader index="03 · Models · Route" title={title}
        lead={isNew ? "A model name of its own: a cloud model behind Forgeyard's keys and limits, or several deployments and providers in tiers."
          : r ? `${API_KIND[r.api] ?? r.api}${r.deployment_id ? " · the route of a deployment" : " · made by an admin"}` : undefined}
        actions={r && admin && r.deployment_id === null && (
          <button type="button" className="btn btn-danger" onClick={() => void remove()}><IconTrash size={16} aria-hidden="true" /> Delete route</button>
        )} />
      {r && <AdvisorBanner pages={[`route:${r.id}`]} />}
      <HelpLinks>
        <HelpLink page="cloud-providers" anchor="route-a-model-to-the-cloud">How do I let a model spill to the cloud?</HelpLink>
        <HelpLink page="cloud-providers" anchor="when-a-request-spills">When does a request move to the next tier?</HelpLink>
        <HelpLink page="cloud-providers" anchor="who-may-use-the-cloud">Which keys may use the cloud?</HelpLink>
      </HelpLinks>
      {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
      {r && (
        <div className="wl-status">
          <span className={`chip chip-big chip-tone-${ROUTE_KIND_TONE[r.kind]}`}>{ROUTE_KIND_LABEL[r.kind]}</span>
          {r.local_only && <span className="chip chip-tone-ok">local only</span>}
          <span className="muted small">{spillRules(r)}</span>
        </div>
      )}
      {(isNew || r) && (
        <form onSubmit={save}>
          {isNew && (
            <section className="panel">
              <div className="grid-2">
                <Field label="Model name" name="rt-name" value={served} onChange={(e) => setServed(e.target.value)} required maxLength={128}
                  placeholder="gpt-4o-mini" hint="What clients send as model. Letters, digits and . _ : / -" />
                <div className="field">
                  <label htmlFor="rt-api">Serves</label>
                  <select id="rt-api" value={apiName} onChange={(e) => { setApiName(e.target.value); setDraft([[]]); }}>
                    {APIS.map((a) => <option key={a} value={a}>{API_KIND[a] ?? a}</option>)}
                  </select>
                  <p className="field-hint">Cloud providers serve chat and embeddings.</p>
                </div>
              </div>
            </section>
          )}
          <section className="panel" aria-labelledby="tiers-title">
            <div className="panel-head"><h2 id="tiers-title">Tiers</h2><span className="muted small">tried in order; a request moves on only before its answer starts</span></div>
            <ol className="tiers">
              {draft.map((tier, i) => (
                <li key={i} className="tier">
                  <div className="tier-head">
                    <span className="tier-n">{i === 0 ? "First" : `Then, tier ${i + 1}`}</span>
                    {admin && draft.length > 1 && (
                      <button type="button" className="btn btn-quiet btn-small" onClick={() => removeTier(i)} aria-label={`Remove tier ${i + 1}`}>
                        <IconX size={14} aria-hidden="true" /> Remove tier
                      </button>
                    )}
                  </div>
                  {tier.length === 0 && <p className="muted small">Nothing in this tier yet.</p>}
                  <ul className="tier-targets">
                    {tier.map((t) => (
                      <li key={t.key} className={t.enabled ? "" : "row-off"}>
                        <span className={`chip ${t.kind === "local" ? "chip-tone-ok" : t.cloud ? "chip-tone-wait" : "chip-tone-ok"}`}>{t.kind === "local" ? "local" : t.cloud ? "cloud" : "own server"}</span>
                        <span className="mono tier-name">{t.name}</span>
                        <span className="muted small">{t.where}</span>
                        {admin && (
                          <span className="tier-edit">
                            {t.kind === "provider" && (
                              <label className="small">Weight <input type="number" min={1} max={100} value={t.weight} aria-label={`Weight of ${t.name}`}
                                onChange={(e) => change(i, t.key, { weight: Math.max(1, Number(e.target.value) || 1) })} /></label>
                            )}
                            <label className="check small"><input type="checkbox" checked={t.enabled} onChange={(e) => change(i, t.key, { enabled: e.target.checked })} /> on</label>
                            <button type="button" className="btn btn-quiet btn-small" aria-label={`Remove ${t.name}`} onClick={() => drop(i, t.key)}><IconX size={14} aria-hidden="true" /></button>
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                  {admin && choices.length > 0 && (
                    <div className="tier-add">
                      <label htmlFor={`add-${i}`} className="sr-only">Add to tier {i + 1}</label>
                      <select id={`add-${i}`} value="" onChange={(e) => addTo(i, e.target.value)}>
                        <option value="">Add to this tier…</option>
                        {choices.map((c) => <option key={c.key} value={c.key}>{c.name} ({c.where})</option>)}
                      </select>
                    </div>
                  )}
                  {i < draft.length - 1 && <IconArrowDown className="tier-arrow" size={16} aria-hidden="true" />}
                </li>
              ))}
            </ol>
            {admin && (
              <button type="button" className="btn btn-quiet btn-small" onClick={() => setDraft((d) => [...d, []])}>
                <IconPlus size={14} aria-hidden="true" /> Add a tier
              </button>
            )}
            {admin && opts && opts.provider_models.length === 0 && ["openai-chat", "openai-embeddings"].includes(apiName) && (
              <p className="muted small">No provider models yet: <Link to="/providers">add a provider</Link> and its models first.</p>
            )}
          </section>
          <section className="panel" aria-labelledby="rules-title">
            <h2 id="rules-title">When to move on</h2>
            <label className="check provider-check">
              <input type="checkbox" checked={localOnly} disabled={!admin} onChange={(e) => setLocalOnly(e.target.checked)} />
              <span><strong>Local only.</strong> Never leaves your network, whatever a key allows: cloud targets are skipped.</span>
            </label>
            <label className="check provider-check">
              <input type="checkbox" checked={longPrompt} disabled={!admin} onChange={(e) => setLongPrompt(e.target.checked)} />
              <span><strong>Spill a prompt that is too long</strong> for the local deployment's context (estimated from its size, plus max_tokens).</span>
            </label>
            <div className="grid-2">
              <Field label="Wait for a free local slot (ms)" name="rt-wait" type="number" min={0} max={10000} value={waitMs} disabled={!admin}
                onChange={(e) => setWaitMs(e.target.value)} hint="In milliseconds. 0: spill at once when every local replica is at its concurrency. At most 10,000 (10 s)." />
            </div>
            <p className="muted small">
              A request always moves on when the tier has no ready replica, cannot be connected to, or answers 429 or 503. It never moves on for
              another error, a timeout once the request was sent, or once the answer has started. Keys decide whether cloud tiers are used at all.
            </p>
          </section>
          {admin && (
            <div className="row-gap">
              <button type="submit" className="btn btn-primary" disabled={busy || (isNew && !served.trim()) || draft.flat().length === 0}>
                {busy ? "Saving…" : isNew ? "Create route" : "Save routing"}
              </button>
              <Link className="btn btn-quiet" to="/models?tab=routes">Cancel</Link>
            </div>
          )}
        </form>
      )}
    </>
  );
}
