import { IconEye, IconPhotoUp, IconPlayerStop } from "@tabler/icons-react";
import { useEffect, useRef, useState, type DragEvent, type PointerEvent as ReactPointerEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { Notice } from "../components/ui";
import { labels, t } from "../i18n/core";
import { useT } from "../i18n/react";
import { notifyUnauthorized } from "../lib/api";
import { ms } from "../lib/gateway";
import { playError } from "../lib/playground";
import { CLASSIFY_TOP_K, DETECT_FLOOR, MAX_LABELS, START_THRESHOLD, START_THRESHOLD_ZERO_SHOT, TASK_HELP, VISION_TYPES, ZERO_SHOT_HELP, boxStyle,
  checkImage, depthGradient, depthTables, imageLimitMb, labelColours, parseLabels, pngUri, promptFrom, shown, targets, toImage,
  type Prompt, type VisionAnswer, type VisionOptions, type VisionTarget, type VisionTask } from "../lib/vision";
import { useApi } from "../lib/useApi";

type Picked = { file: File; url: string; width: number; height: number };
type Result = { task: VisionTask; answer: VisionAnswer; meta: string; prompt?: Prompt };

const RUN_LABEL: Record<VisionTask, string> = labels({
  detect: "playground.vision.run.detect", segment: "playground.vision.run.segment", depth: "playground.vision.run.depth",
  "image-classify": "playground.vision.run.image-classify",
});
// The task's title: the server's in English, ours in another language.
const TASK_TITLE: Record<VisionTask, string> = labels({
  detect: "playground.vision.task.detect", segment: "playground.vision.task.segment", depth: "playground.vision.task.depth",
  "image-classify": "playground.vision.task.image-classify",
});

// What went wrong, in plain words: the gateway's and the vision server's {"error": {code, message}}.
export function visionError(status: number, body: unknown, limitMb: number): string {
  const e = (body as { error?: { code?: string; message?: string } } | null)?.error;
  const w = t("playground.vision.what.model");
  switch (e?.code) {
    case "request_too_large":
    case "image_too_large":
      return t("playground.vision.err.tooLarge", { what: w, mb: String(limitMb) });
    case "invalid_image":
      return t("playground.vision.err.invalid", { what: w });
    case "no_ready_replica":
      return t("playground.vision.err.notReady", { what: w, message: e.message ?? t("playground.vision.err.noReplica") });
    case "upstream_timeout":
      return t("playground.vision.err.timeout", { what: w });
    case "model_not_found":
      return t("playground.vision.err.gone");
    case "labels_required":
      return e.message?.startsWith("This model classifies")
        ? t("playground.vision.err.labelsClassify") : t("playground.vision.err.labelsDetect");
    case "labels_not_supported":
      return e.message?.startsWith("This classifier") ? t("playground.vision.err.fixedClassifier") : t("playground.vision.err.fixedDetector");
  }
  return playError(status, body);
}

// The vision panel for one vision model the Playground's picker chose (1.4, #100): upload an image, then
// boxes, a mask or a depth map.
export function VisionModelPanel({ name }: { name: string }) {
  const tr = useT();
  const { data, error } = useApi<VisionOptions>("/playground/vision/options");
  const found = targets(data).find((x) => x.kind === "model" && x.name === name);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">{tr("common.loading")}</p>;
  if (!found) return <Notice tone="error">{tr("playground.vision.notVision", { name })}</Notice>;
  return <VisionPanel key={found.id} w={found} />;
}

function VisionPanel({ w }: { w: VisionTarget }) {
  const tr = useT();
  const limit = imageLimitMb(w);
  const [image, setImage] = useState<Picked | null>(null);
  const [pickErr, setPickErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [threshold, setThreshold] = useState(w.zero_shot ? START_THRESHOLD_ZERO_SHOT : START_THRESHOLD);
  const [find, setFind] = useState("");          // a zero-shot detector: what to find, comma-separated (1.4.4)
  const [maskAt, setMaskAt] = useState(0);
  const [colour, setColour] = useState(true);
  const [dragOver, setDragOver] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  useEffect(() => () => { if (image) URL.revokeObjectURL(image.url); }, [image]);
  useEffect(() => () => abort.current?.abort(), []);

  const choose = (file: File | undefined) => {
    if (!file) return;
    const why = checkImage(file, limit);
    setPickErr(why);
    if (why) return;
    setErr(null);
    setResult(null);
    setImage({ file, url: URL.createObjectURL(file), width: 0, height: 0 });
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    choose(e.dataTransfer?.files?.[0]);
  };

  const run = async (prompt?: Prompt) => {
    if (!image || busy) return;
    const asked = w.zero_shot ? parseLabels(find) : null;
    if (asked?.error) { setErr(asked.error); return; }
    setBusy(true); setErr(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    try {
      const form = new FormData();
      form.append("image", image.file, image.file.name || "image");
      if (w.task === "detect") form.append("threshold", String(DETECT_FLOOR));
      if (w.task === "image-classify" && !asked) form.append("top_k", String(CLASSIFY_TOP_K));
      if (asked) form.append("labels", JSON.stringify(asked.labels));
      if (prompt?.kind === "point") form.append("points", JSON.stringify([prompt.point]));
      if (prompt?.kind === "box") form.append("box", JSON.stringify(prompt.box));
      form.append("model", w.name);
      const res = await fetch(w.url, {
        method: "POST", credentials: "same-origin", headers: { "X-Forgeyard": "1" }, body: form, signal: ctrl.signal,
      });
      if (res.status === 401) {
        const data = await res.json().catch(() => null);
        notifyUnauthorized((data as { detail?: { reason?: string } } | null)?.detail?.reason);
        return;
      }
      const data = await res.json().catch(() => null);
      if (!res.ok || !data) { setErr(visionError(res.status, data, limit)); return; }
      const answer = data as VisionAnswer;
      const target = res.headers.get("X-Forgeyard-Target");
      const count = w.task === "detect" ? tr("playground.vision.objects", { count: String(answer.detections?.length ?? 0), floor: DETECT_FLOOR.toFixed(2) })
        : w.task === "segment" ? tr("playground.vision.masks", { count: String(answer.masks?.length ?? 0) })
          : w.task === "image-classify" ? `${tr("playground.vision.classes", { count: String(answer.labels?.length ?? 0) })}${answer.multi_label ? tr("playground.vision.ownScore") : ""}`
            : null;
      const meta = [target?.includes("#") ? tr("playground.meta.replica", { n: target.split("#")[1] }) : null, `${answer.width}×${answer.height}`, count,
        answer.ms !== undefined ? tr("playground.vision.modelTime", { time: ms(answer.ms) }) : null,
        tr("playground.vision.totalTime", { time: ms(performance.now() - started) })].filter(Boolean).join(" · ");
      setMaskAt(0);
      setResult({ task: w.task, answer, meta, prompt });
    } catch (e2) {
      if ((e2 as Error).name !== "AbortError") setErr(tr("playground.unreachable"));
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  const onLoaded = (el: HTMLImageElement) => {
    const src = el.getAttribute("src");
    setImage((cur) => (cur && cur.url === src ? { ...cur, width: el.naturalWidth, height: el.naturalHeight } : cur));
  };

  const W = result?.answer.width || image?.width || 0;
  const H = result?.answer.height || image?.height || 0;

  return (
    <section className="panel play vision" aria-labelledby="vis-title">
      <div className="panel-head">
        <h2 id="vis-title"><IconEye size={18} stroke={1.7} aria-hidden="true" /> {tr.lang === "en" ? w.title : TASK_TITLE[w.task]}</h2>
        <HelpLink page="how-to" anchor="try-vision-in-the-playground">{tr("playground.vision.howItWorks")}</HelpLink>
      </div>
      <p className="muted small vis-about">
        {(w.zero_shot && ZERO_SHOT_HELP[w.task]) || TASK_HELP[w.task]} <span className="mono">{w.model}</span>{w.licence && <> <span className="chip mono" title={tr("playground.vision.licence")}>{w.licence}</span></>}
        {" "}{tr.rich("playground.vision.callAt", { call: <code>{w.call}</code> })}
      </p>
      <div className={`vis-drop${dragOver ? " on" : ""}${image ? " has-image" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)} onDrop={onDrop} data-testid="vis-drop">
        <input ref={picker} id="vis-file" type="file" accept={VISION_TYPES.join(",")} className="sr-only"
          onChange={(e) => { choose(e.target.files?.[0]); e.target.value = ""; }} />
        <IconPhotoUp size={22} stroke={1.6} aria-hidden="true" />
        <span>
          {image ? <><span className="mono small vis-name">{image.file.name}</span>{image.width ? <span className="muted small"> · {image.width}×{image.height}</span> : null}</>
            : tr("playground.vision.drop")}{" "}
          <button type="button" className="btn btn-small" onClick={() => picker.current?.click()}>{image ? tr("playground.vision.chooseAnother") : tr("playground.vision.choose")}</button>
        </span>
        <span className="muted small">{tr("playground.vision.types", { mb: String(limit) })}</span>
        <label htmlFor="vis-file" className="sr-only">{tr("playground.vision.image")}</label>
      </div>
      {pickErr && <Notice tone="error">{pickErr}</Notice>}
      {w.zero_shot && (
        <div className="field vis-find">
          <label htmlFor="vis-find">{w.task === "image-classify" ? tr("playground.vision.labels") : tr("playground.vision.findThese")}</label>
          <input id="vis-find" value={find} onChange={(e) => setFind(e.target.value)} autoComplete="off"
            placeholder={w.task === "image-classify" ? tr("playground.vision.labelsPlaceholder") : tr("playground.vision.findPlaceholder")}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void run(); } }} aria-describedby="vis-find-help" />
          <span id="vis-find-help" className="muted small">{w.task === "image-classify"
            ? tr("playground.vision.labelsHelp", { max: String(MAX_LABELS) }) : tr("playground.vision.findHelp", { max: String(MAX_LABELS) })}</span>
        </div>
      )}
      {image && (
        <div className="row-gap vis-actions">
          {w.task !== "segment" && (busy
            ? <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> {tr("playground.stop")}</button>
            : <button type="button" className="btn btn-primary" disabled={!w.ready || (w.zero_shot && !find.trim())} onClick={() => void run()}>{w.zero_shot && w.task === "detect" ? tr("playground.vision.find") : RUN_LABEL[w.task]}</button>)}
          {w.task === "segment" && <span className="small" role="note">{tr("playground.vision.segmentHint")}</span>}
          {busy && <span className="muted small" role="status">{tr("playground.vision.waiting")}</span>}
          {!w.ready && <span className="muted small">{tr("playground.vision.notReady")}</span>}
        </div>
      )}
      {err && <Notice tone="error">{err}</Notice>}
      {image && w.task === "detect" && (
        <Detections image={image} result={result} W={W} H={H} threshold={threshold} setThreshold={setThreshold} onLoaded={onLoaded} />
      )}
      {image && w.task === "segment" && (
        <Segments image={image} result={result} W={W} H={H} maskAt={maskAt} setMaskAt={setMaskAt} onLoaded={onLoaded}
          busy={busy || !w.ready} onPrompt={(p) => void run(p)} />
      )}
      {image && w.task === "depth" && (
        <DepthView image={image} result={result} colour={colour} setColour={setColour} onLoaded={onLoaded} />
      )}
      {image && w.task === "image-classify" && <Classes image={image} result={result} onLoaded={onLoaded} />}
      {result && <span className="msg-meta vis-meta">{result.meta}</span>}
    </section>
  );
}

type StageProps = { image: Picked; result: Result | null; W: number; H: number; onLoaded: (el: HTMLImageElement) => void };

function Detections({ image, result, W, H, threshold, setThreshold, onLoaded }: StageProps & { threshold: number; setThreshold: (v: number) => void }) {
  const tr = useT();
  const all = result?.task === "detect" ? result.answer.detections ?? [] : null;
  const list = all ? shown(all, threshold) : [];
  const colours = labelColours(all ?? []);
  return (
    <>
      {all && (
        <div className="vis-threshold">
          <label htmlFor="vis-threshold">{tr.rich("playground.vision.scoreAtLeast", { value: <span className="mono">{threshold.toFixed(2)}</span> })}</label>
          <input id="vis-threshold" type="range" min={DETECT_FLOOR} max={1} step={0.05} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
          <span className="muted small" role="status">{tr.plural("playground.vision.shown", all.length, { shown: String(list.length) })}</span>
        </div>
      )}
      <div className="vis-stage">
        <img src={image.url} alt={tr("playground.vision.yourImage", { name: image.file.name })} onLoad={(e) => onLoaded(e.currentTarget)} draggable={false} />
        {all && W > 0 && H > 0 && (
          <div className="vis-overlay" aria-label={tr("playground.vision.detected")} role="list">
            {list.map((d, i) => (
              <div key={i} role="listitem" className="vis-box" style={{ ...boxStyle(d.box, W, H), borderColor: colours.get(d.label) }}
                aria-label={`${d.label} ${d.score.toFixed(2)}`}>
                <span className={`vis-tag${d.box[1] / H < 0.06 ? " inside" : ""}`} style={{ background: colours.get(d.label) }}>{d.label} {d.score.toFixed(2)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      {list.length > 0 && (
        <ul className="vis-found" aria-label={tr("playground.vision.objectList")}>
          {list.map((d, i) => (
            <li key={i}><span className="vis-swatch" style={{ background: colours.get(d.label) }} aria-hidden="true" />{d.label} <span className="mono">{d.score.toFixed(2)}</span></li>
          ))}
        </ul>
      )}
      {all && all.length === 0 && <p className="muted small">{tr("playground.vision.noObject", { floor: String(DETECT_FLOOR) })}</p>}
    </>
  );
}

function Segments({ image, result, W, H, maskAt, setMaskAt, onLoaded, busy, onPrompt }: StageProps & {
  maskAt: number; setMaskAt: (i: number) => void; busy: boolean; onPrompt: (p: Prompt) => void;
}) {
  const tr = useT();
  const stage = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ start: [number, number]; at: [number, number]; css: [number, number]; moved: number } | null>(null);
  const masks = result?.task === "segment" ? result.answer.masks ?? [] : null;
  const mask = masks?.[maskAt];

  const where = (e: ReactPointerEvent): { img: [number, number]; css: [number, number] } | null => {
    const el = stage.current?.querySelector("img");
    if (!el || !W || !H) return null;
    const rect = el.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    return { img: toImage(x, y, rect, W, H), css: [x, y] };
  };
  const down = (e: ReactPointerEvent) => {
    if (busy) return;
    const p = where(e);
    if (!p) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setDrag({ start: p.img, at: p.img, css: p.css, moved: 0 });
  };
  const move = (e: ReactPointerEvent) => {
    if (!drag) return;
    const p = where(e);
    if (!p) return;
    setDrag({ ...drag, at: p.img, moved: Math.max(drag.moved, Math.hypot(p.css[0] - drag.css[0], p.css[1] - drag.css[1])) });
  };
  const up = (e: ReactPointerEvent) => {
    if (!drag) return;
    const p = where(e);
    const end = p?.img ?? drag.at;
    const moved = p ? Math.max(drag.moved, Math.hypot(p.css[0] - drag.css[0], p.css[1] - drag.css[1])) : drag.moved;
    setDrag(null);
    onPrompt(promptFrom(drag.start, end, moved));
  };

  const prompt = result?.prompt;
  const live = drag && drag.moved >= 6 ? promptFrom(drag.start, drag.at, drag.moved) : null;
  return (
    <>
      {masks && masks.length > 1 && (
        <div className="segmented vis-masks" role="group" aria-label={tr("playground.vision.maskGroup")}>
          {masks.map((m, i) => (
            <button key={i} type="button" className={i === maskAt ? "on" : ""} aria-pressed={i === maskAt} onClick={() => setMaskAt(i)}>
              {tr("playground.vision.maskButton", { n: String(i + 1), score: m.score.toFixed(2) })}
            </button>
          ))}
        </div>
      )}
      <div ref={stage} className={`vis-stage vis-pick${busy ? " busy" : ""}`} onPointerDown={down} onPointerMove={move} onPointerUp={up}
        onPointerCancel={() => setDrag(null)} data-testid="vis-stage">
        <img src={image.url} alt={tr("playground.vision.pickAlt", { name: image.file.name })} onLoad={(e) => onLoaded(e.currentTarget)} draggable={false} />
        {W > 0 && H > 0 && (
          <div className="vis-overlay">
            {mask && (
              <div className="vis-mask" data-testid="vis-mask" aria-label={tr("playground.vision.maskLabel", { n: String(maskAt + 1), area: tr.num(mask.area), score: mask.score.toFixed(2) })}
                role="img" style={{ maskImage: `url("${pngUri(mask.png)}")` }} />
            )}
            {mask?.box && <div className="vis-box vis-mask-box" style={boxStyle(mask.box, W, H)} />}
            {!drag && prompt?.kind === "box" && <div className="vis-box vis-prompt" style={boxStyle(prompt.box, W, H)} data-testid="vis-prompt" />}
            {!drag && prompt?.kind === "point" && (
              <span className="vis-point" style={{ left: `${prompt.point[0] / W * 100}%`, top: `${prompt.point[1] / H * 100}%` }} data-testid="vis-prompt" />
            )}
            {live?.kind === "box" && <div className="vis-box vis-prompt" style={boxStyle(live.box, W, H)} />}
          </div>
        )}
      </div>
      {mask && <p className="muted small">{tr("playground.vision.maskLine", { n: String(maskAt + 1), total: String(masks?.length), area: tr.num(mask.area), score: mask.score.toFixed(2) })}</p>}
      {masks && masks.length === 0 && <p className="muted small">{tr("playground.vision.noMask")}</p>}
    </>
  );
}

function DepthView({ image, result, colour, setColour, onLoaded }: Omit<StageProps, "W" | "H"> & { colour: boolean; setColour: (v: boolean) => void }) {
  const tr = useT();
  const depth = result?.task === "depth" ? result.answer : null;
  const tables = depthTables();
  return (
    <>
      {depth?.depth_png && (
        <div className="segmented" role="group" aria-label={tr("playground.vision.depthColours")}>
          <button type="button" className={colour ? "on" : ""} aria-pressed={colour} onClick={() => setColour(true)}>{tr("playground.vision.colour")}</button>
          <button type="button" className={!colour ? "on" : ""} aria-pressed={!colour} onClick={() => setColour(false)}>{tr("playground.vision.grey")}</button>
        </div>
      )}
      <svg width="0" height="0" className="vis-defs" aria-hidden="true" focusable="false">
        <filter id="fy-depth-colours" colorInterpolationFilters="sRGB">
          <feComponentTransfer>
            <feFuncR type="table" tableValues={tables.r} />
            <feFuncG type="table" tableValues={tables.g} />
            <feFuncB type="table" tableValues={tables.b} />
          </feComponentTransfer>
        </filter>
      </svg>
      <div className={`vis-pair${depth?.depth_png ? " two" : ""}`}>
        <figure className="vis-fig">
          <img src={image.url} alt={tr("playground.vision.yourImage", { name: image.file.name })} onLoad={(e) => onLoaded(e.currentTarget)} />
          <figcaption className="muted small">{tr("playground.vision.photo")}</figcaption>
        </figure>
        {depth?.depth_png && (
          <figure className="vis-fig">
            <img src={pngUri(depth.depth_png)} alt={tr("playground.vision.depthAlt")} data-testid="vis-depth"
              style={colour ? { filter: "url(#fy-depth-colours)" } : undefined} />
            <figcaption className="vis-legend small">
              <span className="muted">{tr("playground.vision.far")}</span>
              <span className="vis-ramp" style={{ background: colour ? depthGradient() : "linear-gradient(90deg, #000, #fff)" }} aria-hidden="true" />
              <span className="muted">{tr("playground.vision.near")}</span>
              <span className="muted mono">{tr("playground.vision.relative", { min: depth.min?.toFixed(2) ?? "", max: depth.max?.toFixed(2) ?? "" })}</span>
            </figcaption>
          </figure>
        )}
      </div>
    </>
  );
}

// Image classification (1.4.4, #141): the picture and its labels, best first, each with a bar of its score.
function Classes({ image, result, onLoaded }: { image: Picked; result: Result | null; onLoaded: (el: HTMLImageElement) => void }) {
  const tr = useT();
  const answer = result?.task === "image-classify" ? result.answer : null;
  const labels = answer?.labels ?? [];
  return (
    <div className={`vis-pair${answer ? " two" : ""}`}>
      <figure className="vis-fig">
        <img src={image.url} alt={tr("playground.vision.yourImage", { name: image.file.name })} onLoad={(e) => onLoaded(e.currentTarget)} />
      </figure>
      {answer && (
        <div className="vis-classes">
          {labels.length ? (
            <ol aria-label={tr("playground.vision.labels")} className="vis-class-list">
              {labels.map((c, i) => (
                <li key={`${c.label}-${i}`} className={i === 0 ? "best" : undefined}>
                  <span className="vis-class-name">{c.label}</span>
                  <span className="mono small">{c.score.toFixed(3)}</span>
                  <span className="vis-class-bar" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, c.score)) * 100}%` }} /></span>
                </li>
              ))}
            </ol>
          ) : <p className="muted small">{tr("playground.vision.noLabels")}</p>}
          <p className="muted small">{answer.multi_label ? tr("playground.vision.multi") : tr("playground.vision.single")}</p>
        </div>
      )}
    </div>
  );
}
