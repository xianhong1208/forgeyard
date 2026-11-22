import { IconArrowLeft } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth";
import { Notice, PageHeader } from "../components/ui";
import { ApiError, api, errorText } from "../lib/api";
import { FIRST_ANSWER_DEFAULT, editBody, editForm, takesFirstAnswer, type EditForm } from "../lib/models";
import type { CatalogModel, CatalogResponse, Deployment, DeploymentResponse, EditResponse, Machine, MachineVerdict, MachinesResponse, Recipe } from "../lib/types";
import { useApi } from "../lib/useApi";
import { ESTIMATE_DELAY_MS, EstimatePanel, ParamField, ShareHintNotice, Verdicts, paramApplies } from "./ModelDeploy";

// What a change shows next to each changed setting.
const CHANGE_LABEL: Record<string, string> = { share: "GPU sharing", gpus: "GPUs per replica", vram: "Memory per device",
  extra_args: "Extra engine arguments", first_answer_timeout: "Wait for the first answer (s)", replicas: "Replicas", machine_id: "Machine" };

export function DeploymentEditPage() {
  const { id } = useParams();
  const { user } = useAuth();
  const { data } = useApi<DeploymentResponse>(`/deployments/${id}`);
  const { data: cat } = useApi<CatalogResponse>("/models");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  const d = data?.deployment;
  const recipe = cat?.recipes.find((r) => r.name === d?.recipe);
  if (user && user.role === "user") {
    return (<><PageHeader index="02 · Serving" title="Change a deployment" /><Notice>Only admins and operators change deployments: it restarts code on a machine.</Notice></>);
  }
  if (!d || !recipe || !cat) return <><PageHeader index="02 · Serving" title="Change a deployment" /><p className="muted">Loading…</p></>;
  return <EditView key={d.id} d={d} recipe={recipe} model={cat.models.find((m) => m.id === d.model.id)}
    machines={(machines?.machines ?? []).filter((m) => m.status !== "revoked")} />;
}

// The form starts from what the deployment runs with now, once (a reload does not undo edits).
function EditView({ d, recipe, model, machines: usable }: { d: Deployment; recipe: Recipe; model: CatalogModel | undefined; machines: Machine[] }) {
  const navigate = useNavigate();
  const [f, setF] = useState<EditForm>(() => editForm(d, recipe));
  const [plan, setPlan] = useState<EditResponse | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; machines?: MachineVerdict[] } | null>(null);

  const body = editBody(f, recipe);
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
      <PageHeader index="02 · Serving" title={`Change ${d.name}`}
        lead={`${d.model.name} with ${d.recipe}, served as “${d.served_name}”. The model, recipe and names stay; deploy a new one to change them.`} />
      <form className="wl-form deploy-layout" onSubmit={submit} aria-label="Change the deployment">
        <div>
          <section className="panel">
            <h2>GPU sharing and machine</h2>
            <div className="grid-3">
              {recipe.devices.length > 1 && (
                <div className="field"><label htmlFor="e-device">Runs on</label>
                  <select id="e-device" value={f.device} onChange={(e) => set("device", e.target.value as EditForm["device"])}>
                    <option value="gpu">GPU</option><option value="cpu">CPU only</option>
                  </select></div>
              )}
              {f.device === "gpu" && (
                <>
                  <div className="field"><label htmlFor="e-share">GPU sharing</label>
                    <select id="e-share" value={f.share} onChange={(e) => set("share", e.target.value as EditForm["share"])}>
                      <option value="exclusive">Exclusive</option>
                      <option value="vram">Share VRAM</option>
                    </select>
                    <p className="field-hint">Shared cards hold several replicas, each held to its own reservation; they share compute.</p></div>
                  <div className="field"><label htmlFor="e-gpus">GPUs per replica</label>
                    <input id="e-gpus" inputMode="numeric" value={f.gpus} onChange={(e) => set("gpus", e.target.value)} /></div>
                </>
              )}
              <div className="field"><label htmlFor="e-replicas">Replicas</label>
                <input id="e-replicas" inputMode="numeric" value={f.replicas} onChange={(e) => set("replicas", e.target.value)} /></div>
              <div className="field"><label htmlFor="e-machine">Machine</label>
                <select id="e-machine" value={f.machine} onChange={(e) => set("machine", e.target.value)}>
                  <option value="">Automatic</option>
                  {usable.map((m) => <option key={m.id} value={m.id}>Pinned to {m.name} · {m.status}</option>)}
                </select>
                <p className="field-hint">{f.machine ? "Its replicas run on this machine only; a replica elsewhere moves there." : "Forgeyard chooses; a restart goes back to the machine a replica ran on when it still fits."}</p></div>
            </div>
            {f.device === "gpu" && f.share === "exclusive" && plan?.placement.share_hint && (
              <ShareHintNotice hint={plan.placement.share_hint} onShare={() => set("share", "vram")} />
            )}
          </section>
          <section className="panel">
            <h2>Parameters</h2>
            <div className="grid-3">
              {shown.map(([k, p]) => <ParamField key={k} name={k} p={p} value={f.params[k]} onChange={setParam} />)}
            </div>
            <details className="spec-preview" style={{ marginTop: 14 }}>
              <summary>Advanced: {advanced.length ? "request limits, " : ""}memory override, engine arguments{takesFirstAnswer(recipe) ? ", first-answer wait" : ""}</summary>
              <div className="grid-3">
                {advanced.map(([k, p]) => <ParamField key={k} name={k} p={p} value={f.params[k]} onChange={setParam} />)}
                <div className="field"><label htmlFor="e-vram">Memory per device (override)</label>
                  <input id="e-vram" value={f.vram} onChange={(e) => set("vram", e.target.value)} placeholder="the estimate" />
                  <p className="field-hint">Empty: the estimate.</p></div>
                <div className="field"><label htmlFor="e-extra">Extra engine arguments</label>
                  <textarea id="e-extra" className="mono" rows={3} value={f.extraArgs} onChange={(e) => set("extraArgs", e.target.value)} placeholder="one per line" /></div>
                {takesFirstAnswer(recipe) && (
                  <div className="field"><label htmlFor="e-first">Wait for the first answer (seconds)</label>
                    <input id="e-first" inputMode="numeric" value={f.firstAnswer} onChange={(e) => set("firstAnswer", e.target.value)} placeholder={String(FIRST_ANSWER_DEFAULT)} />
                    <p className="field-hint">Streamed answers only. Empty: {FIRST_ANSWER_DEFAULT}. A replica that has not started streaming by then gets the request stopped (504), never resent; non-streamed answers keep the read timeout. Changing it restarts nothing.</p></div>
                )}
              </div>
            </details>
          </section>
          <section className="panel" aria-labelledby="what-title">
            <h2 id="what-title">What saving does</h2>
            {planError ? <Notice tone="error">{planError}</Notice> : !plan ? <p className="muted small">Checking…</p> : changes.length === 0 ? (
              <p className="muted small">Nothing has changed yet.</p>
            ) : (
              <>
                <ul className="edit-changes" aria-label="Changes">
                  {changes.map(([k, v]) => (
                    <li key={k}><span className="strong">{CHANGE_LABEL[k] ?? recipe.params[k]?.label ?? k}</span>: <span className="mono">{changeValue(k, v.before, usable)}</span> → <span className="mono">{changeValue(k, v.after, usable)}</span></li>
                  ))}
                </ul>
                <p className="edit-plan">{plan.restart_text}</p>
                {blocked && <Notice tone="error">It does not fit with these settings right now, so nothing would restart: see where it would go.</Notice>}
              </>
            )}
          </section>
          {error && (
            <Notice tone="error">
              <strong>{error.machines ? "It does not fit anywhere right now." : error.text}</strong>
              {error.machines && <Verdicts list={error.machines} />}
            </Notice>
          )}
          <div className="form-foot">
            <span />
            <div className="row-gap">
              <button className="btn btn-primary" disabled={busy || !plan || changes.length === 0 || blocked}>{busy ? "Saving…" : running && plan?.restart !== "none" ? "Save and restart" : "Save"}</button>
              <Link to={`/serving/${d.id}`} className="btn btn-quiet">Cancel</Link>
            </div>
          </div>
        </div>
        <aside className="estimate-side" aria-label="Estimate">
          <EstimatePanel est={plan} error={planError} pending={!plan && !planError} />
        </aside>
      </form>
    </>
  );
}

function changeValue(k: string, v: unknown, machines: { id: number; name: string }[]): string {
  if (v === null || v === undefined) return k === "machine_id" ? "automatic" : k === "vram" ? "the estimate" : "—";
  if (k === "machine_id") return machines.find((m) => m.id === v)?.name ?? `machine ${String(v)}`;
  if (k === "vram" && typeof v === "number") return `${(v / 1024 ** 3).toFixed(2)} GiB`;
  if (k === "share") return v === "vram" ? "Share VRAM" : "Exclusive";
  if (Array.isArray(v)) return v.join(" ") || "none";
  return String(v);
}
