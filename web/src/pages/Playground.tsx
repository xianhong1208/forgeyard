import { IconDownload, IconPhoto, IconPhotoAi, IconPlayerStop, IconSend, IconSpeakerphone, IconTrash, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { Notice, PageHeader } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { API_KIND, IMAGE_MIME, SPEECH_MIME, answerError, chatContent, imageSizes, ms, pcmRate, pcmToWav, readEvents, readImage,
  type Attached, type ImageOptions, type Voice } from "../lib/gateway";
import { ROUTE_KIND_LABEL, targetHeader } from "../lib/routing";
import type { RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Vision } from "./PlaygroundVision";

type Msg = { role: "user" | "assistant"; content: string; meta?: string; error?: boolean; images?: Attached[] };

// The playground talks to /api/playground/<endpoint>: the gateway's routing, as the signed-in
// user (the session cookie and the CSRF header), counted as "console" in usage. No API key.
async function post(endpoint: string, body: unknown, signal?: AbortSignal, form?: FormData, localOnly = false): Promise<Response> {
  const headers: Record<string, string> = { "X-Forgeyard": "1" };
  if (localOnly) headers["X-Forgeyard-Local-Only"] = "1";
  if (!form) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/playground/${endpoint}`, {
    method: "POST", credentials: "same-origin", headers, signal, body: form ?? JSON.stringify(body),
  });
  if (res.status === 401) {
    const data = await res.json().catch(() => null);
    notifyUnauthorized((data as { detail?: { reason?: string } } | null)?.detail?.reason);
  }
  return res;
}

async function failure(res: Response): Promise<string> {
  const data = await res.json().catch(() => null);
  return answerError(res.status, data);
}

export function PlaygroundPage() {
  const { data, error } = useApi<RoutesResponse>("/routes");
  const { user } = useAuth();
  const canDeploy = user?.role === "admin" || user?.role === "operator";
  // Cloud-only models need the cloud setting the signed-in person may give a key.
  const routes = (data?.routes ?? []).filter((r) => r.usable !== false);
  const [picked, setModel] = useState<string>("");
  // Until someone picks one: a ready chat model, else anything ready, else the first.
  const fallback = routes.find((r) => r.api === "openai-chat" && r.endpoints.length) ?? routes.find((r) => r.endpoints.length) ?? routes[0];
  const ready = (r: (typeof routes)[number]) => r.endpoints.length > 0 || r.kind === "cloud";
  const model = picked || fallback?.served_name || "";
  const route = routes.find((r) => r.served_name === model);
  // Two tabs (1.3): deployed models, and exposed vision workloads (?tab=vision).
  const [search, setSearch] = useSearchParams();
  const tab = search.get("tab") === "vision" ? "vision" : "models";

  return (
    <>
      <PageHeader index="04 · Playground" title="Playground"
        lead="Try a deployed model or an exposed vision workload through the gateway, as yourself: the same routing and limits as an API key, without creating one. Requests count as “console” in Usage." />
      <div className="tabs" role="tablist" aria-label="Playground">
        <button type="button" role="tab" aria-selected={tab === "models"} className={tab === "models" ? "on" : ""}
          onClick={() => setSearch({})}>Models</button>
        <button type="button" role="tab" aria-selected={tab === "vision"} className={tab === "vision" ? "on" : ""}
          onClick={() => setSearch({ tab: "vision" })}>Vision</button>
      </div>
      {tab === "vision" ? <Vision canCreate={canDeploy} /> : <>
      {error && <Notice tone="error">{error}</Notice>}
      {data && routes.length === 0 && (
        <section className="panel play-empty">
          <h2>Nothing to try yet</h2>
          <p className="muted">
            The Playground sends messages to a deployed model, as you, without an API key. No model is deployed yet.{" "}
            {canDeploy ? "Deploy one from the catalog; it shows up here once it is ready." : "Admins and operators deploy models; ask one to deploy the model you need."}
          </p>
          {canDeploy && <Link to="/models/deploy" className="btn btn-primary">Deploy a model</Link>}
        </section>
      )}
      {routes.length > 0 && (
        <div className="play-bar">
          <div className="field">
            <label htmlFor="play-model">Model</label>
            <select id="play-model" value={model} onChange={(e) => setModel(e.target.value)}>
              {routes.map((r) => (
                <option key={r.served_name} value={r.served_name}>
                  {r.served_name} · {API_KIND[r.api] ?? r.api}{r.kind && r.kind !== "local" ? ` · ${ROUTE_KIND_LABEL[r.kind]}` : ""}{ready(r) ? "" : " (not ready)"}
                </option>
              ))}
            </select>
          </div>
          {route && (
            <p className="muted small play-ready">
              {route.kind === "cloud" ? "Served by a cloud provider." : route.endpoints.length ? `${route.endpoints.length} ready replica${route.endpoints.length > 1 ? "s" : ""}` : "No replica is ready yet."}
              {route.kind === "mixed" ? " Spills to the cloud when local is busy or down." : ""}{" "}
              {route.deployment_id ? <Link to={`/models/deployments/${route.deployment_id}`}>Deployment</Link>
                : route.id ? <Link to={`/models/routes/${route.id}`}>Route</Link> : null}
            </p>
          )}
        </div>
      )}
      {route?.api === "openai-chat" && <Chat key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-embeddings" && <Embeddings key={route.served_name} model={route.served_name} />}
      {route?.api === "rerank" && <Rerank key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-audio" && <Transcribe key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-speech" && <Speak key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-images" && <Imagine key={route.served_name} model={route.served_name} />}
      </>}
    </>
  );
}

function Chat({ model }: { model: string }) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [maxTokens, setMaxTokens] = useState(256);
  const [temperature, setTemperature] = useState(0.7);
  const [thinking, setThinking] = useState(false);
  const [localOnly, setLocalOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  // Images for the next message (1.3), read in the browser and sent as data: URIs.
  const [attached, setAttached] = useState<Attached[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [messages]);

  const attach = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    setAttachError(null);
    try {
      const read = await Promise.all(files.map(readImage));
      setAttached((cur) => [...cur, ...read]);
    } catch (err) {
      setAttachError((err as Error).message);
    }
  };

  const update = (fn: (last: Msg) => Msg) => setMessages((m) => [...m.slice(0, -1), fn(m[m.length - 1])]);

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if ((!text && !attached.length) || busy) return;
    const history = [...messages.filter((m) => !m.error), { role: "user" as const, content: text, images: attached.length ? attached : undefined }];
    setMessages([...history, { role: "assistant", content: "" }]);
    setInput("");
    setAttached([]);
    setBusy(true);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    let first: number | null = null;
    let usage: { prompt_tokens?: number; completion_tokens?: number } | null = null;
    try {
      const res = await post("chat/completions", {
        model, stream: true, max_tokens: maxTokens, temperature, stream_options: { include_usage: true },
        chat_template_kwargs: { enable_thinking: thinking },
        messages: history.map(({ role, content, images }) => ({ role, content: chatContent(content, images) })),
      }, ctrl.signal, undefined, localOnly);
      if (!res.ok || !res.body) {
        const why = await failure(res);
        update((m) => ({ ...m, content: why, error: true }));
        return;
      }
      const replica = res.headers.get("X-Forgeyard-Replica");
      const target = res.headers.get("X-Forgeyard-Target");
      const spilled = res.headers.get("X-Forgeyard-Spilled");
      const who = target && !target.startsWith("local:") ? targetHeader(target) : replica ? `replica ${replica.split("#")[1]}` : null;
      await readEvents(res.body, (d) => {
        if (d === "[DONE]") return;
        let chunk: { choices?: { delta?: { content?: string; reasoning_content?: string } }[]; usage?: typeof usage; error?: { message?: string } };
        try { chunk = JSON.parse(d); } catch { return; }
        if (chunk.error) {
          update((m) => ({ ...m, content: `${m.content}\n[${chunk.error?.message ?? "error"}]`, error: true }));
          return;
        }
        if (chunk.usage) usage = chunk.usage;
        const delta = chunk.choices?.[0]?.delta;
        const piece = (delta?.content ?? "") || (delta?.reasoning_content ?? "");
        if (piece) {
          if (first === null) first = performance.now();
          update((m) => ({ ...m, content: m.content + piece }));
        }
      });
      const total = performance.now() - started;
      const u = usage as { prompt_tokens?: number; completion_tokens?: number } | null;
      const out = u?.completion_tokens;
      const meta = [who, spilled ? `spilled: ${spilled}` : null, first !== null ? `first token ${ms(first - started)}` : null,
        `${ms(total)}`, u ? `${u.prompt_tokens ?? 0} in · ${out ?? 0} out` : null,
        out && first !== null && total - (first - started) > 0 ? `${(out / ((total - (first - started)) / 1000)).toFixed(1)} tok/s` : null]
        .filter(Boolean).join(" · ");
      update((m) => ({ ...m, meta }));
    } catch (err) {
      if ((err as Error).name === "AbortError") update((m) => ({ ...m, meta: "stopped" }));
      else update((m) => ({ ...m, content: "Cannot reach the Forgeyard server.", error: true }));
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  return (
    <section className="panel play" aria-labelledby="chat-title">
      <div className="panel-head">
        <h2 id="chat-title">Chat</h2>
        <button type="button" className="btn btn-quiet btn-small" disabled={busy || !messages.length} onClick={() => setMessages([])}>
          <IconTrash size={15} aria-hidden="true" /> Clear
        </button>
      </div>
      <div className="transcript" aria-live="polite" aria-label="Conversation">
        {messages.length === 0 && <p className="muted small">Ask something. Answers stream in as the model writes them.</p>}
        {messages.map((m, i) => (
          <div key={i} className={`msg msg-${m.role}${m.error ? " msg-error" : ""}`}>
            <span className="msg-role">{m.role === "user" ? "You" : model}</span>
            {m.images && (
              <div className="msg-images">
                {m.images.map((img, k) => <img key={k} src={img.uri} alt={`Attached: ${img.name}`} />)}
              </div>
            )}
            {(m.content || m.role === "assistant") && (
              <div className="msg-text">{m.content || (busy && i === messages.length - 1 ? <span className="cursor" aria-label="Writing" /> : "")}
                {busy && i === messages.length - 1 && m.content && <span className="cursor" aria-hidden="true" />}</div>
            )}
            {m.meta && <span className="msg-meta">{m.meta}</span>}
          </div>
        ))}
        <div ref={end} />
      </div>
      {(attached.length > 0 || attachError) && (
        <div className="attached" aria-label="Images for the next message">
          {attached.map((img, i) => (
            <span key={i} className="attached-item">
              <img src={img.uri} alt="" />
              <span className="small mono attached-name">{img.name}</span>
              <button type="button" className="btn btn-quiet btn-small" aria-label={`Remove ${img.name}`}
                onClick={() => setAttached((cur) => cur.filter((_, j) => j !== i))}><IconX size={14} aria-hidden="true" /></button>
            </span>
          ))}
          {attachError && <span className="field-error small">{attachError}</span>}
        </div>
      )}
      <form className="chat-form chat-form-media" onSubmit={send}>
        <input ref={picker} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden onChange={(e) => void attach(e)}
          aria-label="Attach images" data-testid="attach-input" />
        <button type="button" className="btn btn-quiet" disabled={busy} onClick={() => picker.current?.click()} title="Attach an image (for models that read images)">
          <IconPhoto size={16} aria-hidden="true" /><span className="sr-only">Attach an image</span>
        </button>
        <label htmlFor="chat-input" className="sr-only">Message</label>
        <textarea id="chat-input" value={input} rows={2} placeholder="Write a message…" onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(e); } }} />
        {busy ? (
          <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
        ) : (
          <button type="submit" className="btn btn-primary" disabled={!input.trim() && !attached.length}><IconSend size={16} aria-hidden="true" /> Send</button>
        )}
      </form>
      <div className="chat-settings">
        <label>Max tokens <input type="number" min={1} max={32768} value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value) || 1)} /></label>
        <label>Temperature <input type="number" min={0} max={2} step={0.1} value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} /></label>
        <label className="check"><input type="checkbox" checked={thinking} onChange={(e) => setThinking(e.target.checked)} /> Thinking (models that support it)</label>
        <label className="check" title="Sends X-Forgeyard-Local-Only: 1"><input type="checkbox" checked={localOnly} onChange={(e) => setLocalOnly(e.target.checked)} /> Local only (never the cloud)</label>
      </div>
    </section>
  );
}

function Embeddings({ model }: { model: string }) {
  const [input, setInput] = useState("Forgeyard runs models on your own machines.");
  const [out, setOut] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const go = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null); setOut(null);
    const started = performance.now();
    try {
      const res = await post("embeddings", { model, input });
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      const v: number[] = data.data?.[0]?.embedding ?? [];
      setOut(`${v.length} dimensions in ${ms(performance.now() - started)} · ${data.usage?.prompt_tokens ?? "?"} tokens\n[${v.slice(0, 8).map((x) => x.toFixed(4)).join(", ")}, …]`);
    } catch { setErr("Cannot reach the Forgeyard server."); } finally { setBusy(false); }
  };
  return (
    <section className="panel play" aria-labelledby="emb-title">
      <h2 id="emb-title">Embeddings</h2>
      <form onSubmit={go}>
        <div className="field"><label htmlFor="emb-input">Text</label><textarea id="emb-input" rows={3} value={input} onChange={(e) => setInput(e.target.value)} /></div>
        <button type="submit" className="btn btn-primary" disabled={busy || !input.trim()} style={{ marginTop: 10 }}>{busy ? "Waiting…" : "Embed"}</button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && <pre className="logs try-answer play-out" aria-label="Answer">{out}</pre>}
    </section>
  );
}

function Rerank({ model }: { model: string }) {
  const [query, setQuery] = useState("What is the capital of France?");
  const [docs, setDocs] = useState("Paris is the capital and largest city of France.\nThe Eiffel Tower was completed in 1889.\nBerlin is the capital of Germany.");
  const [out, setOut] = useState<{ index: number; relevance_score: number }[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const list = docs.split("\n").map((d) => d.trim()).filter(Boolean);
  const go = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null); setOut(null);
    try {
      const res = await post("rerank", { model, query, documents: list });
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      setOut([...(data.results ?? [])].sort((a, b) => b.relevance_score - a.relevance_score));
    } catch { setErr("Cannot reach the Forgeyard server."); } finally { setBusy(false); }
  };
  return (
    <section className="panel play" aria-labelledby="rr-title">
      <h2 id="rr-title">Rerank</h2>
      <form onSubmit={go}>
        <div className="field"><label htmlFor="rr-query">Query</label><input id="rr-query" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
        <div className="field" style={{ marginTop: 10 }}><label htmlFor="rr-docs">Documents, one per line</label><textarea id="rr-docs" rows={4} value={docs} onChange={(e) => setDocs(e.target.value)} /></div>
        <button type="submit" className="btn btn-primary" disabled={busy || !query.trim() || !list.length} style={{ marginTop: 10 }}>{busy ? "Waiting…" : "Rank"}</button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && (
        <ol className="ranked" aria-label="Ranked documents">
          {out.map((r) => <li key={r.index}><span className="mono small">{r.relevance_score.toFixed(3)}</span><span>{list[r.index]}</span></li>)}
        </ol>
      )}
    </section>
  );
}

function Transcribe({ model }: { model: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [out, setOut] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setBusy(true); setErr(null); setOut(null);
    const started = performance.now();
    try {
      const form = new FormData();
      form.append("model", model);
      form.append("response_format", "json");
      form.append("file", file);
      const res = await post("audio/transcriptions", null, undefined, form);
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      setOut(`${(data.text ?? "").trim()}\n\n(${ms(performance.now() - started)})`);
    } catch { setErr("Cannot reach the Forgeyard server."); } finally { setBusy(false); }
  };
  return (
    <section className="panel play" aria-labelledby="asr-title">
      <h2 id="asr-title">Speech to text</h2>
      <form onSubmit={go}>
        <div className="field"><label htmlFor="asr-file">Audio file (mp3, wav, m4a…)</label>
          <input id="asr-file" type="file" accept="audio/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></div>
        <button type="submit" className="btn btn-primary" disabled={busy || !file} style={{ marginTop: 10 }}>{busy ? "Transcribing…" : "Transcribe"}</button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && <pre className="logs try-answer play-out" aria-label="Transcript">{out}</pre>}
    </section>
  );
}

type VoicesAnswer = { voices: Voice[]; default: string | null; formats?: string[]; default_format?: string; stream_formats?: string[]; max_input_chars?: number };

const SPEAK_SAMPLE = "Forgeyard runs models on your own machines. 歡迎使用 Forgeyard，模型在你自己的機器上執行。";

// Text to speech (1.3): the model's own voices, a format it makes, and the answer in a player. With
// "Stream", the audio is asked for as it is made (stream_format audio) and the time to the first audio
// is shown; the player still plays the whole file.
function Speak({ model }: { model: string }) {
  const [text, setText] = useState(SPEAK_SAMPLE);
  const [info, setInfo] = useState<VoicesAnswer | null>(null);
  const [infoErr, setInfoErr] = useState<string | null>(null);
  const [voice, setVoice] = useState("");
  const [format, setFormat] = useState("");
  const [stream, setStream] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [audio, setAudio] = useState<{ url: string; name: string; meta: string } | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const res = await fetch(`/api/playground/audio/voices?model=${encodeURIComponent(model)}`, { credentials: "same-origin", headers: { "X-Forgeyard": "1" } });
        if (res.status === 401) { notifyUnauthorized(); return; }
        if (!res.ok) { if (live) setInfoErr(await failure(res)); return; }
        const data = (await res.json()) as VoicesAnswer;
        if (!live) return;
        setInfo(data);
        setVoice(data.default ?? data.voices[0]?.id ?? "");
        setFormat(data.default_format ?? "");
      } catch {
        if (live) setInfoErr("Cannot reach the Forgeyard server.");
      }
    })();
    return () => { live = false; };
  }, [model]);

  useEffect(() => () => { if (audio) URL.revokeObjectURL(audio.url); }, [audio]);

  const formats = info?.formats ?? ["wav"];
  const canStream = (info?.stream_formats ?? []).includes("audio") && (format === "pcm" || format === "wav" || format === "mp3");
  const cap = info?.max_input_chars;
  const chars = [...text].length;

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true); setErr(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    try {
      const body: Record<string, unknown> = { model, input: text };
      if (voice) body.voice = voice;
      if (format) body.response_format = format;
      if (stream && canStream) body.stream_format = "audio";
      const res = await post("audio/speech", body, ctrl.signal);
      if (!res.ok || !res.body) { setErr(await failure(res)); return; }
      const reader = res.body.getReader();
      const parts: Uint8Array[] = [];
      let first: number | null = null;
      let size = 0;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value?.length) {
          if (first === null) first = performance.now();
          parts.push(value);
          size += value.length;
        }
      }
      const total = performance.now() - started;
      let data: Uint8Array<ArrayBuffer> = new Uint8Array(size);
      let at = 0;
      for (const p of parts) { data.set(p, at); at += p.length; }
      const fmt = format || info?.default_format || "wav";
      if (fmt === "pcm") data = pcmToWav(data, pcmRate(res.headers));
      const blob = new Blob([data], { type: SPEECH_MIME[fmt] ?? "application/octet-stream" });
      const used = res.headers.get("X-Forgeyard-Voice");
      const replica = res.headers.get("X-Forgeyard-Replica");
      const meta = [replica ? `replica ${replica.split("#")[1]}` : null, used ? `voice ${used}` : voice ? `voice ${voice}` : null,
        stream && canStream && first !== null ? `first bytes ${ms(first - started)}` : null, `${ms(total)}`,
        `${(size / 1024).toFixed(0)} KiB ${fmt}`, `${chars} characters`].filter(Boolean).join(" · ");
      setAudio((cur) => {
        if (cur) URL.revokeObjectURL(cur.url);
        return { url: URL.createObjectURL(blob), name: `${model}.${fmt === "pcm" ? "wav" : fmt === "opus" ? "ogg" : fmt}`, meta };
      });
    } catch (e2) {
      if ((e2 as Error).name !== "AbortError") setErr("Cannot reach the Forgeyard server.");
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  return (
    <section className="panel play" aria-labelledby="speak-title">
      <h2 id="speak-title">Speak</h2>
      {infoErr && <Notice tone="error">{infoErr}</Notice>}
      <form onSubmit={go} className="speak-form">
        <div className="field">
          <label htmlFor="speak-text">Text</label>
          <textarea id="speak-text" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
          <p className={`field-hint${cap && chars > cap ? " field-error" : ""}`}>{chars.toLocaleString("en-US")}{cap ? ` of at most ${cap.toLocaleString("en-US")}` : ""} characters</p>
        </div>
        <div className="speak-options">
          <div className="field">
            <label htmlFor="speak-voice">Voice</label>
            <select id="speak-voice" value={voice} onChange={(e) => setVoice(e.target.value)} disabled={!info}>
              {info && info.voices.length === 0 && <option value="">The model's own voice</option>}
              {(info?.voices ?? []).map((v) => (
                <option key={v.id} value={v.id}>{v.name}{v.language ? ` · ${v.language}` : ""}{v.gender ? ` · ${v.gender}` : ""}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="speak-format">Format</label>
            <select id="speak-format" value={format} onChange={(e) => setFormat(e.target.value)} disabled={!info}>
              {formats.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
          <label className="check speak-stream" title={canStream ? "Sends stream_format: audio" : "This model or format does not stream"}>
            <input type="checkbox" checked={stream && canStream} disabled={!canStream} onChange={(e) => setStream(e.target.checked)} /> Stream
          </label>
        </div>
        <div className="row-gap">
          {busy ? (
            <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={!text.trim() || (cap !== undefined && chars > cap)}>
              <IconSpeakerphone size={16} aria-hidden="true" /> Speak
            </button>
          )}
        </div>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {audio && (
        <div className="speak-out" aria-label="Speech">
          <audio controls src={audio.url} aria-label="The speech" />
          <div className="speak-meta">
            <span className="msg-meta">{audio.meta}</span>
            <a className="btn btn-quiet btn-small" href={audio.url} download={audio.name}><IconDownload size={14} aria-hidden="true" /> Download</a>
          </div>
        </div>
      )}
    </section>
  );
}

const IMAGE_SAMPLE = "A lighthouse on a rocky coast at sunset, warm light, oil painting";

type Made = { url: string; name: string };

function b64Blob(b64: string, mime: string): Blob {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// Image generation (1.3): a prompt, a size the deployment takes and how many; the images come back as
// base64 and are shown with a download link each. Requests wait in the gateway's queue while the model
// is busy, so a long wait is normal on a CPU.
function Imagine({ model }: { model: string }) {
  const [prompt, setPrompt] = useState(IMAGE_SAMPLE);
  const [opts, setOpts] = useState<ImageOptions | null>(null);
  const [optsErr, setOptsErr] = useState<string | null>(null);
  const [size, setSize] = useState("");
  const [n, setN] = useState(1);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [made, setMade] = useState<{ images: Made[]; meta: string } | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const res = await fetch(`/api/playground/images/options?model=${encodeURIComponent(model)}`, { credentials: "same-origin", headers: { "X-Forgeyard": "1" } });
        if (res.status === 401) { notifyUnauthorized(); return; }
        if (!res.ok) { if (live) setOptsErr(await failure(res)); return; }
        const data = (await res.json()) as ImageOptions;
        if (!live) return;
        setOpts(data);
        setSize(data.default_size);
      } catch {
        if (live) setOptsErr("Cannot reach the Forgeyard server.");
      }
    })();
    return () => { live = false; };
  }, [model]);

  useEffect(() => () => { made?.images.forEach((i) => URL.revokeObjectURL(i.url)); }, [made]);

  const sizes = opts ? imageSizes(opts) : [];
  const counts = Array.from({ length: Math.min(opts?.max_n ?? 1, 8) }, (_, i) => i + 1);
  const cap = opts?.max_prompt_chars;
  const chars = prompt.length;

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || busy) return;
    setBusy(true); setErr(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    try {
      const res = await post("images/generations", { model, prompt, n, size: size || undefined }, ctrl.signal);
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      const fmt: string = data.output_format ?? "png";
      const mime = IMAGE_MIME[fmt] ?? "image/png";
      const images: Made[] = (data.data ?? []).filter((d: { b64_json?: string }) => d.b64_json).map((d: { b64_json: string }, i: number) => ({
        url: URL.createObjectURL(b64Blob(d.b64_json, mime)), name: `${model}-${i + 1}.${fmt === "jpeg" ? "jpg" : fmt}`,
      }));
      const replica = res.headers.get("X-Forgeyard-Replica");
      const meta = [replica ? `replica ${replica.split("#")[1]}` : null, `${images.length} image${images.length === 1 ? "" : "s"}`,
        data.size ?? null, fmt.toUpperCase(), ms(performance.now() - started)].filter(Boolean).join(" · ");
      setMade((cur) => {
        cur?.images.forEach((i) => URL.revokeObjectURL(i.url));
        return { images, meta };
      });
    } catch (e2) {
      if ((e2 as Error).name !== "AbortError") setErr("Cannot reach the Forgeyard server.");
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  return (
    <section className="panel play" aria-labelledby="img-title">
      <h2 id="img-title">Image</h2>
      {optsErr && <Notice tone="error">{optsErr}</Notice>}
      <form onSubmit={go} className="img-form">
        <div className="field">
          <label htmlFor="img-prompt">Prompt</label>
          <textarea id="img-prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <p className={`field-hint${cap && chars > cap ? " field-error" : ""}`}>{chars.toLocaleString("en-US")}{cap ? ` of at most ${cap.toLocaleString("en-US")}` : ""} characters</p>
        </div>
        <div className="img-options">
          <div className="field">
            <label htmlFor="img-size">Size</label>
            <select id="img-size" value={size} onChange={(e) => setSize(e.target.value)} disabled={!opts}>
              {sizes.map((s) => <option key={s} value={s}>{s}{s === opts?.default_size ? " (default)" : ""}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="img-n">Images</label>
            <select id="img-n" value={n} onChange={(e) => setN(Number(e.target.value))} disabled={!opts}>
              {counts.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
        </div>
        <div className="row-gap">
          {busy ? (
            <>
              <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
              <span className="muted small" role="status">Making {n === 1 ? "the image" : `${n} images`}… this takes a while, longer on a CPU or when others are waiting.</span>
            </>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={!opts || !prompt.trim() || (cap !== undefined && chars > cap)}>
              <IconPhotoAi size={16} aria-hidden="true" /> Generate
            </button>
          )}
        </div>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {made && (
        <div className="img-out" aria-label="Generated images">
          <span className="msg-meta">{made.meta}</span>
          <div className="img-grid">
            {made.images.map((i, k) => (
              <figure key={i.url} className="img-item">
                <img src={i.url} alt={`Image ${k + 1} for: ${prompt.slice(0, 80)}`} />
                <figcaption>
                  <a className="btn btn-quiet btn-small" href={i.url} download={i.name}><IconDownload size={14} aria-hidden="true" /> Download</a>
                </figcaption>
              </figure>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
