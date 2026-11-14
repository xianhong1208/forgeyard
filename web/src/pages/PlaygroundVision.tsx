import { IconEye, IconPhotoUp, IconPlayerStop, IconTemplate } from "@tabler/icons-react";
import { useEffect, useRef, useState, type DragEvent, type PointerEvent as ReactPointerEvent } from "react";
import { Link } from "react-router";

import { HelpLink } from "../components/HelpLink";
import { Notice } from "../components/ui";
import { notifyUnauthorized } from "../lib/api";
import { answerError, ms } from "../lib/gateway";
import { DETECT_FLOOR, TASK_HELP, VISION_TYPES, boxStyle, checkImage, depthGradient, depthTables, imageLimitMb, labelColours,
  pngUri, promptFrom, shown, toImage, workloadLabel,
  type Prompt, type VisionAnswer, type VisionOptions, type VisionTask, type VisionWorkload } from "../lib/vision";
import { useApi } from "../lib/useApi";

type Picked = { file: File; url: string; width: number; height: number };
type Result = { task: VisionTask; answer: VisionAnswer; meta: string; prompt?: Prompt };

const RUN_LABEL: Record<VisionTask, string> = { detect: "Detect objects", segment: "Segment", depth: "Estimate depth" };

// What went wrong, in plain words: the gateway's and the vision server's {"error": {code, message}}.
export function visionError(status: number, body: unknown, limitMb: number): string {
  const e = (body as { error?: { code?: string; message?: string } } | null)?.error;
  switch (e?.code) {
    case "request_too_large":
    case "image_too_large":
      return `The image is too large for this workload: it takes at most ${limitMb} MiB.`;
    case "invalid_image":
      return "The workload could not read this image. Send a PNG, JPEG or WebP picture.";
    case "workload_busy":
    case "key_busy":
      return "The workload is busy with other images right now. Try again in a moment.";
    case "no_ready_replica":
      return `The workload is not ready: ${e.message ?? "no replica is running."}`;
    case "upstream_timeout":
      return "The workload did not answer in time. A CPU can take a while on a large image; try a smaller one.";
    case "workload_not_found":
      return "This workload is no longer exposed. Reload the page to see the ones that are.";
  }
  return answerError(status, body);
}

export function Vision({ canCreate }: { canCreate: boolean }) {
  const { data, error } = useApi<VisionOptions>("/playground/vision/options");
  const workloads = data?.workloads ?? [];
  const [picked, setPicked] = useState("");
  const fallback = workloads.find((w) => w.ready) ?? workloads[0];
  const w = workloads.find((x) => x.name === picked) ?? fallback;

  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  if (!workloads.length) {
    return (
      <section className="panel play-empty" aria-labelledby="vis-empty-title">
        <h2 id="vis-empty-title">No vision workload to try yet</h2>
        <p className="muted">
          The Vision tab sends a picture to an exposed vision workload: object detection, segmentation or depth estimation.
          None is exposed yet.{" "}
          {canCreate ? "Start one from a template on the Workloads page; it shows up here once it runs."
            : "Admins and operators start them from a template; ask one to start the one you need."}
        </p>
        <ul className="vis-kinds">
          {data.tasks.map((t) => <li key={t.task}><span className="strong">{t.title}</span> <span className="muted small mono">{t.model}</span></li>)}
        </ul>
        <div className="row-gap">
          {canCreate && <Link to="/workloads" className="btn btn-primary"><IconTemplate size={16} aria-hidden="true" /> Start from a template</Link>}
          <HelpLink page="how-to" anchor="try-a-vision-workload-in-the-playground">How the Vision tab works</HelpLink>
        </div>
      </section>
    );
  }
  return (
    <>
      <div className="play-bar vis-bar">
        <div className="field">
          <label htmlFor="vis-workload">Workload</label>
          <select id="vis-workload" value={w?.name ?? ""} onChange={(e) => setPicked(e.target.value)}>
            {workloads.map((x) => <option key={x.name} value={x.name}>{workloadLabel(x)}</option>)}
          </select>
        </div>
        {w && (
          <p className="muted small play-ready">
            {w.ready ? `${w.ready} ready replica${w.ready > 1 ? "s" : ""}` : (w.reason ?? "No replica is ready yet.")}{" "}
            <Link to={`/workloads/${w.workload_id}`}>Workload</Link>
          </p>
        )}
      </div>
      {w && <VisionPanel key={w.name} w={w} />}
    </>
  );
}

function VisionPanel({ w }: { w: VisionWorkload }) {
  const limit = imageLimitMb(w);
  const [image, setImage] = useState<Picked | null>(null);
  const [pickErr, setPickErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [threshold, setThreshold] = useState(0.5);
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
    setBusy(true); setErr(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    try {
      const form = new FormData();
      form.append("image", image.file, image.file.name || "image");
      if (w.task === "detect") form.append("threshold", String(DETECT_FLOOR));
      if (prompt?.kind === "point") form.append("points", JSON.stringify([prompt.point]));
      if (prompt?.kind === "box") form.append("box", JSON.stringify(prompt.box));
      const res = await fetch(`/api/playground/vision/${encodeURIComponent(w.name)}/${w.task}`, {
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
      const count = w.task === "detect" ? `${answer.detections?.length ?? 0} objects above ${DETECT_FLOOR.toFixed(2)}` : w.task === "segment" ? `${answer.masks?.length ?? 0} masks` : null;
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
        <HelpLink page="how-to" anchor="try-a-vision-workload-in-the-playground">How it works</HelpLink>
      </div>
      <p className="muted small vis-about">
        {TASK_HELP[w.task]} <span className="mono">{w.model}</span> <span className="chip mono" title="The model's licence">{w.licence}</span>
        {" "}Programs call it at <code>POST {w.path}{w.endpoint}</code> with an API key.
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
      {image && (
        <div className="row-gap vis-actions">
          {w.task !== "segment" && (busy
            ? <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
            : <button type="button" className="btn btn-primary" disabled={!w.ready} onClick={() => void run()}>{RUN_LABEL[w.task]}</button>)}
          {w.task === "segment" && <span className="small" role="note">Click a point on the picture, or drag a box around something.</span>}
          {busy && <span className="muted small" role="status">Waiting for the workload…</span>}
          {!w.ready && <span className="muted small">The workload has no ready replica.</span>}
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
