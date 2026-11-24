import { IconAlertTriangle, IconCheck, IconCpu, IconLock, IconPlus, IconSearch } from "@tabler/icons-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { Notice } from "../components/ui";
import { labels, t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { ApiError, api, errorText } from "../lib/api";
import {
  compact, deployLink, emptyPick, fitsLine, nameFor, pickFromList, pickFromSearch, searchLicenceProblem, visionProblem,
  type InspectMeta, type InspectResponse, type Pick, type RecommendedModel, type RecommendedResponse, type SearchResponse, type Suggestion,
} from "../lib/addmodel";
import { when } from "../lib/format";
import { bytes } from "../lib/machines";
import { TASK_LABEL, billions, gib, mediaText, sentText } from "../lib/models";
import type { Task } from "../lib/types";
import { useApi } from "../lib/useApi";
import { licencePage, licenceTerms } from "../lib/licences";

type Mode = "list" | "search" | "source";
export const SEARCH_DELAY_MS = 500;

const FORMAT_LABEL: Record<string, string> = { safetensors: "safetensors", gguf: "GGUF", ctranslate2: "CTranslate2", onnx: "ONNX", diffusers: "diffusers" };
const formatLabel = (f: string) => (f === "other" ? tNow("addmodel.format.other") : FORMAT_LABEL[f] ?? f);

// What each file beside an image model's main file is (stable-diffusion.cpp).
const COMPONENT_LABEL: Record<string, string> = { vae: "VAE", t5xxl: "T5-XXL", clip_l: "CLIP-L", clip_g: "CLIP-G" };
const componentLabel = (k: string) => (k === "llm" ? tNow("addmodel.component.llm") : COMPONENT_LABEL[k] ?? k);

export function imageFacts(img: NonNullable<InspectMeta["image"]>, d?: InspectMeta["diffusers"]): string {
  const parts = d ? Object.entries(d.components).map(([k, c]) => `${k} ${billions(c.params)}`)
    : Object.entries(img.components ?? {}).map(([k, p]) => `${componentLabel(k)} ${p}`);
  return [`${img.size}x${img.size}`, ...parts].join(" · ");
}

// A video pipeline's own size and frame rate, and its components (1.3).
export function videoFacts(v: NonNullable<InspectMeta["video"]>, d?: InspectMeta["diffusers"]): string {
  const parts = d ? Object.entries(d.components).map(([k, c]) => `${k} ${billions(c.params)}`) : [];
  return [tNow("addmodel.videoSize", { width: String(v.size[0]), height: String(v.size[1]), fps: String(v.fps) }), ...parts].join(" · ");
}

const LANGUAGE: Record<string, string> = labels({ zh: "addmodel.lang.zh", en: "addmodel.lang.en", ja: "addmodel.lang.ja", ko: "addmodel.lang.ko",
  de: "addmodel.lang.de", fr: "addmodel.lang.fr", ru: "addmodel.lang.ru", pt: "addmodel.lang.pt", es: "addmodel.lang.es", it: "addmodel.lang.it",
  multilingual: "addmodel.lang.multilingual" });

// "Chinese, English +8" (中文、英文 +8) for a speech model's languages.
export function languagesText(langs: string[]): string {
  const named = langs.slice(0, 2).map((l) => LANGUAGE[l] ?? l).join(tNow("library.listSep"));
  return langs.length > 2 ? `${named} +${langs.length - 2}` : named;
}

export function AddModel({ onAdded, empty }: { onAdded: () => void; empty: boolean }) {
  const t = useT();
  const [open, setOpen] = useState(empty);
  if (!open) {
    return (
      <p className="add-model-open">
        <button type="button" className="btn" onClick={() => setOpen(true)}>
          <IconPlus size={16} aria-hidden="true" /> {t("addmodel.open")}
        </button>
      </p>
    );
  }
  return <AddModelPanel onAdded={onAdded} onClose={() => setOpen(false)} />;
}

function AddModelPanel({ onAdded, onClose }: { onAdded: () => void; onClose: () => void }) {
  const t = useT();
  const [mode, setMode] = useState<Mode>("list");
  const [pick, setPick] = useState<Pick | null>(null);
  const [added, setAdded] = useState<{ name: string; suggestion: Suggestion | null } | null>(null);
  const rec = useApi<RecommendedResponse>("/models/recommended");
  const choose = (p: Pick) => { setAdded(null); setPick(p); };
  return (
    <section className="panel panel-link add-model" aria-labelledby="add-model-title">
      <div className="panel-head">
        <h2 id="add-model-title">{t("addmodel.open")}</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose}>{t("common.close")}</button>
      </div>
      <p className="muted small add-model-lead">{t("addmodel.lead")}</p>
      {added && (
        <Notice tone="ok">
          {t.rich("addmodel.added", { name: <strong>{added.name}</strong>, link: (x) => <Link to={deployLink(added.name, added.suggestion)}>{x}</Link> })}
        </Notice>
      )}
      <div className="segmented add-model-modes" role="group" aria-label={t("addmodel.modes")}>
        {(["list", "search", "source"] as const).map((m) => (
          <button key={m} type="button" className={mode === m ? "on" : ""} aria-pressed={mode === m} onClick={() => { setMode(m); setPick(null); }}>{t(`addmodel.mode.${m}`)}</button>
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
  const t = useT();
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted small">{t("addmodel.rec.loading")}</p>;
  return (
    <div className="rec-groups">
      {data.groups.map((g) => (
        <section key={g.id} className="rec-group" aria-labelledby={`rec-${g.id}`}>
          <h3 id={`rec-${g.id}`}>{sentText(`addmodel.group.${g.id}.title`, g.title)}</h3>
          <p className="muted small">{sentText(`addmodel.group.${g.id}.description`, g.description)}</p>
          <ul className="rec-list">
            {data.models.filter((m) => m.group === g.id).map((m) => (
              <li key={m.name} className={`rec${chosen?.source === m.source && chosen.file === (m.file ?? "") ? " rec-on" : ""}`}>
                <div className="rec-head">
                  <span className="strong">{m.title}</span>
                  <span className="chip">{TASK_LABEL[m.task]}</span>
                  {m.device === "cpu" && <span className="chip"><IconCpu size={11} aria-hidden="true" /> CPU</span>}
                  {m.inputs.length > 0 && <span className="chip chip-tone-run">{mediaText(m.inputs)}</span>}
                  {m.languages && m.languages.length > 0 && <span className="chip" title={t("addmodel.rec.languages")}>{languagesText(m.languages)}</span>}
                  <span className={`chip mono${licenceTerms(m.license) ? " chip-tone-run" : ""}`} title={m.license_from ? t("addmodel.rec.licenceFrom", { source: m.license_from.replace("hf://", "") }) : t("addmodel.rec.licence")}>{m.license}</span>
                </div>
                <p className="rec-note small">{m.note}</p>
                <p className="rec-size small muted">
                  {t(m.device === "cpu" ? "addmodel.rec.sizeRam" : "addmodel.rec.sizeGpu", { download: String(m.download_gib), memory: String(m.memory_gib) })}
                </p>
                {licenceTerms(m.license) && (
                  <p className="rec-terms small">
                    {licenceTerms(m.license)}{" "}
                    {licencePage(m.source, m.license_from) && <a href={licencePage(m.source, m.license_from)!} target="_blank" rel="noreferrer">{t("addmodel.rec.theLicence")}</a>}
                  </p>
                )}
                {m.gated && (
                  <p className="rec-gated small"><IconLock size={13} aria-hidden="true" /> {t("addmodel.rec.gated")}</p>
                )}
                <div className="rec-foot">
                  <span className="mono small muted rec-src">{m.source.replace("hf://", "")}{m.file ? ` · ${m.file}` : ""}</span>
                  {m.in_catalog
                    ? <span className="chip chip-tone-ok">{t("addmodel.inCatalog", { name: m.in_catalog })}</span>
                    : <button type="button" className="btn btn-small" onClick={() => onPick(m)}>{t("addmodel.choose")}</button>}
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
  const t = useT();
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
      <form className="hub-form" onSubmit={submit} role="search" aria-label={t("addmodel.search.aria")}>
        <div className="field hub-q">
          <label htmlFor="hub-q">{t("addmodel.search.label")}</label>
          <input id="hub-q" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="qwen3, whisper, bge-m3…" maxLength={100} />
        </div>
        <div className="field">
          <label htmlFor="hub-task">{t("addmodel.search.task")}</label>
          <select id="hub-task" value={task} onChange={(e) => setTask(e.target.value)}>
            <option value="">{t("addmodel.search.anyTask")}</option>
            {Object.entries(TASK_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="hub-format">{t("addmodel.search.format")}</label>
          <select id="hub-format" value={format} onChange={(e) => setFormat(e.target.value)}>
            <option value="">{t("addmodel.search.anyFormat")}</option>
            <option value="safetensors">safetensors (GPU)</option>
            <option value="gguf">{t("addmodel.search.gguf")}</option>
            <option value="ctranslate2">{t("addmodel.search.ctranslate2")}</option>
            <option value="onnx">{t("addmodel.search.onnx")}</option>
            <option value="diffusers">{t("addmodel.search.diffusers")}</option>
          </select>
        </div>
        <button type="submit" className="btn hub-go" disabled={busy}><IconSearch size={16} aria-hidden="true" /> {busy ? t("addmodel.search.searching") : t("addmodel.search.go")}</button>
      </form>
      <p className="muted small hub-hint">{t("addmodel.search.hint")}</p>
      {error && (
        <Notice tone={error.offline ? "info" : "error"}>
          {error.text}{" "}
          {error.offline && <button type="button" className="linklike linklike-text" onClick={onFallback}>{t("addmodel.search.showRecommended")}</button>}
        </Notice>
      )}
      {res && res.results.length === 0 && <p className="muted small">{t("addmodel.search.nothing")}</p>}
      {res && res.results.length > 0 && (
        <div className="table-wrap">
          <table className="table hub-table">
            <thead><tr><th>{t("addmodel.search.col.model")}</th><th>{t("addmodel.search.col.what")}</th><th>{t("addmodel.search.col.size")}</th>
              <th>{t("addmodel.search.col.downloads")}</th><th>{t("addmodel.search.col.updated")}</th><th><span className="sr-only">{t("addmodel.search.col.actions")}</span></th></tr></thead>
            <tbody>
              {res.results.map((r) => {
                // A repository on the recommended list is added the way the list says (its revision,
                // recipe and engine arguments): BGE-M3 is only servable through its safetensors revision.
                const listed = recommended.filter((m) => m.source === r.source);
                const entry = listed.length === 1 ? listed[0] : undefined;
                const servable = r.recipes.length > 0 || Boolean(entry);
                return (
                  <tr key={r.id} className={chosen?.source === r.source ? "row-on" : undefined}>
                    <td data-label={t("addmodel.search.col.model")}>
                      <div className="mono small hub-id">{r.id}</div>
                      {entry && <span className="chip chip-tone-ok">{t("addmodel.search.recommended")}</span>}{" "}
                      {r.gated && <span className="chip chip-tone-wait"><IconLock size={11} aria-hidden="true" /> {t("addmodel.search.needsToken")}</span>}
                    </td>
                    <td data-label={t("addmodel.search.col.what")} className="small">
                      {r.task ? TASK_LABEL[r.task] : r.pipeline_tag ?? t("addmodel.search.unknownTask")} · {formatLabel(r.format)}
                      {r.license && <div className="mono muted">{r.license}</div>}
                    </td>
                    <td data-label={t("addmodel.search.col.size")} className="small">
                      {r.weights_bytes ? bytes(r.weights_bytes) : r.params ? t("addmodel.search.params", { count: compact(r.params) }) : "—"}
                    </td>
                    <td data-label={t("addmodel.search.col.downloads")} className="small">{compact(r.downloads)} <span className="muted">{t("addmodel.search.likes", { count: compact(r.likes) })}</span></td>
                    <td data-label={t("addmodel.search.col.updated")} className="small">{r.last_modified ? when(r.last_modified) : "—"}</td>
                    <td className="cell-actions">
                      {r.in_catalog ? <span className="chip chip-tone-ok">{t("addmodel.inCatalog", { name: r.in_catalog })}</span>
                        : searchLicenceProblem(r) ? <span className="muted small">{searchLicenceProblem(r)}</span>
                        : servable ? <button type="button" className="btn btn-small" onClick={() => onPick(entry ? pickFromList(entry) : pickFromSearch(r))}>{t("addmodel.choose")}</button>
                          : <span className="muted small" title={t("addmodel.search.cannotTitle")}>{t("addmodel.search.cannot")}</span>}
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
  const t = useT();
  const [source, setSource] = useState("");
  const [revision, setRevision] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const s = source.trim();
    const full = /^(hf|local):\/\/|^https?:\/\//.test(s) ? s : `hf://${s}`;
    onPick({ ...emptyPick(), source: full, revision: revision.trim(), name: "", from: "source" });
  };
  return (
    <form className="source-form" onSubmit={submit} aria-label={t("addmodel.source.aria")}>
      <div className="grid-2">
        <div className="field">
          <label htmlFor="m-source">{t("addmodel.source.label")}</label>
          <input id="m-source" className="mono" value={source} onChange={(e) => setSource(e.target.value)} placeholder="Qwen/Qwen3-4B" required />
          <p className="field-hint">{t.rich("addmodel.source.hint", { repo: <code>org/name</code>, hf: <code>hf://org/name</code>, local: <code>local:///dir</code> })}</p>
        </div>
        <div className="field">
          <label htmlFor="m-rev">{t("addmodel.source.revision")}</label>
          <input id="m-rev" value={revision} onChange={(e) => setRevision(e.target.value)} placeholder="main" />
          <p className="field-hint">{t("addmodel.source.revisionHint")}</p>
        </div>
      </div>
      <div className="row-gap" style={{ marginTop: 12 }}><button className="btn" disabled={!source.trim()}>{t("addmodel.source.go")}</button></div>
    </form>
  );
}

// ------------------------------------------------------------------ review and add
function Review({ initial, onCancel, onAdded }: { initial: Pick; onCancel: () => void; onAdded: (name: string, s: Suggestion | null) => void }) {
  const t = useT();
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
      // An image model's files beside the main one: what the inspection found (or the recommended list named).
      if (f.task === "image" && res?.meta.image?.components) body.components = res.meta.image.components;
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
  const params = meta?.weights ? Object.values(meta.weights.params).reduce((a, b) => a + b, 0)
    : meta?.diffusers ? Object.values(meta.diffusers.components).reduce((a, c) => a + c.params, 0) : meta?.gguf?.elements;
  const label = f.title || f.source.replace(/^hf:\/\//, "");
  return (
    <form className="review" onSubmit={add} aria-label={t("addmodel.review.aria", { label })}>
      <div className="panel-head review-head">
        <h3>{label}</h3>
        <button type="button" className="btn btn-quiet btn-small" onClick={onCancel}>{t("addmodel.review.another")}</button>
      </div>
      {inspecting && <p className="waiting small" role="status"><span className="pulse" aria-hidden="true" /> {t("addmodel.review.reading", { source: f.source.replace(/^hf:\/\//, "") })}</p>}
      {error && !inspecting && <Notice tone="error">{error}</Notice>}
      {needsToken && (
        <div className="token-row">
          <div className="field">
            <label htmlFor="m-token">{t("addmodel.review.token")}</label>
            <input id="m-token" type="password" autoComplete="off" value={f.token} onChange={(e) => set("token", e.target.value)} placeholder="hf_…" />
            <p className="field-hint">{t("addmodel.review.tokenHint")}</p>
          </div>
          <button type="button" className="btn" disabled={!f.token.trim() || inspecting} onClick={() => setAsk((cur) => ({ ...cur, round: cur.round + 1 }))}>{t("addmodel.review.readWithToken")}</button>
        </div>
      )}
      {isLocal && (
        <>
          <Notice>{t("addmodel.review.local")}</Notice>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="m-format">{t("addmodel.review.format")}</label>
              <select id="m-format" value={f.format} onChange={(e) => set("format", e.target.value)} required>
                <option value="">{t("addmodel.review.choosePlaceholder")}</option>
                <option value="safetensors">safetensors</option>
                <option value="gguf">GGUF</option>
                <option value="ctranslate2">CTranslate2</option>
                <option value="onnx">ONNX</option>
                <option value="diffusers">diffusers</option>
              </select>
            </div>
            {f.format === "gguf" && (
              <div className="field">
                <label htmlFor="m-lfile">{t("addmodel.review.localFile")}</label>
                <input id="m-lfile" className="mono" value={f.file} onChange={(e) => set("file", e.target.value)} required placeholder="model-Q4_K_M.gguf" />
              </div>
            )}
          </div>
        </>
      )}
      {meta && (
        <dl className="facts inspect-facts">
          <div className="fact"><dt>{t("addmodel.review.format")}</dt><dd>{formatLabel(meta.format)}{meta.file ? ` · ${meta.file}` : ""}</dd></div>
          <div className="fact"><dt>{t("addmodel.fact.pinned")}</dt><dd className="mono small">{meta.revision ? `${meta.revision.slice(0, 12)} (${meta.ref || "main"})` : "—"}</dd></div>
          <div className="fact"><dt>{t("addmodel.fact.download")}</dt><dd>{t("addmodel.fact.downloadValue", { size: bytes(meta.size_bytes), count: String(meta.file_count) })}</dd></div>
          {params ? <div className="fact"><dt>{t("addmodel.fact.parameters")}</dt><dd>{billions(params)}</dd></div> : null}
          {meta.vision && <div className="fact"><dt>{t("addmodel.fact.vision")}</dt><dd className="small">{TASK_LABEL[meta.vision.task]} · {meta.vision.architecture}{meta.vision.zero_shot ? t("addmodel.fact.zeroShot") : meta.vision.labels ? t("addmodel.fact.classes", { count: String(meta.vision.labels), examples: (meta.vision.label_examples ?? []).slice(0, 4).join(", ") }) : ""}</dd></div>}
          {meta.modalities !== undefined && !meta.vision && <div className="fact"><dt>{t("addmodel.fact.takes")}</dt><dd>{meta.modalities?.length === 0 ? t("addmodel.fact.textOnly") : t("addmodel.fact.textAnd", { media: String(mediaText(meta.modalities)?.toLowerCase()) })}</dd></div>}
          {meta.license !== undefined && <div className="fact"><dt>{t("addmodel.fact.licence")}</dt><dd className="mono small">{meta.license ?? t("addmodel.fact.notStated")}{meta.gated ? t("addmodel.fact.gated") : ""}</dd></div>}
          {meta.video && <div className="fact"><dt>{t("addmodel.fact.pipeline")}</dt><dd className="small">{meta.diffusers?.pipeline ? `${meta.diffusers.pipeline}: ` : ""}{videoFacts(meta.video, meta.diffusers)}</dd></div>}
          {meta.image && <div className="fact"><dt>{meta.diffusers ? t("addmodel.fact.pipeline") : t("addmodel.fact.imageModel")}</dt><dd className="small">{meta.diffusers?.pipeline ? `${meta.diffusers.pipeline}: ` : ""}{imageFacts(meta.image, meta.diffusers)}</dd></div>}
          {meta.config && !meta.vision && <div className="fact"><dt>{t("addmodel.fact.shape")}</dt><dd>{t("addmodel.fact.shapeValue", { layers: String(meta.config.layers), kv: String(meta.config.kv_heads), context: t.num(meta.config.max_context), dtype: meta.config.dtype })}</dd></div>}
        </dl>
      )}
      {meta?.gguf_files && meta.gguf_files.length > 1 && (
        <div className="field gguf-pick">
          <label htmlFor="m-file">{t("addmodel.gguf.file")}</label>
          <select id="m-file" value={f.file || meta.file || ""} onChange={(e) => set("file", e.target.value)}>
            {meta.gguf_files.map((g) => <option key={g.path} value={g.path}>{g.path} ({bytes(g.size)})</option>)}
          </select>
          <p className="field-hint">{t("addmodel.gguf.hint")}</p>
        </div>
      )}
      {meta?.mmproj_files && meta.mmproj_files.length > 0 && (
        <div className="field gguf-pick">
          <label htmlFor="m-mmproj">{t("addmodel.mmproj.label")}</label>
          <select id="m-mmproj" value={f.mmproj || meta.mmproj?.path || "none"} onChange={(e) => set("mmproj", e.target.value)}>
            {meta.mmproj_files.map((g) => <option key={g.path} value={g.path}>{g.path} ({bytes(g.size)})</option>)}
            <option value="none">{t("addmodel.mmproj.none")}</option>
          </select>
          <p className="field-hint">{t.rich("addmodel.mmproj.hint", { flag: <code>--mmproj</code> })}</p>
        </div>
      )}
      {s && !isLocal && <SuggestionPanel s={s} device={f.device} onDevice={(d) => set("device", d)} />}
      {(meta || isLocal) && (
        <>
          <div className="grid-3">
            <div className="field">
              <label htmlFor="m-name">{t("addmodel.name")}</label>
              <input id="m-name" className="mono" value={f.name} onChange={(e) => set("name", e.target.value)} required pattern="[a-z0-9]([a-z0-9._\-]{0,62}[a-z0-9])?" />
              <p className="field-hint">{t("addmodel.nameHint")}</p>
            </div>
            <div className="field">
              <label htmlFor="m-title">{t("addmodel.title")}</label>
              <input id="m-title" value={f.title} onChange={(e) => set("title", e.target.value)} placeholder={t("addmodel.titlePlaceholder")} />
            </div>
            <div className="field">
              <label htmlFor="m-task">{t("addmodel.task")}</label>
              <select id="m-task" value={f.task} onChange={(e) => set("task", e.target.value)} required>
                <option value="">{t("addmodel.review.choosePlaceholder")}</option>
                {(Object.entries(TASK_LABEL) as [Task, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </div>
          </div>
          {Object.keys(f.engine_args).length > 0 && (
            <p className="small muted engine-args">
              {t.rich("addmodel.engineArgs", { names: Object.keys(f.engine_args).join(", "), args: <code>{Object.values(f.engine_args)[0].slice(0, 2).join(" ")}…</code> })}
            </p>
          )}
          {meta && visionProblem(meta, f.task as Task) && <Notice tone="error">{visionProblem(meta, f.task as Task)}</Notice>}
          <div className="form-foot">
            <span className="muted small">{t("addmodel.foot")}</span>
            <button className="btn btn-primary" disabled={busy || inspecting || (!meta && !isLocal) || Boolean(meta && visionProblem(meta, f.task as Task))}>{busy ? t("addmodel.adding") : t("addmodel.add")}</button>
          </div>
        </>
      )}
    </form>
  );
}

function SuggestionPanel({ s, device, onDevice }: { s: Suggestion; device: string; onDevice: (d: "gpu" | "cpu") => void }) {
  const t = useT();
  if (!s.recipe) return <Notice tone="error">{s.error}</Notice>;
  const est = s.estimate;
  const where = fitsLine(s);
  const cpu = s.device === "cpu";
  return (
    <div className="suggest" aria-label={t("addmodel.suggest.aria")}>
      <div className="suggest-main">
        <p className="suggest-line">
          {t.rich(cpu ? "addmodel.suggest.cpu" : "addmodel.suggest.gpu", { recipe: <strong className="mono">{s.recipe}</strong> })}
          {est ? t.rich(cpu ? "addmodel.suggest.reservesRam" : "addmodel.suggest.reservesGpu", { amount: <strong>{gib(est.amount)}</strong> }) : null}{t("addmodel.suggest.end")}
        </p>
        {s.devices && s.devices.length > 1 && (
          <div className="segmented segmented-small" role="group" aria-label={t("addmodel.suggest.estimateFor")}>
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
        <p className="suggest-fit"><IconCheck size={16} aria-hidden="true" /> {t("addmodel.suggest.fits", { where })}</p>
      ) : est ? (
        <p className="suggest-nofit"><IconAlertTriangle size={16} aria-hidden="true" /> {(s.summary ?? t("addmodel.suggest.noFit")).replace(/([^.。])$/, "$1.")}
          {" "}{t(cpu ? "addmodel.suggest.stillAdd" : "addmodel.suggest.stillAddGpu")}</p>
      ) : null}
      <p className="muted small">{t("addmodel.suggest.defaults", { also: s.recipes.length > 1 ? t("addmodel.suggest.also", { list: s.recipes.filter((r) => r !== s.recipe).join(", ") }) : "" })}</p>
    </div>
  );
}
