import { IconBrain, IconCpu, IconMicrophone, IconPlus, IconSortDescending, IconVectorBezier2 } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { API_LABEL, TASK_LABEL, downloadFraction, downloadText, gib, params } from "../lib/models";
import type { CatalogModel, CatalogResponse, Deployment, DeploymentsResponse, ModelSources, Recipe } from "../lib/types";
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
  const tab = search.get("tab") === "catalog" ? "catalog" : "deployments";
  const deps = useApi<DeploymentsResponse>("/deployments");
  const cat = useApi<CatalogResponse>("/models");
  usePoll(deps.reload, WORKLOADS_POLL_MS);
  const canDeploy = cat.data?.can_deploy ?? false;

  return (
    <>
      <PageHeader
        index="03 · Models"
        title="Models"
        lead="Models from the catalog, served with an engine recipe on the machine that fits them. Each deployment reserves the memory it was estimated to need; until the gateway (M4) each replica answers on its own port."
        actions={canDeploy && (
          <Link to="/models/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>
        )}
      />
      <div className="tabs" role="tablist" aria-label="Models">
        <button type="button" role="tab" aria-selected={tab === "deployments"} className={tab === "deployments" ? "on" : ""}
          onClick={() => setSearch({})}>Deployments{deps.data ? ` (${deps.data.deployments.length})` : ""}</button>
        <button type="button" role="tab" aria-selected={tab === "catalog"} className={tab === "catalog" ? "on" : ""}
          onClick={() => setSearch({ tab: "catalog" })}>Catalog and recipes</button>
      </div>
      {tab === "deployments" ? (
        <>
          {deps.error && <Notice tone="error">{deps.error}</Notice>}
          {deps.data && deps.data.deployments.length === 0 && (
            <section className="empty">
              <div className="empty-art empty-art-lanes" aria-hidden="true">
                <span className="lane" /><span className="lane lane-hot" /><span className="lane" />
              </div>
              <div className="empty-copy">
                <h2>No deployments yet</h2>
                <p>{canDeploy ? "Pick a model from the catalog and a recipe: Deploy a model." : "Admins and operators deploy models; they show up here with their endpoints."}</p>
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
      {data.can_manage && <AddModel onAdded={reload} />}
      <section className="panel" aria-labelledby="catalog-title">
        <div className="panel-head"><h2 id="catalog-title">Catalog</h2><span className="muted small">{data.models.length} model{data.models.length === 1 ? "" : "s"}</span></div>
        {data.models.length === 0 ? (
          <p className="muted small">{data.can_manage ? "Nothing yet: add a model above (hf://org/name)." : "An admin adds models to the catalog."}</p>
        ) : (
          <div className="table-wrap">
            <table className="table catalog-table">
              <thead><tr><th>Model</th><th>Task</th><th>Format</th><th>Size</th><th>Architecture</th><th>Recipes</th><th /></tr></thead>
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
        <div className="strong">{m.title ?? m.name}</div>
        <div className="mono small muted" title={m.revision ?? undefined}>{m.source}{m.file ? ` · ${m.file}` : ""}{m.revision ? ` @ ${m.revision.slice(0, 10)}` : ""}</div>
        {error && <div className="field-error">{error}</div>}
      </td>
      <td data-label="Task"><span className="chip">{m.task_label}</span></td>
      <td data-label="Format" className="mono small">{m.format}</td>
      <td data-label="Size" className="small">{bytes(m.size_bytes)}</td>
      <td data-label="Architecture" className="small">
        {m.summary.architecture ?? "—"}{m.summary.params ? ` · ${params(m.summary.params)} params` : ""}
        {m.summary.layers ? <div className="muted">{m.summary.layers} layers{m.summary.kv_heads ? `, ${m.summary.kv_heads} KV heads` : ""}{m.summary.max_context ? `, ${m.summary.max_context} ctx` : ""}</div> : null}
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
            <span className="mono small variant-what">{v.runtime === "container" ? v.image : `process: ${v.engine?.split("/").pop() ?? ""}`}</span>
            <span className="muted small">{[v.compute_capability && `cc ${v.compute_capability}`, v.driver && `driver ${v.driver}`, v.arch && v.arch.join(" ")].filter(Boolean).join(" · ")}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}

const EXAMPLES = [
  { source: "hf://Qwen/Qwen3-4B", name: "qwen3-4b", task: "chat" },
  { source: "hf://Qwen/Qwen3-Embedding-0.6B", name: "qwen3-embedding-0.6b", task: "embed" },
  { source: "hf://Qwen/Qwen3-Reranker-0.6B", name: "qwen3-reranker-0.6b", task: "rerank" },
  { source: "hf://Systran/faster-whisper-small", name: "faster-whisper-small", task: "asr" },
  { source: "hf://ggml-org/Qwen3-1.7B-GGUF", name: "qwen3-1.7b-q4km", task: "chat" },
];

type InspectMeta = { format: string; revision?: string; task_hint?: string | null; size_bytes: number; file?: string; file_count: number;
  gguf_files?: { path: string; size: number }[]; config?: { layers: number; kv_heads: number; max_context: number; dtype: string };
  weights?: { params: Record<string, number> }; gguf?: { layers: number; architecture: string; elements: number } };

function AddModel({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ source: "", revision: "", file: "", name: "", title: "", task: "", token: "" });
  const [meta, setMeta] = useState<{ meta: InspectMeta; recipes: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof typeof f, v: string) => setF((cur) => ({ ...cur, [k]: v }));
  const payload = () => ({ source: f.source.trim(), revision: f.revision.trim() || undefined, file: f.file.trim() || undefined,
    token: f.token.trim() || undefined });
  const inspect = async () => {
    setBusy(true); setError(null); setMeta(null);
    try {
      const r = await api<{ meta: InspectMeta; recipes: string[] }>("/models/inspect", { method: "POST", body: payload() });
      setMeta(r);
      if (!f.task && r.meta.task_hint) set("task", r.meta.task_hint);
      if (!f.name) set("name", f.source.split("/").pop()?.toLowerCase().replace(/[^a-z0-9._-]+/g, "-") ?? "");
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api("/models", { method: "POST", body: { ...payload(), name: f.name.trim(), title: f.title.trim() || undefined, task: f.task || undefined } });
      setOpen(false); setMeta(null); setF({ source: "", revision: "", file: "", name: "", title: "", task: "", token: "" });
      onAdded();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  };
  if (!open) return <p><button type="button" className="btn" onClick={() => setOpen(true)}><IconPlus size={16} aria-hidden="true" /> Add a model to the catalog</button></p>;
  const params = meta?.meta.weights ? Object.values(meta.meta.weights.params).reduce((a, b) => a + b, 0) : meta?.meta.gguf?.elements;
  return (
    <form className="panel panel-link" onSubmit={add} aria-label="Add a model">
      <h2>Add a model</h2>
      <p className="muted small">Hugging Face repositories (hf://org/name), a GGUF file from an allowed host, or a local directory allowed below. The model is inspected (config, headers) without downloading its weights, and pinned to the commit it resolves to.</p>
      <p className="examples small">Examples: {EXAMPLES.map((x) => (
        <button key={x.name} type="button" className="linklike" onClick={() => setF({ ...f, source: x.source, name: x.name, task: x.task })}>{x.source.replace("hf://", "")}</button>
      ))}</p>
      <div className="grid-3">
        <div className="field"><label htmlFor="m-source">Source</label><input id="m-source" className="mono" value={f.source} onChange={(e) => set("source", e.target.value)} placeholder="hf://Qwen/Qwen3-4B" required /></div>
        <div className="field"><label htmlFor="m-rev">Revision</label><input id="m-rev" value={f.revision} onChange={(e) => set("revision", e.target.value)} placeholder="main" /></div>
        <div className="field"><label htmlFor="m-token">Hugging Face token</label><input id="m-token" type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)} placeholder="only for gated models; not stored" /></div>
      </div>
      <div className="row-gap" style={{ marginTop: 12 }}>
        <button type="button" className="btn" disabled={busy || !f.source.trim()} onClick={() => void inspect()}>{busy && !meta ? "Inspecting…" : "Inspect"}</button>
        <button type="button" className="btn btn-quiet" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
      {meta && (
        <>
          <dl className="facts inspect-facts">
            <div className="fact"><dt>Format</dt><dd>{meta.meta.format}{meta.meta.file ? ` · ${meta.meta.file}` : ""}</dd></div>
            <div className="fact"><dt>Pinned to</dt><dd className="mono small">{meta.meta.revision ?? "—"}</dd></div>
            <div className="fact"><dt>Download</dt><dd>{bytes(meta.meta.size_bytes)} in {meta.meta.file_count} files</dd></div>
            {params ? <div className="fact"><dt>Parameters</dt><dd>{(params / 1e9).toFixed(2)} B</dd></div> : null}
            {meta.meta.config && <div className="fact"><dt>Shape</dt><dd>{meta.meta.config.layers} layers, {meta.meta.config.kv_heads} KV heads, {meta.meta.config.max_context} tokens, {meta.meta.config.dtype}</dd></div>}
            <div className="fact"><dt>Recipes</dt><dd>{meta.recipes.join(", ") || "none"}</dd></div>
          </dl>
          {meta.meta.gguf_files && meta.meta.gguf_files.length > 1 && (
            <div className="field narrow"><label htmlFor="m-file">GGUF file</label>
              <select id="m-file" value={f.file || meta.meta.file || ""} onChange={(e) => set("file", e.target.value)}>
                {meta.meta.gguf_files.map((g) => <option key={g.path} value={g.path}>{g.path} ({bytes(g.size)})</option>)}
              </select></div>
          )}
          <div className="grid-3">
            <div className="field"><label htmlFor="m-name">Name</label><input id="m-name" className="mono" value={f.name} onChange={(e) => set("name", e.target.value)} required /></div>
            <div className="field"><label htmlFor="m-title">Title</label><input id="m-title" value={f.title} onChange={(e) => set("title", e.target.value)} /></div>
            <div className="field"><label htmlFor="m-task">Task</label>
              <select id="m-task" value={f.task} onChange={(e) => set("task", e.target.value)} required>
                <option value="">Choose…</option>
                {Object.entries(TASK_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select></div>
          </div>
          <div className="form-foot"><span /><button className="btn btn-primary" disabled={busy}>Add to the catalog</button></div>
        </>
      )}
    </form>
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
