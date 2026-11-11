import { IconPhoto, IconPlayerStop, IconSend, IconTrash, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth";
import { Notice, PageHeader } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { API_KIND, answerError, chatContent, ms, readEvents, readImage, type Attached } from "../lib/gateway";
import { ROUTE_KIND_LABEL, targetHeader } from "../lib/routing";
import type { RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

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

  return (
    <>
      <PageHeader index="04 · Playground" title="Playground"
        lead="Try a deployed model through the gateway, as yourself: the same routing and limits of the replicas as an API key, without creating one. Requests count as “console” in Usage." />
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
