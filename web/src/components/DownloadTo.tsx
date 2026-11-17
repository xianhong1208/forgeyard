// "Download to…" (1.4, issue #35): download a model, and by default the engine of a recipe, to chosen machines
// before deploying it. The dialog lists every machine and whether it can take it; the list shows progress
// (the same bar and words as a deployment's download) and Cancel.
import { IconDownload, IconPuzzle, IconServer, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { api, errorText } from "../lib/api";
import { TARGET_LABEL, TARGET_TONE, downloadLine, isActive, pickable, targetDetail } from "../lib/downloads";
import { bytes } from "../lib/machines";
import { downloadFraction } from "../lib/models";
import type { CatalogModel, CatalogResponse, DownloadTarget, DownloadTargets, ModelDownload } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Notice } from "./ui";

type Started = { downloads: ModelDownload[]; skipped: { machine_id: number; name: string; state: string; reason: string }[] };

// The dialog. `model`: the model to download (from the Library). Without it (from a machine's page) the
// person picks one; `machineId` is ticked when it can take the download.
export function DownloadDialog({ model, machineId, onClose, onStarted }: {
  model?: CatalogModel; machineId?: number; onClose: () => void; onStarted: (text: string) => void;
}) {
  const catalog = useApi<CatalogResponse>(model ? null : "/models");
  const choices = (catalog.data?.models ?? []).filter((m) => m.kind !== "local");
  const [picked, setPicked] = useState<number | null>(model?.id ?? null);
  const modelId = model?.id ?? picked ?? choices[0]?.id ?? null;
  const [engine, setEngine] = useState(true);
  const [recipe, setRecipe] = useState("");
  const query = modelId === null ? null
    : `/downloads/targets?model_id=${modelId}&engine=${engine}${recipe ? `&recipe=${encodeURIComponent(recipe)}` : ""}`;
  const targets = useApi<DownloadTargets>(query);
  const t = targets.data && targets.data.model.id === modelId ? targets.data : null;
  const [chosen, setChosen] = useState<number[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Ticked at first: the machine the dialog was opened from, when it can take it.
  const ticked = chosen ?? (t && machineId !== undefined && t.machines.some((x) => x.machine_id === machineId && pickable(x)) ? [machineId] : []);
  const usable = t ? ticked.filter((id) => t.machines.some((x) => x.machine_id === id && pickable(x))) : [];

  useEffect(() => {
    box.current?.querySelector<HTMLElement>("select, input, button")?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const toggle = (id: number) => setChosen((ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id]));
  const title = model ? model.title ?? model.name : t ? t.model.title ?? t.model.name : "a model";

  const start = async () => {
    if (!t || usable.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<Started>("/downloads", { method: "POST", body: {
        model_id: t.model.id, machine_ids: usable, engine, ...(engine && t.recipe ? { recipe: t.recipe } : {}) } });
      const names = r.downloads.map((d) => d.machine?.name).filter(Boolean).join(", ");
      const skipped = r.skipped.length ? ` Not started on ${r.skipped.map((s) => `${s.name} (${s.reason})`).join("; ")}` : "";
      onStarted(`Downloading ${t.model.name}${engine && t.recipe ? ` and the ${t.recipe} engine` : ""} to ${names}.${skipped}`);
      onClose();
    } catch (e) {
      setError(errorText(e));
      targets.reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dlg panel" role="dialog" aria-modal="true" aria-labelledby="dlg-title" ref={box}>
        <div className="dlg-head">
          <h2 id="dlg-title"><IconDownload size={18} aria-hidden="true" /> Download {model ? title : "a model"} to…</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label="Close" onClick={onClose}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">
          The model goes into each machine's model cache now, so a deployment there starts without waiting for it. Nothing is
          started. A download that is stopped goes on from where it was when it is started again.
        </p>
        {!model && (
          <div className="field">
            <label htmlFor="dlg-model">Model</label>
            {catalog.error ? <p className="field-error">{catalog.error}</p> : (
              <select id="dlg-model" value={modelId ?? ""} onChange={(e) => { setPicked(Number(e.target.value)); setChosen(null); setRecipe(""); }}>
                {choices.length === 0 && <option value="">{catalog.data ? "The catalog has no model to download" : "Loading…"}</option>}
                {choices.map((m) => <option key={m.id} value={m.id}>{m.title ?? m.name} · {bytes(m.size_bytes)}</option>)}
              </select>
            )}
          </div>
        )}
        <div className="dlg-engine">
          <label className="check">
            <input type="checkbox" checked={engine} onChange={(e) => setEngine(e.target.checked)} />
            Also the engine:
          </label>
          <select aria-label="Engine" value={recipe || t?.recipe || ""} disabled={!engine || !t || t.recipes.length < 2}
            onChange={(e) => setRecipe(e.target.value)}>
            {(t?.recipes ?? []).map((r) => <option key={r} value={r}>{r}</option>)}
            {t && t.recipes.length === 0 && <option value="">no engine serves it</option>}
          </select>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        {targets.error && <Notice tone="error">{targets.error}</Notice>}
        {!t && !targets.error && <p className="muted small">Loading the machines…</p>}
        {t && t.machines.length === 0 && <p className="empty-line">No machine has joined yet.</p>}
        {t && t.machines.length > 0 && (
          <fieldset className="dlg-machines">
            <legend className="small muted">Machines</legend>
            {t.machines.map((x) => <TargetRow key={x.machine_id} x={x} on={usable.includes(x.machine_id)} onToggle={() => toggle(x.machine_id)} />)}
          </fieldset>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button type="button" className="btn btn-primary" disabled={busy || usable.length === 0 || !t?.can_download} onClick={() => void start()}>
            <IconDownload size={16} aria-hidden="true" />
            {busy ? "Starting…" : usable.length ? `Download to ${usable.length} machine${usable.length === 1 ? "" : "s"}` : "Download"}
          </button>
        </div>
      </div>
    </div>
  );
}

function TargetRow({ x, on, onToggle }: { x: DownloadTarget; on: boolean; onToggle: () => void }) {
  const can = pickable(x);
  const frac = x.download ? downloadFraction({ download: x.download.download }) : null;
  return (
    <label className={`dlg-target${can ? "" : " dlg-target-off"}`}>
      <input type="checkbox" checked={on} disabled={!can} onChange={onToggle} aria-describedby={`dlg-t-${x.machine_id}`} />
      <span className="dlg-target-main">
        <span className="dlg-target-name"><IconServer size={14} aria-hidden="true" /> <strong className="mono">{x.name}</strong>
          <span className={`chip chip-tone-${TARGET_TONE[x.state]}`}>{TARGET_LABEL[x.state]}</span></span>
        <small id={`dlg-t-${x.machine_id}`}>
          {x.state === "ok" || x.state === "cached" ? targetDetail(x) : x.note}
          {x.state === "ok" && x.note ? ` · ${x.note}` : ""}
        </small>
        {frac !== null && <span className="bar bar-dl" aria-hidden="true"><span style={{ width: `${Math.round(frac * 100)}%` }} /></span>}
      </span>
    </label>
  );
}

// Downloads in a list: on a model's row (one line per machine) or a machine's page (one per model).
export function DownloadList({ list, show, canCancel, onChanged, onError }: {
  list: ModelDownload[]; show: "machine" | "model"; canCancel: boolean;
  onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);
  if (list.length === 0) return null;
  const cancel = async (d: ModelDownload) => {
    const what = `${d.model?.name ?? "the model"} to ${d.machine?.name ?? "the machine"}`;
    if (!window.confirm(`Cancel the download of ${what}? What is on disk is kept: started again, it goes on from there.`)) return;
    setBusy(d.id);
    try {
      await api(`/downloads/${d.id}/cancel`, { method: "POST" });
      onChanged(`Cancelled the download of ${what}.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <ul className="dllist" aria-label="Downloads">
      {list.map((d) => {
        const frac = d.download ? downloadFraction({ download: d.download }) : null;
        const active = isActive(d);
        const Icon = d.download?.phase === "engine" || d.download?.phase === "image" ? IconPuzzle : show === "machine" ? IconServer : IconDownload;
        return (
          <li key={d.id} className={`dlrow dlrow-${d.state}`}>
            <span className="runlist-k" aria-hidden="true"><Icon size={15} stroke={1.7} /></span>
            <span className="dlrow-main">
              <strong className="mono">
                {show === "machine"
                  ? (d.machine ? <Link to={`/machines/${d.machine.id}`}>{d.machine.name}</Link> : "a removed machine")
                  : (d.model?.title ?? d.model?.name ?? "a removed model")}
              </strong>
              <small>{downloadLine(d)}</small>
              {active && frac !== null && (
                <span className="bar bar-dl" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(frac * 100)}
                  aria-label={`Download of ${d.model?.name ?? "the model"} to ${d.machine?.name ?? "the machine"}`}>
                  <span style={{ width: `${Math.round(frac * 100)}%` }} />
                </span>
              )}
            </span>
            <span className={`chip chip-tone-${d.state === "done" ? "ok" : d.state === "failed" ? "bad" : active ? "wait" : "off"}`}>{d.state}</span>
            {canCancel && active && (
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => void cancel(d)}
                aria-label={`Cancel the download to ${d.machine?.name ?? "the machine"}`}>
                {busy === d.id ? "Cancelling…" : "Cancel"}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
