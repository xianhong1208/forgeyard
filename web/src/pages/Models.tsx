import { IconBrain, IconCloud, IconCpu, IconLock, IconMicrophone, IconPhoto, IconPlus, IconSortDescending, IconVectorBezier2 } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Notice, PageHeader } from "../components/ui";
import { AddModel } from "./AddModel";
import { api, errorText } from "../lib/api";
import { exact, imageTag, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { API_KIND } from "../lib/gateway";
import { API_LABEL, TASK_LABEL, downloadFraction, downloadText, gib, mediaText, params } from "../lib/models";
import { ROUTE_KIND_LABEL, ROUTE_KIND_TONE, targetName, targetWhere, tiers } from "../lib/routing";
import type { CatalogModel, CatalogResponse, Deployment, DeploymentsResponse, ModelSources, Recipe, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATE_TONE, STATUS_TONE, gpuLabel, stateLabel } from "../lib/workloads";
import { usePoll, WORKLOADS_POLL_MS } from "./Workloads";

const KINDS = [
  { icon: IconBrain, name: "Chat", detail: "vLLM for safetensors, llama.cpp for GGUF (GPU or CPU)" },
  { icon: IconVectorBezier2, name: "Embeddings", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconSortDescending, name: "Rerank", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconMicrophone, name: "Speech to text", detail: "faster-whisper behind speaches" },
];

export function ModelsPage() {
  const [search, setSearch] = useSearchParams();
  const tab = search.get("tab") === "catalog" ? "catalog" : search.get("tab") === "routes" ? "routes" : "deployments";
  const deps = useApi<DeploymentsResponse>("/deployments");
  const cat = useApi<CatalogResponse>("/models");
  usePoll(deps.reload, WORKLOADS_POLL_MS);
  const canDeploy = cat.data?.can_deploy ?? false;

  return (
    <>
      <PageHeader
        index="03 · Models"
        title="Models"
        lead="Models run here as deployments: a model from the catalog, served by an engine on a machine where it fits. Every ready deployment answers at /v1 with an API key, and in the Playground."
        actions={canDeploy && (
          <Link to="/models/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>
        )}
      />
      <AdvisorBanner pages={["models"]} />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="5-add-a-recommended-model">How do I deploy my first model?</HelpLink>
        <HelpLink page="concepts" anchor="models-recipes-and-estimates">Models, recipes and estimates</HelpLink>
        <HelpLink page="troubleshooting" anchor="a-deployment-does-not-fit">A deployment does not fit</HelpLink>
      </HelpLinks>
      <div className="tabs" role="tablist" aria-label="Models">
        <button type="button" role="tab" aria-selected={tab === "deployments"} className={tab === "deployments" ? "on" : ""}
          onClick={() => setSearch({})}>Deployments{deps.data ? ` (${deps.data.deployments.length})` : ""}</button>
        <button type="button" role="tab" aria-selected={tab === "routes"} className={tab === "routes" ? "on" : ""}
          onClick={() => setSearch({ tab: "routes" })}>Routes and cloud</button>
        <button type="button" role="tab" aria-selected={tab === "catalog"} className={tab === "catalog" ? "on" : ""}
          onClick={() => setSearch({ tab: "catalog" })}>Catalog and recipes</button>
      </div>
      {tab === "routes" ? <Routes /> : tab === "deployments" ? (
        <>
          {deps.error && <Notice tone="error">{deps.error}</Notice>}
          {deps.data && deps.data.deployments.length === 0 && (
            <section className="empty">
              <div className="empty-art empty-art-lanes" aria-hidden="true">
                <span className="lane" /><span className="lane lane-hot" /><span className="lane" />
              </div>
              <div className="empty-copy">
                <h2>No deployments yet</h2>
                <p>
                  A deployment runs a model on one of your machines and makes it callable at <code>/v1</code>.{" "}
                  {!canDeploy ? "Admins and operators deploy models; once one is ready, try it in the Playground or call it with an API key."
                    : cat.data && cat.data.models.length === 0 ? "Start by adding a model to the catalog, then deploy it."
                      : "Choose a model from the catalog; Forgeyard estimates its memory and picks a machine where it fits."}
                </p>
                {canDeploy && (
                  <p className="empty-actions">
                    {cat.data && cat.data.models.length === 0
                      ? (cat.data.can_manage
                        ? <button type="button" className="btn btn-primary" onClick={() => setSearch({ tab: "catalog" })}><IconPlus size={16} aria-hidden="true" /> Add a model</button>
                        : <span className="muted">The catalog is empty: an admin adds models.</span>)
                      : <Link to="/models/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>}
                  </p>
                )}
                <ul className="kinds">
                  {KINDS.map(({ icon: Icon, name, detail }) => (
                    <li key={name}><Icon size={18} stroke={1.6} aria-hidden="true" /><span className="kind-name">{name}</span><span className="kind-detail">{detail}</span></li>
                  ))}
                </ul>
              </div>
            </section>
          )}
          {deps.data && deps.data.deployments.length > 0 && (
            <div className="workloads">{deps.data.deployments.map((d) => <DeploymentCard key={d.id} d={d} />)}</div>
          )}
        </>
      ) : (
        <Catalog data={cat.data} error={cat.error} reload={cat.reload} />
      )}
    </>
  );
}

function Routes() {
  const { data, error } = useApi<RoutesResponse>("/routes");
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <>
      <section className="panel routes-intro">
        <div className="panel-head">
          <h2>Model names at /v1</h2>
          {data.is_admin && <Link to="/models/routes/new" className="btn btn-small"><IconPlus size={14} aria-hidden="true" /> New route</Link>}
        </div>
        <p className="muted small">
          Each name clients send as <code>model</code> has targets in tiers: its deployment first, and, if an admin adds one, a cloud provider's model
          to spill to when local is busy or down. A route can also serve a cloud model only. API keys decide whether a request may use the cloud at all.{" "}
          {data.is_admin && <Link to="/providers">Cloud providers</Link>}
        </p>
      </section>
      {data.routes.length === 0 ? <p className="muted">No routes yet: deploy a model, or add a provider and a route.</p> : (
        <div className="workloads">
          {data.routes.map((r) => (
            <article key={r.id} className="wl" aria-label={`Route ${r.served_name}`}>
              <header className="wl-head">
                <div className="wl-title">
                  {r.kind !== "local" && <IconCloud size={16} aria-hidden="true" />}
                  <h2 className="mono"><Link to={`/models/routes/${r.id}`}>{r.served_name}</Link></h2>
                  <span className={`chip chip-tone-${ROUTE_KIND_TONE[r.kind]}`}>{ROUTE_KIND_LABEL[r.kind]}</span>
                  <span className="chip">{API_KIND[r.api] ?? r.api}</span>
                  {r.local_only && <span className="chip chip-tone-ok">local only</span>}
                  {r.usable === false && <span className="chip chip-tone-off" title="Your keys and the playground cannot use cloud-only models">not for you</span>}
                </div>
                <Link className="btn btn-quiet btn-small" to={`/models/routes/${r.id}`}>{data.is_admin ? "Edit routing" : "Details"}</Link>
              </header>
              <ol className="route-tiers">
                {tiers(r).map((tier, i) => (
                  <li key={i}>
                    <span className="tier-n">{i === 0 ? "First" : "Then"}</span>
                    {tier.map((t) => (
                      <span key={t.id} className={`route-target${t.enabled ? "" : " off"}`}>
                        <span className="mono">{targetName(t)}</span> <span className="muted small">{targetWhere(t)}</span>
                      </span>
                    ))}
                  </li>
                ))}
              </ol>
            </article>
          ))}
        </div>
      )}
    </>
  );
}

function DeploymentCard({ d }: { d: Deployment }) {
  const w = d.workload;
  const tone = STATUS_TONE[w.status];
  return (
    <article className={`wl wl-${tone}`} aria-label={`Deployment ${d.name}`}>
      <header className="wl-head">
        <div className="wl-title">
          <span className={`dot dot-tone-${tone}`} aria-hidden="true" />
          <h2 className="mono"><Link to={`/models/deployments/${d.id}`}>{d.name}</Link></h2>
          <span className={`chip chip-tone-${tone}`}>{w.status}</span>
          <span className="chip">{TASK_LABEL[d.model.task]}</span>
          <span className="chip">{d.recipe}</span>
          <span className="muted small">{w.ready}/{w.replicas} ready</span>
        </div>
        <span className="muted small" title={exact(d.created_at)}>{d.created_by ? `${d.created_by} · ` : ""}{when(d.created_at)}</span>
      </header>
      <p className="wl-cmd mono">{d.model.name} as “{d.served_name}” · {d.estimate.device === "gpu" ? `${gib(d.estimate.per_gpu)} per GPU` : `${gib(d.estimate.ram)} RAM`}{d.route ? ` · ${API_LABEL[d.route.api] ?? d.route.api}` : ""}</p>
      <ul className="reps">
        {w.replica_list.map((r) => {
          const frac = downloadFraction(r);
          const ep = d.endpoints.find((e) => e.replica === r.ordinal);
          return (
            <li key={r.ordinal} className="rep">
              <span className="rep-id mono">#{r.ordinal}</span>
              <span className={`chip chip-tone-${STATE_TONE[r.state]}`}>{stateLabel(r)}</span>
              <span className="rep-machine">{r.machine ? <span className="mono">{r.machine.name}</span> : <span className="muted">not placed</span>}</span>
              <span className="rep-gpus mono">{r.gpus.length ? `GPU ${gpuLabel(r)}` : r.machine ? "CPU" : "—"}</span>
              <span className="rep-ports">{ep && <a className="mono" href={ep.url} target="_blank" rel="noreferrer">{ep.url.replace(/^http:\/\//, "").replace(/\/$/, "")}</a>}</span>
              {frac !== null ? (
                <span className="rep-reason"><span className="bar bar-dl" aria-label={downloadText(r) ?? ""}><span style={{ width: `${Math.round(frac * 100)}%` }} /></span> {downloadText(r)}</span>
              ) : r.reason && <span className="rep-reason">{r.reason}</span>}
            </li>
          );
        })}
      </ul>
    </article>
  );
}

function Catalog({ data, error, reload }: { data: CatalogResponse | null; error: string | null; reload: () => void }) {
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <>
      {data.can_manage && <AddModel onAdded={reload} empty={data.models.length === 0} />}
      <section className="panel" aria-labelledby="catalog-title">
        <div className="panel-head"><h2 id="catalog-title">Catalog</h2><span className="muted small">{data.models.length} model{data.models.length === 1 ? "" : "s"}</span></div>
        {data.models.length === 0 ? (
          <p className="muted small catalog-empty">{data.can_manage
            ? "The catalog lists the models that can be deployed. It is empty: pick one of the recommended models above, or search Hugging Face."
            : "The catalog lists the models that can be deployed. It is empty: an admin adds models."}</p>
        ) : (
          <div className="table-wrap">
            <table className="table catalog-table">
              <thead><tr><th>Model</th><th>Task</th><th>Format</th><th>Size</th><th>Architecture</th><th>Licence</th><th>Recipes</th><th /></tr></thead>
              <tbody>{data.models.map((m) => <ModelRow key={m.id} m={m} canManage={data.can_manage} canDeploy={data.can_deploy} reload={reload} />)}</tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel" aria-labelledby="recipes-title">
        <div className="panel-head"><h2 id="recipes-title">Recipes</h2><span className="muted small">how each engine runs, per hardware</span></div>
        <div className="recipes">{data.recipes.map((r) => <RecipeCard key={r.name} r={r} />)}</div>
        {data.can_manage && <AddRecipe onAdded={reload} />}
      </section>
      {data.can_manage && data.sources && <Sources sources={data.sources} onSaved={reload} />}
    </>
  );
}

function ModelRow({ m, canManage, canDeploy, reload }: { m: CatalogModel; canManage: boolean; canDeploy: boolean; reload: () => void }) {
  const [error, setError] = useState<string | null>(null);
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
    <tr>
      <td data-label="Model">
        <div className="cell-stack">
        <div className="strong">{m.title ?? m.name}</div>
        <div className="mono small muted" title={m.revision ?? undefined}>{m.source}{m.file ? ` · ${m.file}` : ""}{m.revision ? ` @ ${m.revision.slice(0, 10)}` : ""}</div>
        {m.mmproj && <div className="mono small muted" title="Projector file (llama.cpp --mmproj)">+ {m.mmproj.path}</div>}
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
        {m.summary.layers ? <div className="muted">{m.summary.layers} layers{m.summary.kv_heads ? `, ${m.summary.kv_heads} KV heads` : ""}{m.summary.max_context ? `, ${m.summary.max_context} ctx` : ""}</div> : null}
      </td>
      <td data-label="Licence" className="small">
        <div className="cell-stack">
          <span className="mono nowrap">{m.license ?? "—"}</span>
          {m.gated && <span className="muted"><IconLock size={11} aria-hidden="true" /> gated</span>}
        </div>
      </td>
      <td data-label="Recipes" className="mono small">{m.recipes.join(", ") || "none fits"}</td>
      <td className="cell-actions">
        {canDeploy && m.recipes.length > 0 && <Link className="btn btn-small" to={`/models/deploy?model=${encodeURIComponent(m.name)}`}>Deploy</Link>}
        {canManage && <button type="button" className="btn btn-quiet btn-small" disabled={m.deployments > 0}
          title={m.deployments ? "Deployed: delete its deployments first" : undefined} onClick={() => void remove()}>Remove</button>}
      </td>
    </tr>
  );
}

function RecipeCard({ r }: { r: Recipe }) {
  return (
    <article className="recipe" aria-label={`Recipe ${r.name}`}>
      <header>
        <h3 className="mono">{r.name}</h3>
        <span className="chip">{r.engine} {r.version}</span>
        {!r.builtin && <span className="chip chip-tone-run">custom</span>}
        {r.share === "vram" && <span className="chip chip-tone-wait">shares a GPU</span>}
      </header>
      <p className="small">{r.description}</p>
      <p className="small muted">{r.tasks.map((t) => TASK_LABEL[t]).join(", ")} · {r.formats.join(", ")} · estimate: {r.estimate}</p>
      <ul className="variants">
        {r.variants.map((v) => (
          <li key={v.name}>
            <span className={`vendor vendor-${v.vendor}`}>{v.vendor === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{v.vendor.toUpperCase()}</span>
            <span className="mono small variant-what" title={v.runtime === "container" ? v.image ?? undefined : undefined}>{v.runtime === "container" ? imageTag(v.image) : `process: ${v.engine?.split("/").pop() ?? ""}`}</span>
            <span className="muted small">{[v.compute_capability && `cc ${v.compute_capability}`, v.driver && `driver ${v.driver}`, v.arch && v.arch.join(" ")].filter(Boolean).join(" · ")}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}

function AddRecipe({ onAdded }: { onAdded: () => void }) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setError(null);
    try {
      await api("/recipes", { method: "POST", body: { yaml: text } });
      setOpen(false); setText(""); onAdded();
    } catch (e) { setError(errorText(e)); }
  };
  if (!open) return <p><button type="button" className="btn btn-quiet btn-small" onClick={() => setOpen(true)}><IconPlus size={14} aria-hidden="true" /> Add a custom recipe</button></p>;
  return (
    <div className="field" style={{ marginTop: 14 }}>
      <label htmlFor="r-yaml">Recipe (YAML, like the built-in ones)</label>
      <textarea id="r-yaml" className="mono" rows={10} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
      {error && <p className="field-error">{error}</p>}
      <div className="row-gap"><button type="button" className="btn" onClick={() => void save()}>Save recipe</button>
        <button type="button" className="btn btn-quiet" onClick={() => setOpen(false)}>Cancel</button></div>
    </div>
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
  return (
    <section className="panel" aria-labelledby="sources-title">
      <h2 id="sources-title">Model sources</h2>
      <p className="muted small">Hugging Face models come from <code>{sources.hf_endpoint}</code>. Anything else only from what is listed here. Private and metadata addresses are always refused{sources.allow_private_hosts ? " (except: private hosts are allowed on this server for a LAN mirror)" : ""}.</p>
      <div className="grid-2">
        <div className="field"><label htmlFor="s-hosts">Hosts for GGUF URLs (one per line, *.example.org for subdomains)</label>
          <textarea id="s-hosts" className="mono" rows={3} value={hosts} onChange={(e) => setHosts(e.target.value)} /></div>
        <div className="field"><label htmlFor="s-dirs">Directories for local:// models</label>
          <textarea id="s-dirs" className="mono" rows={3} value={dirs} onChange={(e) => setDirs(e.target.value)} />
          <p className="field-hint">Agents must also allow them (--model-local-dir).</p></div>
      </div>
      {msg && <div style={{ marginTop: 12 }}><Notice tone={msg.tone}>{msg.text}</Notice></div>}
      <div className="row-gap" style={{ marginTop: 12 }}><button type="button" className="btn" onClick={() => void save()}>Save sources</button></div>
    </section>
  );
}
