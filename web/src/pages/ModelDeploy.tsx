import { IconAlertTriangle, IconCheck } from "@tabler/icons-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";

import { Link, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { Notice, PageHeader } from "../components/ui";
import { ApiError, api, errorText } from "../lib/api";
import { bytes } from "../lib/machines";
import { TASK_LABEL, compatibleRecipes, deployBody, emptyDeploy, gib, recipeDefaults, slug, type DeployForm } from "../lib/models";
import type { CatalogModel, CatalogResponse, EstimateResponse, MachineVerdict, MachinesResponse, Placement, RecipeParam } from "../lib/types";
import { useApi } from "../lib/useApi";

export const ESTIMATE_DELAY_MS = 400;

export function ModelDeployPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const { data: cat } = useApi<CatalogResponse>("/models");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  // ?model=…&recipe=…&device=… from "Deploy it" after adding a model (its suggested recipe and device).
  const [f, setF] = useState<DeployForm>(() => ({ ...emptyDeploy(), model: search.get("model") ?? "", recipe: search.get("recipe") ?? "",
    device: search.get("device") === "cpu" ? "cpu" : "gpu" }));
  const [est, setEst] = useState<EstimateResponse | null>(null);
  const [estError, setEstError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; machines?: MachineVerdict[] } | null>(null);

  const model = cat?.models.find((m) => m.name === f.model);
  const recipes = useMemo(() => compatibleRecipes(model, cat?.recipes ?? []), [model, cat]);
  const recipe = recipes.find((r) => r.name === f.recipe) ?? recipes[0];
  // What the form means: the chosen model's first recipe, a name from the model, a device the
  // recipe has. Derived, so the form follows the catalog without copying it into state.
  const view: DeployForm = {
    ...f, recipe: recipe?.name ?? "", name: f.name || (model ? slug(model.name) : ""),
    device: recipe && !recipe.devices.includes(f.device) ? recipe.devices[0] : f.device,
    params: { ...recipeDefaults(recipe), ...f.params },
  };

  const body = deployBody(view, recipe);
  const bodyKey = JSON.stringify({ ...body, hf_token: undefined });

  useEffect(() => {
    if (!recipe) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      api<EstimateResponse>("/deployments/estimate", { method: "POST", body: { ...JSON.parse(bodyKey), name: JSON.parse(bodyKey).name || "preview" } }).then(
        (r) => { if (alive) { setEst(r); setEstError(null); } },
        (e) => { if (alive) { setEst(null); setEstError(errorText(e)); } },
      );
    }, ESTIMATE_DELAY_MS);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [bodyKey, recipe]);

  if (user && user.role === "user") {
    return (<><PageHeader index="03 · Models" title="Deploy a model" /><Notice>Only admins and operators deploy models: a deployment runs code on a machine.</Notice></>);
  }

  const set = <K extends keyof DeployForm>(k: K, v: DeployForm[K]) => setF((cur) => ({ ...cur, [k]: v }));
  const setParam = (k: string, v: string) => setF((cur) => ({ ...cur, params: { ...cur.params, [k]: v } }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ deployment: { id: number } }>("/deployments", { method: "POST", body });
      navigate(`/models/deployments/${r.deployment.id}`);
    } catch (err) {
      const machines = err instanceof ApiError ? (err.detail.machines as MachineVerdict[] | undefined) : undefined;
      setError({ text: errorText(err), machines });
    } finally {
      setBusy(false);
    }
  };

  const relevant = recipe ? Object.entries(recipe.params).filter(([k]) => !(view.device === "cpu" && k === "gpu_layers") && !(view.device === "gpu" && k === "threads")
    && paramApplies(k, model)) : [];
  const shown = relevant.filter(([, p]) => !p.advanced);
  const advanced = relevant.filter(([, p]) => p.advanced);
  const usable = (machines?.machines ?? []).filter((m) => m.status !== "revoked");

  return (
    <>
      <p className="crumb"><Link to="/models">← Models</Link></p>
      <PageHeader index="03 · Models" title="Deploy a model"
        lead="Pick a model and a recipe; the estimate and where it would go update as you change the parameters. Nothing is downloaded until it is placed." />
      {cat && cat.models.length === 0 && (
        <Notice>
          The catalog is empty, so there is nothing to deploy yet.{" "}
          {cat.can_manage ? <Link to="/models?tab=catalog">Add a model to the catalog</Link> : "An admin adds models to the catalog."}
        </Notice>
      )}
      <form className="wl-form deploy-layout" onSubmit={submit} aria-label="Deploy a model">
        <div>
          <section className="panel">
            <h2>Model and engine</h2>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="d-model">Model</label>
                <select id="d-model" value={f.model} onChange={(e) => setF({ ...emptyDeploy(), model: e.target.value })} required>
                  <option value="">Choose from the catalog…</option>
                  {(cat?.models ?? []).map((m) => <option key={m.id} value={m.name}>{m.title ?? m.name} · {TASK_LABEL[m.task]} · {m.format}</option>)}
                </select>
                {model && <p className="field-hint mono">{model.source}{model.file ? ` · ${model.file}` : ""} · {bytes(model.size_bytes)}</p>}
              </div>
              <div className="field">
                <label htmlFor="d-recipe">Recipe</label>
                <select id="d-recipe" value={view.recipe} onChange={(e) => setF({ ...f, recipe: e.target.value, params: {} })} disabled={!model}>
                  {recipes.map((r) => <option key={r.name} value={r.name}>{r.title} ({r.engine} {r.version})</option>)}
                </select>
                {model && recipes.length === 0 && <p className="field-error">No recipe serves {model.format} {TASK_LABEL[model.task]} models.</p>}
              </div>
            </div>
            <div className="grid-2">
              <div className="field"><label htmlFor="d-name">Deployment name</label>
                <input id="d-name" className="mono" value={view.name} onChange={(e) => set("name", e.target.value)} required /></div>
              <div className="field"><label htmlFor="d-served">Served model name</label>
                <input id="d-served" className="mono" value={f.servedName} onChange={(e) => set("servedName", e.target.value)} placeholder={view.name || "the deployment name"} />
                <p className="field-hint">What clients put in <code>model</code> when they call the gateway.</p></div>
            </div>
          </section>

          {recipe && (
            <section className="panel">
              <h2>Parameters</h2>
              <div className="grid-3">
                {recipe.devices.length > 1 && (
                  <div className="field"><label htmlFor="d-device">Runs on</label>
                    <select id="d-device" value={view.device} onChange={(e) => set("device", e.target.value as "gpu" | "cpu")}>
                      <option value="gpu">GPU</option><option value="cpu">CPU only</option>
                    </select></div>
                )}
                {view.device === "gpu" && (
                  <>
                    <div className="field"><label htmlFor="d-gpus">GPUs per replica</label>
                      <input id="d-gpus" inputMode="numeric" value={f.gpus} onChange={(e) => set("gpus", e.target.value)} /></div>
                    <div className="field"><label htmlFor="d-share">GPU sharing</label>
                      <select id="d-share" value={f.share || recipe.share} onChange={(e) => set("share", e.target.value as DeployForm["share"])}>
                        <option value="exclusive">Exclusive</option>
                        <option value="vram">Share VRAM</option>
                      </select>
                      <p className="field-hint">Shared cards hold several replicas, each held to its own reservation.</p></div>
                  </>
                )}
                {shown.map(([k, p]) => <ParamField key={k} name={k} p={p} value={view.params[k]} onChange={setParam} />)}
                <div className="field"><label htmlFor="d-replicas">Replicas</label>
                  <input id="d-replicas" inputMode="numeric" value={f.replicas} onChange={(e) => set("replicas", e.target.value)} /></div>
                <div className="field"><label htmlFor="d-machine">Machine</label>
                  <select id="d-machine" value={f.machine} onChange={(e) => set("machine", e.target.value)}>
                    <option value="">Best fit (binpack)</option>
                    {usable.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.status}</option>)}
                  </select></div>
              </div>
              <details className="spec-preview" style={{ marginTop: 14 }}>
                <summary>Advanced: {advanced.length ? "request limits, " : ""}memory override, engine arguments, token</summary>
                <p className="field-hint">Every value here has a default that works; change it only when you need to.</p>
                <div className="grid-3">
                  {advanced.map(([k, p]) => <ParamField key={k} name={k} p={p} value={view.params[k]} onChange={setParam} />)}
                  <div className="field"><label htmlFor="d-vram">Memory per device (override)</label>
                    <input id="d-vram" value={f.vram} onChange={(e) => set("vram", e.target.value)} placeholder="e.g. 20Gi" />
                    <p className="field-hint">Replaces the estimate. Required for local:// models.</p></div>
                  <div className="field"><label htmlFor="d-extra">Extra engine arguments</label>
                    <textarea id="d-extra" className="mono" rows={3} value={f.extraArgs} onChange={(e) => set("extraArgs", e.target.value)} placeholder="one per line" /></div>
                  <div className="field"><label htmlFor="d-token">Hugging Face token</label>
                    <input id="d-token" type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)} />
                    <p className="field-hint">Gated models only. Sent only to the agent that downloads; never shown again.</p></div>
                </div>
              </details>
            </section>
          )}
          {error && (
            <Notice tone="error">
              <strong>{error.machines ? "It does not fit anywhere right now." : error.text}</strong>
              {error.machines && <Verdicts list={error.machines} />}
            </Notice>
          )}
          <div className="form-foot">
            <details className="spec-preview"><summary>Request that will be sent</summary>
              <pre className="mono" aria-label="Deployment JSON">{JSON.stringify({ ...body, hf_token: body.hf_token ? "••••" : undefined }, null, 2)}</pre></details>
            <div className="row-gap">
              <button className="btn btn-primary" disabled={busy || !recipe || est?.placement.fits === false}>{busy ? "Deploying…" : "Deploy"}</button>
              <Link to="/models" className="btn btn-quiet">Cancel</Link>
            </div>
          </div>
        </div>
        <aside className="estimate-side" aria-label="Estimate">
          <EstimatePanel est={est} error={estError} pending={!!recipe && !est && !estError} />
        </aside>
      </form>
    </>
  );
}

// The media parameters (1.3) only mean something for a model that takes media: hidden for one that
// is known to take none of them (a catalog entry from before 1.3 is unknown, so they show).
export function paramApplies(name: string, model: CatalogModel | undefined): boolean {
  const m = model?.modalities;
  if (m === null || m === undefined) return true;
  if (name === "images") return m.includes("image") || m.includes("audio");
  if (name === "image_tokens") return m.includes("image");
  if (name === "videos" || name === "video_fps") return m.includes("video");
  return true;
}

function ParamField({ name, p, value, onChange }: { name: string; p: RecipeParam; value: string | undefined; onChange: (k: string, v: string) => void }) {
  return (
    <div className="field">
      <label htmlFor={`d-p-${name}`}>{p.label ?? name}</label>
      {p.type === "choice" ? (
        <select id={`d-p-${name}`} value={value} onChange={(e) => onChange(name, e.target.value)}>
          {(p.choices ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      ) : (
        <input id={`d-p-${name}`} inputMode="numeric" value={value ?? ""} onChange={(e) => onChange(name, e.target.value)} />
      )}
      {p.help && <p className="field-hint">{p.help}</p>}
    </div>
  );
}

function EstimatePanel({ est, error, pending }: { est: EstimateResponse | null; error: string | null; pending: boolean }) {
  if (error) return <section className="panel"><h2>Estimate</h2><Notice tone="error">{error}</Notice></section>;
  if (!est) return <section className="panel"><h2>Estimate</h2><p className="muted small">{pending ? "Estimating…" : "Choose a model and a recipe."}</p></section>;
  const e = est.estimate;
  const total = e.amount || 1;
  return (
    <section className="panel" aria-labelledby="est-title">
      <h2 id="est-title">Estimate</h2>
      <p className="est-total"><strong>{gib(e.amount)}</strong> <span className="muted">{e.device === "gpu" ? `per GPU${e.gpus > 1 ? ` (${e.gpus} GPUs)` : ""}` : "of RAM"} · {e.method}</span></p>
      <div className="stack-bar" aria-hidden="true">
        {e.parts.map((p, i) => <span key={p.label} className={`seg seg-${i}`} style={{ width: `${(p.bytes / total) * 100}%` }} />)}
      </div>
      <ul className="parts">
        {e.parts.map((p, i) => (
          <li key={p.label}><span className={`swatch seg-${i}`} aria-hidden="true" /><span>{p.label}</span><span className="mono small">{gib(p.bytes)}</span>
            {p.detail && <span className="part-detail muted">{p.detail}</span>}</li>
        ))}
      </ul>
      {e.notes.map((n) => <p key={n} className="small muted">{n}</p>)}
      <PlacementView p={est.placement} />
    </section>
  );
}

function PlacementView({ p }: { p: Placement }) {
  return (
    <div className="placement">
      <h3 className="sub">Where it would go</h3>
      {p.fits ? (
        <ul className="placed">
          {p.placed.map((x) => (
            <li key={x.replica}><IconCheck size={15} aria-hidden="true" className="ok-ic" />
              <span>Replica {x.replica}: <strong>{x.machine}</strong> {x.gpus.length ? `GPU ${x.gpus.map((g) => `#${g}`).join(", ")}` : "CPU"} · {x.variant}{x.runtime === "process" ? " (process)" : ""}
                {x.gpu_memory_utilization && <> · <span className="mono">--gpu-memory-utilization {x.gpu_memory_utilization}</span></>}
                {x.cached && <> · model cached there</>}{!x.online && <> · waits for it to come online</>}</span></li>
          ))}
        </ul>
      ) : (
        <div className="nofit" role="alert"><IconAlertTriangle size={16} aria-hidden="true" /> <span>{p.failed_replica ? `Only ${p.failed_replica} of the replicas fit.` : "It does not fit anywhere right now."}</span></div>
      )}
      <Verdicts list={p.machines} />
    </div>
  );
}

function Verdicts({ list }: { list: MachineVerdict[] }) {
  return (
    <ul className="verdicts">
      {list.map((m) => (
        <li key={m.machine_id} className={m.fits ? "fits" : "nope"}>
          <span className="mono strong">{m.name}</span>{m.status !== "online" && <span className="muted small"> ({m.status})</span>}
          {m.short_by ? <span className="chip chip-tone-bad">{gib(m.short_by)} short</span> : m.fits ? <span className="chip chip-tone-ok">fits</span> : null}
          <div className="small verdict-reason">{m.reason || (m.fits ? `${m.variant} · ${(m.cards ?? []).map((c) => `${c.index === null ? "RAM" : `#${c.index}`} ${gib(c.available)} available`).join(", ")}` : "")}</div>
        </li>
      ))}
    </ul>
  );
}
