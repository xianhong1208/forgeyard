import { IconBrain, IconCloud, IconCpu, IconLock, IconMicrophone, IconMovie, IconPhoto, IconPhotoAi, IconPlus, IconSortDescending, IconSpeakerphone, IconVectorBezier2 } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { KindMark, ModelKindIcon } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBlock, CopyBox, Notice, PageHeader } from "../components/ui";
import { AddModel } from "./AddModel";
import { api, errorText } from "../lib/api";
import { engineCarry } from "../lib/engineImages";
import { MODEL_KIND, groupByKind, machineKind, whereOnMachine } from "../lib/fleet";
import { imageTag, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { TASK_LABEL, downloadFraction, downloadText, gib, mediaText, params } from "../lib/models";
import { FALLBACK_ANCHOR, fallbackProviders, routeHome } from "../lib/routing";
import type { CatalogModel, CatalogResponse, Deployment, DeploymentsResponse, Machine, MachinesResponse, ModelSources, Recipe, RecipeVariant, RouteInfo, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATUS_TONE, stateLabel } from "../lib/workloads";
import { usePoll, WORKLOADS_POLL_MS } from "./Workloads";

const KINDS = [
  { icon: IconBrain, name: "Chat", detail: "vLLM for safetensors, llama.cpp for GGUF (GPU or CPU)" },
  { icon: IconVectorBezier2, name: "Embeddings", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconSortDescending, name: "Rerank", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconMicrophone, name: "Speech to text", detail: "faster-whisper behind speaches" },
  { icon: IconSpeakerphone, name: "Text to speech", detail: "Qwen3-TTS on vLLM-Omni, llama.cpp TTS GGUFs, Kokoro on speaches" },
  { icon: IconPhotoAi, name: "Images", detail: "diffusers pipelines on vLLM-Omni; stable-diffusion.cpp on AMD (Vulkan), NVIDIA (CUDA) and CPUs" },
  { icon: IconMovie, name: "Video", detail: "Wan text-to-video pipelines on vLLM-Omni (NVIDIA), as jobs at /v1/videos" },
];

export function ModelsPage() {
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();
  const raw = search.get("tab");
  const tab = modelsTab(raw);
  // Old addresses: the Serving tab (1.4, before that "routes") is gone; a link that names a route
  // opens where that route lives now, any other lands on Deployments. "catalog" was the Library.
  const routeRef = tab === "serving" ? search.get("route") : null;
  const legacy = useApi<RoutesResponse>(routeRef ? "/routes" : null);
  useEffect(() => {
    if (tab === "serving") {
      if (!routeRef) setSearch({}, { replace: true });
      else if (legacy.data || legacy.error) {
        const r = legacy.data?.routes.find((x) => String(x.id) === routeRef || x.served_name === routeRef);
        if (r) navigate(routeHome(r), { replace: true });
        else setSearch({}, { replace: true });
      }
    } else if (raw !== null && raw !== tab && tab === "library") setSearch({ tab }, { replace: true });
  }, [tab, raw, routeRef, legacy.data, legacy.error, navigate, setSearch]);
  const deps = useApi<DeploymentsResponse>("/deployments");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  const cat = useApi<CatalogResponse>("/models");
  // For the "→ provider" mark of a deployment with a cloud fallback.
  const routes = useApi<RoutesResponse>("/routes");
  usePoll(deps.reload, WORKLOADS_POLL_MS);
  const canDeploy = cat.data?.can_deploy ?? false;
  const library = tab === "library";

  return (
    <>
      <PageHeader
        index="01 · Models"
        title="Models"
        lead="Models run here as deployments, grouped by what they do; a deployment's page also holds its cloud fallback. The library is what you can deploy. Every ready deployment answers at /v1 with an API key, and in the Playground."
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
        <button type="button" role="tab" aria-selected={!library} className={!library ? "on" : ""}
          onClick={() => setSearch({})}>Deployments{deps.data ? ` (${deps.data.deployments.length})` : ""}</button>
        <button type="button" role="tab" aria-selected={library} className={library ? "on" : ""}
          onClick={() => setSearch({ tab: "library" })}>Library</button>
      </div>
      {!library ? (
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
                        ? <button type="button" className="btn btn-primary" onClick={() => setSearch({ tab: "library" })}><IconPlus size={16} aria-hidden="true" /> Add a model</button>
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
            <ByKind list={deps.data.deployments} machines={machines?.machines ?? []} canDeploy={canDeploy} routes={routes.data?.routes ?? []} />
          )}
        </>
      ) : (
        <Catalog data={cat.data} error={cat.error} reload={cat.reload} />
      )}
    </>
  );
}

export type ModelsTab = "deployments" | "library" | "serving";

// The tab in the address. "serving" is an old address only (the tab is gone in 1.4, #81); the names
// before 1.4 ("routes", "catalog") still work.
export function modelsTab(raw: string | null): ModelsTab {
  if (raw === "serving" || raw === "routes") return "serving";
  if (raw === "library" || raw === "catalog" || raw === "model-library") return "library";
  return "deployments";
}

// 1.4 (#34): the deployments in one section per kind, each with its icon, what it does and its path
// at /v1; one row per deployment with where it runs, its memory and its state. Kinds with none are
// named once at the end.
function ByKind({ list, machines, canDeploy, routes }: { list: Deployment[]; machines: Machine[]; canDeploy: boolean; routes: RouteInfo[] }) {
  const groups = groupByKind(list);
  const none = groups.filter(([, l]) => l.length === 0).map(([k]) => k);
  return (
    <>
      {groups.filter(([, l]) => l.length > 0).map(([k, l]) => {
        const K = MODEL_KIND[k];
        return (
          <section key={k} className="panel kindsec" aria-labelledby={`kind-${k}`}>
            <div className="kindsec-head">
              <span className="kindicon"><ModelKindIcon kind={k} size={18} /></span>
              <div className="kindsec-title">
                <h2 id={`kind-${k}`}>{K.label} <span className="muted small">· {l.length}</span></h2>
                <span className="muted">{K.hint}</span>
              </div>
              <span className="kindsec-api mono">{K.api}</span>
            </div>
            {l.map((d) => <DeploymentRow key={d.id} d={d} machines={machines} cloud={fallbackProviders(routes, d.id)} />)}
          </section>
        );
      })}
      {none.length > 0 && (
        <p className="kinds-none small">
          <span className="muted">None deployed yet:</span>
          {none.map((k) => <span key={k} className="kinds-none-item"><ModelKindIcon kind={k} size={14} /> {MODEL_KIND[k].label}</span>)}
          {canDeploy && <Link to="/models/deploy">Deploy a model</Link>}
        </p>
      )}
    </>
  );
}

function DeploymentRow({ d, machines, cloud }: { d: Deployment; machines: Machine[]; cloud: string[] }) {
  const w = d.workload;
  const tone = STATUS_TONE[w.status];
  const placed = w.replica_list.filter((r) => r.machine);
  // What needs saying about a replica: a download in progress, why it waits, why it is where it is.
  const notes = w.replica_list.flatMap((r) => {
    const frac = downloadFraction(r);
    const tag = w.replica_list.length > 1 ? `#${r.ordinal} ` : "";
    if (frac !== null) return [<span key={r.ordinal} className="deprow-note"><span className="bar bar-dl" aria-label={downloadText(r) ?? ""}><span style={{ width: `${Math.round(frac * 100)}%` }} /></span> {tag}{downloadText(r)}</span>];
    const text = r.not_picked_up ?? r.reason ?? r.placement_note;
    return text && w.status !== "stopped" ? [<span key={r.ordinal} className={`deprow-note${r.not_picked_up ? " rep-stuck" : ""}`}>{tag}{text}</span>] : [];
  });
  return (
    <div className="deprow" role="group" aria-label={`Deployment ${d.name}`}>
      <div className="deprow-main">
        <div className="deprow-name">
          <span className={`dot dot-tone-${tone}`} aria-hidden="true" />
          <Link to={`/models/deployments/${d.id}`}>{d.name}</Link>
          {cloud.length > 0 && (
            <Link className="fallback-mark" to={`/models/deployments/${d.id}#${FALLBACK_ANCHOR}`}
              title={`When ${d.name} is busy or down, requests may move on to ${cloud.join(", ")}`}>
              <IconCloud size={12} aria-hidden="true" /> → {cloud.join(", ")}
            </Link>
          )}
        </div>
        <div className="deprow-model">{d.model.name} · {d.recipe}{d.served_name !== d.name ? <> · as “{d.served_name}”</> : null}</div>
      </div>
      <div className="where">
        {placed.length ? placed.map((r) => {
          const m = machines.find((x) => x.id === r.machine!.id);
          return (
            <Link key={r.ordinal} to={`/machines/${r.machine!.id}`} className="where-chip" title={`Replica #${r.ordinal}: ${stateLabel(r)}`}>
              {m && <KindMark kind={machineKind(m)} size={20} />}
              <span className="mono">{r.machine!.name}</span>
              <span>{whereOnMachine(r)}</span>
            </Link>
          );
        }) : <span className="small muted">{w.status === "stopped" ? "stopped" : "waiting for a machine"}</span>}
      </div>
      <span className="deprow-mem">{d.estimate.device === "gpu" ? `${gib(d.estimate.per_gpu)} / GPU` : `${gib(d.estimate.ram)} RAM`}</span>
      <span className={`chip chip-tone-${tone} deprow-state`}>{w.status === "running" ? `${w.ready}/${w.replicas} ready` : w.status}</span>
      {notes.length > 0 && <div className="deprow-notes">{notes}</div>}
    </div>
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
          <div className="table-wrap catalog-wrap">
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
          <span className="mono nowrap" title={m.license_from ? `The licence of ${m.license_from.replace("hf://", "")}, which this is converted from` : undefined}>{m.license ?? "—"}{m.license_from ? "*" : ""}</span>
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
      <OfflineImages name={r.name} variants={r.variants} />
    </article>
  );
}

// The commands that carry a container image to a machine that cannot reach the registry, for the
// variant picked (one at a time: the recipe card is narrow and the commands are long).
function OfflineImages({ name, variants }: { name: string; variants: RecipeVariant[] }) {
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
          <>Forgeyard checks the loaded image against the ids recorded for it, so this recipe works offline as it is.</>
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
  const seeds = sources.env_seeds ?? [];
  return (
    <section className="panel" aria-labelledby="sources-title">
      <h2 id="sources-title">Model sources</h2>
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
