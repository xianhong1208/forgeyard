import { IconArrowLeft } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth";
import { LastTrialLine } from "../components/EngineTrials";
import { ReplicasField, ScaleAdvancedFields } from "../components/Replicas";
import { Notice, PageHeader } from "../components/ui";
import { ApiError, api, errorText } from "../lib/api";
import { FIRST_ANSWER_DEFAULT, editBody, editForm, recipeDefaults, takesFirstAnswer, type EditForm } from "../lib/models";
import { engineHref } from "../lib/pages";
import type { CatalogModel, CatalogResponse, Deployment, DeploymentEngine, DeploymentResponse, EditOutage, EditResponse, Machine, MachineVerdict, MachinesResponse,
  Recipe } from "../lib/types";
import { canAutoscale, scaleEditParams, scaleForm, type ScaleForm } from "../lib/scale";
import { useApi } from "../lib/useApi";
import { SleepSetting, WakeWaitField } from "../components/Sleep";
import { labels, t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { ESTIMATE_DELAY_MS, EstimatePanel, ParamField, ShareHintNotice, Verdicts, paramApplies } from "./ModelDeploy";

// What a change shows next to each changed setting.
const CHANGE_LABEL: Record<string, string> = labels({ recipe: "deploy.change.recipe", share: "deploy.change.share", gpus: "deploy.change.gpus", vram: "deploy.change.vram",
  extra_args: "deploy.change.extra_args", first_answer_timeout: "deploy.change.first_answer_timeout", replicas: "deployment.replicas",
  machine_id: "deployment.col.machine", sleep_when_idle: "deployment.sleep.label", idle_minutes: "deploy.change.idle_minutes",
  wake_wait: "deploy.change.wake_wait", autoscale: "deploy.change.autoscale", min_replicas: "deploy.change.min_replicas",
  max_replicas: "deploy.change.max_replicas", autoscale_target: "deploy.change.autoscale_target",
  scale_up_seconds: "deploy.change.scale_up_seconds", scale_down_minutes: "deploy.change.scale_down_minutes" });

export function DeploymentEditPage() {
  const t = useT();
  const { id } = useParams();
  const { user } = useAuth();
  const { data } = useApi<DeploymentResponse>(`/deployments/${id}`);
  const { data: cat } = useApi<CatalogResponse>("/models");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  const d = data?.deployment;
  const recipe = cat?.recipes.find((r) => r.name === d?.recipe);
  if (user && user.role === "user") {
    return (<><PageHeader index={t("deploy.eyebrow")} title={t("deploy.edit.title")} /><Notice>{t("deploy.edit.notAllowed")}</Notice></>);
  }
  if (!d || !recipe || !cat) return <><PageHeader index={t("deploy.eyebrow")} title={t("deploy.edit.title")} /><p className="muted">{t("common.loading")}</p></>;
  return <EditView key={d.id} d={d} recipe={recipe} recipes={cat.recipes} model={cat.models.find((m) => m.id === d.model.id)}
    machines={(machines?.machines ?? []).filter((m) => m.status !== "revoked")} />;
}

// The form starts from what the deployment runs with now, once (a reload does not undo edits).
function EditView({ d, recipe: current, recipes, model, machines: usable }: {
  d: Deployment; recipe: Recipe; recipes: Recipe[]; model: CatalogModel | undefined; machines: Machine[];
}) {
  const t = useT();
  const navigate = useNavigate();
  const [f, setF] = useState<EditForm>(() => editForm(d, current));
  // 1.5.2 (#95): the engine version it would run (the built-in one or a copy); its parameters are the form's.
  const [engine, setEngine] = useState(d.recipe);
  const recipe = recipes.find((r) => r.name === engine) ?? current;
  const [plan, setPlan] = useState<EditResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; machines?: MachineVerdict[] } | null>(null);
  const [scale, setScale] = useState<ScaleForm>(() => scaleForm(d.params));   // 1.5.0: fixed or automatic replicas

  const body = editBody(f, recipe);
  if (engine !== d.recipe) body.recipe = engine;
  const auto = canAutoscale(recipe) && scale.mode === "auto";
  body.params = { ...(body.params as Record<string, unknown>), ...scaleEditParams(scale) };
  if (auto) delete body.replicas;   // the autoscaler sets the number; the bounds keep it within them
  const bodyKey = JSON.stringify(body);

  useEffect(() => {
    let alive = true;
    const timer = window.setTimeout(() => {
      api<EditResponse>(`/deployments/${d.id}`, { method: "PATCH", body: { ...JSON.parse(bodyKey), dry_run: true } }).then(
        (r) => { if (alive) { setPlan(r); setPlanError(null); } },
        (e) => { if (alive) { setPlan(null); setPlanError(errorText(e)); } },
      );
    }, ESTIMATE_DELAY_MS);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [bodyKey, d.id]);

  const set = <K extends keyof EditForm>(k: K, v: EditForm[K]) => setF((cur) => ({ ...cur, [k]: v }));
  // Another version's parameters start from what the deployment set; one it did not have starts at its default.
  const chooseEngine = (name: string) => {
    const next = recipes.find((r) => r.name === name);
    setEngine(name);
    if (next) setF((cur) => ({ ...cur, params: { ...recipeDefaults(next), ...cur.params } }));
  };
  const setParam = (k: string, v: string) => setF((cur) => ({ ...cur, params: { ...cur.params, [k]: v } }));
  const running = d.workload.desired_state === "running";
  const changes = Object.entries(plan?.changes ?? {});
  const more = Number(plan?.changes.replicas?.after ?? 0) > Number(plan?.changes.replicas?.before ?? 0);
  // Like a deploy: what would be placed now must fit (a stopped deployment is only saved).
  const blocked = running && plan !== null && (plan.restart !== "none" || more) && !plan.placement.fits;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api<EditResponse>(`/deployments/${d.id}`, { method: "PATCH", body });
      navigate(`/serving/${d.id}`);
    } catch (err) {
      const list = err instanceof ApiError ? (err.detail.machines as MachineVerdict[] | undefined) : undefined;
      setError({ text: errorText(err), machines: list });
    } finally {
      setBusy(false);
    }
  };

  const relevant = Object.entries(recipe.params).filter(([k]) => !(f.device === "cpu" && k === "gpu_layers") && !(f.device === "gpu" && k === "threads")
    && paramApplies(k, model));
  const shown = relevant.filter(([, p]) => !p.advanced);
  const advanced = relevant.filter(([, p]) => p.advanced);

  return (
    <>
      <p className="crumb"><Link to={`/serving/${d.id}`}><IconArrowLeft size={14} aria-hidden="true" /> {d.name}</Link></p>
      <PageHeader index={t("deploy.eyebrow")} title={t("deploy.edit.titleName", { name: d.name })}
        lead={t("deploy.edit.lead", { model: d.model.name, recipe: d.recipe, served: d.served_name })} />
      <form className="wl-form deploy-layout" onSubmit={submit} aria-label={t("deploy.edit.formLabel")}>
        <div>
          <EngineChoice d={d} engine={engine} onChange={chooseEngine} />
          <section className="panel">
            <h2>{t("deploy.edit.sharing")}</h2>
            <div className="grid-3">
              {recipe.devices.length > 1 && (
                <div className="field"><label htmlFor="e-device">{t("deploy.runsOn")}</label>
                  <select id="e-device" value={f.device} onChange={(e) => set("device", e.target.value as EditForm["device"])}>
                    <option value="gpu">GPU</option><option value="cpu">{t("deploy.cpuOnly")}</option>
                  </select></div>
              )}
              {f.device === "gpu" && (
                <>
                  <div className="field"><label htmlFor="e-share">{t("deploy.change.share")}</label>
                    <select id="e-share" value={f.share} onChange={(e) => set("share", e.target.value as EditForm["share"])}>
                      <option value="exclusive">{t("deploy.exclusive")}</option>
                      <option value="vram">{t("deploy.shareVram")}</option>
                    </select>
                    <p className="field-hint">{t("deploy.edit.shareHint")}</p></div>
                  <div className="field"><label htmlFor="e-gpus">{t("deploy.change.gpus")}</label>
                    <input id="e-gpus" inputMode="numeric" value={f.gpus} onChange={(e) => set("gpus", e.target.value)} /></div>
                </>
              )}
              <ReplicasField id="e" count={f.replicas} onCount={(v) => set("replicas", v)} scale={scale} onScale={setScale}
                allowAuto={canAutoscale(recipe)} sleepOn={f.sleepOn} now={d.workload.replica_list.length} />
              <div className="field"><label htmlFor="e-machine">{t("deployment.col.machine")}</label>
                <select id="e-machine" value={f.machine} onChange={(e) => set("machine", e.target.value)}>
                  <option value="">{t("deploy.edit.automatic")}</option>
                  {usable.map((m) => <option key={m.id} value={m.id}>{t("deploy.edit.pinnedOption", { name: m.name, status: t.maybe(`common.status.${m.status}`, m.status) })}</option>)}
                </select>
                <p className="field-hint">{f.machine ? t("deploy.edit.pinnedHint") : t("deploy.edit.autoHint")}</p></div>
            </div>
            {f.device === "gpu" && f.share === "exclusive" && plan?.placement.share_hint && (
              <ShareHintNotice hint={plan.placement.share_hint} onShare={() => set("share", "vram")} />
            )}
          </section>
          <section className="panel">
            <h2>{t("deploy.parameters")}</h2>
            <div className="grid-3">
              {shown.map(([k, p]) => <ParamField key={k} name={k} p={p} value={f.params[k]} onChange={setParam} />)}
            </div>
            <details className="spec-preview" style={{ marginTop: 14 }}>
              <summary>{t("deploy.edit.advanced", { scaling: auto ? t("deploy.adv.scaling") : "", limits: advanced.length ? t("deploy.adv.limits") : "",
                first: takesFirstAnswer(recipe) ? t("deploy.adv.firstEdit") : "" })}</summary>
              <div className="grid-3">
                {auto && <ScaleAdvancedFields id="e" scale={scale} onScale={setScale} />}
                {advanced.map(([k, p]) => <ParamField key={k} name={k} p={p} value={f.params[k]} onChange={setParam} />)}
                <div className="field"><label htmlFor="e-vram">{t("deploy.vramOverride")}</label>
                  <input id="e-vram" value={f.vram} onChange={(e) => set("vram", e.target.value)} placeholder={t("deploy.theEstimate")} />
                  <p className="field-hint">{t("deploy.edit.vramHint")}</p></div>
                <div className="field"><label htmlFor="e-extra">{t("deploy.change.extra_args")}</label>
                  <textarea id="e-extra" className="mono" rows={3} value={f.extraArgs} onChange={(e) => set("extraArgs", e.target.value)} placeholder={t("deploy.onePerLine")} /></div>
                {takesFirstAnswer(recipe) && (
                  <div className="field"><label htmlFor="e-first">{t("deploy.firstAnswer")}</label>
                    <input id="e-first" inputMode="numeric" value={f.firstAnswer} onChange={(e) => set("firstAnswer", e.target.value)} placeholder={String(FIRST_ANSWER_DEFAULT)} />
                    <p className="field-hint">{t("deploy.edit.firstHint", { def: String(FIRST_ANSWER_DEFAULT) })}</p></div>
                )}
                <WakeWaitField id="e-wake" value={f.wakeWait} onChange={(v) => set("wakeWait", v)} />
              </div>
            </details>
          </section>
          <SleepSetting prefix="e" on={f.sleepOn} minutes={f.idleMinutes} last={d.last_start}
            onChange={(on, minutes) => setF((cur) => ({ ...cur, sleepOn: on, idleMinutes: minutes }))} />
          <section className="panel" aria-labelledby="what-title">
            <h2 id="what-title">{t("deploy.edit.what")}</h2>
            {planError ? <Notice tone="error">{planError}</Notice> : !plan ? <p className="muted small">{t("deploy.edit.checking")}</p> : changes.length === 0 ? (
              <p className="muted small">{t("deploy.edit.nothing")}</p>
            ) : (
              <>
                <ul className="edit-changes" aria-label={t("deploy.edit.changes")}>
                  {changes.map(([k, v]) => (
                    <li key={k}>{t.rich("deploy.edit.changeLine", { label: <span className="strong">{CHANGE_LABEL[k] ?? recipe.params[k]?.label ?? k}</span>,
                      before: <span className="mono">{changeValue(k, v.before, usable)}</span>, after: <span className="mono">{changeValue(k, v.after, usable)}</span> })}</li>
                  ))}
                </ul>
                {(plan.dropped ?? []).length > 0 && (
                  <p className="small muted">{t("deploy.edit.dropped", { recipe: plan.recipe ?? engine, names: (plan.dropped ?? []).join(", ") })}</p>
                )}
                <p className="edit-plan">{t.maybe(`deploy.restart.${plan.restart}`, plan.restart_text)}</p>
                {plan.outage && <OutageNotice o={plan.outage} engine={plan.recipe ?? engine} />}
                {blocked && <Notice tone="error">{t("deploy.edit.blocked")}</Notice>}
              </>
            )}
          </section>
          {error && (
            <Notice tone="error">
              <strong>{error.machines ? t("deploy.noFit") : error.text}</strong>
              {error.machines && <Verdicts list={error.machines} />}
            </Notice>
          )}
          <div className="form-foot">
            <span />
            <div className="row-gap">
              <button className="btn btn-primary" disabled={busy || !plan || changes.length === 0 || blocked}>{busy ? t("deploy.edit.saving") : running && plan?.restart !== "none" ? t("deploy.edit.saveRestart") : t("machine.save")}</button>
              <Link to={`/serving/${d.id}`} className="btn btn-quiet">{t("common.cancel")}</Link>
            </div>
          </div>
        </div>
        <aside className="estimate-side" aria-label={t("deploy.estimate")}>
          <EstimatePanel est={plan} error={planError} pending={!plan && !planError} />
        </aside>
      </form>
    </>
  );
}

// 1.5.2 (#95): the engine versions it can run: its built-in engine and the copies of it ("Try this version").
function EngineChoice({ d, engine, onChange }: { d: Deployment; engine: string; onChange: (name: string) => void }) {
  const t = useT();
  const list = d.engines ?? [];
  if (list.length === 0) return null;
  const chosen = list.find((e) => e.name === engine);
  const builtin = list.find((e) => e.builtin);
  return (
    <section className="panel" aria-labelledby="engine-title">
      <h2 id="engine-title">{t("deploy.edit.engine")}</h2>
      <div className="grid-2">
        <div className="field"><label htmlFor="e-engine">{t("deploy.edit.engineVersion")}</label>
          <select id="e-engine" value={engine} disabled={list.length < 2} onChange={(e) => onChange(e.target.value)}>
            {list.map((e) => <option key={e.name} value={e.name}>{engineOption(e)}</option>)}
          </select>
          <p className="field-hint">{list.length < 2
            ? t.rich("deploy.edit.engineOnlyOne", { link: (x) => <Link to={engineHref(builtin?.name ?? d.recipe)}>{x}</Link> })
            : chosen && !chosen.builtin
              ? t("deploy.edit.engineCopyHint", { builtin: chosen.based_on ?? builtin?.name ?? "" })
              : t("deploy.edit.engineBuiltinHint")}</p>
          {/* 1.5.2 (#95, PR 4): how its last trial went, when switching to it. Never a gate (community edition). */}
          {chosen && engine !== d.recipe && <LastTrialLine name={chosen.name} builtin={chosen.builtin} last={chosen.last_trial} />}
        </div>
      </div>
    </section>
  );
}

function engineOption(e: DeploymentEngine): string {
  const title = e.title.endsWith(` ${e.version}`) ? e.title : `${e.title} ${e.version}`;
  return `${e.name} · ${title} · ${e.builtin ? tNow("deploy.edit.engineBuiltin") : tNow("deploy.edit.engineCopy")}`;
}

// 1.5.2 (#95): before a change that restarts the only running replica: about how long the model does not answer, and
// how to avoid it. It only says so; saving does what it always did.
function OutageNotice({ o, engine }: { o: EditOutage; engine: string }) {
  const t = useT();
  return (
    <Notice tone="warn">
      <div className="edit-outage" aria-label={t("deploy.outage.label")}>
        <p><strong>{t("deploy.outage.lead", { seconds: String(o.seconds) })}</strong> {t(`deploy.outage.source.${o.source}`)}</p>
        {o.first_start && <p>{t("deploy.outage.firstStart")}</p>}
        {o.image_missing.length > 0 && (
          <p>{t.rich("deploy.outage.image", { machines: o.image_missing.join(", "), link: (x) => <Link to={engineHref(engine)}>{x}</Link> })}</p>
        )}
        <p>{t("deploy.outage.avoid")}</p>
        <ul>
          {o.avoid.map((a) => <li key={a}>{t(`deploy.outage.avoid.${a}`)}</li>)}
        </ul>
      </div>
    </Notice>
  );
}

function changeValue(k: string, v: unknown, machines: { id: number; name: string }[]): string {
  const on = tNow("deploy.value.on");
  const off = tNow("deploy.value.off");
  if (k === "autoscale") return v === true ? on : off;
  if (v === null || v === undefined) return k === "machine_id" ? tNow("deploy.value.automatic") : k === "vram" ? tNow("deploy.theEstimate") : k === "sleep_when_idle" ? off : "—";
  if (k === "sleep_when_idle") return v === true ? on : off;
  if (k === "machine_id") return machines.find((m) => m.id === v)?.name ?? tNow("deployment.machineN", { id: String(v) });
  if (k === "vram" && typeof v === "number") return `${(v / 1024 ** 3).toFixed(2)} GiB`;
  if (k === "share") return v === "vram" ? tNow("deploy.shareVram") : tNow("deploy.exclusive");
  if (Array.isArray(v)) return v.join(" ") || tNow("deploy.value.none");
  return String(v);
}
