// 1.4.3 (#140): an engine's page, /models/engines/<name>: what it serves, its variants (image or program per kind of
// hardware, and how each is pinned), which machines have that image or program, its parameters (folded to one line),
// and the deployments and catalog models that use it. Admins edit and delete a custom engine, and copy a built-in one
// as a custom engine.
import { IconCopy, IconCpu, IconEngine, IconPencil, IconRefresh, IconTrash } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { CheckBar, EngineUpdates } from "../components/EngineUpdates";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Crumb, FoldButton, HardwareChips, Jump, MachineCell, ModelCards, Section } from "../components/LibraryPage";
import { Notice } from "../components/ui";
import { pluralForm } from "../i18n/format";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { imageTag, when } from "../lib/format";
import { engineDescription } from "../lib/engines";
import { API_LABEL, TASK_LABEL, mediaText } from "../lib/models";
import { digestOf, engineHref, pinned, stale, whereText } from "../lib/pages";
import { useDocumentTitle } from "../lib/title";
import type { CatalogResponse, Deployment, DeploymentsResponse, EnginePage as Page, EngineUpdatesResponse, Machine, MachinesResponse, Recipe,
  RecipeParam } from "../lib/types";
import { useApi } from "../lib/useApi";
import { EngineForm, OfflineImages } from "./Models";
import { DeploymentRow } from "./Serving";

export function EnginePage() {
  const { name = "" } = useParams();
  // Another engine's page starts afresh (its own listing, asked once).
  return <EngineLoader key={name} name={name} />;
}

function EngineLoader({ name }: { name: string }) {
  const t = useT();
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
  useDocumentTitle(data ? data.recipe.name : t("enginepage.doc"));
  // Once per page: the connected machines whose listing is missing or old list their images again.
  useEffect(() => {
    if (!page.data || asked.current) return;
    asked.current = true;
    if (stale(page.data, Date.now())) void list();
  }, [page.data, list]);
  return (
    <>
      <Crumb to="/models?tab=engines" label={t("library.tab.engines")} />
      {page.error && <Notice tone="error">{page.error}</Notice>}
      {!data && !page.error && <p className="muted">{t("common.loading")}</p>}
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
  const t = useT();
  const navigate = useNavigate();
  // 1.5.2 (#95): newer versions of a built-in engine, as the last check found them.
  const updates = useApi<EngineUpdatesResponse>(r.builtin ? "/engine-updates" : null);
  const [checked, setChecked] = useState<EngineUpdatesResponse | null>(null);
  const upd = checked ?? updates.data;
  // A card's badge links here (#updates): once the result is in, the section is where it should be.
  const scrolled = useRef(false);
  useEffect(() => {
    if (!upd || scrolled.current || window.location.hash !== "#updates") return;
    scrolled.current = true;
    document.getElementById("updates")?.scrollIntoView?.({ block: "start" });
  }, [upd]);
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
    if (!window.confirm(t("library.deleteEngineConfirm", { name: r.name }))) return;
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
            <p className="eyebrow">{t("enginepage.eyebrow", { kind: r.builtin ? t("enginepage.builtin") : t("enginepage.custom") })}</p>
            <h1 className="mono">{r.name}</h1>
            <p className="small muted lp-sub">{r.title} {r.version}</p>
          </div>
        </div>
        {p.can_manage && (
          <div className="page-actions">
            {!r.builtin && <button type="button" className="btn" onClick={() => { setNote(null); setForm("edit"); }}><IconPencil size={15} aria-hidden="true" /> {t("library.edit")}</button>}
            {!r.builtin && (
              <button type="button" className="btn btn-quiet" disabled={p.deployments.length > 0} title={p.deployments.length ? t("enginepage.inUse") : undefined}
                onClick={() => void remove()}><IconTrash size={15} aria-hidden="true" /> {t("library.delete")}</button>
            )}
            {r.builtin && <button type="button" className="btn btn-quiet" onClick={() => { setNote(null); setForm("copy"); }}
              title={t("enginepage.copyTitle")}><IconCopy size={15} aria-hidden="true" /> {t("enginepage.copy")}</button>}
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
        <span>{t.rich("enginepage.serves", { tasks: <strong>{r.tasks.map((x) => TASK_LABEL[x]).join(t("library.listSep"))}</strong> })}</span>
        <span>{t.rich("enginepage.formats", { formats: <strong>{r.formats.join(", ")}</strong> })}</span>
        <span>{t.rich(pluralForm(r.variants.length, t.lang) === "one" ? "enginepage.variants.one" : "enginepage.variants.other", { count: <strong>{r.variants.length}</strong> })}</span>
        {r.share === "vram" ? <span className="chip chip-tone-wait">{t("library.sharesGpu")}</span> : <span className="muted">{t("enginepage.ownCard")}</span>}
        {!r.builtin && <span className="chip chip-tone-run">{t("library.custom")}</span>}
      </p>
      <Jump links={[["overview", t("enginepage.jump.overview")], ["variants", t("enginepage.jump.variants")],
        ...(r.builtin ? [["updates", t("engineupdates.title")] as [string, string]] : []), ["machines", t("enginepage.jump.machines")],
        ["params", t("enginepage.jump.params")], ["deployments", t("enginepage.jump.deployments")]]} />

      <Section id="overview" title={t("enginepage.jump.overview")}><EngineAbout r={r} /></Section>

      <Section id="variants" title={t("enginepage.jump.variants")} count={r.variants.length} note={t("enginepage.variantsNote")}>
        <EngineVariants r={r} />
      </Section>

      {r.builtin && (
        <Section id="updates" title={t("engineupdates.title")}
          count={upd?.result?.engines[r.name]?.candidates.length || undefined}
          actions={<CheckBar compact data={upd} onChecked={(res) => setChecked({ result: res, can_check: true })} />}>
          <EngineUpdates u={upd?.result?.engines[r.name]} version={r.version} />
        </Section>
      )}

      <Section id="machines" title={t("enginepage.jump.machines")} count={t("enginepage.thereOn", { count: String(here) })}
        actions={canList ? <button type="button" className="btn btn-quiet btn-small" onClick={relist} disabled={listing}
          title={t("enginepage.listTitle")}>
          <IconRefresh size={14} aria-hidden="true" /> {listing ? t("enginepage.listing") : t("enginepage.listAgain")}</button> : undefined}
        note={t("enginepage.machinesNote")}>
        <EngineMachines p={p} machines={machines} />
      </Section>

      <Section id="params" title={t("enginepage.jump.params")} actions={paramList.length ? <FoldButton open={paramsOpen} setOpen={setParamsOpen} what={t("enginepage.paramsWhat")} controls="params-list" /> : undefined}>
        <p className="lp-fold-line">
          <strong>{t.plural("enginepage.params", paramList.length)}</strong>{advanced ? <span className="muted">{t("enginepage.advanced", { count: String(advanced) })}</span> : null}
          {paramList.length ? <span className="muted">{t("enginepage.paramsWhere")}</span> : <span className="muted">{t("enginepage.runsAsIs")}</span>}
        </p>
        {paramsOpen && <div id="params-list" className="lp-fold-body"><EngineParams r={r} /></div>}
      </Section>

      <Section id="deployments" title={t("enginepage.deployments")} count={p.deployments.length}>
        {deployments === null ? <p className="muted small">{t("common.loading")}</p> : mine.length === 0
          ? <p className="empty-line">{t("enginepage.noDeployment")}</p>
          : <div className="lp-deps">{mine.map((d) => <DeploymentRow key={d.id} d={d} machines={machines ?? []} cloud={[]} />)}</div>}
      </Section>
      <Section id="models" title={t("enginepage.modelsTitle")} count={p.models.length}>
        <ModelCards list={models} empty={t("enginepage.noModel")} />
      </Section>
      <HelpLinks>
        <HelpLink page="concepts" anchor="models-engines-and-estimates">{t("library.help.concepts")}</HelpLink>
        <HelpLink page="how-to" anchor="offline-machines">{t("library.help.offline")}</HelpLink>
        {r.builtin && <HelpLink page="how-to" anchor="see-whether-an-engine-has-a-newer-version">{t("engineupdates.help")}</HelpLink>}
      </HelpLinks>
    </>
  );
}

function EngineAbout({ r }: { r: Recipe }) {
  const t = useT();
  return (
    <>
      <p className="lp-desc">{engineDescription(r)}</p>
      <dl className="facts lp-facts">
        <div className="fact"><dt>{t("enginepage.fact.engine")}</dt><dd>{r.title} <span className="mono small">{r.version}</span></dd></div>
        <div className="fact"><dt>{t("enginepage.fact.serves")}</dt><dd>{r.tasks.map((x) => <span key={x} className="lp-api">{TASK_LABEL[x]} <code>{API_LABEL[r.api[x]] ?? r.api[x]}</code></span>)}</dd></div>
        <div className="fact"><dt>{t("enginepage.fact.formats")}</dt><dd className="mono small">{r.formats.join(", ")}</dd></div>
        {r.media && <div className="fact"><dt>{t("enginepage.fact.takes")}</dt><dd>{r.media.inputs.length ? mediaText(r.media.inputs) : t("enginepage.fact.textOnly")}{r.media.files ? t("enginepage.fact.files") : ""}</dd></div>}
        {r.tts && <div className="fact"><dt>{t("enginepage.fact.speech")}</dt><dd>{t("enginepage.fact.speechValue", { formats: r.tts.formats.join(", "), stream: r.tts.stream_formats.join(", ") || t("enginepage.fact.noStream") })}{r.tts.default_voice ? t("enginepage.fact.voice", { voice: r.tts.default_voice }) : ""}</dd></div>}
        {r.image && <div className="fact"><dt>{t("enginepage.fact.images")}</dt><dd>{t("enginepage.fact.imagesValue", { formats: r.image.output_formats.join(", "), count: String(r.image.max_n) })}</dd></div>}
        <div className="fact"><dt>{t("enginepage.fact.gpu")}</dt><dd>{r.share === "vram" ? t("enginepage.fact.shares") : t("enginepage.fact.exclusive")}{r.devices.includes("cpu") ? t("enginepage.fact.alsoCpu") : ""}</dd></div>
        <div className="fact"><dt>{t("enginepage.fact.estimate")}</dt><dd className="mono small">{r.estimate}</dd></div>
      </dl>
    </>
  );
}

function EngineVariants({ r }: { r: Recipe }) {
  const t = useT();
  return (
    <>
      <div className="table-wrap">
        <table className="table lp-variants">
          <thead><tr><th>{t("enginepage.col.variant")}</th><th>{t("enginepage.col.runs")}</th><th>{t("enginepage.col.image")}</th>
            <th>{t("enginepage.col.pinned")}</th><th>{t("enginepage.col.needs")}</th></tr></thead>
          <tbody>{r.variants.map((v) => {
            const pin = pinned(v);
            return (
              <tr key={v.name}>
                <td data-label={t("enginepage.col.variant")}><span className="lp-variant"><span className={`vendor vendor-${v.vendor}`}>{v.vendor === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{v.vendor.toUpperCase()}</span> <span className="mono small">{v.name}</span></span></td>
                <td data-label={t("enginepage.col.runs")} className="small">{v.runtime === "container" ? t("enginepage.runtime.container") : t("enginepage.runtime.process")}</td>
                <td data-label={t("enginepage.col.image")} className="mono small lp-break" title={v.image ?? v.engine ?? undefined}>{v.runtime === "container" ? imageTag(v.image) : v.engine?.split("/").pop()}</td>
                <td data-label={t("enginepage.col.pinned")} className="small">
                  {pin === "digest" ? <div className="cell-stack"><span className="chip chip-tone-ok">{t("enginepage.byDigest")}</span><span className="mono muted" title={v.image ?? undefined}>{digestOf(v.image)}…</span>{v.image_check ? <span className="muted">{t("enginepage.worksOffline")}</span> : null}</div>
                    : pin === "tag" ? <span className="chip chip-tone-wait">{t("enginepage.tagOnly")}</span> : <span className="muted">{v.engine?.startsWith("http") ? t("enginepage.archive") : t("enginepage.command")}</span>}
                </td>
                <td data-label={t("enginepage.col.needs")} className="small muted">{[v.compute_capability && t("enginepage.compute", { value: v.compute_capability }), v.driver && t("enginepage.driver", { value: v.driver }), v.arch && v.arch.join(" ")].filter(Boolean).join(" · ") || "—"}</td>
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
  const t = useT();
  if (!p.machines.length) return <p className="empty-line">{t.rich("library.noMachines", { link: (x) => <Link to="/machines">{x}</Link> })}</p>;
  return (
    <div className="table-wrap">
      <table className="table lp-machines">
        <thead><tr><th>{t("enginepage.col.machine")}</th><th>{t("enginepage.col.variantRuns")}</th><th>{t("enginepage.col.there")}</th>
          <th><span className="sr-only">{t("enginepage.col.actions")}</span></th></tr></thead>
        <tbody>{p.machines.map((x) => (
          <tr key={x.machine_id}>
            <td data-label={t("enginepage.col.machine")}><MachineCell id={x.machine_id} name={x.name} status={x.status} machines={machines} /></td>
            <td data-label={t("enginepage.col.variant")} className="small">{x.variant
              ? <span><span className="mono">{x.variant}</span> <span className="muted">· {x.runtime ? t.maybe(`enginepage.runtime.${x.runtime}`, x.runtime) : x.runtime}</span></span>
              : <span className="muted lp-reason" title={x.note ?? undefined}>{x.note ?? t("enginepage.noneForHardware")}</span>}</td>
            <td data-label={t("enginepage.col.thereShort")} className="small">
              {!x.variant ? <span className="muted">—</span> : (
                <div className="cell-stack">
                  <span>{x.present === true ? <span className="chip chip-tone-ok">{t("enginepage.here")}</span> : x.present === false ? <span className="chip">{t("enginepage.notHere")}</span> : <span className="chip chip-tone-wait">{t("enginepage.notKnown")}</span>}</span>
                  <span className="muted">{x.present === null && x.note ? x.note : whereText(x.kind, x.present)}{x.listed_at ? t("enginepage.listed", { when: when(x.listed_at) }) : ""}</span>
                </div>
              )}
            </td>
            <td className="cell-actions">
              <div className="catalog-actions">
                {x.kind === "image" && x.present === false && x.connected && p.can_manage && (
                  <Link className="btn btn-quiet btn-small" to={`/machines/${x.machine_id}#engine-images`}>{t("enginepage.copyOrUpload")}</Link>
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
  const t = useT();
  const list = Object.entries(r.params);
  return (
    <div className="table-wrap">
      <table className="table lp-params">
        <thead><tr><th>{t("enginepage.col.param")}</th><th>{t("enginepage.col.default")}</th><th>{t("enginepage.col.range")}</th><th>{t("enginepage.col.does")}</th></tr></thead>
        <tbody>{list.map(([k, x]) => (
          <tr key={k}>
            <td data-label={t("enginepage.col.param")}><div className="cell-stack"><span className="strong">{x.label ?? k}</span><span className="mono small muted">{k}{x.advanced ? t("enginepage.advancedTag") : ""}</span></div></td>
            <td data-label={t("enginepage.col.default")} className="mono small">{String(x.default)}</td>
            <td data-label={t("enginepage.col.range")} className="small muted">{range(x) || "—"}</td>
            <td data-label={t("enginepage.col.does")} className="small">{x.help ?? <span className="muted">—</span>}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
