import { IconCpu, IconLock, IconPhoto, IconPlus } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { AbilityChips } from "../components/AbilityChips";
import { TrialCardChip } from "../components/EngineTrials";
import { DownloadDialog, DownloadList } from "../components/DownloadTo";
import { CheckBar, UpdateBadge } from "../components/EngineUpdates";
import { EngineImage } from "../components/EngineImage";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBlock, CopyBox, Notice, PageHeader } from "../components/ui";
import { useT } from "../i18n/react";
import { AddModel } from "./AddModel";
import { api, errorText } from "../lib/api";
import { isActive, recentDownloads } from "../lib/downloads";
import { engineCarry } from "../lib/engineImages";
import { imageTag, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { HARDWARE_LABEL, engineDescription, engineHardware, engineRuns, freeName, renameRecipe } from "../lib/engines";
import { engineHref, modelHref } from "../lib/pages";
import { TASK_LABEL, mediaText, params, shortSource, taskLabel } from "../lib/models";
import type { CatalogModel, CatalogResponse, DownloadsResponse, EngineUpdate, EngineUpdatesResponse, ModelDownload, ModelSources, Recipe,
  RecipeVariant } from "../lib/types";
import { useApi } from "../lib/useApi";

// 1.4 (#92): Models is the model library: what can be deployed (Catalog), how each kind of model
// runs (Engines: recipes in the API and in recipe files) and, for admins, where models may come from
// (Sources). What runs now is on Serving.
export type LibraryTab = "catalog" | "engines" | "sources";

// The tab in the address. Sources is for admins: anyone else gets the Catalog. 1.3's Models page
// showed the deployments with no tab or ?tab=deployments and the routes at ?tab=routes: those open
// Serving (null here). The names used only while 1.4 was made (serving, library, model-library) are
// gone, so they land on the Catalog like any unknown tab.
export function libraryTab(raw: string | null, admin: boolean): LibraryTab | null {
  if (raw === "deployments" || raw === "routes") return null;
  if (raw === "engines") return "engines";
  if (raw === "sources" && admin) return "sources";
  return "catalog";
}

export function ModelsPage() {
  const [search] = useSearchParams();
  const { user } = useAuth();
  const tab = libraryTab(search.get("tab"), user?.role === "admin");
  if (tab === null) return <Navigate to="/serving" replace />;
  return <Library tab={tab} />;
}

function Library({ tab }: { tab: LibraryTab }) {
  const [, setSearch] = useSearchParams();
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const t = useT();
  const cat = useApi<CatalogResponse>("/models");
  const tabs: { id: LibraryTab; label: string }[] = [
    { id: "catalog", label: cat.data ? t("library.tab.catalogCount", { count: String(cat.data.models.length) }) : t("library.tab.catalog") },
    { id: "engines", label: t("library.tab.engines") },
    ...(admin ? [{ id: "sources" as const, label: t("library.tab.sources") }] : []),
  ];
  return (
    <>
      <PageHeader
        index={t("library.index")}
        title={t("library.title")}
        lead={t("library.lead")}
        actions={cat.data?.can_deploy && (
          <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> {t("library.deploy")}</Link>
        )}
      />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="5-add-a-recommended-model">{t("library.help.first")}</HelpLink>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">{t("library.help.concepts")}</HelpLink>
        <HelpLink page="how-to" anchor="download-a-model-before-deploying-it">{t("library.help.download")}</HelpLink>
      </HelpLinks>
      <div className="tabs" role="tablist" aria-label={t("library.title")}>
        {tabs.map((x) => (
          <button key={x.id} type="button" role="tab" aria-selected={tab === x.id} className={tab === x.id ? "on" : ""}
            onClick={() => setSearch(x.id === "catalog" ? {} : { tab: x.id })}>{x.label}</button>
        ))}
      </div>
      {cat.error ? <Notice tone="error">{cat.error}</Notice>
        : !cat.data ? <p className="muted">{t("common.loading")}</p>
          : tab === "catalog" ? <Catalog data={cat.data} reload={cat.reload} />
            : tab === "engines" ? <Engines data={cat.data} reload={cat.reload} />
              : cat.data.sources ? <Sources sources={cat.data.sources} onSaved={cat.reload} /> : null}
    </>
  );
}

// 1.4: the downloads ahead of a deployment ("Download to…"), polled while one is under way.
const DOWNLOADS_POLL_MS = 2000;

function Catalog({ data, reload }: { data: CatalogResponse; reload: () => void }) {
  const t = useT();
  const dl = useApi<DownloadsResponse>("/downloads");
  const [dialog, setDialog] = useState<CatalogModel | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const running = (dl.data?.downloads ?? []).some(isActive);
  const reloadDl = dl.reload;
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => { if (document.visibilityState !== "hidden") reloadDl(); }, DOWNLOADS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, reloadDl]);
  const byModel = (id: number): ModelDownload[] => recentDownloads((dl.data?.downloads ?? []).filter((d) => d.model?.id === id), "machine");
  const said = (tone: "ok" | "error") => (text: string) => { setNote({ tone, text }); reloadDl(); };
  return (
    <>
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {dialog && <DownloadDialog model={dialog} onClose={() => setDialog(null)} onStarted={said("ok")} />}
      {data.can_manage && <AddModel onAdded={reload} empty={data.models.length === 0} />}
      <section className="panel" aria-labelledby="catalog-title">
        <div className="panel-head"><h2 id="catalog-title">{t("library.tab.catalog")}</h2><span className="muted small">{t.plural("library.models", data.models.length)}</span></div>
        {data.models.length === 0 ? (
          <p className="muted small catalog-empty">{data.can_manage ? t("library.catalog.emptyManage") : t("library.catalog.emptyUser")}</p>
        ) : (
          <div className="table-wrap catalog-wrap">
            <table className="table catalog-table">
              <thead><tr><th>{t("library.col.model")}</th><th>{t("library.col.task")}</th><th>{t("library.col.format")}</th><th>{t("library.col.size")}</th>
                <th>{t("library.col.architecture")}</th><th>{t("library.col.licence")}</th><th>{t("library.col.engines")}</th><th /></tr></thead>
              <tbody>{data.models.map((m) => (
                <ModelRow key={m.id} m={m} canManage={data.can_manage} canDeploy={data.can_deploy} reload={reload}
                  downloads={byModel(m.id)} onDownload={() => setDialog(m)} onChanged={said("ok")} onError={said("error")} />
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

// The engines (recipes in the API and in recipe files): how each kind of model runs, per hardware.
// Admins add one (from scratch or from a copy of a built-in engine), and change or delete their own.
type EngineFormFor = { mode: "add" } | { mode: "edit"; name: string };

function Engines({ data, reload }: { data: CatalogResponse; reload: () => void }) {
  const t = useT();
  const [form, setForm] = useState<EngineFormFor | null>(null);
  const [note, setNote] = useState<string | null>(null);
  // 1.5.2 (#95): the last check for newer versions (everyone reads it; admins check again).
  const updates = useApi<EngineUpdatesResponse>("/engine-updates");
  const [checked, setChecked] = useState<EngineUpdatesResponse | null>(null);
  const upd = checked ?? updates.data;
  const done = (text: string) => { setForm(null); setNote(text); reload(); };
  const onChecked = (r: NonNullable<EngineUpdatesResponse["result"]>) => {
    setChecked({ result: r, can_check: true });
    const newer = Object.values(r.engines).filter((e) => e.candidates.length > 0).length;
    setNote(newer ? t("engineupdates.checked", { count: String(newer) }) : t("engineupdates.checkedNone"));
  };
  return (
    <>
      {note && <Notice tone="ok">{note}</Notice>}
      {data.can_manage && !form && (
        <p className="add-model-open">
          <button type="button" className="btn" onClick={() => { setNote(null); setForm({ mode: "add" }); }}>
            <IconPlus size={16} aria-hidden="true" /> {t("library.addEngine")}
          </button>
        </p>
      )}
      {form && <EngineForm key={form.mode === "edit" ? form.name : "add"} edit={form.mode === "edit" ? form.name : null}
        recipes={data.recipes} onDone={done} onCancel={() => setForm(null)} />}
      <section className="panel" aria-labelledby="engines-title">
        <div className="panel-head"><h2 id="engines-title">{t("library.tab.engines")}</h2><span className="muted small">{t("library.engines.lead")}</span></div>
        <CheckBar data={upd} onChecked={onChecked} />
        <div className="recipes">{data.recipes.map((r) => (
          <EngineCard key={r.name} r={r} canManage={data.can_manage} update={r.builtin ? upd?.result?.engines[r.name] : undefined}
            onEdit={() => { setNote(null); setForm({ mode: "edit", name: r.name }); }} onDeleted={done} />
        ))}</div>
      </section>
    </>
  );
}

function ModelRow({ m, canManage, canDeploy, reload, downloads, onDownload, onChanged, onError }: {
  m: CatalogModel; canManage: boolean; canDeploy: boolean; reload: () => void; downloads: ModelDownload[];
  onDownload: () => void; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const src = shortSource(m);
  const remove = async () => {
    if (!window.confirm(t("library.removeConfirm", { name: m.name }))) return;
    try {
      await api(`/models/${m.id}`, { method: "DELETE" });
      reload();
    } catch (e) {
      setError(errorText(e));
    }
  };
  return (
    <>
    <tr className={downloads.length ? "has-dl" : undefined}>
      <td data-label={t("library.col.model")} className="cat-model">
        <div className="cell-stack">
        <div className="strong"><Link to={modelHref(m.name)} className="name-link">{m.title ?? m.name}</Link></div>
        <div className="mono small muted catalog-src" title={src.full}>{src.text}{m.mmproj ? t("library.projector") : ""}</div>
        {error && <div className="field-error">{error}</div>}
        </div>
      </td>
      <td data-label={t("library.col.task")} className="cat-task">
        <div className="chip-row">
          <span className="chip">{taskLabel(m)}</span>
          {m.modalities && m.modalities.length > 0 && (
            <span className="chip chip-tone-run" title={t("library.takesTitle")}><IconPhoto size={11} aria-hidden="true" /> {mediaText(m.modalities)}</span>
          )}
          <AbilityChips m={m} />
        </div>
      </td>
      <td data-label={t("library.col.format")} className="mono small cat-format">{m.format}</td>
      <td data-label={t("library.col.size")} className="small cat-size">{bytes(m.size_bytes)}</td>
      <td data-label={t("library.col.architecture")} className="small cat-arch">
        {m.summary.architecture ?? "—"}{m.summary.params ? t("library.paramsSuffix", { params: params(m.summary.params) }) : ""}
        {m.vision?.zero_shot ? <div className="muted" title={t("library.zeroShotTitle")}>{t("library.findsWhatYouName")}</div>
          : m.vision?.labels ? <div className="muted" title={m.vision.label_examples?.join(", ")}>{t("library.classes", { count: String(m.vision.labels) })}</div> : null}
        {m.summary.layers ? <div className="muted">{t("library.layers", { count: String(m.summary.layers) })}{m.summary.kv_heads ? t("library.kvHeads", { count: String(m.summary.kv_heads) }) : ""}{m.summary.max_context ? t("library.ctx", { count: String(m.summary.max_context) }) : ""}</div> : null}
      </td>
      <td data-label={t("library.col.licence")} className="small cat-licence">
        <div className="cell-stack">
          <span className="mono nowrap" title={m.license_from ? t("library.licenceFrom", { source: m.license_from.replace("hf://", "") }) : undefined}>{m.license ?? "—"}{m.license_from ? "*" : ""}</span>
          {m.gated && <span className="muted"><IconLock size={11} aria-hidden="true" /> {t("library.gated")}</span>}
        </div>
      </td>
      <td data-label={t("library.col.engines")} className="mono small cat-engines">{m.recipes.length
        ? m.recipes.map((r, i) => <span key={r}>{i ? ", " : ""}<Link to={engineHref(r)}>{r}</Link></span>) : t("library.noneFits")}</td>
      <td className="cell-actions">
        {/* One column of actions at desktop widths, so the table never clips them (1.4, #92). */}
        <div className="catalog-actions" role="group" aria-label={t("library.actionsFor", { name: m.name })}>
        {canDeploy && m.recipes.length > 0 && <Link className="btn btn-small" to={`/serving/deploy?model=${encodeURIComponent(m.name)}`}>{t("library.deployShort")}</Link>}
        {canDeploy && m.kind !== "local" && (
          <button type="button" className="btn btn-quiet btn-small" onClick={onDownload}
            title={t("library.downloadToTitle", { name: m.name })}>{t("library.downloadTo")}</button>
        )}
        {canManage && <button type="button" className="btn btn-quiet btn-small" disabled={m.deployments > 0}
          title={m.deployments ? t("library.deployedFirst") : undefined} onClick={() => void remove()}>{t("library.remove")}</button>}
        </div>
      </td>
    </tr>
    {downloads.length > 0 && (
      <tr className="dl-subrow">
        <td colSpan={8}>
          <DownloadList list={downloads} show="machine" canCancel={canDeploy} onChanged={onChanged} onError={onError} />
        </td>
      </tr>
    )}
    </>
  );
}

// 1.4 (#92): a short card (name, version, what it is, the hardware it runs on, what it serves); the
// variants, their images and requirements, and the offline commands are behind Details.
function EngineCard({ r, canManage, update, onEdit, onDeleted }: {
  r: Recipe; canManage: boolean; update?: EngineUpdate; onEdit: () => void; onDeleted: (text: string) => void;
}) {
  const t = useT();
  const [error, setError] = useState<string | null>(null);
  const desc = engineDescription(r);
  const serves = t("library.serves", { tasks: r.tasks.map((x) => TASK_LABEL[x]).join(t("library.listSep")) });
  const remove = async () => {
    if (!window.confirm(t("library.deleteEngineConfirm", { name: r.name }))) return;
    setError(null);
    try {
      await api(`/recipes/${encodeURIComponent(r.name)}`, { method: "DELETE" });
      onDeleted(t("library.engineDeleted", { name: r.name }));
    } catch (e) { setError(errorText(e)); }
  };
  return (
    <article className="recipe engine-card" aria-label={t("library.engineLabel", { name: r.name })}>
      {/* Every closed card has the same height (1.4, #92): one header row, two lines of description, one
          row of chips, one line of tasks, and the Details row at the bottom. Only an open card grows. */}
      <header className="engine-head">
        <h3 className="mono" title={r.name}><Link to={engineHref(r.name)} className="name-link">{r.name}</Link></h3>
        <span className="chip engine-version" title={`${r.engine} ${r.version}`}>{r.engine} {r.version}</span>
        {!r.builtin && <span className="chip chip-tone-run engine-custom">{t("library.custom")}</span>}
      </header>
      {/* 1.5.2 (#189): the image it runs (or its program), one line on every card so they keep one height. */}
      <p className="engine-image-line"><EngineImage runs={engineRuns(r.variants)} /></p>
      <p className="small engine-desc" title={desc}>{desc}</p>
      <div className="engine-facts" aria-label={t("library.runsOn", { list: engineHardware(r).map((h) => HARDWARE_LABEL[h]).join(", ") })}>
        {engineHardware(r).map((h) => (
          <span key={h} className={`vendor vendor-${h}`}>{h === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{HARDWARE_LABEL[h]}</span>
        ))}
        {r.share === "vram" && <span className="chip chip-tone-wait">{t("library.sharesGpu")}</span>}
      </div>
      <p className="small muted engine-serves" title={serves}>{serves}</p>
      <div className="engine-foot">
        <details className="engine-details">
          <summary className="small">{t("library.details")}</summary>
          <p className="small muted">{t("library.formatsEstimate", { formats: r.formats.join(", "), estimate: r.estimate })}</p>
          <ul className="variants" aria-label={t("library.variantsOf", { name: r.name })}>
            {r.variants.map((v) => (
              <li key={v.name}>
                <span className={`vendor vendor-${v.vendor}`}>{v.vendor === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{v.vendor.toUpperCase()}</span>
                <span className="mono small variant-what" title={v.runtime === "container" ? v.image ?? undefined : undefined}>{v.runtime === "container" ? imageTag(v.image) : t("library.process", { engine: v.engine?.split("/").pop() ?? "" })}</span>
                <span className="muted small">{[v.compute_capability && t("library.cc", { value: v.compute_capability }), v.driver && t("library.driver", { value: v.driver }), v.arch && v.arch.join(" ")].filter(Boolean).join(" · ")}</span>
              </li>
            ))}
          </ul>
          <OfflineImages name={r.name} variants={r.variants} />
        </details>
        <UpdateBadge u={update} />
        <TrialCardChip last={r.last_trial} running={r.trial_running} recipe={r.name} />
        {canManage && !r.builtin && (
          <div className="engine-actions">
            <button type="button" className="btn btn-quiet btn-small" onClick={onEdit}>{t("library.edit")}</button>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => void remove()}>{t("library.delete")}</button>
          </div>
        )}
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
    </article>
  );
}

// The commands that carry a container image to a machine that cannot reach the registry, for the
// variant picked (one at a time: the recipe card is narrow and the commands are long).
export function OfflineImages({ name, variants }: { name: string; variants: RecipeVariant[] }) {
  const t = useT();
  const byImage = new Map<string, { names: string[]; checked: boolean }>();
  for (const v of variants) {
    if (v.runtime !== "container" || !v.image) continue;
    const entry = byImage.get(v.image) ?? { names: [], checked: Boolean(v.image_check) };
    entry.names.push(v.name);
    byImage.set(v.image, entry);
  }
  const images = [...byImage];
  const [picked, setPicked] = useState(0);
  if (images.length === 0) return null;
  const [image, e] = images[Math.min(picked, images.length - 1)];
  const carry = engineCarry(image);
  const what = e.names.join(", ");
  const id = `offline-${name}`;
  return (
    <details className="docker-advanced recipe-offline">
      <summary className="small">{t("library.offline.summary")}</summary>
      <p className="small">
        {t.rich("library.offline.lead", { link: (text) => <HelpLink page="how-to" anchor="offline-machines">{text}</HelpLink> })}
      </p>
      {images.length > 1 && (
        <div className="field recipe-offline-pick">
          <label htmlFor={id}>{t("library.offline.variant")}</label>
          <select id={id} value={Math.min(picked, images.length - 1)} onChange={(ev) => setPicked(Number(ev.target.value))}>
            {images.map(([img, x], n) => <option key={img} value={n}>{x.names.join(", ")} · {imageTag(img).split("/").pop()}</option>)}
          </select>
        </div>
      )}
      <p className="small mono muted recipe-offline-image">{imageTag(image)}</p>
      <CopyBlock value={carry.save} label={t("library.offline.save", { what })} />
      <CopyBox value={carry.load} label={t("library.offline.load", { what })} />
      <p className="small muted">
        {e.checked ? t("library.offline.checked") : t.rich("library.offline.unchecked", { digest: <code>@sha256:…</code> })}
      </p>
    </details>
  );
}

// Add a custom engine, or change a custom one: its recipe as YAML. A new one can start from a copy of a
// built-in engine under a free name (`from`: that copy at once, as an engine's page's "Copy as a custom engine" does).
// The server checks the YAML; its message is shown here. onDone gets the message and the engine's name.
export function EngineForm({ edit, from, recipes, onDone, onCancel }: {
  edit: string | null; from?: string; recipes: Recipe[]; onDone: (text: string, name: string) => void; onCancel: () => void;
}) {
  const t = useT();
  const [text, setText] = useState("");
  const [start, setStart] = useState(from ?? "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(edit !== null);
  const [error, setError] = useState<string | null>(null);
  const builtins = recipes.filter((r) => r.builtin);
  useEffect(() => {
    if (edit === null) return;
    let alive = true;
    api<{ recipe: Recipe }>(`/recipes/${encodeURIComponent(edit)}`).then(
      (r) => { if (alive) { setText(r.recipe.yaml ?? ""); setLoading(false); } },
      (e) => { if (alive) { setError(errorText(e)); setLoading(false); } },
    );
    return () => { alive = false; };
  }, [edit]);
  const copy = async (name: string) => {
    setStart(name);
    setError(null);
    if (!name) { setText(""); return; }
    try {
      const r = await api<{ recipe: Recipe }>(`/recipes/${encodeURIComponent(name)}`);
      setText(renameRecipe(r.recipe.yaml ?? "", freeName(`${name}-custom`, recipes.map((x) => x.name))));
    } catch (e) { setError(errorText(e)); }
  };
  const first = useRef(from);
  useEffect(() => {
    if (edit === null && first.current) void copy(first.current);
    first.current = undefined;
    // Once, when the form opens with a copy to start from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ recipe: Recipe }>(edit === null ? "/recipes" : `/recipes/${encodeURIComponent(edit)}`,
        { method: edit === null ? "POST" : "PUT", body: { yaml: text } });
      onDone(t(edit === null ? "library.engineAdded" : "library.engineSaved", { name: r.recipe.name }), r.recipe.name);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <section className="panel engine-form" aria-labelledby="engine-form-title">
      <div className="panel-head">
        <h2 id="engine-form-title">{edit === null ? t("library.addEngine") : t("library.form.change", { name: edit })}</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>{t("common.close")}</button>
      </div>
      {edit === null && <p className="engine-form-lead">{t("library.form.lead")}</p>}
      <p className="muted small">
        {t.rich("library.form.what", { link: (x) => <HelpLink page="reference" anchor="built-in-recipes">{x}</HelpLink> })}
      </p>
      {edit === null && (
        <div className="field">
          <label htmlFor="engine-start">{t("library.form.start")}</label>
          <select id="engine-start" value={start} onChange={(e) => void copy(e.target.value)}>
            <option value="">{t("library.form.empty")}</option>
            {builtins.map((r) => <option key={r.name} value={r.name}>{r.name} ({r.engine} {r.version})</option>)}
          </select>
          <p className="field-hint">{t("library.form.startHint")}</p>
        </div>
      )}
      <div className={`field${error ? " has-error" : ""}`}>
        <label htmlFor="engine-yaml">{t("library.form.yaml")}</label>
        <textarea id="engine-yaml" className="mono" rows={16} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
          disabled={loading} aria-invalid={error ? true : undefined} aria-describedby={error ? "engine-yaml-error" : undefined} />
        {error && <p className="field-error" id="engine-yaml-error" role="alert">{error}</p>}
      </div>
      <div className="row-gap">
        <button type="button" className="btn btn-primary" disabled={busy || loading || !text.trim()} onClick={() => void save()}>
          {busy ? t("library.form.saving") : edit === null ? t("library.form.add") : t("library.form.save")}
        </button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </section>
  );
}

function Sources({ sources, onSaved }: { sources: ModelSources; onSaved: () => void }) {
  const [hosts, setHosts] = useState(sources.url_hosts.join("\n"));
  const [dirs, setDirs] = useState(sources.local_dirs.join("\n"));
  const t = useT();
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const save = async () => {
    try {
      await api("/model-sources", { method: "PUT", body: { url_hosts: hosts.split("\n"), local_dirs: dirs.split("\n") } });
      setMsg({ tone: "ok", text: t("library.sources.saved") }); onSaved();
    } catch (e) { setMsg({ tone: "error", text: errorText(e) }); }
  };
  const seeds = sources.env_seeds ?? [];
  return (
    <section className="panel" aria-labelledby="sources-title">
      <h2 id="sources-title">{t("library.tab.sources")}</h2>
      <p className="muted small">{t.rich("library.sources.where", { endpoint: <code>{sources.hf_endpoint}</code>,
        except: sources.allow_private_hosts ? t("library.sources.except") : "" })}</p>
      {seeds.length > 0 && (
        <p className="from-env small" role="note">
          <span className="chip chip-tone-wait">{t("library.sources.fromEnv")}</span>
          <span>{t.rich("library.sources.seeds", { names: seeds.map((n, i) => <span key={n}>{i ? ", " : ""}<code>{n}</code></span>) })}</span>
        </p>
      )}
      <div className="grid-2">
        <div className="field"><label htmlFor="s-hosts">{t("library.sources.hosts")}</label>
          <textarea id="s-hosts" className="mono" rows={3} value={hosts} onChange={(e) => setHosts(e.target.value)} /></div>
        <div className="field"><label htmlFor="s-dirs">{t("library.sources.dirs")}</label>
          <textarea id="s-dirs" className="mono" rows={3} value={dirs} onChange={(e) => setDirs(e.target.value)} />
          <p className="field-hint">{t("library.sources.dirsHint")}</p></div>
      </div>
      {msg && <div style={{ marginTop: 12 }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      <div className="row-gap" style={{ marginTop: 12 }}><button type="button" className="btn" onClick={() => void save()}>{t("library.sources.save")}</button></div>
      <HfToken info={sources.hf_token} onSaved={onSaved} />
    </section>
  );
}

// 1.3.1 (D2): one Hugging Face token for the server, for gated models. Write-only: only whether one is
// set and its last four characters ever come back.
function HfToken({ info, onSaved }: { info?: ModelSources["hf_token"]; onSaved: () => void }) {
  const t = useT();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const send = async (method: "PUT" | "DELETE") => {
    setBusy(true);
    setMsg(null);
    try {
      await api("/hf-token", method === "PUT" ? { method, body: { token: token.trim() } } : { method });
      setToken("");
      setMsg({ tone: "ok", text: t(method === "PUT" ? "library.hf.saved" : "library.hf.removed") });
      onSaved();
    } catch (e) { setMsg({ tone: "error", text: errorText(e) }); } finally { setBusy(false); }
  };
  return (
    <div className="hf-token">
      <h3 id="hf-token-title">{t("library.hf.title")}</h3>
      <p className="muted small">{t("library.hf.lead")}</p>
      <p className="small hf-token-state">
        {info?.set
          ? <>{t.rich("library.hf.set", { chip: (x) => <span className="chip chip-tone-ok">{x}</span>, last4: <code>…{info.last4}</code> })}{info.updated_by ? t("library.hf.by", { name: info.updated_by }) : null}{info.updated_at ? <> · {when(info.updated_at)}</> : null}</>
          : t.rich("library.hf.notSet", { chip: (x) => <span className="chip">{x}</span> })}
      </p>
      <form className="token-row" onSubmit={(e) => { e.preventDefault(); if (token.trim()) void send("PUT"); }}>
        <div className="field">
          <label htmlFor="hf-token">{info?.set ? t("library.hf.replace") : t("library.hf.token")}</label>
          <input id="hf-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="hf_…" />
          <p className="field-hint">{t("library.hf.hint")}</p>
        </div>
        <div className="row-gap">
          <button type="submit" className="btn" disabled={busy || !token.trim()}>{t("library.hf.save")}</button>
          {info?.set && <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void send("DELETE")}>{t("library.hf.remove")}</button>}
        </div>
      </form>
      {msg && <div style={{ marginTop: 10 }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
    </div>
  );
}
