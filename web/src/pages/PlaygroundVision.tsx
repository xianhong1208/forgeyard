import { IconEye, IconPhotoUp, IconPlayerStop } from "@tabler/icons-react";
import { useEffect, useRef, useState, type DragEvent, type PointerEvent as ReactPointerEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { Notice } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { answerError, ms } from "../lib/gateway";
import { CLASSIFY_TOP_K, DETECT_FLOOR, MAX_LABELS, START_THRESHOLD, START_THRESHOLD_ZERO_SHOT, TASK_HELP, VISION_TYPES, ZERO_SHOT_HELP, boxStyle,
  checkImage, depthGradient, depthTables, imageLimitMb, labelColours, parseLabels, pngUri, promptFrom, shown, targets, toImage,
  type Prompt, type VisionAnswer, type VisionOptions, type VisionTarget, type VisionTask } from "../lib/vision";
import { useApi } from "../lib/useApi";

type Picked = { file: File; url: string; width: number; height: number };
type Result = { task: VisionTask; answer: VisionAnswer; meta: string; prompt?: Prompt };

const RUN_LABEL: Record<VisionTask, string> = { detect: "Detect objects", segment: "Segment", depth: "Estimate depth", "image-classify": "Classify" };

// What went wrong, in plain words: the gateway's and the vision server's {"error": {code, message}}.
export function visionError(status: number, body: unknown, limitMb: number, what: "app" | "model" = "app"): string {
  const e = (body as { error?: { code?: string; message?: string } } | null)?.error;
  switch (e?.code) {
    case "request_too_large":
    case "image_too_large":
      return `The image is too large for this ${what}: it takes at most ${limitMb} MiB.`;
    case "invalid_image":
      return `The ${what} could not read this image. Send a PNG, JPEG or WebP picture.`;
    case "workload_busy":
    case "key_busy":
      return `The ${what} is busy with other images right now. Try again in a moment.`;
    case "no_ready_replica":
      return `The ${what} is not ready: ${e.message ?? "no replica is running."}`;
    case "upstream_timeout":
      return `The ${what} did not answer in time. A CPU can take a while on a large image; try a smaller one.`;
    case "model_not_found":
      return "This model is no longer deployed. Reload the page to see the ones that are.";
    case "labels_required":
      return what === "model" && e.message?.startsWith("This model classifies")
        ? "This model chooses among the labels you give: type two or more, separated by commas."
        : "This model finds what you name: type one or more things to find, separated by commas.";
    case "labels_not_supported":
      return e.message?.startsWith("This classifier")
        ? "This classifier knows a fixed set of classes and cannot be given labels."
        : "This detector knows a fixed set of classes and cannot be told what to find.";
    case "workload_not_found":
      return "This app is no longer exposed. Reload the page to see the ones that are.";
  }
  return answerError(status, body);
}

// The vision panel for one vision model the Playground's picker chose (1.4, #100): upload an image, then
// boxes, a mask or a depth map.
export function VisionModelPanel({ name }: { name: string }) {
  const { data, error } = useApi<VisionOptions>("/playground/vision/options");
  const t = targets(data).find((x) => x.kind === "model" && x.name === name);
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  if (!t) return <Notice tone="error">{name} is not a vision model any more. Reload the page.</Notice>;
  return <VisionPanel key={t.id} w={t} />;
}

function VisionPanel({ w }: { w: VisionTarget }) {
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
      if (!res.ok || !data) { setErr(visionError(res.status, data, limit, "model")); return; }
      const answer = data as VisionAnswer;
      const target = res.headers.get("X-Forgeyard-Target");
      const count = w.task === "detect" ? `${answer.detections?.length ?? 0} objects above ${DETECT_FLOOR.toFixed(2)}`
        : w.task === "segment" ? `${answer.masks?.length ?? 0} masks`
          : w.task === "image-classify" ? `${answer.labels?.length ?? 0} labels${answer.multi_label ? ", each scored on its own" : ""}` : null;
      const meta = [target?.includes("#") ? `replica ${target.split("#")[1]}` : null, `${answer.width}×${answer.height}`, count,
        answer.ms !== undefined ? `model ${ms(answer.ms)}` : null, `total ${ms(performance.now() - started)}`].filter(Boolean).join(" · ");
      setMaskAt(0);
      setResult({ task: w.task, answer, meta, prompt });
    } catch (e2) {
      if ((e2 as Error).name !== "AbortError") setErr("Cannot reach the Forgeyard server.");
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
        <h2 id="vis-title"><IconEye size={18} stroke={1.7} aria-hidden="true" /> {w.title}</h2>
        <HelpLink page="how-to" anchor="try-vision-in-the-playground">How it works</HelpLink>
      </div>
      <p className="muted small vis-about">
        {(w.zero_shot && ZERO_SHOT_HELP[w.task]) || TASK_HELP[w.task]} <span className="mono">{w.model}</span>{w.licence && <> <span className="chip mono" title="The model's licence">{w.licence}</span></>}
        {" "}Programs call it at <code>{w.call}</code> with an API key.
      </p>
      <div className={`vis-drop${dragOver ? " on" : ""}${image ? " has-image" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)} onDrop={onDrop} data-testid="vis-drop">
        <input ref={picker} id="vis-file" type="file" accept={VISION_TYPES.join(",")} className="sr-only"
          onChange={(e) => { choose(e.target.files?.[0]); e.target.value = ""; }} />
        <IconPhotoUp size={22} stroke={1.6} aria-hidden="true" />
        <span>
          {image ? <><span className="mono small vis-name">{image.file.name}</span>{image.width ? <span className="muted small"> · {image.width}×{image.height}</span> : null}</>
            : "Drop an image here, or"}{" "}
          <button type="button" className="btn btn-small" onClick={() => picker.current?.click()}>{image ? "Choose another" : "Choose an image"}</button>
        </span>
        <span className="muted small">PNG, JPEG or WebP, at most {limit} MiB.</span>
        <label htmlFor="vis-file" className="sr-only">Image</label>
      </div>
      {pickErr && <Notice tone="error">{pickErr}</Notice>}
      {w.zero_shot && (
        <div className="field vis-find">
          <label htmlFor="vis-find">{w.task === "image-classify" ? "Labels" : "Find these"}</label>
          <input id="vis-find" value={find} onChange={(e) => setFind(e.target.value)} autoComplete="off"
            placeholder={w.task === "image-classify" ? "a cat, a dog, a screenshot, an invoice" : "cat, remote control"}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void run(); } }} aria-describedby="vis-find-help" />
          <span id="vis-find-help" className="muted small">{w.task === "image-classify"
            ? <>What the picture may be, separated by commas (at most {MAX_LABELS}). Each is read as “This is a photo of …”.</>
            : <>What to look for, separated by commas (at most {MAX_LABELS}). Plain names work best: “cat”, “red car”.</>}</span>
        </div>
      )}
      {image && (
        <div className="row-gap vis-actions">
          {w.task !== "segment" && (busy
            ? <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
            : <button type="button" className="btn btn-primary" disabled={!w.ready || (w.zero_shot && !find.trim())} onClick={() => void run()}>{w.zero_shot && w.task === "detect" ? "Find" : RUN_LABEL[w.task]}</button>)}
          {w.task === "segment" && <span className="small" role="note">Click a point on the picture, or drag a box around something.</span>}
          {busy && <span className="muted small" role="status">Waiting for the model…</span>}
          {!w.ready && <span className="muted small">The model has no ready replica.</span>}
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
  const all = result?.task === "detect" ? result.answer.detections ?? [] : null;
  const list = all ? shown(all, threshold) : [];
  const colours = labelColours(all ?? []);
  return (
    <>
      {all && (
        <div className="vis-threshold">
          <label htmlFor="vis-threshold">Score at least <span className="mono">{threshold.toFixed(2)}</span></label>
          <input id="vis-threshold" type="range" min={DETECT_FLOOR} max={1} step={0.05} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} />
          <span className="muted small" role="status">{list.length} of {all.length} object{all.length === 1 ? "" : "s"} shown</span>
        </div>
      )}
      <div className="vis-stage">
        <img src={image.url} alt={`Your image: ${image.file.name}`} onLoad={(e) => onLoaded(e.currentTarget)} draggable={false} />
        {all && W > 0 && H > 0 && (
          <div className="vis-overlay" aria-label="Detected objects" role="list">
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
        <ul className="vis-found" aria-label="Objects">
          {list.map((d, i) => (
            <li key={i}><span className="vis-swatch" style={{ background: colours.get(d.label) }} aria-hidden="true" />{d.label} <span className="mono">{d.score.toFixed(2)}</span></li>
          ))}
        </ul>
      )}
      {all && all.length === 0 && <p className="muted small">No object scored above {DETECT_FLOOR}. Try another picture.</p>}
    </>
  );
}

function Segments({ image, result, W, H, maskAt, setMaskAt, onLoaded, busy, onPrompt }: StageProps & {
  maskAt: number; setMaskAt: (i: number) => void; busy: boolean; onPrompt: (p: Prompt) => void;
}) {
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
        <div className="segmented vis-masks" role="group" aria-label="Masks">
          {masks.map((m, i) => (
            <button key={i} type="button" className={i === maskAt ? "on" : ""} aria-pressed={i === maskAt} onClick={() => setMaskAt(i)}>
              Mask {i + 1} · {m.score.toFixed(2)}
            </button>
          ))}
        </div>
      )}
      <div ref={stage} className={`vis-stage vis-pick${busy ? " busy" : ""}`} onPointerDown={down} onPointerMove={move} onPointerUp={up}
        onPointerCancel={() => setDrag(null)} data-testid="vis-stage">
        <img src={image.url} alt={`Your image: ${image.file.name}. Click a point or drag a box.`} onLoad={(e) => onLoaded(e.currentTarget)} draggable={false} />
        {W > 0 && H > 0 && (
          <div className="vis-overlay">
            {mask && (
              <div className="vis-mask" data-testid="vis-mask" aria-label={`Mask ${maskAt + 1}: ${mask.area.toLocaleString("en-US")} pixels, score ${mask.score.toFixed(2)}`}
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
      {mask && <p className="muted small">Mask {maskAt + 1} of {masks?.length}: {mask.area.toLocaleString("en-US")} pixels, score {mask.score.toFixed(2)}. Click or drag again for another.</p>}
      {masks && masks.length === 0 && <p className="muted small">No mask came back. Try a point or box on something else.</p>}
    </>
  );
}

function DepthView({ image, result, colour, setColour, onLoaded }: Omit<StageProps, "W" | "H"> & { colour: boolean; setColour: (v: boolean) => void }) {
  const depth = result?.task === "depth" ? result.answer : null;
  const t = depthTables();
  return (
    <>
      {depth?.depth_png && (
        <div className="segmented" role="group" aria-label="Depth colours">
          <button type="button" className={colour ? "on" : ""} aria-pressed={colour} onClick={() => setColour(true)}>Colour</button>
          <button type="button" className={!colour ? "on" : ""} aria-pressed={!colour} onClick={() => setColour(false)}>Grey</button>
        </div>
      )}
      <svg width="0" height="0" className="vis-defs" aria-hidden="true" focusable="false">
        <filter id="fy-depth-colours" colorInterpolationFilters="sRGB">
          <feComponentTransfer>
            <feFuncR type="table" tableValues={t.r} />
            <feFuncG type="table" tableValues={t.g} />
            <feFuncB type="table" tableValues={t.b} />
          </feComponentTransfer>
        </filter>
      </svg>
      <div className={`vis-pair${depth?.depth_png ? " two" : ""}`}>
        <figure className="vis-fig">
          <img src={image.url} alt={`Your image: ${image.file.name}`} onLoad={(e) => onLoaded(e.currentTarget)} />
          <figcaption className="muted small">Photo</figcaption>
        </figure>
        {depth?.depth_png && (
          <figure className="vis-fig">
            <img src={pngUri(depth.depth_png)} alt="Depth map: brighter is nearer" data-testid="vis-depth"
              style={colour ? { filter: "url(#fy-depth-colours)" } : undefined} />
            <figcaption className="vis-legend small">
              <span className="muted">far</span>
              <span className="vis-ramp" style={{ background: colour ? depthGradient() : "linear-gradient(90deg, #000, #fff)" }} aria-hidden="true" />
              <span className="muted">near</span>
              <span className="muted mono">relative depth {depth.min?.toFixed(2)}–{depth.max?.toFixed(2)}</span>
            </figcaption>
          </figure>
        )}
      </div>
    </>
  );
}

// Image classification (1.4.4, #141): the picture and its labels, best first, each with a bar of its score.
function Classes({ image, result, onLoaded }: { image: Picked; result: Result | null; onLoaded: (el: HTMLImageElement) => void }) {
  const answer = result?.task === "image-classify" ? result.answer : null;
  const labels = answer?.labels ?? [];
  return (
    <div className={`vis-pair${answer ? " two" : ""}`}>
      <figure className="vis-fig">
        <img src={image.url} alt={`Your image: ${image.file.name}`} onLoad={(e) => onLoaded(e.currentTarget)} />
      </figure>
      {answer && (
        <div className="vis-classes">
          {labels.length ? (
            <ol aria-label="Labels" className="vis-class-list">
              {labels.map((c, i) => (
                <li key={`${c.label}-${i}`} className={i === 0 ? "best" : undefined}>
                  <span className="vis-class-name">{c.label}</span>
                  <span className="mono small">{c.score.toFixed(3)}</span>
                  <span className="vis-class-bar" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, c.score)) * 100}%` }} /></span>
                </li>
              ))}
            </ol>
          ) : <p className="muted small">No labels came back.</p>}
          <p className="muted small">{answer.multi_label
            ? "Each label is scored on its own (0 to 1), so several can fit, or none."
            : "The scores share 1 between the labels: the best one is the model's answer."}</p>
        </div>
      )}
    </div>
  );
}
