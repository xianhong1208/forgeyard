import { IconDownload, IconMovie, IconPlayerPlay, IconTrash, IconX } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { Notice } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { answerError, elapsed, videoFrames, videoSizes, type Video, type VideoOptions } from "../lib/gateway";

// The Playground's Video panel (1.3): a video is a job. It is asked for at /api/playground/videos (the
// gateway's /v1/videos rules, as the signed-in person: the session cookie and the CSRF header, counted as
// "console" in Usage), followed until it is made, then played from …/content. Jobs belong to the person,
// so a video asked for earlier is still listed when the page is opened again. No API key.
const SAMPLE = "A red kite flying over a green hill on a sunny day, slow camera pan.";
const POLL_MS = 3000;
const HEADERS = { "X-Forgeyard": "1" };

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(`/api/playground/videos${path}`, { credentials: "same-origin", ...init,
    headers: { ...HEADERS, ...(init.headers as Record<string, string> | undefined) } });
  if (res.status === 401) {
    const data = await res.clone().json().catch(() => null);
    notifyUnauthorized((data as { detail?: { reason?: string } } | null)?.detail?.reason);
  }
  return res;
}

async function failure(res: Response): Promise<string> {
  return answerError(res.status, await res.json().catch(() => null));
}

const STATUS_LABEL: Record<Video["status"], string> = {
  queued: "Waiting for the replica", in_progress: "Being made", completed: "Made", failed: "Failed",
};

export const contentUrl = (id: string) => `/api/playground/videos/${encodeURIComponent(id)}/content`;

function when(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function Videos({ model }: { model: string }) {
  const [prompt, setPrompt] = useState(SAMPLE);
  const [opts, setOpts] = useState<VideoOptions | null>(null);
  const [optsErr, setOptsErr] = useState<string | null>(null);
  const [size, setSize] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [job, setJob] = useState<Video | null>(null);
  const [mine, setMine] = useState<Video[]>([]);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const live = useRef(true);

  useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);

  const loadMine = useCallback(async () => {
    try {
      const res = await call("?limit=20");
      if (!res.ok) return;
      const data = (await res.json()) as { data: Video[] };
      if (live.current) setMine(data.data.filter((v) => v.model === model));
    } catch {
      // The list is a convenience: the panel works without it.
    }
  }, [model]);

  useEffect(() => {
    void (async () => {
      await loadMine();
      try {
        const res = await call(`/options?model=${encodeURIComponent(model)}`);
        if (!res.ok) { if (live.current) setOptsErr(await failure(res)); return; }
        const data = (await res.json()) as VideoOptions;
        if (!live.current) return;
        setOpts(data);
        setSize(data.default_size);
        setSeconds(data.seconds);
      } catch {
        if (live.current) setOptsErr("Cannot reach the Forgeyard server.");
      }
    })();
  }, [model, loadMine]);

  // Follow the job at work: ask for it every few seconds until it is made or failed.
  const active = job !== null && (job.status === "queued" || job.status === "in_progress");
  useEffect(() => {
    if (!active || !job) return;
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
    const poll = setInterval(async () => {
      try {
        const res = await call(`/${encodeURIComponent(job.id)}`);
        if (!live.current) return;
        if (res.status === 404) { setJob(null); setErr("The video is gone (deleted)."); return; }
        if (!res.ok) return;
        const v = (await res.json()) as Video;
        setJob(v);
        if (v.status === "completed" || v.status === "failed") void loadMine();
      } catch {
        // Try again at the next tick.
      }
    }, POLL_MS);
    return () => { clearInterval(tick); clearInterval(poll); };
  }, [active, job, loadMine]);

  const sizes = opts ? videoSizes(opts) : [];
  const lengths = Array.from({ length: opts?.max_seconds ?? 0 }, (_, i) => i + 1);
  const cap = opts?.max_prompt_chars;
  const chars = prompt.length;

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || sending || active) return;
    setSending(true); setErr(null);
    try {
      const res = await call("", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, prompt, size: size || undefined, seconds: String(seconds || opts?.seconds || 5) }) });
      if (!res.ok) { setErr(await failure(res)); return; }
      const v = (await res.json()) as Video;
      setJob(v);
      setNow(Date.now() / 1000);
      void loadMine();
    } catch {
      setErr("Cannot reach the Forgeyard server.");
    } finally {
      setSending(false);
    }
  };

  const remove = async (v: Video) => {
    setErr(null);
    try {
      const res = await call(`/${encodeURIComponent(v.id)}`, { method: "DELETE" });
      if (!res.ok && res.status !== 404) { setErr(await failure(res)); return; }
      if (job?.id === v.id) setJob(null);
      void loadMine();
    } catch {
      setErr("Cannot reach the Forgeyard server.");
    }
  };

  const shown = job;
  const expired = shown?.expires_at ? shown.expires_at <= now : false;
  return (
    <section className="panel play" aria-labelledby="vid-title">
      <h2 id="vid-title">Video</h2>
      <p className="muted small vid-lead">A video is a job: it is asked for, made on one replica, then fetched. It takes minutes, longer when others
        are waiting; this page follows it, and it keeps going if you leave.</p>
      {optsErr && <Notice tone="error">{optsErr}</Notice>}
      <form onSubmit={go} className="img-form">
        <div className="field">
          <label htmlFor="vid-prompt">Prompt</label>
          <textarea id="vid-prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <p className={`field-hint${cap && chars > cap ? " field-error" : ""}`}>{chars.toLocaleString("en-US")}{cap ? ` of at most ${cap.toLocaleString("en-US")}` : ""} characters</p>
        </div>
        <div className="img-options">
          <div className="field">
            <label htmlFor="vid-size">Size</label>
            <select id="vid-size" value={size} onChange={(e) => setSize(e.target.value)} disabled={!opts}>
              {sizes.map((s) => <option key={s} value={s}>{s}{s === opts?.default_size ? " (default)" : ""}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="vid-seconds">Length</label>
            <select id="vid-seconds" value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} disabled={!opts}>
              {lengths.map((s) => <option key={s} value={s}>{s} s</option>)}
            </select>
          </div>
          {opts && <p className="field-hint vid-frames">{videoFrames(seconds || opts.seconds, opts.fps)} frames at {opts.fps} fps</p>}
        </div>
        <div className="row-gap">
          <button type="submit" className="btn btn-primary" disabled={!opts || sending || active || !prompt.trim() || (cap !== undefined && chars > cap)}>
            <IconMovie size={16} aria-hidden="true" /> Generate
          </button>
          {active && <button type="button" className="btn" onClick={() => job && remove(job)}><IconX size={16} aria-hidden="true" /> Cancel</button>}
        </div>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {shown && active && (
        <div className="vid-job" role="status" aria-label="Video job">
          <div className="vid-job-head">
            <span className={`chip vid-chip vid-${shown.status}`}>{STATUS_LABEL[shown.status]}</span>
            <span className="mono small">{elapsed(now - shown.created_at)}</span>
          </div>
          <span className={`bar vid-bar${shown.progress ? "" : " vid-bar-busy"}`} aria-hidden="true">
            <span style={shown.progress ? { width: `${shown.progress}%` } : undefined} />
          </span>
          <p className="muted small">{shown.size} · {shown.seconds} s · <span className="mono">{shown.id}</span></p>
        </div>
      )}
      {shown && shown.status === "failed" && (
        <div style={{ marginTop: 12 }}><Notice tone="error">{shown.error?.message ?? "The video failed."}</Notice></div>
      )}
      {shown && shown.status === "completed" && (
        <figure className="vid-out" aria-label="Generated video">
          {expired ? <p className="muted">This video expired and was deleted.</p>
            : <video src={contentUrl(shown.id)} controls playsInline preload="metadata" aria-label={`Video for: ${prompt.slice(0, 80)}`} />}
          <figcaption>
            <span className="msg-meta">{shown.size} · {shown.seconds} s · made in {elapsed((shown.completed_at ?? now) - shown.created_at)} · kept until {when(shown.expires_at)}</span>
            {!expired && <a className="btn btn-quiet btn-small" href={contentUrl(shown.id)} download={`${model}-${shown.id.slice(-8)}.mp4`}><IconDownload size={14} aria-hidden="true" /> Download</a>}
          </figcaption>
        </figure>
      )}
      {mine.length > 0 && (
        <div className="vid-mine">
          <h3 className="small">Your videos with this model</h3>
          <ul className="vid-list">
            {mine.map((listed) => listed.id === shown?.id ? shown : listed).map((v) => (
              <li key={v.id} className={v.id === shown?.id ? "on" : ""}>
                <span className={`chip vid-chip vid-${v.status}`}>{STATUS_LABEL[v.status]}</span>
                <span className="small">{when(v.created_at)} · {v.size} · {v.seconds} s</span>
                <span className="vid-list-actions">
                  {v.id !== shown?.id && (v.status === "completed" || v.status === "queued" || v.status === "in_progress") && (
                    <button type="button" className="btn btn-quiet btn-small" onClick={() => { setJob(v); setNow(Date.now() / 1000); }}>
                      <IconPlayerPlay size={14} aria-hidden="true" /> {v.status === "completed" ? "Show" : "Follow"}
                    </button>
                  )}
                  <button type="button" className="btn btn-quiet btn-small" aria-label={`Delete the video of ${when(v.created_at)}`} onClick={() => remove(v)}>
                    <IconTrash size={14} aria-hidden="true" />
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
