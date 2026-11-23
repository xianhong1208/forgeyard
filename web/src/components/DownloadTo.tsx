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
import { modelHref } from "../lib/pages";
import type { CatalogModel, CatalogResponse, DownloadTarget, DownloadTargets, ModelDownload } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Notice } from "./ui";
import { useT } from "../i18n/react";

type Started = { downloads: ModelDownload[]; skipped: { machine_id: number; name: string; state: string; reason: string }[] };

// The dialog. `model`: the model to download (from the Library). Without it (from a machine's page) the
// person picks one; `machineId` is ticked when it can take the download.
export function DownloadDialog({ model, machineId, onClose, onStarted }: {
  model?: CatalogModel; machineId?: number; onClose: () => void; onStarted: (text: string) => void;
}) {
  const t = useT();
  const catalog = useApi<CatalogResponse>(model ? null : "/models");
  const choices = (catalog.data?.models ?? []).filter((m) => m.kind !== "local");
  const [picked, setPicked] = useState<number | null>(model?.id ?? null);
  const modelId = model?.id ?? picked ?? choices[0]?.id ?? null;
  const [engine, setEngine] = useState(true);
  const [recipe, setRecipe] = useState("");
  const query = modelId === null ? null
    : `/downloads/targets?model_id=${modelId}&engine=${engine}${recipe ? `&recipe=${encodeURIComponent(recipe)}` : ""}`;
  const targets = useApi<DownloadTargets>(query);
  const tg = targets.data && targets.data.model.id === modelId ? targets.data : null;
  const [chosen, setChosen] = useState<number[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Ticked at first: the machine the dialog was opened from, when it can take it.
  const ticked = chosen ?? (tg && machineId !== undefined && tg.machines.some((x) => x.machine_id === machineId && pickable(x)) ? [machineId] : []);
  const usable = tg ? ticked.filter((id) => tg.machines.some((x) => x.machine_id === id && pickable(x))) : [];

  useEffect(() => {
    box.current?.querySelector<HTMLElement>("select, input, button")?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const toggle = (id: number) => setChosen((ticked.includes(id) ? ticked.filter((x) => x !== id) : [...ticked, id]));
  const title = model ? model.title ?? model.name : tg ? tg.model.title ?? tg.model.name : t("machine.dlg.aModel");

  const start = async () => {
    if (!tg || usable.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<Started>("/downloads", { method: "POST", body: {
        model_id: tg.model.id, machine_ids: usable, engine, ...(engine && tg.recipe ? { recipe: tg.recipe } : {}) } });
      const names = r.downloads.map((d) => d.machine?.name).filter(Boolean).join(", ");
      const skipped = r.skipped.length ? t("machine.dlg.skipped", { list: r.skipped.map((s) => `${s.name} (${s.reason})`).join("; ") }) : "";
      onStarted(t("machine.dlg.started", { model: tg.model.name, engine: engine && tg.recipe ? t("machine.dlg.andEngine", { recipe: tg.recipe }) : "",
        names, skipped }));
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
          <h2 id="dlg-title"><IconDownload size={18} aria-hidden="true" /> {model ? t.rich("machine.dlg.title", { what: <Link to={modelHref(model.name)} onClick={onClose}>{title}</Link> })
            : t("machine.dlg.titleAny")}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label={t("common.close")} onClick={onClose}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">{t("machine.dlg.lead")}</p>
        {!model && (
          <div className="field">
            <label htmlFor="dlg-model">{t("machine.dlg.model")}</label>
            {catalog.error ? <p className="field-error">{catalog.error}</p> : (
              <select id="dlg-model" value={modelId ?? ""} onChange={(e) => { setPicked(Number(e.target.value)); setChosen(null); setRecipe(""); }}>
                {choices.length === 0 && <option value="">{catalog.data ? t("machine.dlg.noModel") : t("common.loading")}</option>}
                {choices.map((m) => <option key={m.id} value={m.id}>{m.title ?? m.name} · {bytes(m.size_bytes)}</option>)}
              </select>
            )}
          </div>
        )}
        <div className="dlg-engine">
          <label className="check">
            <input type="checkbox" checked={engine} onChange={(e) => setEngine(e.target.checked)} />
            {t("machine.dlg.alsoEngine")}
          </label>
          <select aria-label={t("machine.dlg.engine")} value={recipe || tg?.recipe || ""} disabled={!engine || !tg || tg.recipes.length < 2}
            onChange={(e) => setRecipe(e.target.value)}>
            {(tg?.recipes ?? []).map((r) => <option key={r} value={r}>{r}</option>)}
            {tg && tg.recipes.length === 0 && <option value="">{t("machine.dlg.noEngine")}</option>}
          </select>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        {targets.error && <Notice tone="error">{targets.error}</Notice>}
        {!tg && !targets.error && <p className="muted small">{t("machine.dlg.loadingMachines")}</p>}
        {tg && tg.machines.length === 0 && <p className="empty-line">{t("machine.dlg.noMachines")}</p>}
        {tg && tg.machines.length > 0 && (
          <fieldset className="dlg-machines">
            <legend className="small muted">{t("shell.nav.machines")}</legend>
            {tg.machines.map((x) => <TargetRow key={x.machine_id} x={x} on={usable.includes(x.machine_id)} onToggle={() => toggle(x.machine_id)} />)}
          </fieldset>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={onClose}>{t("common.cancel")}</button>
          <button type="button" className="btn btn-primary" disabled={busy || usable.length === 0 || !tg?.can_download} onClick={() => void start()}>
            <IconDownload size={16} aria-hidden="true" />
            {busy ? t("machine.dlg.starting") : usable.length ? t.plural("machine.dlg.downloadTo", usable.length) : t("machine.dlg.download")}
          </button>
        </div>
      </div>
    </div>
  );
}

function TargetRow({ x, on, onToggle }: { x: DownloadTarget; on: boolean; onToggle: () => void }) {
  useT();   // TARGET_LABEL and targetDetail read the language now: follow a change
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
  const t = useT();
  const [busy, setBusy] = useState<number | null>(null);
  if (list.length === 0) return null;
  const pair = (d: ModelDownload) => ({ model: d.model?.name ?? t("machine.dl.theModel"), machine: d.machine?.name ?? t("machine.dl.theMachine") });
  const cancel = async (d: ModelDownload) => {
    if (!window.confirm(t("machine.dl.cancelQuestion", pair(d)))) return;
    setBusy(d.id);
    try {
      await api(`/downloads/${d.id}/cancel`, { method: "POST" });
      onChanged(t("machine.dl.cancelledOne", pair(d)));
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <ul className="dllist" aria-label={t("machine.cache.downloads")}>
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
                  ? (d.machine ? <Link to={`/machines/${d.machine.id}`}>{d.machine.name}</Link> : t("machine.dl.removedMachine"))
                  : d.model ? <Link to={modelHref(d.model.name)}>{d.model.title ?? d.model.name}</Link> : t("machine.dl.removedModel")}
              </strong>
              <small>{downloadLine(d)}</small>
              {active && frac !== null && (
                <span className="bar bar-dl" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(frac * 100)}
                  aria-label={t("machine.dl.progressLabel", pair(d))}>
                  <span style={{ width: `${Math.round(frac * 100)}%` }} />
                </span>
              )}
            </span>
            <span className={`chip chip-tone-${d.state === "done" ? "ok" : d.state === "failed" ? "bad" : active ? "wait" : "off"}`}>{t.maybe(`machine.dl.state.${d.state}`, d.state)}</span>
            {canCancel && active && (
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={() => void cancel(d)}
                aria-label={t("machine.dl.cancelLabel", { machine: pair(d).machine })}>
                {busy === d.id ? t("machine.dl.cancelling") : t("common.cancel")}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
