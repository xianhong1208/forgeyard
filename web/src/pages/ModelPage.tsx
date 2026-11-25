// 1.4.3 (#140): a catalog model's page, /models/<name>: what it is, which machines have it and where it fits, its
// deployments, the engines that run it, and its files (folded to one line). Everyone signed in may look; operators
// and admins deploy and download it; admins change its display name and remove it.
import { IconAlertTriangle, IconDownload, IconExternalLink, IconLanguage, IconLock, IconPencil, IconPhoto, IconPlus, IconTrash } from "@tabler/icons-react";
import { AbilityChips } from "../components/AbilityChips";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { DownloadDialog, DownloadList } from "../components/DownloadTo";
import { ModelKindIcon } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Crumb, EngineCards, FoldButton, Jump, MachineCell, Section } from "../components/LibraryPage";
import { Notice } from "../components/ui";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { bytes } from "../lib/machines";
import { embedSummary, embedsImages, embedsText, englishOnly, languagesLine } from "../lib/encoders";
import { abilities, startImage } from "../lib/imageEdit";
import { API_LABEL, VISION_TASKS, gib, mediaText, params, shortSource, taskLabel } from "../lib/models";
import { catalogKind, engineHref, filesLine, headlineEstimate, hfRepo, machineFit, presenceText } from "../lib/pages";
import { useDocumentTitle } from "../lib/title";
import type { CatalogModel, Deployment, DeploymentsResponse, Machine, MachinesResponse, ModelPage as Page, ModelPageMachine } from "../lib/types";
import { useApi } from "../lib/useApi";
import { DeploymentRow } from "./Serving";

export function ModelPage() {
  const { name = "" } = useParams();
  const t = useT();
  const page = useApi<Page>(`/models/${encodeURIComponent(name)}`);
  const ms = useApi<MachinesResponse>("/machines");
  const deps = useApi<DeploymentsResponse>("/deployments");
  const m = page.data?.model ?? null;
  useDocumentTitle(m ? (m.title ?? m.name) : t("modelpage.doc"));
  return (
    <>
      <Crumb to="/models" label={t("library.tab.catalog")} />
      {page.error && <Notice tone="error">{page.error}</Notice>}
      {!page.data && !page.error && <p className="muted">{t("common.loading")}</p>}
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
  const t = useT();
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
    if (!window.confirm(t("library.removeConfirm", { name: m.name }))) return;
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
            <p className="eyebrow">{t("modelpage.eyebrow", { task: taskLabel(m) })}</p>
            <h1>{m.title ?? m.name}</h1>
            {/* The name under a display name (the name itself is the heading otherwise). */}
            <p className="mono small muted lp-sub">{m.title && m.title !== m.name ? `${m.name} · ` : ""}{shortSource(m).text}</p>
            {p.can_manage && !renaming && (
              <button type="button" className="btn btn-quiet btn-small lp-rename" onClick={() => setRenaming(true)}>
                <IconPencil size={14} aria-hidden="true" /> {t("modelpage.displayName")}
              </button>
            )}
          </div>
        </div>
        <div className="page-actions">
          {p.can_deploy && m.recipes.length > 0 && <Link className="btn btn-primary" to={deployHref}><IconPlus size={16} aria-hidden="true" /> {t("modelpage.deploy")}</Link>}
          {canDownload && <button type="button" className="btn" onClick={() => setDialog({})}><IconDownload size={16} aria-hidden="true" /> {t("library.downloadTo")}</button>}
          {p.can_manage && (
            <button type="button" className="btn btn-quiet" disabled={m.deployments > 0} title={m.deployments ? t("library.deployedFirst") : undefined}
              onClick={() => void remove()}><IconTrash size={15} aria-hidden="true" /> {t("library.remove")}</button>
          )}
        </div>
      </header>
      {renaming && <RenameForm m={m} onDone={(text) => { setRenaming(false); setNote({ tone: "ok", text }); reload(); }} onCancel={() => setRenaming(false)} />}
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {dialog && <DownloadDialog model={m} machineId={dialog.machineId} onClose={() => setDialog(null)}
        onStarted={(text) => { setNote({ tone: "ok", text }); reload(); }} />}
      <p className="mk-facts">
        <span className="chip">{taskLabel(m)}</span>
        {m.modalities && m.modalities.length > 0 && <span className="chip chip-tone-run"><IconPhoto size={11} aria-hidden="true" /> {mediaText(m.modalities)}</span>}
        {m.task === "embed" && m.embed && <span className={`chip${embedsImages(m.embed) ? " chip-tone-run" : ""}`}>{embedsImages(m.embed) && <><IconPhoto size={11} aria-hidden="true" />{" "}</>}{embedSummary(m.embed)}</span>}
        {m.classify?.zero_shot && <span className="chip chip-tone-wait">{t("modelpage.zeroShot")}</span>}
        <AbilityChips m={m} />
        {m.languages && m.languages.length > 0 && <LanguagesChip m={m} />}
        <span><strong>{bytes(m.size_bytes)}</strong> {m.format}</span>
        {m.summary.params ? <span>{t.rich("modelpage.params", { params: <strong>{params(m.summary.params)}</strong> })}</span> : null}
        <span className="mono">{m.license ?? t("modelpage.noLicence")}{m.license_from ? "*" : ""}</span>
        {m.gated && <span><IconLock size={12} aria-hidden="true" /> {t("modelpage.gated")}</span>}
        {est && recipe && <span>{t.rich("modelpage.needs", { amount: <strong>{est.device === "gpu" ? t("modelpage.perGpu", { size: gib(est.per_gpu) }) : t("modelpage.ram", { size: gib(est.ram) })}</strong>, recipe })}</span>}
        <span>{m.deployments ? <strong>{t("modelpage.deployed", { count: String(m.deployments) })}</strong> : t("modelpage.notDeployed")}</span>
      </p>
      <LanguageWarning m={m} />
      <PhotosNote m={m} />
      <Jump links={[["overview", t("modelpage.jump.overview")], ["machines", t("modelpage.jump.machines")], ["deployments", t("modelpage.jump.deployments")],
        ["engines", t("modelpage.jump.engines")], ["files", t("modelpage.jump.files")]]} />

      <Section id="overview" title={t("modelpage.jump.overview")}><ModelAbout p={p} /></Section>

      <Section id="machines" title={t("modelpage.jump.machines")} count={t.plural("modelpage.caches", cachedOn)}
        actions={<RecipePicker m={m} recipe={recipe} setRecipe={setRecipe} />}
        note={t.rich("modelpage.machinesNote", { recipe: recipe ?? t("modelpage.theEngine"),
          link: (x) => <HelpLink page="concepts" anchor="models-engines-and-estimates">{x}</HelpLink> })}>
        {downloading.length > 0 && <DownloadList list={downloading} show="machine" canCancel={p.can_deploy}
          onChanged={(text) => { setNote({ tone: "ok", text }); reload(); }} onError={(text) => setNote({ tone: "error", text })} />}
        <ModelMachines p={p} recipe={recipe} machines={machines} onDownload={(id) => setDialog({ machineId: id })} />
      </Section>

      <Section id="deployments" title={t("modelpage.jump.deployments")} count={m.deployments}
        actions={p.can_deploy && m.recipes.length > 0 ? <Link className="btn btn-small" to={deployHref}><IconPlus size={14} aria-hidden="true" /> {t("modelpage.deploy")}</Link> : undefined}>
        {deployments === null ? <p className="muted small">{t("common.loading")}</p> : mine.length === 0
          ? <p className="empty-line">{t("modelpage.notDeployedLine")}{canDownload ? t("modelpage.downloadFirst") : ""}</p>
          : <div className="lp-deps">{mine.map((d) => <DeploymentRow key={d.id} d={d} machines={machines ?? []} cloud={[]} />)}</div>}
      </Section>

      <Section id="engines" title={t("modelpage.enginesTitle")} count={p.recipes.length}>
        <EngineCards list={p.recipes} empty={t("modelpage.noEngine")} />
      </Section>

      <Section id="files" title={t("modelpage.jump.files")} actions={p.files.files.length ? <FoldButton open={filesOpen} setOpen={setFilesOpen} what={t("modelpage.filesWhat")} controls="files-list" /> : undefined}>
        <FilesLine p={p} />
        {filesOpen && <div id="files-list" className="lp-fold-body"><FileTable p={p} /></div>}
      </Section>
      <HelpLinks>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">{t("library.help.concepts")}</HelpLink>
        <HelpLink page="how-to" anchor="download-a-model-before-deploying-it">{t("library.help.download")}</HelpLink>
      </HelpLinks>
    </>
  );
}

// 1.5.4: the languages its card names, beside the task: prominent for a text classifier or an embedding model that
// reads texts (a zero-shot classifier answers in whatever language its labels are, right or wrong).
function LanguagesChip({ m }: { m: CatalogModel }) {
  const t = useT();
  const line = languagesLine(m.languages ?? []);
  const warn = englishOnly(m.languages) && (m.task === "classify" || (m.task === "embed" && embedsText(m.embed)));
  return (
    <span className={`chip${warn ? " chip-tone-run" : m.classify?.zero_shot ? " chip-tone-ok" : ""}`} title={t("modelpage.lang.title", { names: line.full })}>
      <IconLanguage size={11} aria-hidden="true" /> {englishOnly(m.languages) ? t("modelpage.lang.englishOnly") : line.text}
    </span>
  );
}

// 1.5.4: an English-only model that reads texts says so before anyone sends it Chinese: a zero-shot classifier
// answers wrong with confidence (1 or 2 of 12 Traditional Chinese texts in our test), a CLIP-like model puts a
// Chinese text in the wrong place, so text -> image search finds the wrong pictures.
function LanguageWarning({ m }: { m: CatalogModel }) {
  const t = useT();
  if (!englishOnly(m.languages)) return null;
  if (m.task === "classify") {
    return <p className="lang-warn" role="note"><IconAlertTriangle size={16} aria-hidden="true" /> <span>{m.classify?.zero_shot ? t("modelpage.lang.warnZeroShot") : t("modelpage.lang.warnClassify")}</span></p>;
  }
  if (m.task === "embed" && embedsText(m.embed)) {
    return <p className="lang-warn" role="note"><IconAlertTriangle size={16} aria-hidden="true" /> <span>{embedsImages(m.embed) ? t("modelpage.lang.warnClip") : t("modelpage.lang.warnEmbed")}</span></p>;
  }
  return null;
}

// 1.5.5 (#143, Q8): a model that takes people's images (edits, a start image) gets no filter from Forgeyard: say so, and
// point at the model card's responsible-use notes and licence.
function PhotosNote({ m }: { m: CatalogModel }) {
  const t = useT();
  if (!abilities(m).length) return null;
  const repo = hfRepo(m);
  return (
    <p className="lang-warn" role="note"><IconAlertTriangle size={16} aria-hidden="true" /> <span>{t("modelpage.photos")}{repo && (
      <> <a href={`https://huggingface.co/${repo}`} target="_blank" rel="noreferrer" className="lp-ext">{t("modelpage.photosCard")} <IconExternalLink size={12} aria-hidden="true" /></a></>
    )}</span></p>
  );
}

// An admin changes the name the console shows; the catalog name stays, because deployments record it.
function RenameForm({ m, onDone, onCancel }: { m: CatalogModel; onDone: (text: string) => void; onCancel: () => void }) {
  const t = useT();
  const [title, setTitle] = useState(m.title ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ model: CatalogModel }>(`/models/${m.id}`, { method: "PATCH", body: { title: title.trim() } });
      onDone(r.model.title ? t("modelpage.shownAs", { name: m.name, title: r.model.title }) : t("modelpage.shownByName", { name: m.name }));
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <form className="panel lp-rename-form" aria-label={t("modelpage.displayName")} onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <div className={`field${error ? " has-error" : ""}`}>
        <label htmlFor="model-title">{t("modelpage.displayName")}</label>
        <input id="model-title" value={title} maxLength={120} placeholder={m.name} onChange={(e) => setTitle(e.target.value)} autoFocus />
        {error ? <p className="field-error" role="alert">{error}</p>
          : <p className="field-hint">{t.rich("modelpage.renameHint", { name: <code>{m.name}</code> })}</p>}
      </div>
      <div className="row-gap">
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>{busy ? t("modelpage.saving") : t("modelpage.save")}</button>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </form>
  );
}

function ModelAbout({ p }: { p: Page }) {
  const m = p.model;
  const t = useT();
  const repo = hfRepo(m);
  const path = apiPath(p);
  const added = new Date(m.created_at.endsWith("Z") || m.created_at.includes("+") ? m.created_at : `${m.created_at}Z`);
  return (
    <dl className="facts lp-facts">
      <div className="fact"><dt>{t("modelpage.fact.source")}</dt><dd className="mono small lp-break">{m.source}{m.file ? ` · ${m.file}` : ""}{repo && (
        <> <a href={`https://huggingface.co/${repo}`} target="_blank" rel="noreferrer" className="lp-ext">Hugging Face <IconExternalLink size={12} aria-hidden="true" /></a></>
      )}</dd></div>
      {m.kind !== "local" && <div className="fact"><dt>{t("modelpage.fact.revision")}</dt><dd className="mono small" title={m.revision ?? undefined}>{m.revision ? m.revision.slice(0, 12) : "—"}{m.ref && m.ref !== m.revision ? <span className="muted">{t("modelpage.fact.pinnedFrom", { ref: m.ref })}</span> : null}</dd></div>}
      <div className="fact"><dt>{t("modelpage.fact.licence")}</dt><dd><span className="mono small">{m.license ?? "—"}</span>{m.license_from ? <span className="muted small">{t("modelpage.fact.licenceFrom", { source: m.license_from.replace("hf://", "") })}</span> : null}{m.gated ? <span className="muted small">{t("modelpage.fact.gated")}</span> : null}</dd></div>
      <div className="fact"><dt>{t("modelpage.fact.task")}</dt><dd>{taskLabel(m)}{path && <><span className="muted small">{t("modelpage.fact.at")}</span><code>{path}</code></>}</dd></div>
      {m.task === "chat" && <div className="fact"><dt>{t("modelpage.fact.takes")}</dt><dd>{m.modalities === null || m.modalities === undefined ? <span className="muted">{t("modelpage.fact.notRecorded")}</span> : m.modalities.length ? t("modelpage.fact.textAnd", { media: String(mediaText(m.modalities)?.toLowerCase()) }) : t("modelpage.fact.text")}</dd></div>}
      <div className="fact"><dt>{t("modelpage.fact.architecture")}</dt><dd>{m.summary.architecture ?? m.vision?.architecture ?? "—"}{m.summary.params ? t("modelpage.fact.paramsSuffix", { params: params(m.summary.params) }) : ""}{m.summary.dtype ? ` · ${m.summary.dtype}` : ""}</dd></div>
      {(m.summary.layers || m.summary.max_context) ? (
        <div className="fact"><dt>{t("modelpage.fact.shape")}</dt><dd>{[m.summary.layers && t("modelpage.fact.layers", { count: String(m.summary.layers) }), m.summary.kv_heads && t("modelpage.fact.kvHeads", { count: String(m.summary.kv_heads) }), m.summary.head_dim && t("modelpage.fact.head", { count: String(m.summary.head_dim) }), m.summary.max_context && t("modelpage.fact.context", { count: t.num(m.summary.max_context) })].filter(Boolean).join(" · ")}</dd></div>
      ) : null}
      {m.task === "embed" && m.embed ? <div className="fact"><dt>{t("modelpage.fact.embeds")}</dt><dd>{embedSummary(m.embed)}{embedsImages(m.embed) ? <span className="muted small">{embedsText(m.embed) ? t("modelpage.fact.embedsBoth") : t("modelpage.fact.embedsImages")}</span> : null}</dd></div> : null}
      {m.classify?.zero_shot ? <div className="fact"><dt>{t("modelpage.fact.labels")}</dt><dd>{t("modelpage.fact.labelsAsked")} <span className="muted small">{t.rich("modelpage.fact.labelsAskedHow", { labels: <code>labels</code>, template: <code>hypothesis_template</code> })}</span></dd></div> : null}
      {m.languages && m.languages.length > 0 ? <div className="fact"><dt>{t("modelpage.fact.languages")}</dt><dd>{languagesLine(m.languages, 50).text}</dd></div> : null}
      {!m.classify?.zero_shot && m.classify?.labels ? <div className="fact"><dt>{t("modelpage.fact.labels")}</dt><dd>{m.classify.labels}{m.classify.label_names?.length ? <span className="muted small"> · {m.classify.label_names.slice(0, 8).join(", ")}{m.classify.label_names.length > 8 ? ", …" : ""}</span> : null}{m.classify.problem === "multi_label_classification" ? <span className="muted small">{t("modelpage.fact.multiLabel")}</span> : null}</dd></div> : null}
      {m.vision?.zero_shot ? <div className="fact"><dt>{t("modelpage.fact.classes")}</dt><dd>{t("modelpage.fact.findsWhatYouName")} <span className="muted small">{t.rich("modelpage.fact.zeroShot", { labels: <code>labels</code> })}</span></dd></div> : null}
      {!m.vision?.zero_shot && m.vision?.labels ? <div className="fact"><dt>{t("modelpage.fact.classes")}</dt><dd>{m.vision.labels}{m.vision.label_examples?.length ? <span className="muted small"> · {m.vision.label_examples.slice(0, 6).join(", ")}…</span> : null}</dd></div> : null}
      {m.image?.size ? <div className="fact"><dt>{t("modelpage.fact.nativeSize")}</dt><dd>{t("modelpage.fact.nativeSizeValue", { size: String(m.image.size) })}{m.image.pipeline ? <span className="muted small"> · {m.image.pipeline}</span> : null}</dd></div> : null}
      {m.task === "image" && (m.image?.edit ?? 0) > 0 ? <div className="fact"><dt>{t("modelpage.fact.edits")}</dt><dd>{t.plural("modelpage.fact.editsValue", m.image?.edit ?? 0)}<span className="muted small">{m.image?.generate === false ? t("modelpage.fact.editsOnly") : ""}{t("modelpage.fact.editsHow")}</span></dd></div> : null}
      {m.task === "video" && startImage(m.video?.image_input) !== "none" ? <div className="fact"><dt>{t("modelpage.fact.startImage")}</dt><dd>{startImage(m.video?.image_input) === "required" ? t("modelpage.fact.startRequired") : t("modelpage.fact.startOptional")}<span className="muted small">{t("modelpage.fact.startHow")}</span></dd></div> : null}
      {m.voices && m.voices.length ? <div className="fact"><dt>{t("modelpage.fact.voices")}</dt><dd>{m.voices.join(", ")}</dd></div> : null}
      <div className="fact"><dt>{t("modelpage.fact.added")}</dt><dd>{m.created_by ? t("modelpage.fact.addedBy", { name: m.created_by }) : ""}{t.lang === "en" ? added.toLocaleDateString() : t.date(added)}</dd></div>
    </dl>
  );
}

function RecipePicker({ m, recipe, setRecipe }: { m: CatalogModel; recipe: string | null; setRecipe: (r: string) => void }) {
  const t = useT();
  if (m.recipes.length < 2) return m.recipes.length ? <span className="small muted">{t.rich("modelpage.with", { engine: <Link to={engineHref(m.recipes[0])} className="mono">{m.recipes[0]}</Link> })}</span> : null;
  return (
    <div className="segmented lp-seg" role="radiogroup" aria-label={t("modelpage.engineFor")}>
      {m.recipes.map((r) => (
        <button key={r} type="button" role="radio" aria-checked={recipe === r} className={recipe === r ? "on" : ""} onClick={() => setRecipe(r)}>{r}</button>
      ))}
    </div>
  );
}

function CacheCell({ x }: { x: ModelPageMachine }) {
  const t = useT();
  const c = x.cache;
  if (c.cached) {
    return (
      <div className="cell-stack">
        <span><span className="chip chip-tone-ok">{t("modelpage.inCache")}</span> {c.bytes !== null ? bytes(c.bytes) : ""}</span>
        {c.used_by.length > 0 && <span className="muted">{t("modelpage.usedBy", { names: c.used_by.map((u) => u.name).join(", ") })}</span>}
      </div>
    );
  }
  return <span className="muted">{c.reported ? t("modelpage.notCached") : t("modelpage.noReport")}</span>;
}

function ModelMachines({ p, recipe, machines, onDownload }: {
  p: Page; recipe: string | null; machines: Machine[] | null; onDownload: (machineId: number) => void;
}) {
  const m = p.model;
  const t = useT();
  if (!p.machines.length) return <p className="empty-line">{t.rich("library.noMachines", { link: (x) => <Link to="/machines">{x}</Link> })}</p>;
  const fit = recipe ? p.fits[recipe] : undefined;
  const errors = fit ? Object.values(fit).flatMap((f) => (f?.error ? [f.error] : [])) : [];
  return (
    <>
      {errors.length > 0 && <p className="field-error">{errors.join(" ")}</p>}
      <div className="table-wrap">
        <table className="table lp-machines">
          <thead><tr><th>{t("modelpage.col.machine")}</th><th>{t("modelpage.col.cache")}</th><th>{t("modelpage.col.fits")}</th><th>{t("modelpage.col.engine")}</th>
            <th><span className="sr-only">{t("modelpage.col.actions")}</span></th></tr></thead>
          <tbody>{p.machines.map((x) => {
            const f = machineFit(fit, x.machine_id);
            const v = f?.verdict ?? null;
            const eng = recipe ? x.engines[recipe] : undefined;
            const online = x.status === "online";
            return (
              <tr key={x.machine_id}>
                <td data-label={t("modelpage.col.machine")}><MachineCell id={x.machine_id} name={x.name} status={x.status} machines={machines} /></td>
                <td data-label={t("modelpage.col.cache")} className="small"><CacheCell x={x} /></td>
                <td data-label={t("modelpage.col.fits")} className="small">
                  {!v ? <span className="muted">—</span> : (
                    <div className="cell-stack">
                      <span>{v.fits
                        ? <span className={`chip ${online ? "chip-tone-ok" : "chip-tone-wait"}`}>{online ? t("modelpage.fits") : t("modelpage.wouldFit")}{f?.device === "cpu" ? t("modelpage.onCpu") : ""}</span>
                        : v.share_fits ? <span className="chip chip-tone-wait">{t("modelpage.fitsShared")}</span> : <span className="chip chip-tone-bad">{t("modelpage.noFit")}</span>}
                        {v.fits && v.leftover != null ? <span className="muted">{v.gpus?.length ? t("modelpage.leftOnGpu", { size: gib(v.leftover), gpus: v.gpus.join(", #") }) : t("modelpage.left", { size: gib(v.leftover) })}</span> : null}</span>
                      {(!v.fits || !online) && v.reason && <span className="muted lp-reason" title={v.reason}>{v.reason}</span>}
                    </div>
                  )}
                </td>
                <td data-label={t("modelpage.col.engine")} className="small">
                  {eng?.variant ? (
                    <div className="cell-stack">
                      <span><Link to={engineHref(recipe!)} className="mono">{recipe}</Link> <span className="muted">· {eng.variant}</span></span>
                      <span className="muted">{presenceText(eng.kind, eng.present)}</span>
                    </div>
                  ) : <span className="muted" title={eng?.note ?? undefined}>{recipe ? t("modelpage.noVariant") : "—"}</span>}
                </td>
                <td className="cell-actions">
                  <div className="catalog-actions">
                    {p.can_deploy && recipe && v?.fits && online && (
                      <Link className="btn btn-small" to={`/serving/deploy?model=${encodeURIComponent(m.name)}&recipe=${encodeURIComponent(recipe)}&machine=${x.machine_id}${f?.device === "cpu" ? "&device=cpu" : ""}`}>{t("modelpage.deployHere")}</Link>
                    )}
                    {p.can_deploy && m.kind !== "local" && !x.cache.cached && !x.download && x.connected && (
                      <button type="button" className="btn btn-quiet btn-small" onClick={() => onDownload(x.machine_id)}>{t("modelpage.downloadHere")}</button>
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
  const t = useT();
  if (!f.recorded) {
    return <p className="lp-fold-line">{m.kind === "local"
      ? t.rich("modelpage.files.local", { dir: <span className="mono small">{m.source.replace(/^local:\/\//, "")}</span> })
      : t.rich("modelpage.files.notRecorded", { muted: (x) => <span className="muted">{x}</span> })}</p>;
  }
  const line = filesLine(f, m.file);
  return (
    <p className="lp-fold-line">
      <strong>{t.plural("modelpage.files.downloaded", line.count)}</strong> · {bytes(f.downloaded_bytes)}
      {line.used ? t.rich("modelpage.files.uses", { path: <span className="mono small">{line.used.path}</span> }) : null}
      {line.others > 0 ? <span className="muted">{t.plural("modelpage.files.others", line.others)}</span> : null}
    </p>
  );
}

function FileTable({ p }: { p: Page }) {
  const t = useT();
  return (
    <div className="table-wrap">
      <table className="table lp-files">
        <thead><tr><th>{t("modelpage.files.col.file")}</th><th>{t("modelpage.files.col.size")}</th><th>{t("modelpage.files.col.what")}</th></tr></thead>
        <tbody>{p.files.files.map((f) => (
          <tr key={f.path}>
            <td data-label={t("modelpage.files.col.file")} className="mono small lp-break">{f.path}</td>
            <td data-label={t("modelpage.files.col.size")} className="small nowrap">{f.size === null ? "—" : bytes(f.size)}</td>
            <td data-label={t("modelpage.files.col.what")} className="small">
              {f.downloaded
                ? <span className="chip chip-tone-ok">{f.role ? t("modelpage.files.roleDownloaded", { role: f.role }) : t("modelpage.files.downloadedChip")}</span>
                : <span className="muted">{t("modelpage.files.roleNot", { role: f.role ?? "" })}</span>}
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
