// 1.4.3 (#140): a catalog model's page, /models/<name>: what it is, which machines have it and where it fits, its
// deployments, the engines that run it, and its files (folded to one line). Everyone signed in may look; operators
// and admins deploy and download it; admins change its display name and remove it.
import { IconDownload, IconExternalLink, IconLock, IconPencil, IconPhoto, IconPlus, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { DownloadDialog, DownloadList } from "../components/DownloadTo";
import { ModelKindIcon } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Crumb, EngineCards, FoldButton, Jump, MachineCell, Section } from "../components/LibraryPage";
import { Notice } from "../components/ui";
import { api, errorText } from "../lib/api";
import { bytes } from "../lib/machines";
import { API_LABEL, VISION_TASKS, gib, mediaText, params, shortSource } from "../lib/models";
import { catalogKind, engineHref, filesLine, headlineEstimate, hfRepo, machineFit, presenceText } from "../lib/pages";
import { useDocumentTitle } from "../lib/title";
import type { CatalogModel, Deployment, DeploymentsResponse, Machine, MachinesResponse, ModelPage as Page, ModelPageMachine } from "../lib/types";
import { useApi } from "../lib/useApi";
import { DeploymentRow } from "./Serving";

export function ModelPage() {
  const { name = "" } = useParams();
  const page = useApi<Page>(`/models/${encodeURIComponent(name)}`);
  const ms = useApi<MachinesResponse>("/machines");
  const deps = useApi<DeploymentsResponse>("/deployments");
  const m = page.data?.model ?? null;
  useDocumentTitle(m ? (m.title ?? m.name) : "Model");
  return (
    <>
      <Crumb to="/models" label="Catalog" />
      {page.error && <Notice tone="error">{page.error}</Notice>}
      {!page.data && !page.error && <p className="muted">Loading…</p>}
      {page.data && (
        <ModelView key={page.data.model.id} p={page.data} machines={ms.data?.machines ?? null}
          deployments={deps.data?.deployments ?? null} reload={() => { page.reload(); deps.reload(); }} />
      )}
    </>
  );
}

function apiPath(p: Page): string | null {
  const m = p.model;
  if (VISION_TASKS.includes(m.task)) return `/v1/vision/${m.task === "image-classify" ? "classify" : m.task}`;
  const r = p.recipes[0];
  return r ? API_LABEL[r.api[m.task]] ?? null : null;
}

function ModelView({ p, machines, deployments, reload }: {
  p: Page; machines: Machine[] | null; deployments: Deployment[] | null; reload: () => void;
}) {
  const m = p.model;
  const navigate = useNavigate();
  const [recipe, setRecipe] = useState<string | null>(m.recipes[0] ?? null);
  const [dialog, setDialog] = useState<{ machineId?: number } | null>(null);
  const [filesOpen, setFilesOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const mine = (deployments ?? []).filter((d) => d.model.id === m.id);
  const cachedOn = p.machines.filter((x) => x.cache.cached).length;
  const est = headlineEstimate(recipe ? p.fits[recipe] : undefined);
  const deployHref = `/serving/deploy?model=${encodeURIComponent(m.name)}`;
  const canDownload = p.can_deploy && m.kind !== "local";
  const downloading = p.machines.flatMap((x) => (x.download ? [x.download] : []));

  const remove = async () => {
    if (!window.confirm(`Remove ${m.name} from the catalog? Machines keep their cached copy.`)) return;
    try {
      await api(`/models/${m.id}`, { method: "DELETE" });
      navigate("/models");
    } catch (e) { setNote({ tone: "error", text: errorText(e) }); }
  };

  return (
    <>
      <header className="page-header lp-header">
        <div className="mk-title">
          <span className="kindicon lp-kindicon"><ModelKindIcon kind={catalogKind(m)} size={26} /></span>
          <div className="lp-head-text">
            <p className="eyebrow">Model · {m.task_label}</p>
            <h1>{m.title ?? m.name}</h1>
            {/* The name under a display name (the name itself is the heading otherwise). */}
            <p className="mono small muted lp-sub">{m.title && m.title !== m.name ? `${m.name} · ` : ""}{shortSource(m).text}</p>
            {p.can_manage && !renaming && (
              <button type="button" className="btn btn-quiet btn-small lp-rename" onClick={() => setRenaming(true)}>
                <IconPencil size={14} aria-hidden="true" /> Display name
              </button>
            )}
          </div>
        </div>
        <div className="page-actions">
          {p.can_deploy && m.recipes.length > 0 && <Link className="btn btn-primary" to={deployHref}><IconPlus size={16} aria-hidden="true" /> Deploy</Link>}
          {canDownload && <button type="button" className="btn" onClick={() => setDialog({})}><IconDownload size={16} aria-hidden="true" /> Download to…</button>}
          {p.can_manage && (
            <button type="button" className="btn btn-quiet" disabled={m.deployments > 0} title={m.deployments ? "Deployed: delete its deployments first" : undefined}
              onClick={() => void remove()}><IconTrash size={15} aria-hidden="true" /> Remove</button>
          )}
        </div>
      </header>
      {renaming && <RenameForm m={m} onDone={(text) => { setRenaming(false); setNote({ tone: "ok", text }); reload(); }} onCancel={() => setRenaming(false)} />}
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {dialog && <DownloadDialog model={m} machineId={dialog.machineId} onClose={() => setDialog(null)}
        onStarted={(text) => { setNote({ tone: "ok", text }); reload(); }} />}
      <p className="mk-facts">
        <span className="chip">{m.task_label}</span>
        {m.modalities && m.modalities.length > 0 && <span className="chip chip-tone-run"><IconPhoto size={11} aria-hidden="true" /> {mediaText(m.modalities)}</span>}
        <span><strong>{bytes(m.size_bytes)}</strong> {m.format}</span>
        {m.summary.params ? <span><strong>{params(m.summary.params)}</strong> params</span> : null}
        <span className="mono">{m.license ?? "no licence recorded"}{m.license_from ? "*" : ""}</span>
        {m.gated && <span><IconLock size={12} aria-hidden="true" /> gated</span>}
        {est && recipe && <span>needs <strong>{est.device === "gpu" ? `${gib(est.per_gpu)} per GPU` : `${gib(est.ram)} RAM`}</strong> with {recipe}'s defaults</span>}
        <span>{m.deployments ? <strong>{m.deployments} deployed</strong> : "not deployed"}</span>
      </p>
      <Jump links={[["overview", "Overview"], ["machines", "Machines"], ["deployments", "Deployments"], ["engines", "Engines"], ["files", "Files"]]} />

      <Section id="overview" title="Overview"><ModelAbout p={p} /></Section>

      <Section id="machines" title="Machines" count={`in ${cachedOn} cache${cachedOn === 1 ? "" : "s"}`}
        actions={<RecipePicker m={m} recipe={recipe} setRecipe={setRecipe} />}
        note={<>Whether each machine has it in its model cache (as its agent last reported), and whether a replica with {recipe ?? "the engine"}'s
          defaults fits there now (the estimate). <HelpLink page="concepts" anchor="models-engines-and-estimates">Estimates</HelpLink></>}>
        {downloading.length > 0 && <DownloadList list={downloading} show="machine" canCancel={p.can_deploy}
          onChanged={(text) => { setNote({ tone: "ok", text }); reload(); }} onError={(text) => setNote({ tone: "error", text })} />}
        <ModelMachines p={p} recipe={recipe} machines={machines} onDownload={(id) => setDialog({ machineId: id })} />
      </Section>

      <Section id="deployments" title="Deployments" count={m.deployments}
        actions={p.can_deploy && m.recipes.length > 0 ? <Link className="btn btn-small" to={deployHref}><IconPlus size={14} aria-hidden="true" /> Deploy</Link> : undefined}>
        {deployments === null ? <p className="muted small">Loading…</p> : mine.length === 0
          ? <p className="empty-line">Not deployed.{canDownload ? " Download it to machines first, and a deployment there starts at once." : ""}</p>
          : <div className="lp-deps">{mine.map((d) => <DeploymentRow key={d.id} d={d} machines={machines ?? []} cloud={[]} />)}</div>}
      </Section>

      <Section id="engines" title="Engines that run it" count={p.recipes.length}>
        <EngineCards list={p.recipes} empty="No engine runs this format and task." />
      </Section>

      <Section id="files" title="Files" actions={p.files.files.length ? <FoldButton open={filesOpen} setOpen={setFilesOpen} what="files" controls="files-list" /> : undefined}>
        <FilesLine p={p} />
        {filesOpen && <div id="files-list" className="lp-fold-body"><FileTable p={p} /></div>}
      </Section>
      <HelpLinks>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">Models, engines and estimates</HelpLink>
        <HelpLink page="how-to" anchor="download-a-model-before-deploying-it">Download a model before deploying it</HelpLink>
      </HelpLinks>
    </>
  );
}

// An admin changes the name the console shows; the catalog name stays, because deployments record it.
function RenameForm({ m, onDone, onCancel }: { m: CatalogModel; onDone: (text: string) => void; onCancel: () => void }) {
  const [title, setTitle] = useState(m.title ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ model: CatalogModel }>(`/models/${m.id}`, { method: "PATCH", body: { title: title.trim() } });
      onDone(r.model.title ? `${m.name} is shown as “${r.model.title}”.` : `${m.name} is shown by its name.`);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <form className="panel lp-rename-form" aria-label="Display name" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <div className={`field${error ? " has-error" : ""}`}>
        <label htmlFor="model-title">Display name</label>
        <input id="model-title" value={title} maxLength={120} placeholder={m.name} onChange={(e) => setTitle(e.target.value)} autoFocus />
        {error ? <p className="field-error" role="alert">{error}</p>
          : <p className="field-hint">What the console shows for this model. Its name stays <code>{m.name}</code>: deployments and API keys refer to it. Empty: the name.</p>}
      </div>
      <div className="row-gap">
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>{busy ? "Saving…" : "Save"}</button>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function ModelAbout({ p }: { p: Page }) {
  const m = p.model;
  const repo = hfRepo(m);
  const path = apiPath(p);
  return (
    <dl className="facts lp-facts">
      <div className="fact"><dt>Source</dt><dd className="mono small lp-break">{m.source}{m.file ? ` · ${m.file}` : ""}{repo && (
        <> <a href={`https://huggingface.co/${repo}`} target="_blank" rel="noreferrer" className="lp-ext">Hugging Face <IconExternalLink size={12} aria-hidden="true" /></a></>
      )}</dd></div>
      {m.kind !== "local" && <div className="fact"><dt>Revision</dt><dd className="mono small" title={m.revision ?? undefined}>{m.revision ? m.revision.slice(0, 12) : "—"}{m.ref && m.ref !== m.revision ? <span className="muted"> (pinned from {m.ref})</span> : null}</dd></div>}
      <div className="fact"><dt>Licence</dt><dd><span className="mono small">{m.license ?? "—"}</span>{m.license_from ? <span className="muted small"> · the licence of {m.license_from.replace("hf://", "")}, which this is converted from</span> : null}{m.gated ? <span className="muted small"> · gated: needs a Hugging Face token</span> : null}</dd></div>
      <div className="fact"><dt>Task</dt><dd>{m.task_label}{path && <><span className="muted small"> at </span><code>{path}</code></>}</dd></div>
      {m.task === "chat" && <div className="fact"><dt>Takes</dt><dd>{m.modalities === null || m.modalities === undefined ? <span className="muted">not recorded</span> : m.modalities.length ? `Text, ${mediaText(m.modalities)?.toLowerCase()}` : "Text"}</dd></div>}
      <div className="fact"><dt>Architecture</dt><dd>{m.summary.architecture ?? m.vision?.architecture ?? "—"}{m.summary.params ? ` · ${params(m.summary.params)} params` : ""}{m.summary.dtype ? ` · ${m.summary.dtype}` : ""}</dd></div>
      {(m.summary.layers || m.summary.max_context) ? (
        <div className="fact"><dt>Shape</dt><dd>{[m.summary.layers && `${m.summary.layers} layers`, m.summary.kv_heads && `${m.summary.kv_heads} KV heads`, m.summary.head_dim && `head ${m.summary.head_dim}`, m.summary.max_context && `${m.summary.max_context.toLocaleString()} tokens of context`].filter(Boolean).join(" · ")}</dd></div>
      ) : null}
      {m.classify?.labels ? <div className="fact"><dt>Labels</dt><dd>{m.classify.labels}{m.classify.label_names?.length ? <span className="muted small"> · {m.classify.label_names.slice(0, 8).join(", ")}{m.classify.label_names.length > 8 ? ", …" : ""}</span> : null}{m.classify.problem === "multi_label_classification" ? <span className="muted small"> · each label scored on its own</span> : null}</dd></div> : null}
      {m.vision?.zero_shot ? <div className="fact"><dt>Classes</dt><dd>Finds what you name <span className="muted small">· zero-shot: each request lists what to find (<code>labels</code>)</span></dd></div> : null}
      {!m.vision?.zero_shot && m.vision?.labels ? <div className="fact"><dt>Classes</dt><dd>{m.vision.labels}{m.vision.label_examples?.length ? <span className="muted small"> · {m.vision.label_examples.slice(0, 6).join(", ")}…</span> : null}</dd></div> : null}
      {m.image?.size ? <div className="fact"><dt>Native size</dt><dd>{m.image.size} × {m.image.size} px{m.image.pipeline ? <span className="muted small"> · {m.image.pipeline}</span> : null}</dd></div> : null}
      {m.voices && m.voices.length ? <div className="fact"><dt>Voices</dt><dd>{m.voices.join(", ")}</dd></div> : null}
      <div className="fact"><dt>Added</dt><dd>{m.created_by ? `by ${m.created_by} · ` : ""}{new Date(m.created_at.endsWith("Z") || m.created_at.includes("+") ? m.created_at : `${m.created_at}Z`).toLocaleDateString()}</dd></div>
    </dl>
  );
}

function RecipePicker({ m, recipe, setRecipe }: { m: CatalogModel; recipe: string | null; setRecipe: (r: string) => void }) {
  if (m.recipes.length < 2) return m.recipes.length ? <span className="small muted">with <Link to={engineHref(m.recipes[0])} className="mono">{m.recipes[0]}</Link></span> : null;
  return (
    <div className="segmented lp-seg" role="radiogroup" aria-label="Engine for the estimate">
      {m.recipes.map((r) => (
        <button key={r} type="button" role="radio" aria-checked={recipe === r} className={recipe === r ? "on" : ""} onClick={() => setRecipe(r)}>{r}</button>
      ))}
    </div>
  );
}

function CacheCell({ x }: { x: ModelPageMachine }) {
  const c = x.cache;
  if (c.cached) {
    return (
      <div className="cell-stack">
        <span><span className="chip chip-tone-ok">in cache</span> {c.bytes !== null ? bytes(c.bytes) : ""}</span>
        {c.used_by.length > 0 && <span className="muted">used by {c.used_by.map((u) => u.name).join(", ")}</span>}
      </div>
    );
  }
  return <span className="muted">{c.reported ? "not cached" : "no report"}</span>;
}

function ModelMachines({ p, recipe, machines, onDownload }: {
  p: Page; recipe: string | null; machines: Machine[] | null; onDownload: (machineId: number) => void;
}) {
  const m = p.model;
  if (!p.machines.length) return <p className="empty-line">No machine has joined yet. <Link to="/machines">Add a machine</Link>.</p>;
  const fit = recipe ? p.fits[recipe] : undefined;
  const errors = fit ? Object.values(fit).flatMap((f) => (f?.error ? [f.error] : [])) : [];
  return (
    <>
      {errors.length > 0 && <p className="field-error">{errors.join(" ")}</p>}
      <div className="table-wrap">
        <table className="table lp-machines">
          <thead><tr><th>Machine</th><th>Cache</th><th>Fits?</th><th>Engine there</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{p.machines.map((x) => {
            const f = machineFit(fit, x.machine_id);
            const v = f?.verdict ?? null;
            const eng = recipe ? x.engines[recipe] : undefined;
            const online = x.status === "online";
            return (
              <tr key={x.machine_id}>
                <td data-label="Machine"><MachineCell id={x.machine_id} name={x.name} status={x.status} machines={machines} /></td>
                <td data-label="Cache" className="small"><CacheCell x={x} /></td>
                <td data-label="Fits?" className="small">
                  {!v ? <span className="muted">—</span> : (
                    <div className="cell-stack">
                      <span>{v.fits
                        ? <span className={`chip ${online ? "chip-tone-ok" : "chip-tone-wait"}`}>{online ? "fits" : "would fit"}{f?.device === "cpu" ? " on the CPU" : ""}</span>
                        : v.share_fits ? <span className="chip chip-tone-wait">fits if shared</span> : <span className="chip chip-tone-bad">does not fit</span>}
                        {v.fits && v.leftover != null ? <span className="muted"> {gib(v.leftover)} left{v.gpus?.length ? ` on GPU #${v.gpus.join(", #")}` : ""}</span> : null}</span>
                      {(!v.fits || !online) && v.reason && <span className="muted lp-reason" title={v.reason}>{v.reason}</span>}
                    </div>
                  )}
                </td>
                <td data-label="Engine there" className="small">
                  {eng?.variant ? (
                    <div className="cell-stack">
                      <span><Link to={engineHref(recipe!)} className="mono">{recipe}</Link> <span className="muted">· {eng.variant}</span></span>
                      <span className="muted">{presenceText(eng.kind, eng.present)}</span>
                    </div>
                  ) : <span className="muted" title={eng?.note ?? undefined}>{recipe ? "no variant for its hardware" : "—"}</span>}
                </td>
                <td className="cell-actions">
                  <div className="catalog-actions">
                    {p.can_deploy && recipe && v?.fits && online && (
                      <Link className="btn btn-small" to={`/serving/deploy?model=${encodeURIComponent(m.name)}&recipe=${encodeURIComponent(recipe)}&machine=${x.machine_id}${f?.device === "cpu" ? "&device=cpu" : ""}`}>Deploy here</Link>
                    )}
                    {p.can_deploy && m.kind !== "local" && !x.cache.cached && !x.download && x.connected && (
                      <button type="button" className="btn btn-quiet btn-small" onClick={() => onDownload(x.machine_id)}>Download here</button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </>
  );
}

function FilesLine({ p }: { p: Page }) {
  const f = p.files;
  const m = p.model;
  if (!f.recorded) {
    return <p className="lp-fold-line">{m.kind === "local"
      ? <>A directory on the machines themselves: <span className="mono small">{m.source.replace(/^local:\/\//, "")}</span>. Each machine's cache report lists what it holds.</>
      : <>The file list was not recorded for this model. <span className="muted">Remove it and add it again to record it.</span></>}</p>;
  }
  const line = filesLine(f, m.file);
  return (
    <p className="lp-fold-line">
      <strong>{line.count} file{line.count === 1 ? "" : "s"} downloaded</strong> · {bytes(f.downloaded_bytes)}
      {line.used ? <> · uses <span className="mono small">{line.used.path}</span></> : null}
      {line.others > 0 ? <span className="muted"> · {line.others} other file{line.others === 1 ? "" : "s"} in the repository, not downloaded</span> : null}
    </p>
  );
}

function FileTable({ p }: { p: Page }) {
  return (
    <div className="table-wrap">
      <table className="table lp-files">
        <thead><tr><th>File</th><th>Size</th><th>What it is</th></tr></thead>
        <tbody>{p.files.files.map((f) => (
          <tr key={f.path}>
            <td data-label="File" className="mono small lp-break">{f.path}</td>
            <td data-label="Size" className="small nowrap">{f.size === null ? "—" : bytes(f.size)}</td>
            <td data-label="What it is" className="small">
              {f.downloaded
                ? <span className="chip chip-tone-ok">{f.role ? `${f.role} · downloaded` : "downloaded"}</span>
                : <span className="muted">{f.role}, not downloaded</span>}
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
