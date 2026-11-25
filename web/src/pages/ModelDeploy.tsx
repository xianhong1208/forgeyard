import { IconAlertTriangle, IconCheck } from "@tabler/icons-react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { Link, useNavigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { EngineImage } from "../components/EngineImage";
import { ReplicasField, ScaleAdvancedFields } from "../components/Replicas";
import { Notice, PageHeader } from "../components/ui";
import { ApiError, api, errorText } from "../lib/api";
import { engineRuns } from "../lib/engines";
import { inputParamApplies } from "../lib/imageEdit";
import { bytes } from "../lib/machines";
import { FIRST_ANSWER_DEFAULT, TASK_LABEL, compatibleRecipes, deployBody, emptyDeploy, gib, recipeDefaults, slug, takesFirstAnswer, type DeployForm } from "../lib/models";
import { sameModelStart } from "../lib/sleep";
import type { CatalogModel, CatalogResponse, ContextHint, DeploymentsResponse, Estimate, EstimateResponse, MachineVerdict, MachinesResponse, Placement, RecipeParam, ShareHint } from "../lib/types";
import { canAutoscale, emptyScale, scaleDeployParams, type ScaleForm } from "../lib/scale";
import { useApi } from "../lib/useApi";
import { SleepSetting, WakeWaitField } from "../components/Sleep";
import { useT } from "../i18n/react";

export const ESTIMATE_DELAY_MS = 400;

export function ModelDeployPage() {
  const t = useT();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const { data: cat } = useApi<CatalogResponse>("/models");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  // 1.5.0: another deployment of the same model and engine tells how long a start takes.
  const { data: deps } = useApi<DeploymentsResponse>("/deployments");
  // ?model=…&recipe=…&device=… from "Deploy it" after adding a model (its suggested recipe and device).
  const [f, setF] = useState<DeployForm>(() => ({ ...emptyDeploy(), model: search.get("model") ?? "", recipe: search.get("recipe") ?? "",
    machine: search.get("machine") ?? "",
    device: search.get("device") === "cpu" ? "cpu" : "gpu" }));
  const [est, setEst] = useState<EstimateResponse | null>(null);
  const [estError, setEstError] = useState<string | null>(null);
  // The context hint (D9) of the last estimate that worked: still offered while a context that is too
  // long makes the estimate fail, so one click puts it back in range.
  const [ctxHint, setCtxHint] = useState<{ key: string; hint: ContextHint } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; machines?: MachineVerdict[] } | null>(null);
  const [scale, setScale] = useState<ScaleForm>(emptyScale);   // 1.5.0: a fixed number or automatic replicas

  const model = cat?.models.find((m) => m.name === f.model);
  const recipes = useMemo(() => compatibleRecipes(model, cat?.recipes ?? []), [model, cat]);
  const recipe = recipes.find((r) => r.name === f.recipe) ?? recipes[0];
  // What the form means: the chosen model's first recipe, a name from the model, a device the
  // recipe has. Derived, so the form follows the catalog without copying it into state.
  const view: DeployForm = {
    ...f, recipe: recipe?.name ?? "", name: f.name || (model ? slug(model.name) : ""),
    device: recipe && !recipe.devices.includes(f.device) ? recipe.devices[0] : f.device,
    params: { ...recipeDefaults(recipe, model?.summary?.max_context), ...f.params },
  };

  const body = deployBody(view, recipe);
  const auto = canAutoscale(recipe) && scale.mode === "auto";
  if (auto) {
    const chosen = scaleDeployParams(scale);
    body.params = { ...(body.params as Record<string, unknown>), ...chosen.params };
    body.replicas = chosen.replicas;
  }
  const bodyKey = JSON.stringify({ ...body, hf_token: undefined });
  // What the context hint depends on besides the context itself: another model, recipe or machine needs a new one.
  const hintKey = JSON.stringify([view.model, view.recipe, view.device, f.machine]);

  useEffect(() => {
    if (!recipe) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      api<EstimateResponse>("/deployments/estimate", { method: "POST", body: { ...JSON.parse(bodyKey), name: JSON.parse(bodyKey).name || "preview" } }).then(
        (r) => { if (alive) { setEst(r); setEstError(null); if (r.context_hint) setCtxHint({ key: hintKey, hint: r.context_hint }); } },
        (e) => { if (alive) { setEst(null); setEstError(errorText(e)); } },
      );
    }, ESTIMATE_DELAY_MS);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [bodyKey, recipe, hintKey]);

  if (user && user.role === "user") {
    return (<><PageHeader index={t("deploy.eyebrow")} title={t("deploy.title")} /><Notice>{t("deploy.notAllowed")}</Notice></>);
  }

  const set = <K extends keyof DeployForm>(k: K, v: DeployForm[K]) => setF((cur) => ({ ...cur, [k]: v }));
  const setParam = (k: string, v: string) => setF((cur) => ({ ...cur, params: { ...cur.params, [k]: v } }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ deployment: { id: number } }>("/deployments", { method: "POST", body });
      navigate(`/serving/${r.deployment.id}`);
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
  const same = sameModelStart(deps?.deployments ?? [], view.model, view.recipe);

  return (
    <>
      <p className="crumb"><Link to="/serving">← {t("shell.nav.serving")}</Link></p>
      <PageHeader index={t("deploy.eyebrow")} title={t("deploy.title")} lead={t("deploy.lead")} />
      {cat && cat.models.length === 0 && (
        <Notice>
          {t("deploy.emptyCatalog")}{" "}
          {cat.can_manage ? <Link to="/models">{t("deploy.addToCatalog")}</Link> : t("deploy.adminAdds")}
        </Notice>
      )}
      <form className="wl-form deploy-layout" onSubmit={submit} aria-label={t("deploy.title")}>
        <div>
          <section className="panel">
            <h2>{t("deploy.modelEngine")}</h2>
            <div className="grid-2">
              <div className="field">
                <label htmlFor="d-model">{t("machine.dlg.model")}</label>
                <select id="d-model" value={f.model} onChange={(e) => setF({ ...emptyDeploy(), model: e.target.value })} required>
                  <option value="">{t("deploy.choose")}</option>
                  {(cat?.models ?? []).map((m) => <option key={m.id} value={m.name}>{m.title ?? m.name} · {TASK_LABEL[m.task]} · {m.format}</option>)}
                </select>
                {model && <p className="field-hint mono">{model.source}{model.file ? ` · ${model.file}` : ""} · {bytes(model.size_bytes)}</p>}
              </div>
              <div className="field">
                <label htmlFor="d-recipe">{t("machine.dlg.engine")}</label>
                <select id="d-recipe" value={view.recipe} onChange={(e) => setF({ ...f, recipe: e.target.value, params: {} })} disabled={!model}>
                  {recipes.map((r) => <option key={r.name} value={r.name}>{r.title} ({r.engine} {r.version})</option>)}
                </select>
                {recipe && <p className="field-hint engine-image-line"><EngineImage runs={engineRuns(recipe.variants, view.device)} /></p>}
                {recipe && model && transformersEngine(recipe, model.task) && <p className="field-hint">{t("deploy.anyCpu")}</p>}
                {model && recipes.length === 0 && <p className="field-error">{t("deploy.noEngine", { format: model.format, task: TASK_LABEL[model.task] })}</p>}
              </div>
            </div>
            <div className="grid-2">
              <div className="field"><label htmlFor="d-name">{t("deploy.name")}</label>
                <input id="d-name" className="mono" value={view.name} onChange={(e) => set("name", e.target.value)} required /></div>
              <div className="field"><label htmlFor="d-served">{t("deploy.served")}</label>
                <input id="d-served" className="mono" value={f.servedName} onChange={(e) => set("servedName", e.target.value)} placeholder={view.name || t("deploy.servedPlaceholder")} />
                <p className="field-hint">{t.rich("deploy.servedHint", { model: <code>model</code> })}</p></div>
            </div>
          </section>

          {recipe && (
            <section className="panel">
              <h2>{t("deploy.parameters")}</h2>
              <div className="grid-3">
                {recipe.devices.length > 1 && (
                  <div className="field"><label htmlFor="d-device">{t("deploy.runsOn")}</label>
                    <select id="d-device" value={view.device} onChange={(e) => set("device", e.target.value as "gpu" | "cpu")}>
                      <option value="gpu">GPU</option><option value="cpu">{t("deploy.cpuOnly")}</option>
                    </select></div>
                )}
                {view.device === "gpu" && (
                  <>
                    <div className="field"><label htmlFor="d-gpus">{t("deploy.change.gpus")}</label>
                      <input id="d-gpus" inputMode="numeric" value={f.gpus} onChange={(e) => set("gpus", e.target.value)} /></div>
                    <div className="field"><label htmlFor="d-share">{t("deploy.change.share")}</label>
                      <select id="d-share" value={f.share || recipe.share} onChange={(e) => set("share", e.target.value as DeployForm["share"])}>
                        <option value="exclusive">{t("deploy.exclusive")}</option>
                        <option value="vram">{t("deploy.shareVram")}</option>
                      </select>
                      <p className="field-hint">{t("deploy.shareHint")}</p></div>
                  </>
                )}
                {shown.map(([k, p]) => (
                  <ParamField key={k} name={k} p={p} value={view.params[k]} onChange={setParam}
                    extra={ctxHint && ctxHint.key === hintKey && ctxHint.hint.param === k
                      ? <ContextHintLine hint={ctxHint.hint} value={view.params[k]} pinned={!!f.machine} onPick={(n) => setParam(k, String(n))} /> : null} />
                ))}
                <ReplicasField id="d" count={f.replicas} onCount={(v) => set("replicas", v)} scale={scale} onScale={setScale}
                  allowAuto={canAutoscale(recipe)} sleepOn={f.sleepOn} />
                <div className="field"><label htmlFor="d-machine">{t("deployment.col.machine")}</label>
                  <select id="d-machine" value={f.machine} onChange={(e) => set("machine", e.target.value)}>
                    <option value="">{t("deploy.bestFit")}</option>
                    {usable.map((m) => <option key={m.id} value={m.id}>{m.name} · {t.maybe(`common.status.${m.status}`, m.status)}</option>)}
                  </select></div>
              </div>
              {view.device === "gpu" && (f.share || recipe.share) === "exclusive" && est?.placement.share_hint && (
                <ShareHintNotice hint={est.placement.share_hint} onShare={() => set("share", "vram")} />
              )}
              <details className="spec-preview" style={{ marginTop: 14 }}>
                <summary>{t("deploy.advanced", { scaling: auto ? t("deploy.adv.scaling") : "", limits: advanced.length ? t("deploy.adv.limits") : "",
                  first: takesFirstAnswer(recipe) ? t("deploy.adv.firstDeploy") : "" })}</summary>
                <p className="field-hint">{t("deploy.advancedHint")}</p>
                <div className="grid-3">
                  {auto && <ScaleAdvancedFields id="d" scale={scale} onScale={setScale} />}
                  {advanced.map(([k, p]) => <ParamField key={k} name={k} p={p} value={view.params[k]} onChange={setParam} />)}
                  <div className="field"><label htmlFor="d-vram">{t("deploy.vramOverride")}</label>
                    <input id="d-vram" value={f.vram} onChange={(e) => set("vram", e.target.value)} placeholder={t("deploy.vramPlaceholder")} />
                    <p className="field-hint">{t("deploy.vramHint")}</p></div>
                  <div className="field"><label htmlFor="d-extra">{t("deploy.change.extra_args")}</label>
                    <textarea id="d-extra" className="mono" rows={3} value={f.extraArgs} onChange={(e) => set("extraArgs", e.target.value)} placeholder={t("deploy.onePerLine")} /></div>
                  {takesFirstAnswer(recipe) && (
                    <div className="field"><label htmlFor="d-first">{t("deploy.firstAnswer")}</label>
                      <input id="d-first" inputMode="numeric" value={f.firstAnswer} onChange={(e) => set("firstAnswer", e.target.value)} placeholder={String(FIRST_ANSWER_DEFAULT)} />
                      <p className="field-hint">{t("deploy.firstHint")}</p></div>
                  )}
                  <WakeWaitField id="d-wake" value={f.wakeWait} onChange={(v) => set("wakeWait", v)} />
                  <div className="field"><label htmlFor="d-token">{t("deploy.token")}</label>
                    <input id="d-token" type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)}
                      placeholder={cat?.hf_token_set ? t("deploy.tokenServer") : undefined} />
                    <p className="field-hint">{cat?.hf_token_set ? t("deploy.tokenSetHint") : t("deploy.tokenUnsetHint")}{t("deploy.tokenSent")}</p></div>
                </div>
              </details>
            </section>
          )}
          {recipe && (
            <SleepSetting prefix="d" on={f.sleepOn} minutes={f.idleMinutes}
              onChange={(on, minutes) => setF((cur) => ({ ...cur, sleepOn: on, idleMinutes: minutes }))}
              last={same?.last} other={same?.name} />
          )}
          {error && (
            <Notice tone="error">
              <strong>{error.machines ? t("deploy.noFit") : error.text}</strong>
              {error.machines && <Verdicts list={error.machines} />}
            </Notice>
          )}
          <div className="form-foot">
            <details className="spec-preview"><summary>{t("deploy.request")}</summary>
              <pre className="mono" aria-label={t("deploy.json")}>{JSON.stringify({ ...body, hf_token: body.hf_token ? "••••" : undefined }, null, 2)}</pre></details>
            <div className="row-gap">
              <button className="btn btn-primary" disabled={busy || !recipe || est?.placement.fits === false}>{busy ? t("deploy.deploying") : t("deploy.deploy")}</button>
              <Link to="/serving" className="btn btn-quiet">{t("common.cancel")}</Link>
            </div>
          </div>
        </div>
        <aside className="estimate-side" aria-label={t("deploy.estimate")}>
          <EstimatePanel est={est} error={estError} pending={!!recipe && !est && !estError} />
        </aside>
      </form>
    </>
  );
}

// 1.5.4 (#141, #144): Forgeyard's transformers server (the forgeyard-vision image) serving an embedding or a text
// classification model: it runs on any CPU (x86-64 or Arm), a GPU optional.
export function transformersEngine(recipe: { engine: string }, task: string): boolean {
  return recipe.engine === "forgeyard-vision" && (task === "embed" || task === "classify");
}

// The estimate's part for weights a CPU holds in float32 (forgeyard/models/estimate.py, method files, cpu_float32).
export const FLOAT32_PART = "Model weights in float32";

// The media parameters (1.3) only mean something for a model that takes media: hidden for one that
// is known to take none of them (a catalog entry from before 1.3 is unknown, so they show).
export function paramApplies(name: string, model: CatalogModel | undefined): boolean {
  // 1.5.5 (#143): Input images and Largest input for a model that edits; Largest start image for one that takes it.
  if (!inputParamApplies(name, model)) return false;
  // Text to speech (1.3): the input cap is for speech models only, and they take no media in chat.
  if (name === "max_input_chars") return !model || model.task === "tts";
  if (model?.task === "tts" && ["images", "image_tokens", "videos", "video_fps", "max_body_mb"].includes(name)) return false;
  const m = model?.modalities;
  if (m === null || m === undefined) return true;
  if (name === "images") return m.includes("image") || m.includes("audio");
  if (name === "image_tokens") return m.includes("image");
  if (name === "videos" || name === "video_fps") return m.includes("video");
  return true;
}

// 1.3.1: an exclusive model that needs half its card or less: say that sharing would leave room.
export function ShareHintNotice({ hint, onShare }: { hint: ShareHint; onShare: () => void }) {
  const t = useT();
  return (
    <div className="share-hint" role="note" aria-label={t("deploy.shareHintLabel")}>
      <p>{hint.text}{hint.machine && hint.gpu !== null ? <span className="muted"> ({hint.machine} GPU #{hint.gpu})</span> : null}</p>
      <button type="button" className="btn btn-quiet" onClick={onShare}>{t("deploy.useShare")}</button>
    </div>
  );
}

export function ParamField({ name, p, value, onChange, extra }: {
  name: string; p: RecipeParam; value: string | undefined; onChange: (k: string, v: string) => void; extra?: ReactNode;
}) {
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
      {extra}
    </div>
  );
}

// D9: the context keeps its default; the model's maximum and the largest that fits are one click away.
export function ContextHintLine({ hint, value, pinned, onPick }: { hint: ContextHint; value: string | undefined; pinned: boolean; onPick: (n: number) => void }) {
  const t = useT();
  if (!hint.model_max) return null;
  const current = Number(value);
  const pick = (n: number, label: string) => (
    <button type="button" className="linkish" disabled={current === n} onClick={() => onPick(n)} aria-label={t("deploy.ctx.use", { label, n })}>
      {t.num(n)}
    </button>
  );
  const where = pinned ? t("deploy.ctx.onMachine") : t("deploy.ctx.here");
  return (
    <p className="ctx-hint" aria-label={t("deploy.ctx.label")}>
      <span>{t.rich("deploy.ctx.max", { pick: pick(hint.model_max, t("deploy.ctx.maxLabel")) })}</span>
      <span aria-hidden="true">·</span>
      {hint.fits_max
        ? <span>{t.rich("deploy.ctx.fits", { where, pick: pick(hint.fits_max, t("deploy.ctx.fitsLabel")) })}</span>
        : <span>{t("deploy.ctx.none", { where })}</span>}
    </p>
  );
}

export function EstimatePanel({ est, error, pending }: { est: { estimate: Estimate; placement: Placement } | null; error: string | null; pending: boolean }) {
  const t = useT();
  if (error) return <section className="panel"><h2>{t("deploy.estimate")}</h2><Notice tone="error">{error}</Notice></section>;
  if (!est) return <section className="panel"><h2>{t("deploy.estimate")}</h2><p className="muted small">{pending ? t("deploy.estimating") : t("deploy.chooseFirst")}</p></section>;
  const e = est.estimate;
  const total = e.amount || 1;
  return (
    <section className="panel" aria-labelledby="est-title">
      <h2 id="est-title">{t("deploy.estimate")}</h2>
      <p className="est-total"><strong>{gib(e.amount)}</strong> <span className="muted">{e.device === "gpu"
        ? t("deploy.est.perGpu", { gpus: e.gpus > 1 ? t("deploy.est.gpus", { count: e.gpus }) : "" }) : t("deploy.est.ofRam")} · {e.method}</span></p>
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
      {e.parts.some((p) => p.label === FLOAT32_PART) && <p className="small muted est-float32">{t("deploy.est.float32")}</p>}
      <PlacementView p={est.placement} />
    </section>
  );
}

export function PlacementView({ p }: { p: Placement }) {
  const t = useT();
  return (
    <div className="placement">
      <h3 className="sub">{t("deploy.where")}</h3>
      {p.fits ? (
        <ul className="placed">
          {p.placed.map((x) => (
            <li key={x.replica}><IconCheck size={15} aria-hidden="true" className="ok-ic" />
              <span>{t.rich("deploy.placed", { replica: x.replica, machine: <strong>{x.machine}</strong> })} {x.gpus.length ? `GPU ${x.gpus.map((g) => `#${g}`).join(", ")}` : "CPU"} · {x.variant}{x.runtime === "process" ? t("deploy.placedProcess") : ""}
                {x.gpu_memory_utilization && <> · <span className="mono">--gpu-memory-utilization {x.gpu_memory_utilization}</span></>}
                {x.previous && t("deploy.placedNow")}{x.cached && t("deploy.placedCached")}{!x.online && t("deploy.placedOffline")}</span></li>
          ))}
        </ul>
      ) : (
        <div className="nofit" role="alert"><IconAlertTriangle size={16} aria-hidden="true" /> <span>{p.failed_replica ? t("deploy.onlySome", { count: p.failed_replica }) : t("deploy.noFit")}</span></div>
      )}
      <Verdicts list={p.machines} />
    </div>
  );
}

export function Verdicts({ list }: { list: MachineVerdict[] }) {
  const t = useT();
  return (
    <ul className="verdicts">
      {list.map((m) => (
        <li key={m.machine_id} className={m.fits ? "fits" : "nope"}>
          <span className="mono strong">{m.name}</span>{m.status !== "online" && <span className="muted small"> ({t.maybe(`common.status.${m.status}`, m.status)})</span>}
          {m.short_by ? <span className="chip chip-tone-bad">{t("deploy.short", { size: gib(m.short_by) })}</span> : m.fits ? <span className="chip chip-tone-ok">{t("machine.target.ok")}</span> : null}
          <div className="small verdict-reason">{m.reason || (m.fits ? `${m.variant} · ${(m.cards ?? []).map((c) => t("deploy.available", { card: c.index === null ? "RAM" : `#${c.index}`, size: gib(c.available) })).join(", ")}` : "")}</div>
          {m.share_fits && (
            <div className="small share-fits">
              {m.share_fits.both
                ? t.rich("deploy.shareFits.both", { strong: (x) => <strong>{x}</strong>, names: m.share_fits.exclusive.join(", ") })
                : m.share_fits.exclusive.length
                  ? t.rich("deploy.shareFits.others", { strong: (x) => <strong>{x}</strong>, names: m.share_fits.exclusive.join(", ") })
                  : t.rich("deploy.shareFits.here", { strong: (x) => <strong>{x}</strong> })}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}
