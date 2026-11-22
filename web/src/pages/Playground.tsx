import { IconCheck, IconChevronDown, IconDownload, IconMoon, IconPhoto, IconPhotoAi, IconPlayerStop, IconPlus, IconSend, IconSpeakerphone, IconTrash, IconX } from "@tabler/icons-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { useAuth } from "../auth";
import { ModelKindIcon } from "../components/Fleet";
import { Notice, PageHeader } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { IMAGE_MIME, SPEECH_MIME, answerError, chatContent, imageSizes, ms, pcmRate, pcmToWav, readEvents, readImage,
  type Attached, type ImageOptions, type Voice } from "../lib/gateway";
import { MODEL_KIND, type ModelKind } from "../lib/fleet";
import { firstVision, pickerDetail, pickerGroups, routeReady, routeSleep } from "../lib/playground";
import { api, errorText } from "../lib/api";
import { elapsed, seconds, sleepState, wakeSeconds } from "../lib/sleep";
import { usePoll } from "../lib/poll";
import { when } from "../lib/format";
import { targetHeader } from "../lib/routing";
import type { ClassifyAnswer, Deployment, DeploymentsResponse, RouteInfo, RoutesResponse, TextClassifier } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Videos } from "./PlaygroundVideo";
import { VisionModelPanel } from "./PlaygroundVision";

type Msg = { role: "user" | "assistant"; content: string; meta?: string; error?: boolean; images?: Attached[] };

// The playground talks to /api/playground/<endpoint>: the gateway's routing, as the signed-in
// user (the session cookie and the CSRF header), counted as "console" in usage. No API key.
async function post(endpoint: string, body: unknown, signal?: AbortSignal, form?: FormData, localOnly = false): Promise<Response> {
  window.dispatchEvent(new Event(SENT_EVENT));   // 1.5.0: an asleep model starts waking now; the page looks again soon
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
  const { data, error, reload: reloadRoutes } = useApi<RoutesResponse>("/routes");
  // For the kind of a chat model (multimodal when its deployment reads images, audio or video).
  const deps = useApi<DeploymentsResponse>("/deployments");
  const { user } = useAuth();
  const canDeploy = user?.role === "admin" || user?.role === "operator";
  const [search, setSearch] = useSearchParams();
  const loaded = !!data && (!!deps.data || !!deps.error);
  // Cloud-only models need the cloud setting the signed-in person may give a key.
  const routes = useMemo(() => (data?.routes ?? []).filter((r) => r.usable !== false), [data]);
  // 1.5.0: the deployments seen asleep or waking here stay offered while they run (see pickerGroups).
  const [woken, setWoken] = useState<ReadonlySet<number>>(() => new Set());
  const groups = useMemo(() => (loaded ? pickerGroups(routes, deps.data?.deployments ?? [], woken) : []), [loaded, routes, deps.data, woken]);
  const offered = groups.flatMap(([, l]) => l);
  // The model in the address (?model=), else the first one offered: a chat model when there is one.
  const route = offered.find((r) => r.served_name === search.get("model")) ?? offered[0];
  const waiting = routes.filter((r) => !routeReady(r) && r.deployment_id != null && !routeSleep(r, deps.data?.deployments ?? [])).length;
  // 1.5.0 (#37): the chosen model's deployment while it is asleep or waking; followed until it is ready.
  const dep = route?.deployment_id != null ? deps.data?.deployments.find((d) => d.id === route.deployment_id) : undefined;
  const sleeping = dep ? sleepState(dep) : null;
  if (dep && sleeping && !woken.has(dep.id)) setWoken(new Set(woken).add(dep.id));   // during render, guarded (React's pattern)
  const { reload: reloadDeps } = deps;
  const follow = useCallback(() => { if (sleeping) { reloadDeps(); reloadRoutes(); } }, [sleeping, reloadDeps, reloadRoutes]);
  usePoll(follow, SLEEP_POLL_MS);
  // A request to an asleep model wakes it: look again shortly after one is sent, not only at the next poll.
  useEffect(() => {
    const soon = () => window.setTimeout(follow, SENT_LOOK_MS);
    window.addEventListener(SENT_EVENT, soon);
    return () => window.removeEventListener(SENT_EVENT, soon);
  }, [follow]);
  const sleepOf = (r: RouteInfo) => routeSleep(r, deps.data?.deployments ?? []);
  const legacy = search.get("tab");

  // 1.3's Vision tab (?tab=vision) is part of the one picker now: its address opens the first ready vision
  // model, or the picker when there is none, replacing the old address in the history.
  const vision = firstVision(groups);
  useEffect(() => {
    if (legacy === null || !(loaded || error)) return;
    setSearch(legacy === "vision" && vision ? { model: vision } : {}, { replace: true });
  }, [legacy, loaded, error, vision, setSearch]);

  const pick = (name: string) => setSearch({ model: name }, { replace: true });

  return (
    <>
      <PageHeader index="05 · Playground" title="Playground"
        lead="Try a deployed model through the gateway, as yourself: the same routing and limits as an API key, without creating one. Requests count as “console” in Usage." />
      {error && <Notice tone="error">{error}</Notice>}
      {loaded && offered.length === 0 && (
        <section className="panel play-empty" aria-labelledby="play-empty-title">
          <h2 id="play-empty-title">Nothing to try yet</h2>
          <p className="muted">
            The Playground sends requests to a ready model, as you, without an API key.{" "}
            {waiting > 0 ? <>{waiting === 1 ? "One deployment is" : `${waiting} deployments are`} not ready: <Link to="/serving">Serving</Link> shows whether each is stopped, starting or waiting for a machine.{" "}</>
              : "No model is deployed yet. "}
            {canDeploy ? <>Deploy one from <strong>Models → Catalog</strong>: chat, embeddings, speech, images, video or vision. It shows up here once it is ready.</>
              : "Admins and operators deploy models; ask one to deploy the model you need."}
          </p>
          {canDeploy && <Link to="/models" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy from the catalog</Link>}
        </section>
      )}
      {route && (
        <div className="play-bar">
          <ModelPicker groups={groups} value={route} onPick={pick} startOpen={legacy === "vision" && !vision} sleepOf={sleepOf} />
          <p className="muted small play-ready">
            {route.kind === "cloud" ? "Served by a cloud provider." : sleeping === "asleep" ? "Asleep: a request wakes it."
              : sleeping === "waking" ? "Waking: no replica is ready yet." : `${route.endpoints.length} ready replica${route.endpoints.length > 1 ? "s" : ""}`}
            {route.kind === "mixed" ? " Spills to the cloud when local is busy or down." : ""}{" "}
            {route.deployment_id ? <Link to={`/serving/${route.deployment_id}`}>Deployment</Link>
              : route.id ? <Link to={`/cloud/models/${route.id}`}>Details</Link> : null}
          </p>
        </div>
      )}
      {dep && sleeping && <WakeNotice d={dep} canWake={canDeploy} onWoken={() => { reloadDeps(); reloadRoutes(); }} />}
      {route?.api === "openai-chat" && <Chat key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-embeddings" && <Embeddings key={route.served_name} model={route.served_name} />}
      {route?.api === "rerank" && <Rerank key={route.served_name} model={route.served_name} />}
      {route?.api === "classify" && <Classify key={route.served_name} model={route.served_name}
        labels={deps.data?.deployments.find((d) => d.id === route.deployment_id)?.model.classify ?? null} />}
      {route?.api === "openai-audio" && <Transcribe key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-speech" && <Speak key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-images" && <Imagine key={route.served_name} model={route.served_name} />}
      {route?.api === "openai-videos" && <Videos key={route.served_name} model={route.served_name} />}
      {route?.api.startsWith("vision-") && <VisionModelPanel key={route.served_name} name={route.served_name} />}
    </>
  );
}

// 1.5.0 (#37): how often the Playground asks again while the chosen model is asleep or waking, and how soon after a
// request is sent (the gateway starts the wake as it takes the request).
const SLEEP_POLL_MS = 2000;
const SENT_LOOK_MS = 400;
const SENT_EVENT = "forgeyard:playground-sent";

// 1.5.0 (#37): the chosen model is asleep (a message wakes it; operators may also Wake now) or waking (about how
// long, from the wake under way or its last start). The request itself is sent as to any model: the gateway holds it.
function WakeNotice({ d, canWake, onWoken }: { d: Deployment; canWake: boolean; onWoken: () => void }) {
  const state = sleepState(d);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, tick] = useState(0);
  useEffect(() => {
    if (state !== "waking") return;
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [state]);
  const expect = wakeSeconds(d);
  const about = expect ? ` (about ${seconds(expect)})` : "";
  const wait = d.sleep?.wake_wait ?? 300;
  const wake = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/deployments/${d.id}/start`, { method: "POST" });
      onWoken();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  if (state === "waking") {
    const waiting = d.sleep?.waking?.waiting ?? 0;
    return (
      <div className="play-wake" role="status" aria-live="polite">
        <IconMoon size={16} aria-hidden="true" />
        <span><strong>Waking the model{about}…</strong> <span className="mono">{elapsed(d.sleep?.waking?.since)}</span>. A request sent to it waits
          until a replica is ready (up to {wait} s){waiting ? `; ${waiting} waiting now` : ""}.</span>
      </div>
    );
  }
  return (
    <div className="play-wake" role="status">
      <IconMoon size={16} aria-hidden="true" />
      <span>
        <strong>{d.name} is asleep</strong>{d.sleep?.asleep_since ? ` since ${when(d.sleep.asleep_since)}` : ""}: nobody used it for {d.sleep?.idle_minutes ?? 30} min, so it holds no memory.{" "}
        Send a request and it wakes: the request waits until it is ready{about}, up to {wait} s, or is answered by a cloud tier
        meanwhile when you may use one.
        {error && <span className="field-error"> {error}</span>}
      </span>
      {canWake && <button type="button" className="btn" disabled={busy} onClick={() => void wake()}>{busy ? "Waking…" : "Wake now"}</button>}
    </div>
  );
}

// One picker for every kind (1.4, #100): a button naming the model and its kind, opening a list grouped
// by kind with Serving's labels and icons. A listbox: arrows move, Enter or Space picks, Escape closes.
function ModelPicker({ groups, value, onPick, startOpen, sleepOf }: {
  groups: [ModelKind, RouteInfo[]][]; value: RouteInfo; onPick: (name: string) => void; startOpen: boolean;
  sleepOf: (r: RouteInfo) => string | null;   // 1.5.0: "asleep" / "waking" beside the name
}) {
  const flat = groups.flatMap(([k, l]) => l.map((r) => ({ k, r })));
  const at = Math.max(0, flat.findIndex((x) => x.r.served_name === value.served_name));
  const kind = flat[at]?.k ?? "chat";
  const [open, setOpen] = useState(startOpen);
  const [active, setActive] = useState(at);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const optId = (i: number) => `play-model-opt-${i}`;

  useEffect(() => {
    if (!open) return;
    list.current?.focus();
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  useEffect(() => {
    if (open) document.getElementById(optId(active))?.scrollIntoView?.({ block: "nearest" });
  }, [open, active]);

  const show = (v: boolean) => {
    setActive(at);
    setOpen(v);
  };

  const choose = (i: number) => {
    const x = flat[i];
    if (x) onPick(x.r.served_name);
    setOpen(false);
    button.current?.focus();
  };

  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    const last = flat.length - 1;
    const move: Record<string, number> = { ArrowDown: Math.min(last, active + 1), ArrowUp: Math.max(0, active - 1), Home: 0, End: last };
    if (e.key in move) { e.preventDefault(); setActive(move[e.key]); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(active); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); button.current?.focus(); }
    else if (e.key === "Tab") setOpen(false);
  };

  let i = -1;
  return (
    <div className="field picker" ref={box}>
      <label id="play-model-label" htmlFor="play-model">Model</label>
      <button id="play-model" ref={button} type="button" className="picker-button" aria-haspopup="listbox" aria-expanded={open}
        aria-controls={open ? "play-model-list" : undefined} aria-labelledby="play-model-label play-model"
        onClick={() => show(!open)}
        onKeyDown={(e) => { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); show(true); } }}>
        <span className="kindicon kindicon-sm"><ModelKindIcon kind={kind} size={15} /></span>
        <span className="picker-name mono">{value.served_name}</span>{" "}
        <span className="picker-kind">{MODEL_KIND[kind].label}{pickerDetail(value) ? ` · ${pickerDetail(value)}` : ""}</span>
        <IconChevronDown size={16} aria-hidden="true" className="picker-caret" />
      </button>
      {open && (
        <div id="play-model-list" ref={list} className="picker-panel" role="listbox" aria-labelledby="play-model-label" tabIndex={-1}
          aria-activedescendant={optId(active)} onKeyDown={keys}>
          {groups.map(([k, l]) => (
            <div key={k} role="group" aria-labelledby={`play-kind-${k}`} className="picker-group">
              <div id={`play-kind-${k}`} className="picker-group-head">
                <span className="kindicon kindicon-sm" aria-hidden="true"><ModelKindIcon kind={k} size={15} /></span>
                <span className="picker-group-label">{MODEL_KIND[k].label}</span>{" "}
                <span className="picker-group-api mono">{MODEL_KIND[k].api}</span>
              </div>
              {l.map((r) => {
                const n = ++i;
                const detail = pickerDetail(r);
                return (
                  <div key={r.served_name} id={optId(n)} role="option" aria-selected={r.served_name === value.served_name}
                    className={`picker-option${n === active ? " active" : ""}`} onMouseMove={() => setActive(n)} onClick={() => choose(n)}>
                    <span className="mono picker-option-name">{r.served_name}</span>
                    {detail && <>{" "}<span className="picker-option-detail">{detail}</span></>}
                    {sleepOf(r) && <>{" "}<span className="picker-option-detail picker-asleep"><IconMoon size={13} aria-hidden="true" /> {sleepOf(r)}</span></>}
                    {r.served_name === value.served_name && <IconCheck size={15} aria-hidden="true" className="picker-check" />}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
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

const CLASSIFY_SAMPLE = "I love how quiet this laptop is.\nThe delivery was late and the box was crushed.\n這家餐廳的服務很好，下次還會再來。";

// Text classification (1.4.4): texts, one per line, to /v1/classify; for each, every label's probability, the
// most likely first (the names come from the model's config.json, kept in the catalog).
function Classify({ model, labels }: { model: string; labels: TextClassifier | null }) {
  const [text, setText] = useState(CLASSIFY_SAMPLE);
  const [out, setOut] = useState<{ texts: string[]; answer: ClassifyAnswer; took: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const list = text.split("\n").map((t) => t.trim()).filter(Boolean);
  const go = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null); setOut(null);
    const started = performance.now();
    try {
      const res = await post("classify", { model, input: list });
      if (!res.ok) { setErr(await failure(res)); return; }
      setOut({ texts: list, answer: await res.json(), took: performance.now() - started });
    } catch { setErr("Cannot reach the Forgeyard server."); } finally { setBusy(false); }
  };
  const multi = labels?.problem === "multi_label_classification";
  return (
    <section className="panel play" aria-labelledby="cls-title">
      <h2 id="cls-title">Text classification</h2>
      <p className="muted small">
        {labels?.labels ? `${labels.labels} labels${labels.label_names?.length ? `: ${labels.label_names.slice(0, 6).join(", ")}${labels.label_names.length > 6 ? ", …" : "."}` : "."} ` : ""}
        {multi ? "Each label is scored on its own (multi-label), so the probabilities do not add up to 1." : "The probabilities of the labels add up to 1."}
      </p>
      <form onSubmit={go}>
        <div className="field"><label htmlFor="cls-input">Texts, one per line</label><textarea id="cls-input" rows={4} value={text} onChange={(e) => setText(e.target.value)} /></div>
        <button type="submit" className="btn btn-primary" disabled={busy || !list.length} style={{ marginTop: 10 }}>{busy ? "Waiting…" : "Classify"}</button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && (
        <>
          <ol className="cls-results" aria-label="Labels">
            {[...out.answer.data].sort((a, b) => a.index - b.index).map((d) => (
              <ClassifyResult key={d.index} text={out.texts[d.index] ?? ""} d={d} names={labels?.label_names ?? []} />
            ))}
          </ol>
          <p className="muted small">{out.answer.usage?.prompt_tokens != null ? `${out.answer.usage.prompt_tokens} tokens · ` : ""}{ms(out.took)}</p>
        </>
      )}
    </section>
  );
}

const CLASSIFY_SHOWN = 5;

function ClassifyResult({ text, d, names }: { text: string; d: ClassifyAnswer["data"][number]; names: string[] }) {
  const [all, setAll] = useState(false);
  // Without the names (a local model), the engine still names the most likely label.
  const top = d.probs.indexOf(Math.max(...d.probs));
  const ranked = d.probs.map((p, i) => ({ i, p, name: names[i] ?? (i === top && d.label ? d.label : `label ${i}`) }))
    .sort((a, b) => b.p - a.p);
  const shown = all ? ranked : ranked.slice(0, CLASSIFY_SHOWN);
  return (
    <li className="cls-item">
      <p className="cls-text">{text}</p>
      <p className="cls-top"><strong>{d.label ?? ranked[0]?.name}</strong> <span className="mono small">{(ranked[0]?.p ?? 0).toFixed(3)}</span></p>
      <ul className="cls-bars" aria-label={`Probabilities for text ${d.index + 1}`}>
        {shown.map((r) => (
          <li key={r.i}>
            <span className="cls-name" title={r.name}>{r.name}</span>
            <span className="cls-bar" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, r.p)) * 100}%` }} /></span>
            <span className="mono small">{r.p.toFixed(3)}</span>
          </li>
        ))}
      </ul>
      {ranked.length > CLASSIFY_SHOWN && (
        <button type="button" className="linkish small" onClick={() => setAll(!all)}>
          {all ? "Show the top five" : `Show all ${ranked.length} labels`}
        </button>
      )}
    </li>
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
