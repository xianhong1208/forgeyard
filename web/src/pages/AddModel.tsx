import { IconAlertTriangle, IconCheck, IconCpu, IconLock, IconPlus, IconSearch } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { Notice } from "../components/ui";
import { ApiError, api, errorText } from "../lib/api";
import {
  compact, deployLink, emptyPick, fitsLine, nameFor, pickFromList, pickFromSearch,
  type InspectResponse, type Pick, type RecommendedModel, type RecommendedResponse, type SearchResponse, type Suggestion,
} from "../lib/addmodel";
import { when } from "../lib/format";
import { bytes } from "../lib/machines";
import { TASK_LABEL, gib, mediaText } from "../lib/models";
import type { Task } from "../lib/types";
import { useApi } from "../lib/useApi";

type Mode = "list" | "search" | "source";
export const SEARCH_DELAY_MS = 500;

const FORMAT_LABEL: Record<string, string> = { safetensors: "safetensors", gguf: "GGUF", ctranslate2: "CTranslate2", onnx: "ONNX", other: "other" };

const LANGUAGE: Record<string, string> = { zh: "Chinese", en: "English", ja: "Japanese", ko: "Korean", de: "German", fr: "French",
  ru: "Russian", pt: "Portuguese", es: "Spanish", it: "Italian" };

// "Chinese, English +8" for a speech model's languages.
export function languagesText(langs: string[]): string {
  const named = langs.slice(0, 2).map((l) => LANGUAGE[l] ?? l).join(", ");
  return langs.length > 2 ? `${named} +${langs.length - 2}` : named;
}

export function AddModel({ onAdded, empty }: { onAdded: () => void; empty: boolean }) {
  const [open, setOpen] = useState(empty);
  if (!open) {
    return (
      <p className="add-model-open">
        <button type="button" className="btn" onClick={() => setOpen(true)}>
          <IconPlus size={16} aria-hidden="true" /> Add a model
        </button>
      </p>
    );
  }
  return <AddModelPanel onAdded={onAdded} onClose={() => setOpen(false)} />;
}

function AddModelPanel({ onAdded, onClose }: { onAdded: () => void; onClose: () => void }) {
  const [mode, setMode] = useState<Mode>("list");
  const [pick, setPick] = useState<Pick | null>(null);
  const [added, setAdded] = useState<{ name: string; suggestion: Suggestion | null } | null>(null);
  const rec = useApi<RecommendedResponse>("/models/recommended");
  const choose = (p: Pick) => { setAdded(null); setPick(p); };
  return (
    <section className="panel panel-link add-model" aria-labelledby="add-model-title">
      <div className="panel-head">
        <h2 id="add-model-title">Add a model</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose}>Close</button>
      </div>
      <p className="muted small add-model-lead">
        Pick a model; Forgeyard reads its configuration from Hugging Face (not the weights), says what it needs and where it
        fits, and pins it to the exact version it finds. Machines download it when you deploy it.
      </p>
      {added && (
        <Notice tone="ok">
          <strong>{added.name}</strong> is in the catalog. <Link to={deployLink(added.name, added.suggestion)}>Deploy it</Link>, or add another.
        </Notice>
      )}
      <div className="segmented add-model-modes" role="group" aria-label="How to find the model">
        {([["list", "Recommended"], ["search", "Search Hugging Face"], ["source", "Enter a source"]] as const).map(([m, label]) => (
          <button key={m} type="button" className={mode === m ? "on" : ""} aria-pressed={mode === m} onClick={() => { setMode(m); setPick(null); }}>{label}</button>
        ))}
      </div>
      {!pick && mode === "list" && <RecommendedList data={rec.data} error={rec.error} onPick={(m) => choose(pickFromList(m))} chosen={pick} />}
      {/* Kept mounted while a result is reviewed, so "Choose another" comes back to the same results. */}
      {mode === "search" && <div hidden={Boolean(pick)}><HubSearch recommended={rec.data?.models ?? []} onPick={choose} onFallback={() => setMode("list")} chosen={pick} /></div>}
      {!pick && mode === "source" && <SourceForm onPick={choose} />}
      {pick && (
        <Review key={`${pick.source}|${pick.revision}|${pick.file}|${pick.from}`} initial={pick}
          onCancel={() => setPick(null)}
          onAdded={(name, s) => { setPick(null); setAdded({ name, suggestion: s }); onAdded(); rec.reload(); }} />
      )}
    </section>
  );
}

// ------------------------------------------------------------------ the recommended list
function RecommendedList({ data, error, onPick, chosen }: {
  data: RecommendedResponse | null; error: string | null; onPick: (m: RecommendedModel) => void; chosen: Pick | null;
}) {
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted small">Loading the list…</p>;
  return (
    <div className="rec-groups">
      {data.groups.map((g) => (
        <section key={g.id} className="rec-group" aria-labelledby={`rec-${g.id}`}>
          <h3 id={`rec-${g.id}`}>{g.title}</h3>
          <p className="muted small">{g.description}</p>
          <ul className="rec-list">
            {data.models.filter((m) => m.group === g.id).map((m) => (
              <li key={m.name} className={`rec${chosen?.source === m.source && chosen.file === (m.file ?? "") ? " rec-on" : ""}`}>
                <div className="rec-head">
                  <span className="strong">{m.title}</span>
                  <span className="chip">{TASK_LABEL[m.task]}</span>
                  {m.device === "cpu" && <span className="chip"><IconCpu size={11} aria-hidden="true" /> CPU</span>}
                  {m.inputs.length > 0 && <span className="chip chip-tone-run">{mediaText(m.inputs)}</span>}
                  {m.languages && m.languages.length > 0 && <span className="chip" title="Languages it speaks">{languagesText(m.languages)}</span>}
                  <span className="chip mono" title={m.license_from ? `Licence of ${m.license_from.replace("hf://", "")}, which this is converted from` : "Licence"}>{m.license}</span>
                </div>
                <p className="rec-note small">{m.note}</p>
                <p className="rec-size small muted">
                  About {m.download_gib} GiB to download; one copy needs about {m.memory_gib} GiB of {m.device === "cpu" ? "RAM" : "GPU memory"}.
                </p>
                {m.gated && (
                  <p className="rec-gated small"><IconLock size={13} aria-hidden="true" /> Needs a Hugging Face token and accepting the licence on Hugging Face.</p>
                )}
                <div className="rec-foot">
                  <span className="mono small muted rec-src">{m.source.replace("hf://", "")}{m.file ? ` · ${m.file}` : ""}</span>
                  {m.in_catalog
                    ? <span className="chip chip-tone-ok">In the catalog as {m.in_catalog}</span>
                    : <button type="button" className="btn btn-small" onClick={() => onPick(m)}>Choose</button>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ Hugging Face search
function HubSearch({ recommended, onPick, onFallback, chosen }: {
  recommended: RecommendedModel[]; onPick: (p: Pick) => void; onFallback: () => void; chosen: Pick | null;
}) {
  const [q, setQ] = useState("");
  const [task, setTask] = useState("");
  const [format, setFormat] = useState("");
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<{ text: string; offline: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState(0);

  useEffect(() => {
    if (!asked && q.trim().length < 2) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({ q: q.trim(), limit: "20" });
      if (task) params.set("task", task);
      if (format) params.set("format", format);
      setBusy(true);
      api<SearchResponse>(`/models/search?${params.toString()}`).then(
        (r) => { if (alive) { setRes(r); setError(null); } },
        (e) => {
          if (!alive) return;
          // 503 hub_unreachable is the server saying it has no way to Hugging Face: show its words.
          const offline = e instanceof ApiError && e.code === "hub_unreachable";
          setRes(null);
          setError({ text: offline ? e.message : errorText(e), offline });
        },
      ).finally(() => alive && setBusy(false));
    }, SEARCH_DELAY_MS);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [q, task, format, asked]);

  const submit = (e: FormEvent) => { e.preventDefault(); setAsked((n) => n + 1); };

  return (
    <div className="hub">
      <form className="hub-form" onSubmit={submit} role="search" aria-label="Hugging Face search">
        <div className="field hub-q">
          <label htmlFor="hub-q">Search Hugging Face</label>
          <input id="hub-q" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="qwen3, whisper, bge-m3…" maxLength={100} />
        </div>
        <div className="field">
          <label htmlFor="hub-task">Task</label>
          <select id="hub-task" value={task} onChange={(e) => setTask(e.target.value)}>
            <option value="">Any task</option>
            {Object.entries(TASK_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="hub-format">Format</label>
          <select id="hub-format" value={format} onChange={(e) => setFormat(e.target.value)}>
            <option value="">Any format</option>
            <option value="safetensors">safetensors (GPU)</option>
            <option value="gguf">GGUF (CPU or GPU)</option>
            <option value="ctranslate2">CTranslate2 (speech)</option>
            <option value="onnx">ONNX (Kokoro, Piper voices)</option>
          </select>
        </div>
        <button type="submit" className="btn hub-go" disabled={busy}><IconSearch size={16} aria-hidden="true" /> {busy ? "Searching…" : "Search"}</button>
      </form>
      <p className="muted small hub-hint">The server asks Hugging Face, most downloaded first. Only models in safetensors, GGUF, CTranslate2 or ONNX (speaches voices) can be served.</p>
      {error && (
        <Notice tone={error.offline ? "info" : "error"}>
          {error.text}{" "}
          {error.offline && <button type="button" className="linklike linklike-text" onClick={onFallback}>Show the recommended models</button>}
        </Notice>
      )}
      {res && res.results.length === 0 && <p className="muted small">Nothing found. Try a shorter name, or another task or format.</p>}
      {res && res.results.length > 0 && (
        <div className="table-wrap">
          <table className="table hub-table">
            <thead><tr><th>Model</th><th>What</th><th>Size</th><th>Downloads</th><th>Updated</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {res.results.map((r) => {
                // A repository on the recommended list is added the way the list says (its revision,
                // recipe and engine arguments): BGE-M3 is only servable through its safetensors revision.
                const listed = recommended.filter((m) => m.source === r.source);
                const entry = listed.length === 1 ? listed[0] : undefined;
                const servable = r.recipes.length > 0 || Boolean(entry);
                return (
                  <tr key={r.id} className={chosen?.source === r.source ? "row-on" : undefined}>
                    <td data-label="Model">
                      <div className="mono small hub-id">{r.id}</div>
                      {entry && <span className="chip chip-tone-ok">recommended</span>}{" "}
                      {r.gated && <span className="chip chip-tone-wait"><IconLock size={11} aria-hidden="true" /> needs a token</span>}
                    </td>
                    <td data-label="What" className="small">
                      {r.task ? TASK_LABEL[r.task] : r.pipeline_tag ?? "unknown task"} · {FORMAT_LABEL[r.format] ?? r.format}
                    </td>
                    <td data-label="Size" className="small">
                      {r.weights_bytes ? bytes(r.weights_bytes) : r.params ? `${compact(r.params)} params` : "—"}
                    </td>
                    <td data-label="Downloads" className="small">{compact(r.downloads)} <span className="muted">· {compact(r.likes)} likes</span></td>
                    <td data-label="Updated" className="small">{r.last_modified ? when(r.last_modified) : "—"}</td>
                    <td className="cell-actions">
                      {r.in_catalog ? <span className="chip chip-tone-ok">In the catalog as {r.in_catalog}</span>
                        : servable ? <button type="button" className="btn btn-small" onClick={() => onPick(entry ? pickFromList(entry) : pickFromSearch(r))}>Choose</button>
                          : <span className="muted small" title="No recipe reads this format for this task">Cannot be served</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ typing a source
function SourceForm({ onPick }: { onPick: (p: Pick) => void }) {
  const [source, setSource] = useState("");
  const [revision, setRevision] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const s = source.trim();
    const full = /^(hf|local):\/\/|^https?:\/\//.test(s) ? s : `hf://${s}`;
    onPick({ ...emptyPick(), source: full, revision: revision.trim(), name: "", from: "source" });
  };
  return (
    <form className="source-form" onSubmit={submit} aria-label="Enter a source">
      <div className="grid-2">
        <div className="field">
          <label htmlFor="m-source">Source</label>
          <input id="m-source" className="mono" value={source} onChange={(e) => setSource(e.target.value)} placeholder="Qwen/Qwen3-4B" required />
          <p className="field-hint">A Hugging Face repository (<code>org/name</code> or <code>hf://org/name</code>), the https:// address of a .gguf file from an allowed host, or <code>local:///dir</code> from an allowed directory.</p>
        </div>
        <div className="field">
          <label htmlFor="m-rev">Revision</label>
          <input id="m-rev" value={revision} onChange={(e) => setRevision(e.target.value)} placeholder="main" />
          <p className="field-hint">A branch, tag or commit. Empty: main.</p>
        </div>
      </div>
      <div className="row-gap" style={{ marginTop: 12 }}><button className="btn" disabled={!source.trim()}>Look it up</button></div>
    </form>
  );
}

// ------------------------------------------------------------------ review and add
function Review({ initial, onCancel, onAdded }: { initial: Pick; onCancel: () => void; onAdded: (name: string, s: Suggestion | null) => void }) {
  const [f, setF] = useState<Pick>(initial);
  const [res, setRes] = useState<InspectResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needsToken, setNeedsToken] = useState(initial.gated);
  const [busy, setBusy] = useState(false);
  // What the inspection depends on, changed only by the person (not by what an answer fills in).
  const [ask, setAsk] = useState({ file: initial.file, mmproj: initial.mmproj, task: initial.task, device: initial.device as string, format: "", round: 0 });
  const [answered, setAnswered] = useState<string | null>(null);
  const isLocal = f.source.startsWith("local://");
  const set = (k: keyof Pick, v: string) => {
    setF((cur) => ({ ...cur, [k]: v }));
    if (k === "file" || k === "mmproj" || k === "task" || k === "device" || k === "format") setAsk((cur) => ({ ...cur, [k]: v }));
  };

  // Inspect on open, and again after a token, a GGUF file, the task, the device or the format is chosen.
  const key = JSON.stringify(ask);
  const skip = isLocal && !ask.format;
  const inspecting = !skip && answered !== key;
  useEffect(() => {
    if (skip) return;
    let alive = true;
    const body = { source: f.source, revision: f.revision || undefined, file: ask.file || undefined, mmproj: ask.mmproj || undefined, token: f.token.trim() || undefined,
      task: ask.task || undefined, recipe: f.recipe || undefined, device: ask.device || undefined, format: ask.format || undefined };
    api<InspectResponse>("/models/inspect", { method: "POST", body }).then(
      (r) => {
        if (!alive) return;
        setRes(r);
        setError(null);
        setAnswered(key);
        setF((cur) => ({ ...cur, task: cur.task || r.meta.task_hint || "", file: cur.file || r.meta.file || "",
          name: cur.name || nameFor(r.meta.file ? r.meta.file.replace(/\.gguf$/i, "") : cur.source) }));
      },
      (e) => {
        if (!alive) return;
        setRes(null);
        setAnswered(key);
        const text = errorText(e);
        setError(text);
        if (/gated or private/.test(text)) setNeedsToken(true);
      },
    );
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-inspect only when what it depends on changes
  }, [key, skip]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { source: f.source, revision: f.revision || undefined, file: f.file || undefined, mmproj: f.mmproj || undefined,
        token: f.token.trim() || undefined, name: f.name.trim(), title: f.title.trim() || undefined, task: f.task || undefined,
        format: f.format || undefined };
      if (Object.keys(f.engine_args).length) body.engine_args = f.engine_args;
      const r = await api<{ model: { name: string } }>("/models", { method: "POST", body });
      onAdded(r.model.name, res?.suggestion ?? null);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const meta = res?.meta;
  const s = res?.suggestion;
  const params = meta?.weights ? Object.values(meta.weights.params).reduce((a, b) => a + b, 0) : meta?.gguf?.elements;
  const label = f.title || f.source.replace(/^hf:\/\//, "");
  return (
    <form className="review" onSubmit={add} aria-label={`Add ${label}`}>
      <div className="panel-head review-head">
        <h3>{label}</h3>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>Choose another</button>
      </div>
      {inspecting && <p className="waiting small" role="status"><span className="pulse" aria-hidden="true" /> Reading {f.source.replace(/^hf:\/\//, "")} on Hugging Face…</p>}
      {error && !inspecting && <Notice tone="error">{error}</Notice>}
      {needsToken && (
        <div className="token-row">
          <div className="field">
            <label htmlFor="m-token">Hugging Face token</label>
            <input id="m-token" type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)} placeholder="hf_…" />
            <p className="field-hint">
              This model is gated: accept its licence on huggingface.co with your account, then paste a read token from your
              Hugging Face settings. It is used to read the model now and is not stored; deployments ask for it again.
            </p>
          </div>
          <button type="button" className="btn" disabled={!f.token.trim() || inspecting} onClick={() => setAsk((cur) => ({ ...cur, round: cur.round + 1 }))}>Read it with this token</button>
        </div>
      )}
      {isLocal && (
        <>
          <Notice>The server cannot look inside a local directory: the machines that have it load the model. Say what format it is; when deploying, give the memory it needs.</Notice>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="m-format">Format</label>
              <select id="m-format" value={f.format} onChange={(e) => set("format", e.target.value)} required>
                <option value="">Choose…</option>
                <option value="safetensors">safetensors</option>
                <option value="gguf">GGUF</option>
                <option value="ctranslate2">CTranslate2</option>
                <option value="onnx">ONNX</option>
              </select>
            </div>
            {f.format === "gguf" && (
              <div className="field">
                <label htmlFor="m-lfile">GGUF file in the directory</label>
                <input id="m-lfile" className="mono" value={f.file} onChange={(e) => set("file", e.target.value)} required placeholder="model-Q4_K_M.gguf" />
              </div>
            )}
          </div>
        </>
      )}
      {meta && (
        <dl className="facts inspect-facts">
          <div className="fact"><dt>Format</dt><dd>{FORMAT_LABEL[meta.format] ?? meta.format}{meta.file ? ` · ${meta.file}` : ""}</dd></div>
          <div className="fact"><dt>Pinned to</dt><dd className="mono small">{meta.revision ? `${meta.revision.slice(0, 12)} (${meta.ref || "main"})` : "—"}</dd></div>
          <div className="fact"><dt>Download</dt><dd>{bytes(meta.size_bytes)} in {meta.file_count} files</dd></div>
          {params ? <div className="fact"><dt>Parameters</dt><dd>{(params / 1e9).toFixed(2)} B</dd></div> : null}
          {meta.modalities !== undefined && <div className="fact"><dt>Takes</dt><dd>{mediaText(meta.modalities) === "Text only" ? "Text only" : `Text, ${mediaText(meta.modalities)?.toLowerCase()}`}</dd></div>}
          {meta.license !== undefined && <div className="fact"><dt>Licence</dt><dd className="mono small">{meta.license ?? "not stated"}{meta.gated ? " · gated" : ""}</dd></div>}
          {meta.config && <div className="fact"><dt>Shape</dt><dd>{meta.config.layers} layers, {meta.config.kv_heads} KV heads, up to {meta.config.max_context.toLocaleString("en-US")} tokens, {meta.config.dtype}</dd></div>}
        </dl>
      )}
      {meta?.gguf_files && meta.gguf_files.length > 1 && (
        <div className="field gguf-pick">
          <label htmlFor="m-file">GGUF file</label>
          <select id="m-file" value={f.file || meta.file || ""} onChange={(e) => set("file", e.target.value)}>
            {meta.gguf_files.map((g) => <option key={g.path} value={g.path}>{g.path} ({bytes(g.size)})</option>)}
          </select>
          <p className="field-hint">Smaller files (Q4) need less memory and run faster; larger ones (Q8, F16) answer a little better.</p>
        </div>
      )}
      {meta?.mmproj_files && meta.mmproj_files.length > 0 && (
        <div className="field gguf-pick">
          <label htmlFor="m-mmproj">Projector (for images)</label>
          <select id="m-mmproj" value={f.mmproj || meta.mmproj?.path || "none"} onChange={(e) => set("mmproj", e.target.value)}>
            {meta.mmproj_files.map((g) => <option key={g.path} value={g.path}>{g.path} ({bytes(g.size)})</option>)}
            <option value="none">None: text only</option>
          </select>
          <p className="field-hint">llama.cpp reads images through this file (<code>--mmproj</code>). F16 is the usual choice; Q8_0 needs about half the memory.</p>
        </div>
      )}
      {s && !isLocal && <SuggestionPanel s={s} device={f.device} onDevice={(d) => set("device", d)} />}
      {(meta || isLocal) && (
        <>
          <div className="grid-3">
            <div className="field">
              <label htmlFor="m-name">Name in the catalog</label>
              <input id="m-name" className="mono" value={f.name} onChange={(e) => set("name", e.target.value)} required pattern="[a-z0-9]([a-z0-9._\-]{0,62}[a-z0-9])?" />
              <p className="field-hint">Lowercase letters, digits, . _ -</p>
            </div>
            <div className="field">
              <label htmlFor="m-title">Title</label>
              <input id="m-title" value={f.title} onChange={(e) => set("title", e.target.value)} placeholder="optional" />
            </div>
            <div className="field">
              <label htmlFor="m-task">Task</label>
              <select id="m-task" value={f.task} onChange={(e) => set("task", e.target.value)} required>
                <option value="">Choose…</option>
                {(Object.entries(TASK_LABEL) as [Task, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </div>
          </div>
          {Object.keys(f.engine_args).length > 0 && (
            <p className="small muted engine-args">
              Comes with engine arguments for {Object.keys(f.engine_args).join(", ")}: <code>{Object.values(f.engine_args)[0].slice(0, 2).join(" ")}…</code>
            </p>
          )}
          <div className="form-foot">
            <span className="muted small">Adding downloads nothing yet. Machines fetch the model when it is deployed.</span>
            <button className="btn btn-primary" disabled={busy || inspecting || (!meta && !isLocal)}>{busy ? "Adding…" : "Add to the catalog"}</button>
          </div>
        </>
      )}
    </form>
  );
}

function SuggestionPanel({ s, device, onDevice }: { s: Suggestion; device: string; onDevice: (d: "gpu" | "cpu") => void }) {
  if (!s.recipe) return <Notice tone="error">{s.error}</Notice>;
  const est = s.estimate;
  const where = fitsLine(s);
  const cpu = s.device === "cpu";
  return (
    <div className="suggest" aria-label="What it needs">
      <div className="suggest-main">
        <p className="suggest-line">
          Runs with <strong className="mono">{s.recipe}</strong> on {cpu ? "the CPU" : "a GPU"}
          {est ? <>; one copy reserves about <strong>{gib(est.amount)}</strong> of {cpu ? "RAM" : "GPU memory"}</> : null}.
        </p>
        {s.devices && s.devices.length > 1 && (
          <div className="segmented segmented-small" role="group" aria-label="Estimate for">
            {s.devices.map((d) => (
              <button key={d} type="button" className={(device || s.device) === d ? "on" : ""} aria-pressed={(device || s.device) === d} onClick={() => onDevice(d)}>
                {d === "gpu" ? "GPU" : "CPU"}
              </button>
            ))}
          </div>
        )}
      </div>
      {s.error && <Notice tone="error">{s.error}</Notice>}
      {est && (
        <ul className="suggest-parts small">
          {est.parts.map((p) => <li key={p.label}><span>{p.label}</span><span className="mono">{gib(p.bytes)}</span></li>)}
        </ul>
      )}
      {where ? (
        <p className="suggest-fit"><IconCheck size={16} aria-hidden="true" /> Fits now on {where}.</p>
      ) : est ? (
        <p className="suggest-nofit"><IconAlertTriangle size={16} aria-hidden="true" /> {(s.summary ?? "Does not fit on any machine right now.").replace(/([^.])$/, "$1.")}
          {" "}You can still add it; deploy it once a machine has room{cpu ? "" : ", or on more GPUs"}.</p>
      ) : null}
      <p className="muted small">With the recipe's defaults ({s.recipes.length > 1 ? `also possible: ${s.recipes.filter((r) => r !== s.recipe).join(", ")}; ` : ""}context and concurrency can be changed when deploying).</p>
    </div>
  );
}
