import { IconDownload, IconMovie, IconPlayerPlay, IconTrash, IconX } from "@tabler/icons-react";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { ImagePick } from "../components/ImagePick";
import { Notice } from "../components/ui";
import { labels, t as tNow } from "../i18n/core";
import { getLang, locale } from "../i18n/lang";
import { useT } from "../i18n/react";
import { notifyUnauthorized } from "../lib/api";
import { videoFrames, videoSizes, type Video, type VideoOptions } from "../lib/gateway";
import { startImage, videoForm, type Picked } from "../lib/imageEdit";
import { clock, playError } from "../lib/playground";

// The Playground's Video panel (1.3): a video is a job. It is asked for at /api/playground/videos (the
// gateway's /v1/videos rules, as the signed-in person: the session cookie and the CSRF header, counted as
// "console" in Usage), followed until it is made, then played from …/content. Jobs belong to the person,
// so a video asked for earlier is still listed when the page is opened again. No API key. 1.5.5 (#143): a model that
// takes a start image (input_reference) gets a picker for one, optional or required as the model says; with an image
// the request is multipart, as the OpenAI SDK sends it.
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
  return playError(res.status, await res.json().catch(() => null));
}

const STATUS_LABEL: Record<Video["status"], string> = labels({
  queued: "playground.video.status.queued", in_progress: "playground.video.status.in_progress", completed: "playground.video.status.completed",
  failed: "playground.video.status.failed",
});

export const contentUrl = (id: string) => `/api/playground/videos/${encodeURIComponent(id)}/content`;

function when(ts: number | null): string {
  if (!ts) return "—";
  const lang = getLang();
  return new Date(ts * 1000).toLocaleString(lang === "en" ? "en-GB" : locale(lang), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function Videos({ model }: { model: string }) {
  const t = useT();
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
  const [start, setStart] = useState<Picked[]>([]);
  const [startedFrom, setStartedFrom] = useState<{ id: string; p: Picked } | null>(null);
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
        if (live.current) setOptsErr(tNow("playground.unreachable"));
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
        if (res.status === 404) { setJob(null); setErr(tNow("playground.video.gone")); return; }
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
  const takes = startImage(opts?.image_input);
  const needsImage = takes === "required" && start.length === 0;

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || sending || active || needsImage) return;
    setSending(true); setErr(null);
    const length = String(seconds || opts?.seconds || 5);
    const image = takes !== "none" ? start[0] : undefined;
    try {
      const res = image
        ? await call("", { method: "POST", body: videoForm(model, prompt, size, length, image) })
        : await call("", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model, prompt, size: size || undefined, seconds: length }) });
      if (!res.ok) { setErr(await failure(res)); return; }
      const v = (await res.json()) as Video;
      setStartedFrom(image ? { id: v.id, p: image } : null);
      setJob(v);
      setNow(Date.now() / 1000);
      void loadMine();
    } catch {
      setErr(t("playground.unreachable"));
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
      setErr(t("playground.unreachable"));
    }
  };

  // A finished video is deleted at expires_at. The clock above only ticks while a job is at work, so wake up at that
  // moment: the player and the download link turn into the expired notice even when nothing else re-renders.
  useEffect(() => {
    if (!job || job.status !== "completed" || !job.expires_at) return;
    const left = job.expires_at * 1000 - Date.now();
    const timer = setTimeout(() => setNow(Date.now() / 1000), Math.min(Math.max(left + 250, 0), 2 ** 31 - 1));
    return () => clearTimeout(timer);
  }, [job]);

  const shown = job;
  const expired = shown?.expires_at ? shown.expires_at <= now : false;
  return (
    <section className="panel play" aria-labelledby="vid-title">
      <div className="panel-head">
        <h2 id="vid-title">{t("playground.video.title")}</h2>
        {takes !== "none" && <HelpLink page="how-to" anchor="make-a-video-from-a-picture">{t("playground.video.howTo")}</HelpLink>}
      </div>
      <p className="muted small vid-lead">{t("playground.video.lead")}</p>
      {optsErr && <Notice tone="error">{optsErr}</Notice>}
      <form onSubmit={go} className="img-form">
        {takes !== "none" && (
          <ImagePick id="vid-start" label={t(takes === "required" ? "playground.video.start.required" : "playground.video.start.optional")} max={1}
            images={start} onChange={setStart} disabled={sending || active}
            hint={t(takes === "required" ? "playground.video.start.hintRequired" : "playground.video.start.hintOptional", { side: String(opts?.max_input_side ?? 0) })} />
        )}
        <div className="field">
          <label htmlFor="vid-prompt">{t("playground.prompt")}</label>
          <textarea id="vid-prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <p className={`field-hint${cap && chars > cap ? " field-error" : ""}`}>{cap ? t("playground.charsOf", { count: chars, max: cap }) : t("playground.chars", { count: chars })}</p>
        </div>
        <div className="img-options">
          <div className="field">
            <label htmlFor="vid-size">{t("playground.size")}</label>
            <select id="vid-size" value={size} onChange={(e) => setSize(e.target.value)} disabled={!opts}>
              {sizes.map((s) => <option key={s} value={s}>{s}{s === opts?.default_size ? t("playground.default") : ""}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="vid-seconds">{t("playground.video.length")}</label>
            <select id="vid-seconds" value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} disabled={!opts}>
              {lengths.map((s) => <option key={s} value={s}>{t("playground.video.seconds", { n: String(s) })}</option>)}
            </select>
          </div>
          {opts && <p className="field-hint vid-frames">{t("playground.video.frames", { frames: String(videoFrames(seconds || opts.seconds, opts.fps)), fps: String(opts.fps) })}</p>}
        </div>
        <div className="row-gap">
          <button type="submit" className="btn btn-primary" disabled={!opts || sending || active || needsImage || !prompt.trim() || (cap !== undefined && chars > cap)}>
            <IconMovie size={16} aria-hidden="true" /> {t("playground.generate")}
          </button>
          {needsImage && opts && <span className="muted small">{t("playground.video.start.needOne")}</span>}
          {active && <button type="button" className="btn" onClick={() => job && remove(job)}><IconX size={16} aria-hidden="true" /> {t("common.cancel")}</button>}
        </div>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {shown && active && (
        <div className="vid-job" role="status" aria-label={t("playground.video.job")}>
          <div className="vid-job-head">
            <span className={`chip vid-chip vid-${shown.status}`}>{STATUS_LABEL[shown.status]}</span>
            <span className="mono small">{clock(now - shown.created_at)}</span>
          </div>
          <span className={`bar vid-bar${shown.progress ? "" : " vid-bar-busy"}`} aria-hidden="true">
            <span style={shown.progress ? { width: `${shown.progress}%` } : undefined} />
          </span>
          <p className="muted small">{t("playground.video.jobLine", { size: shown.size, seconds: shown.seconds })}<span className="mono">{shown.id}</span></p>
          {startedFrom?.id === shown.id && <StartedFrom p={startedFrom.p} />}
        </div>
      )}
      {shown && shown.status === "failed" && (
        <div style={{ marginTop: 12 }}><Notice tone="error">{shown.error?.message ?? t("playground.video.failed")}</Notice></div>
      )}
      {shown && shown.status === "completed" && (
        <figure className="vid-out" aria-label={t("playground.video.made")}>
          {expired ? <p className="muted">{t("playground.video.expired")}</p>
            : <video src={contentUrl(shown.id)} controls playsInline preload="metadata" aria-label={t("playground.video.alt", { prompt: prompt.slice(0, 80) })} />}
          {startedFrom?.id === shown.id && <StartedFrom p={startedFrom.p} />}
          <figcaption>
            <span className="msg-meta">{t("playground.video.caption", { size: shown.size, seconds: shown.seconds, took: clock((shown.completed_at ?? now) - shown.created_at),
              until: when(shown.expires_at) })}</span>
            {!expired && <a className="btn btn-quiet btn-small" href={contentUrl(shown.id)} download={`${model}-${shown.id.slice(-8)}.mp4`}><IconDownload size={14} aria-hidden="true" /> {t("playground.download")}</a>}
          </figcaption>
        </figure>
      )}
      {mine.length > 0 && (
        <div className="vid-mine">
          <h3 className="small">{t("playground.video.mine")}</h3>
          <ul className="vid-list">
            {mine.map((listed) => listed.id === shown?.id ? shown : listed).map((v) => (
              <li key={v.id} className={v.id === shown?.id ? "on" : ""}>
                <span className={`chip vid-chip vid-${v.status}`}>{STATUS_LABEL[v.status]}</span>
                <span className="small">{t("playground.video.listLine", { when: when(v.created_at), size: v.size, seconds: v.seconds })}</span>
                <span className="vid-list-actions">
                  {v.id !== shown?.id && (v.status === "completed" || v.status === "queued" || v.status === "in_progress") && (
                    <button type="button" className="btn btn-quiet btn-small" onClick={() => { setJob(v); setNow(Date.now() / 1000); }}>
                      <IconPlayerPlay size={14} aria-hidden="true" /> {v.status === "completed" ? t("playground.video.show") : t("playground.video.follow")}
                    </button>
                  )}
                  <button type="button" className="btn btn-quiet btn-small" aria-label={t("playground.video.delete", { when: when(v.created_at) })} onClick={() => remove(v)}>
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

// The start image a job was asked with, beside it while it is made and under the video once it is.
function StartedFrom({ p }: { p: Picked }) {
  const t = useT();
  const line = t("playground.video.start.used", { name: p.name });
  return (
    <p className="vid-start small muted"><img className="emb-thumb" src={p.uri} alt="" /><span title={p.name}>{line}</span></p>
  );
}
