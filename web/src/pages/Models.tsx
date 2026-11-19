import { IconCpu, IconLock, IconPhoto, IconPlus } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { DownloadDialog, DownloadList } from "../components/DownloadTo";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBlock, CopyBox, Notice, PageHeader } from "../components/ui";
import { AddModel } from "./AddModel";
import { api, errorText } from "../lib/api";
import { isActive, recentDownloads } from "../lib/downloads";
import { engineCarry } from "../lib/engineImages";
import { imageTag, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { HARDWARE_LABEL, engineHardware, freeName, renameRecipe } from "../lib/engines";
import { engineHref, modelHref } from "../lib/pages";
import { TASK_LABEL, mediaText, params, shortSource } from "../lib/models";
import type { CatalogModel, CatalogResponse, DownloadsResponse, ModelDownload, ModelSources, Recipe, RecipeVariant } from "../lib/types";
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
  const cat = useApi<CatalogResponse>("/models");
  const tabs: { id: LibraryTab; label: string }[] = [
    { id: "catalog", label: `Catalog${cat.data ? ` (${cat.data.models.length})` : ""}` },
    { id: "engines", label: "Engines" },
    ...(admin ? [{ id: "sources" as const, label: "Sources" }] : []),
  ];
  return (
    <>
      <PageHeader
        index="03 · Models"
        title="Models"
        lead="The model library: the models you can deploy, the engines that run them and, for admins, where models may come from. What runs now is on Serving."
        actions={cat.data?.can_deploy && (
          <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>
        )}
      />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="5-add-a-recommended-model">How do I deploy my first model?</HelpLink>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">Models, engines and estimates</HelpLink>
        <HelpLink page="how-to" anchor="download-a-model-before-deploying-it">Download a model before deploying it</HelpLink>
      </HelpLinks>
      <div className="tabs" role="tablist" aria-label="Models">
        {tabs.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} className={tab === t.id ? "on" : ""}
            onClick={() => setSearch(t.id === "catalog" ? {} : { tab: t.id })}>{t.label}</button>
        ))}
      </div>
      {cat.error ? <Notice tone="error">{cat.error}</Notice>
        : !cat.data ? <p className="muted">Loading…</p>
          : tab === "catalog" ? <Catalog data={cat.data} reload={cat.reload} />
            : tab === "engines" ? <Engines data={cat.data} reload={cat.reload} />
              : cat.data.sources ? <Sources sources={cat.data.sources} onSaved={cat.reload} /> : null}
    </>
  );
}

// 1.4: the downloads ahead of a deployment ("Download to…"), polled while one is under way.
const DOWNLOADS_POLL_MS = 2000;

function Catalog({ data, reload }: { data: CatalogResponse; reload: () => void }) {
  const dl = useApi<DownloadsResponse>("/downloads");
  const [dialog, setDialog] = useState<CatalogModel | null>(null);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const running = (dl.data?.downloads ?? []).some(isActive);
  const reloadDl = dl.reload;
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => { if (document.visibilityState !== "hidden") reloadDl(); }, DOWNLOADS_POLL_MS);
    return () => window.clearInterval(t);
  }, [running, reloadDl]);
  const byModel = (id: number): ModelDownload[] => recentDownloads((dl.data?.downloads ?? []).filter((d) => d.model?.id === id), "machine");
  const said = (tone: "ok" | "error") => (text: string) => { setNote({ tone, text }); reloadDl(); };
  return (
    <>
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {dialog && <DownloadDialog model={dialog} onClose={() => setDialog(null)} onStarted={said("ok")} />}
      {data.can_manage && <AddModel onAdded={reload} empty={data.models.length === 0} />}
      <section className="panel" aria-labelledby="catalog-title">
        <div className="panel-head"><h2 id="catalog-title">Catalog</h2><span className="muted small">{data.models.length} model{data.models.length === 1 ? "" : "s"}</span></div>
        {data.models.length === 0 ? (
          <p className="muted small catalog-empty">{data.can_manage
            ? "The catalog lists the models that can be deployed. It is empty: pick one of the recommended models above, or search Hugging Face."
            : "The catalog lists the models that can be deployed. It is empty: an admin adds models."}</p>
        ) : (
          <div className="table-wrap catalog-wrap">
            <table className="table catalog-table">
              <thead><tr><th>Model</th><th>Task</th><th>Format</th><th>Size</th><th>Architecture</th><th>Licence</th><th>Engines</th><th /></tr></thead>
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
  const [form, setForm] = useState<EngineFormFor | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const done = (text: string) => { setForm(null); setNote(text); reload(); };
  return (
    <>
      {note && <Notice tone="ok">{note}</Notice>}
      {data.can_manage && !form && (
        <p className="add-model-open">
          <button type="button" className="btn" onClick={() => { setNote(null); setForm({ mode: "add" }); }}>
            <IconPlus size={16} aria-hidden="true" /> Add a custom engine
          </button>
        </p>
      )}
      {form && <EngineForm key={form.mode === "edit" ? form.name : "add"} edit={form.mode === "edit" ? form.name : null}
        recipes={data.recipes} onDone={done} onCancel={() => setForm(null)} />}
      <section className="panel" aria-labelledby="engines-title">
        <div className="panel-head"><h2 id="engines-title">Engines</h2><span className="muted small">how each kind of model runs, per hardware</span></div>
        <div className="recipes">{data.recipes.map((r) => (
          <EngineCard key={r.name} r={r} canManage={data.can_manage}
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
  const [error, setError] = useState<string | null>(null);
  const src = shortSource(m);
  const remove = async () => {
    if (!window.confirm(`Remove ${m.name} from the catalog? Machines keep their cached copy.`)) return;
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
      <td data-label="Model">
        <div className="cell-stack">
        <div className="strong"><Link to={modelHref(m.name)} className="name-link">{m.title ?? m.name}</Link></div>
        <div className="mono small muted catalog-src" title={src.full}>{src.text}{m.mmproj ? " + projector" : ""}</div>
        {error && <div className="field-error">{error}</div>}
        </div>
      </td>
      <td data-label="Task">
        <div className="chip-row">
          <span className="chip">{m.task_label}</span>
          {m.modalities && m.modalities.length > 0 && (
            <span className="chip chip-tone-run" title="What it takes in chat besides text"><IconPhoto size={11} aria-hidden="true" /> {mediaText(m.modalities)}</span>
          )}
        </div>
      </td>
      <td data-label="Format" className="mono small">{m.format}</td>
      <td data-label="Size" className="small">{bytes(m.size_bytes)}</td>
      <td data-label="Architecture" className="small">
        {m.summary.architecture ?? "—"}{m.summary.params ? ` · ${params(m.summary.params)} params` : ""}
        {m.vision?.zero_shot ? <div className="muted" title="A zero-shot detector: each request names what to find">Finds what you name</div>
          : m.vision?.labels ? <div className="muted" title={m.vision.label_examples?.join(", ")}>{m.vision.labels} classes</div> : null}
        {m.summary.layers ? <div className="muted">{m.summary.layers} layers{m.summary.kv_heads ? `, ${m.summary.kv_heads} KV heads` : ""}{m.summary.max_context ? `, ${m.summary.max_context} ctx` : ""}</div> : null}
      </td>
      <td data-label="Licence" className="small">
        <div className="cell-stack">
          <span className="mono nowrap" title={m.license_from ? `The licence of ${m.license_from.replace("hf://", "")}, which this is converted from` : undefined}>{m.license ?? "—"}{m.license_from ? "*" : ""}</span>
          {m.gated && <span className="muted"><IconLock size={11} aria-hidden="true" /> gated</span>}
        </div>
      </td>
      <td data-label="Engines" className="mono small">{m.recipes.length
        ? m.recipes.map((r, i) => <span key={r}>{i ? ", " : ""}<Link to={engineHref(r)}>{r}</Link></span>) : "none fits"}</td>
      <td className="cell-actions">
        {/* One column of actions at desktop widths, so the table never clips them (1.4, #92). */}
        <div className="catalog-actions" role="group" aria-label={`Actions for ${m.name}`}>
        {canDeploy && m.recipes.length > 0 && <Link className="btn btn-small" to={`/serving/deploy?model=${encodeURIComponent(m.name)}`}>Deploy</Link>}
        {canDeploy && m.kind !== "local" && (
          <button type="button" className="btn btn-quiet btn-small" onClick={onDownload}
            title={`Download ${m.name} to machines now, so a deployment there starts without waiting`}>Download to…</button>
        )}
        {canManage && <button type="button" className="btn btn-quiet btn-small" disabled={m.deployments > 0}
          title={m.deployments ? "Deployed: delete its deployments first" : undefined} onClick={() => void remove()}>Remove</button>}
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
function EngineCard({ r, canManage, onEdit, onDeleted }: { r: Recipe; canManage: boolean; onEdit: () => void; onDeleted: (text: string) => void }) {
  const [error, setError] = useState<string | null>(null);
  const remove = async () => {
    if (!window.confirm(`Delete the engine ${r.name}? A deployment that uses it must be deleted first.`)) return;
    setError(null);
    try {
      await api(`/recipes/${encodeURIComponent(r.name)}`, { method: "DELETE" });
      onDeleted(`Engine ${r.name} deleted.`);
    } catch (e) { setError(errorText(e)); }
  };
  return (
    <article className="recipe engine-card" aria-label={`Engine ${r.name}`}>
      {/* Every closed card has the same height (1.4, #92): one header row, two lines of description, one
          row of chips, one line of tasks, and the Details row at the bottom. Only an open card grows. */}
      <header className="engine-head">
        <h3 className="mono" title={r.name}><Link to={engineHref(r.name)} className="name-link">{r.name}</Link></h3>
        <span className="chip engine-version" title={`${r.engine} ${r.version}`}>{r.engine} {r.version}</span>
        {!r.builtin && <span className="chip chip-tone-run engine-custom">custom</span>}
      </header>
      <p className="small engine-desc" title={r.description}>{r.description}</p>
      <div className="engine-facts" aria-label={`Runs on: ${engineHardware(r).map((h) => HARDWARE_LABEL[h]).join(", ")}`}>
        {engineHardware(r).map((h) => (
          <span key={h} className={`vendor vendor-${h}`}>{h === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{HARDWARE_LABEL[h]}</span>
        ))}
        {r.share === "vram" && <span className="chip chip-tone-wait">shares a GPU</span>}
      </div>
      <p className="small muted engine-serves" title={`Serves ${r.tasks.map((t) => TASK_LABEL[t]).join(", ")}`}>Serves {r.tasks.map((t) => TASK_LABEL[t]).join(", ")}</p>
      <div className="engine-foot">
        <details className="engine-details">
          <summary className="small">Details</summary>
          <p className="small muted">Formats: {r.formats.join(", ")} · estimate: {r.estimate}</p>
          <ul className="variants" aria-label={`Variants of ${r.name}`}>
            {r.variants.map((v) => (
              <li key={v.name}>
                <span className={`vendor vendor-${v.vendor}`}>{v.vendor === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{v.vendor.toUpperCase()}</span>
                <span className="mono small variant-what" title={v.runtime === "container" ? v.image ?? undefined : undefined}>{v.runtime === "container" ? imageTag(v.image) : `process: ${v.engine?.split("/").pop() ?? ""}`}</span>
                <span className="muted small">{[v.compute_capability && `cc ${v.compute_capability}`, v.driver && `driver ${v.driver}`, v.arch && v.arch.join(" ")].filter(Boolean).join(" · ")}</span>
              </li>
            ))}
          </ul>
          <OfflineImages name={r.name} variants={r.variants} />
        </details>
        {canManage && !r.builtin && (
          <div className="engine-actions">
            <button type="button" className="btn btn-quiet btn-small" onClick={onEdit}>Edit</button>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => void remove()}>Delete</button>
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
      <summary className="small">Machine without registry access: load the image</summary>
      <p className="small">
        On a machine that can reach the registry, save the image of the variant you need to a file. Copy it over, load it, and
        deploy as usual. <HelpLink page="how-to" anchor="offline-machines">Offline machines</HelpLink>
      </p>
      {images.length > 1 && (
        <div className="field recipe-offline-pick">
          <label htmlFor={id}>Variant</label>
          <select id={id} value={Math.min(picked, images.length - 1)} onChange={(ev) => setPicked(Number(ev.target.value))}>
            {images.map(([img, x], n) => <option key={img} value={n}>{x.names.join(", ")} · {imageTag(img).split("/").pop()}</option>)}
          </select>
        </div>
      )}
      <p className="small mono muted recipe-offline-image">{imageTag(image)}</p>
      <CopyBlock value={carry.save} label={`Save command for ${what}`} />
      <CopyBox value={carry.load} label={`Load command for ${what}`} />
      <p className="small muted">
        {e.checked ? (
          <>Forgeyard checks the loaded image against the ids recorded for it, so this engine works offline as it is.</>
        ) : (
          <>
            No ids are recorded for this image, so Forgeyard cannot check a loaded copy: name it by its tag only (without{" "}
            <code>@sha256:…</code>) in the recipe.
          </>
        )}
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
      onDone(edit === null ? `Engine ${r.recipe.name} added.` : `Engine ${r.recipe.name} saved.`, r.recipe.name);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <section className="panel engine-form" aria-labelledby="engine-form-title">
      <div className="panel-head">
        <h2 id="engine-form-title">{edit === null ? "Add a custom engine" : `Change ${edit}`}</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>Close</button>
      </div>
      {edit === null && (
        <p className="engine-form-lead">
          Define an engine with your own image and arguments — for example a newer version, copied from a built-in engine. Built-in
          engines are updated with Forgeyard releases.
        </p>
      )}
      <p className="muted small">
        An engine is a recipe in YAML, like the built-in ones: the program or container image per hardware, its parameters and its
        memory estimate. <HelpLink page="reference" anchor="built-in-recipes">Built-in recipes</HelpLink>
      </p>
      {edit === null && (
        <div className="field">
          <label htmlFor="engine-start">Start from a built-in engine</label>
          <select id="engine-start" value={start} onChange={(e) => void copy(e.target.value)}>
            <option value="">Empty: write the YAML yourself</option>
            {builtins.map((r) => <option key={r.name} value={r.name}>{r.name} ({r.engine} {r.version})</option>)}
          </select>
          <p className="field-hint">The copy gets a name of its own; change the variants or parameters you need.</p>
        </div>
      )}
      <div className={`field${error ? " has-error" : ""}`}>
        <label htmlFor="engine-yaml">Engine recipe (YAML)</label>
        <textarea id="engine-yaml" className="mono" rows={16} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
          disabled={loading} aria-invalid={error ? true : undefined} aria-describedby={error ? "engine-yaml-error" : undefined} />
        {error && <p className="field-error" id="engine-yaml-error" role="alert">{error}</p>}
      </div>
      <div className="row-gap">
        <button type="button" className="btn btn-primary" disabled={busy || loading || !text.trim()} onClick={() => void save()}>
          {busy ? "Saving…" : edit === null ? "Add engine" : "Save changes"}
        </button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
      </div>
    </section>
  );
}

function Sources({ sources, onSaved }: { sources: ModelSources; onSaved: () => void }) {
  const [hosts, setHosts] = useState(sources.url_hosts.join("\n"));
  const [dirs, setDirs] = useState(sources.local_dirs.join("\n"));
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const save = async () => {
    try {
      await api("/model-sources", { method: "PUT", body: { url_hosts: hosts.split("\n"), local_dirs: dirs.split("\n") } });
      setMsg({ tone: "ok", text: "Saved." }); onSaved();
    } catch (e) { setMsg({ tone: "error", text: errorText(e) }); }
  };
  const seeds = sources.env_seeds ?? [];
  return (
    <section className="panel" aria-labelledby="sources-title">
      <h2 id="sources-title">Sources</h2>
      <p className="muted small">Hugging Face models come from <code>{sources.hf_endpoint}</code>. Anything else only from what is listed here. Private and metadata addresses are always refused{sources.allow_private_hosts ? " (except: private hosts are allowed on this server for a LAN mirror)" : ""}.</p>
      {seeds.length > 0 && (
        <p className="from-env small" role="note">
          <span className="chip chip-tone-wait">from .env</span>
          <span>These lists come from {seeds.map((n, i) => <span key={n}>{i ? ", " : ""}<code>{n}</code></span>)}: nobody has saved them here
            yet. Saving replaces them, and the environment is then ignored.</span>
        </p>
      )}
      <div className="grid-2">
        <div className="field"><label htmlFor="s-hosts">Hosts for GGUF URLs (one per line, *.example.org for subdomains)</label>
          <textarea id="s-hosts" className="mono" rows={3} value={hosts} onChange={(e) => setHosts(e.target.value)} /></div>
        <div className="field"><label htmlFor="s-dirs">Directories for local:// models</label>
          <textarea id="s-dirs" className="mono" rows={3} value={dirs} onChange={(e) => setDirs(e.target.value)} />
          <p className="field-hint">Agents must also allow them (--model-local-dir).</p></div>
      </div>
      {msg && <div style={{ marginTop: 12 }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      <div className="row-gap" style={{ marginTop: 12 }}><button type="button" className="btn" onClick={() => void save()}>Save sources</button></div>
      <HfToken info={sources.hf_token} onSaved={onSaved} />
    </section>
  );
}

// 1.3.1 (D2): one Hugging Face token for the server, for gated models. Write-only: only whether one is
// set and its last four characters ever come back.
function HfToken({ info, onSaved }: { info?: ModelSources["hf_token"]; onSaved: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const send = async (method: "PUT" | "DELETE") => {
    setBusy(true);
    setMsg(null);
    try {
      await api("/hf-token", method === "PUT" ? { method, body: { token: token.trim() } } : { method });
      setToken("");
      setMsg({ tone: "ok", text: method === "PUT" ? "Token saved." : "Token removed." });
      onSaved();
    } catch (e) { setMsg({ tone: "error", text: errorText(e) }); } finally { setBusy(false); }
  };
  return (
    <div className="hf-token">
      <h3 id="hf-token-title">Hugging Face token</h3>
      <p className="muted small">
        For gated models (Llama, Gemma 3, FLUX.1-schnell…): used to inspect them and sent to the agent that downloads them, only to Hugging
        Face. A deployment's own token (Deploy a model → Advanced) wins for that deployment. Stored encrypted; never shown again.
      </p>
      <p className="small hf-token-state">
        {info?.set
          ? <><span className="chip chip-tone-ok">set</span> ends in <code>…{info.last4}</code>{info.updated_by ? <> · by {info.updated_by}</> : null}{info.updated_at ? <> · {when(info.updated_at)}</> : null}</>
          : <><span className="chip">not set</span> Gated models need a token, here or on each deployment.</>}
      </p>
      <form className="token-row" onSubmit={(e) => { e.preventDefault(); if (token.trim()) void send("PUT"); }}>
        <div className="field">
          <label htmlFor="hf-token">{info?.set ? "Replace the token" : "Token"}</label>
          <input id="hf-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="hf_…" />
          <p className="field-hint">A read token of an account that accepted the model's licence on huggingface.co.</p>
        </div>
        <div className="row-gap">
          <button type="submit" className="btn" disabled={busy || !token.trim()}>Save token</button>
          {info?.set && <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => void send("DELETE")}>Remove token</button>}
        </div>
      </form>
      {msg && <div style={{ marginTop: 10 }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
    </div>
  );
}
