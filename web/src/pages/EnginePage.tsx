// 1.4.3 (#140): an engine's page, /models/engines/<name>: what it serves, its variants (image or program per kind of
// hardware, and how each is pinned), which machines have that image or program, its parameters (folded to one line),
// and the deployments and catalog models that use it. Admins edit and delete a custom engine, and copy a built-in one
// as a custom engine.
import { IconCopy, IconCpu, IconEngine, IconPencil, IconRefresh, IconTrash } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Crumb, FoldButton, HardwareChips, Jump, MachineCell, ModelCards, Section } from "../components/LibraryPage";
import { Notice } from "../components/ui";
import { api, errorText } from "../lib/api";
import { imageTag, when } from "../lib/format";
import { API_LABEL, TASK_LABEL, mediaText } from "../lib/models";
import { digestOf, engineHref, pinned, stale, whereText } from "../lib/pages";
import { useDocumentTitle } from "../lib/title";
import type { CatalogResponse, Deployment, DeploymentsResponse, EnginePage as Page, Machine, MachinesResponse, Recipe, RecipeParam } from "../lib/types";
import { useApi } from "../lib/useApi";
import { EngineForm, OfflineImages } from "./Models";
import { DeploymentRow } from "./Serving";

export function EnginePage() {
  const { name = "" } = useParams();
  // Another engine's page starts afresh (its own listing, asked once).
  return <EngineLoader key={name} name={name} />;
}

function EngineLoader({ name }: { name: string }) {
  const page = useApi<Page>(`/recipes/${encodeURIComponent(name)}`);
  const cat = useApi<CatalogResponse>("/models");
  const ms = useApi<MachinesResponse>("/machines");
  const deps = useApi<DeploymentsResponse>("/deployments");
  // The page again after the connected machines listed their images (?refresh=true): kept, it wins over the first.
  const [fresh, setFresh] = useState<Page | null>(null);
  const [listing, setListing] = useState(false);
  const asked = useRef(false);
  const list = useCallback(() => api<Page>(`/recipes/${encodeURIComponent(name)}?refresh=true`).then(
    (r) => { setFresh(r); setListing(false); }, () => setListing(false)), [name]);
  const data = fresh ?? page.data;
  useDocumentTitle(data ? data.recipe.name : "Engine");
  // Once per page: the connected machines whose listing is missing or old list their images again.
  useEffect(() => {
    if (!page.data || asked.current) return;
    asked.current = true;
    if (stale(page.data, Date.now())) void list();
  }, [page.data, list]);
  return (
    <>
      <Crumb to="/models?tab=engines" label="Engines" />
      {page.error && <Notice tone="error">{page.error}</Notice>}
      {!data && !page.error && <p className="muted">Loading…</p>}
      {data && (
        <EngineView key={data.recipe.name} p={data} recipes={cat.data?.recipes ?? []} catalog={cat.data}
          machines={ms.data?.machines ?? null} deployments={deps.data?.deployments ?? null} listing={listing}
          relist={() => { setListing(true); void list(); }}
          reload={() => { setFresh(null); page.reload(); cat.reload(); }} />
      )}
    </>
  );
}

function EngineView({ p, recipes, catalog, machines, deployments, listing, relist, reload }: {
  p: Page; recipes: Recipe[]; catalog: CatalogResponse | null; machines: Machine[] | null; deployments: Deployment[] | null;
  listing: boolean; relist: () => void; reload: () => void;
}) {
  const r = p.recipe;
  const navigate = useNavigate();
  const [form, setForm] = useState<"edit" | "copy" | null>(null);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const paramList = Object.values(r.params);
  const advanced = paramList.filter((x) => x.advanced).length;
  const mine = (deployments ?? []).filter((d) => d.recipe === r.name);
  const models = (catalog?.models ?? []).filter((m) => p.models.some((x) => x.id === m.id));
  const here = p.machines.filter((x) => x.present === true).length;
  const canList = p.machines.some((x) => x.can_list && x.kind === "image");

  const remove = async () => {
    if (!window.confirm(`Delete the engine ${r.name}? A deployment that uses it must be deleted first.`)) return;
    try {
      await api(`/recipes/${encodeURIComponent(r.name)}`, { method: "DELETE" });
      navigate("/models?tab=engines");
    } catch (e) { setNote({ tone: "error", text: errorText(e) }); }
  };

  return (
    <>
      <header className="page-header lp-header">
        <div className="mk-title">
          <span className="kindicon lp-kindicon"><IconEngine size={26} stroke={1.6} aria-hidden="true" /></span>
          <div className="lp-head-text">
            <p className="eyebrow">03 · Engine · {r.builtin ? "built in" : "custom"}</p>
            <h1 className="mono">{r.name}</h1>
            <p className="small muted lp-sub">{r.title} {r.version}</p>
          </div>
        </div>
        {p.can_manage && (
          <div className="page-actions">
            {!r.builtin && <button type="button" className="btn" onClick={() => { setNote(null); setForm("edit"); }}><IconPencil size={15} aria-hidden="true" /> Edit</button>}
            {!r.builtin && (
              <button type="button" className="btn btn-quiet" disabled={p.deployments.length > 0} title={p.deployments.length ? "In use: delete its deployments first" : undefined}
                onClick={() => void remove()}><IconTrash size={15} aria-hidden="true" /> Delete</button>
            )}
            {r.builtin && <button type="button" className="btn btn-quiet" onClick={() => { setNote(null); setForm("copy"); }}
              title="Add a custom engine, starting from a copy of this one"><IconCopy size={15} aria-hidden="true" /> Copy as a custom engine</button>}
          </div>
        )}
      </header>
      {note && <Notice tone={note.tone}>{note.text}</Notice>}
      {form && (
        <EngineForm key={form} edit={form === "edit" ? r.name : null} from={form === "copy" ? r.name : undefined} recipes={recipes}
          onCancel={() => setForm(null)}
          onDone={(text, name) => {
            setForm(null);
            if (name !== r.name) navigate(engineHref(name));
            else { setNote({ tone: "ok", text }); reload(); }
          }} />
      )}
      <p className="mk-facts">
        <HardwareChips r={r} />
        <span>serves <strong>{r.tasks.map((t) => TASK_LABEL[t]).join(", ")}</strong></span>
        <span>formats <strong>{r.formats.join(", ")}</strong></span>
        <span><strong>{r.variants.length}</strong> variant{r.variants.length === 1 ? "" : "s"}</span>
        {r.share === "vram" ? <span className="chip chip-tone-wait">shares a GPU</span> : <span className="muted">a card of its own</span>}
        {!r.builtin && <span className="chip chip-tone-run">custom</span>}
      </p>
      <Jump links={[["overview", "Overview"], ["variants", "Variants"], ["machines", "Machines"], ["params", "Parameters"], ["deployments", "Deployments and models"]]} />

      <Section id="overview" title="Overview"><EngineAbout r={r} /></Section>

      <Section id="variants" title="Variants" count={r.variants.length}
        note="One per kind of hardware: the image (or program) it runs, whether it is pinned by digest, and what the machine needs.">
        <EngineVariants r={r} />
      </Section>

      <Section id="machines" title="Machines" count={`there on ${here}`}
        actions={canList ? <button type="button" className="btn btn-quiet btn-small" onClick={relist} disabled={listing}
          title="Ask the connected machines to list their images now">
          <IconRefresh size={14} aria-hidden="true" /> {listing ? "Listing…" : "List again"}</button> : undefined}
        note="The variant each machine would run, and whether its image or program is there already: images as each machine last listed them, programs as its cache report says.">
        <EngineMachines p={p} machines={machines} />
      </Section>

      <Section id="params" title="Parameters" actions={paramList.length ? <FoldButton open={paramsOpen} setOpen={setParamsOpen} what="parameters" controls="params-list" /> : undefined}>
        <p className="lp-fold-line">
          <strong>{paramList.length} parameter{paramList.length === 1 ? "" : "s"}</strong>{advanced ? <span className="muted"> · {advanced} advanced</span> : null}
          {paramList.length ? <span className="muted"> · set on Deploy a model and on a deployment's Edit; these are the defaults</span> : <span className="muted"> · it runs as it is</span>}
        </p>
        {paramsOpen && <div id="params-list" className="lp-fold-body"><EngineParams r={r} /></div>}
      </Section>

      <Section id="deployments" title="Deployments" count={p.deployments.length}>
        {deployments === null ? <p className="muted small">Loading…</p> : mine.length === 0
          ? <p className="empty-line">No deployment uses this engine.</p>
          : <div className="lp-deps">{mine.map((d) => <DeploymentRow key={d.id} d={d} machines={machines ?? []} cloud={[]} />)}</div>}
      </Section>
      <Section id="models" title="Catalog models it runs" count={p.models.length}>
        <ModelCards list={models} empty="No catalog model runs on it yet." />
      </Section>
      <HelpLinks>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">Models, engines and estimates</HelpLink>
        <HelpLink page="how-to" anchor="offline-machines">Offline machines</HelpLink>
      </HelpLinks>
    </>
  );
}

function EngineAbout({ r }: { r: Recipe }) {
  return (
    <>
      <p className="lp-desc">{r.description}</p>
      <dl className="facts lp-facts">
        <div className="fact"><dt>Engine</dt><dd>{r.title} <span className="mono small">{r.version}</span></dd></div>
        <div className="fact"><dt>Serves</dt><dd>{r.tasks.map((t) => <span key={t} className="lp-api">{TASK_LABEL[t]} <code>{API_LABEL[r.api[t]] ?? r.api[t]}</code></span>)}</dd></div>
        <div className="fact"><dt>Formats</dt><dd className="mono small">{r.formats.join(", ")}</dd></div>
        {r.media && <div className="fact"><dt>Takes in chat</dt><dd>{r.media.inputs.length ? mediaText(r.media.inputs) : "text only"}{r.media.files ? " · files" : ""}</dd></div>}
        {r.tts && <div className="fact"><dt>Speech</dt><dd>{r.tts.formats.join(", ")} · streams {r.tts.stream_formats.join(", ") || "no"}{r.tts.default_voice ? ` · voice ${r.tts.default_voice}` : ""}</dd></div>}
        {r.image && <div className="fact"><dt>Images</dt><dd>{r.image.output_formats.join(", ")} · up to {r.image.max_n} per request</dd></div>}
        <div className="fact"><dt>GPU</dt><dd>{r.share === "vram" ? "Shares a card with other deployments that share" : "A card of its own (exclusive), unless the deployment says share"}{r.devices.includes("cpu") ? " · also runs on the CPU" : ""}</dd></div>
        <div className="fact"><dt>Estimate</dt><dd className="mono small">{r.estimate}</dd></div>
      </dl>
    </>
  );
}

function EngineVariants({ r }: { r: Recipe }) {
  return (
    <>
      <div className="table-wrap">
        <table className="table lp-variants">
          <thead><tr><th>Variant</th><th>Runs</th><th>Image or program</th><th>Pinned</th><th>Needs</th></tr></thead>
          <tbody>{r.variants.map((v) => {
            const pin = pinned(v);
            return (
              <tr key={v.name}>
                <td data-label="Variant"><span className="lp-variant"><span className={`vendor vendor-${v.vendor}`}>{v.vendor === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{v.vendor.toUpperCase()}</span> <span className="mono small">{v.name}</span></span></td>
                <td data-label="Runs" className="small">{v.runtime === "container" ? "container" : "process"}</td>
                <td data-label="Image or program" className="mono small lp-break" title={v.image ?? v.engine ?? undefined}>{v.runtime === "container" ? imageTag(v.image) : v.engine?.split("/").pop()}</td>
                <td data-label="Pinned" className="small">
                  {pin === "digest" ? <div className="cell-stack"><span className="chip chip-tone-ok">by digest</span><span className="mono muted" title={v.image ?? undefined}>{digestOf(v.image)}…</span>{v.image_check ? <span className="muted">works offline</span> : null}</div>
                    : pin === "tag" ? <span className="chip chip-tone-wait">tag only</span> : <span className="muted">{v.engine?.startsWith("http") ? "release archive (SHA-256)" : "installed command"}</span>}
                </td>
                <td data-label="Needs" className="small muted">{[v.compute_capability && `compute ${v.compute_capability}`, v.driver && `driver ${v.driver}`, v.arch && v.arch.join(" ")].filter(Boolean).join(" · ") || "—"}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      <OfflineImages name={r.name} variants={r.variants} />
    </>
  );
}

function EngineMachines({ p, machines }: { p: Page; machines: Machine[] | null }) {
  if (!p.machines.length) return <p className="empty-line">No machine has joined yet. <Link to="/machines">Add a machine</Link>.</p>;
  return (
    <div className="table-wrap">
      <table className="table lp-machines">
        <thead><tr><th>Machine</th><th>Variant it runs</th><th>Image or program there?</th><th><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{p.machines.map((x) => (
          <tr key={x.machine_id}>
            <td data-label="Machine"><MachineCell id={x.machine_id} name={x.name} status={x.status} machines={machines} /></td>
            <td data-label="Variant" className="small">{x.variant
              ? <span><span className="mono">{x.variant}</span> <span className="muted">· {x.runtime}</span></span>
              : <span className="muted lp-reason" title={x.note ?? undefined}>{x.note ?? "none for its hardware"}</span>}</td>
            <td data-label="There?" className="small">
              {!x.variant ? <span className="muted">—</span> : (
                <div className="cell-stack">
                  <span>{x.present === true ? <span className="chip chip-tone-ok">here</span> : x.present === false ? <span className="chip">not here</span> : <span className="chip chip-tone-wait">not known</span>}</span>
                  <span className="muted">{x.present === null && x.note ? x.note : whereText(x.kind, x.present)}{x.listed_at ? ` · listed ${when(x.listed_at)}` : ""}</span>
                </div>
              )}
            </td>
            <td className="cell-actions">
              <div className="catalog-actions">
                {x.kind === "image" && x.present === false && x.connected && p.can_manage && (
                  <Link className="btn btn-quiet btn-small" to={`/machines/${x.machine_id}#engine-images`}>Copy or upload</Link>
                )}
              </div>
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function range(x: RecipeParam): string {
  if (x.choices) return x.choices.join(" · ");
  if (x.min !== undefined || x.max !== undefined) return `${x.min ?? "…"} – ${x.max ?? "…"}`;
  return "";
}

function EngineParams({ r }: { r: Recipe }) {
  const list = Object.entries(r.params);
  return (
    <div className="table-wrap">
      <table className="table lp-params">
        <thead><tr><th>Parameter</th><th>Default</th><th>Range</th><th>What it does</th></tr></thead>
        <tbody>{list.map(([k, x]) => (
          <tr key={k}>
            <td data-label="Parameter"><div className="cell-stack"><span className="strong">{x.label ?? k}</span><span className="mono small muted">{k}{x.advanced ? " · advanced" : ""}</span></div></td>
            <td data-label="Default" className="mono small">{String(x.default)}</td>
            <td data-label="Range" className="small muted">{range(x) || "—"}</td>
            <td data-label="What it does" className="small">{x.help ?? <span className="muted">—</span>}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
