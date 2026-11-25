// The Playground's Image panel. Image generation (1.3): a prompt, a size the deployment takes and how many; the images
// come back as base64 and are shown with a download link each. Requests wait in the gateway's queue while the model is
// busy, so a long wait is normal on a CPU. 1.5.5 (#143): a model that edits images has an Edit mode: 1 to N images
// (the deployment's Input images), an instruction, how many, a size (or the first image's shape) and a format, sent as
// /v1/images/edits multipart; the images given are shown beside the results. A model that only edits opens in Edit.
import { IconDownload, IconPhotoAi, IconPlayerStop, IconWand } from "@tabler/icons-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { ImagePick } from "../components/ImagePick";
import { Notice } from "../components/ui";
import { t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { notifyUnauthorized } from "../lib/api";
import { IMAGE_MIME, imageSizes, ms, type ImageOptions } from "../lib/gateway";
import { editForm, imageModes, type ImageMode, type Picked } from "../lib/imageEdit";
import { failure, post } from "../lib/playground";

const IMAGE_SAMPLE = "A lighthouse on a rocky coast at sunset, warm light, oil painting";
const EDIT_SAMPLE = "Turn it into a watercolour painting; keep everything where it is.";
const SAMPLES: Record<ImageMode, string> = { generate: IMAGE_SAMPLE, edit: EDIT_SAMPLE };

type Made = { url: string; name: string };
type Result = { images: Made[]; meta: string; inputs: Picked[]; edit: boolean };

function b64Blob(b64: string, mime: string): Blob {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

export function Imagine({ model }: { model: string }) {
  const t = useT();
  const [prompt, setPrompt] = useState(IMAGE_SAMPLE);
  const [opts, setOpts] = useState<ImageOptions | null>(null);
  const [optsErr, setOptsErr] = useState<string | null>(null);
  const [chosen, setChosen] = useState<ImageMode | null>(null);
  const [size, setSize] = useState("");
  const [editSize, setEditSize] = useState("");
  const [format, setFormat] = useState("");
  const [n, setN] = useState(1);
  const [inputs, setInputs] = useState<Picked[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [made, setMade] = useState<Result | null>(null);
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
        setFormat(data.edit_formats?.[0] ?? "");
        // A model that only edits opens in Edit, with an instruction for it.
        if (imageModes(data)[0] === "edit") setPrompt(EDIT_SAMPLE);
      } catch {
        if (live) setOptsErr(tNow("playground.unreachable"));
      }
    })();
    return () => { live = false; };
  }, [model]);

  useEffect(() => () => { made?.images.forEach((i) => URL.revokeObjectURL(i.url)); }, [made]);

  const modes = imageModes(opts);
  const mode: ImageMode = chosen && modes.includes(chosen) ? chosen : modes[0];
  const editing = mode === "edit";
  const most = opts?.edit ?? 0;
  const sizes = opts ? imageSizes(opts) : [];
  const counts = Array.from({ length: Math.min(opts?.max_n ?? 1, 8) }, (_, i) => i + 1);
  const cap = opts?.max_prompt_chars;
  const chars = prompt.length;

  const switchTo = (m: ImageMode) => {
    if (m === mode) return;
    // The sample instruction follows the mode; anything the person wrote stays.
    if (prompt === SAMPLES[mode]) setPrompt(SAMPLES[m]);
    setChosen(m);
    setErr(null);
  };

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || busy || (editing && !inputs.length)) return;
    setBusy(true); setErr(null);
    const ctrl = new AbortController();
    abort.current = ctrl;
    const started = performance.now();
    const given = editing ? inputs : [];
    try {
      const res = editing
        ? await post("images/edits", null, ctrl.signal, editForm(model, prompt, n, editSize, format, given))
        : await post("images/generations", { model, prompt, n, size: size || undefined }, ctrl.signal);
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      const fmt: string = data.output_format ?? "png";
      const mime = IMAGE_MIME[fmt] ?? "image/png";
      const stem = editing ? `${model}-edit` : model;
      const images: Made[] = (data.data ?? []).filter((d: { b64_json?: string }) => d.b64_json).map((d: { b64_json: string }, i: number) => ({
        url: URL.createObjectURL(b64Blob(d.b64_json, mime)), name: `${stem}-${i + 1}.${fmt === "jpeg" ? "jpg" : fmt}`,
      }));
      const replica = res.headers.get("X-Forgeyard-Replica");
      const meta = [replica ? t("playground.meta.replica", { n: replica.split("#")[1] }) : null,
        editing ? t.plural("playground.edit.fromInputs", given.length) : null, t.plural("playground.img.images", images.length),
        data.size ?? null, fmt.toUpperCase(), ms(performance.now() - started)].filter(Boolean).join(" · ");
      setMade((cur) => {
        cur?.images.forEach((i) => URL.revokeObjectURL(i.url));
        return { images, meta, inputs: given, edit: editing };
      });
    } catch (e2) {
      if ((e2 as Error).name !== "AbortError") setErr(t("playground.unreachable"));
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  const ready = !!opts && !!prompt.trim() && !(cap !== undefined && chars > cap) && (!editing || inputs.length > 0);
  return (
    <section className="panel play" aria-labelledby="img-title">
      <div className="panel-head">
        <h2 id="img-title">{t("playground.img.title")}</h2>
        {most > 0 && <HelpLink page="how-to" anchor="edit-an-image">{t("playground.edit.howTo")}</HelpLink>}
      </div>
      {optsErr && <Notice tone="error">{optsErr}</Notice>}
      {modes.length > 1 && (
        <div className="segmented img-modes" role="group" aria-label={t("playground.edit.modes")}>
          {modes.map((m) => (
            <button key={m} type="button" className={mode === m ? "on" : ""} aria-pressed={mode === m} disabled={busy} onClick={() => switchTo(m)}>
              {t(`playground.edit.mode.${m}`)}
            </button>
          ))}
        </div>
      )}
      {editing && modes.length === 1 && <p className="muted small img-lead">{t("playground.edit.onlyEdits")}</p>}
      <form onSubmit={go} className="img-form">
        {editing && (
          <ImagePick id="img-inputs" label={t("playground.edit.inputs", { count: String(inputs.length), max: String(most) })} max={most}
            images={inputs} onChange={setInputs} disabled={busy}
            hint={t(most > 1 ? "playground.edit.inputsHint.other" : "playground.edit.inputsHint.one", { side: String(opts?.max_input_side ?? 0) })} />
        )}
        <div className="field">
          <label htmlFor="img-prompt">{editing ? t("playground.edit.instruction") : t("playground.prompt")}</label>
          <textarea id="img-prompt" rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          <p className={`field-hint${cap && chars > cap ? " field-error" : ""}`}>{cap ? t("playground.charsOf", { count: chars, max: cap }) : t("playground.chars", { count: chars })}</p>
        </div>
        <div className="img-options">
          <div className="field">
            <label htmlFor="img-size">{t("playground.size")}</label>
            {editing ? (
              <select id="img-size" value={editSize} onChange={(e) => setEditSize(e.target.value)} disabled={!opts}>
                <option value="">{t("playground.edit.sameShape")}</option>
                {sizes.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            ) : (
              <select id="img-size" value={size} onChange={(e) => setSize(e.target.value)} disabled={!opts}>
                {sizes.map((s) => <option key={s} value={s}>{s}{s === opts?.default_size ? t("playground.default") : ""}</option>)}
              </select>
            )}
          </div>
          <div className="field">
            <label htmlFor="img-n">{t("playground.img.count")}</label>
            <select id="img-n" value={n} onChange={(e) => setN(Number(e.target.value))} disabled={!opts}>
              {counts.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          {editing && (opts?.edit_formats?.length ?? 0) > 0 && (
            <div className="field">
              <label htmlFor="img-format">{t("playground.edit.format")}</label>
              <select id="img-format" value={format} onChange={(e) => setFormat(e.target.value)}>
                {opts?.edit_formats?.map((f) => <option key={f} value={f}>{f.toUpperCase()}</option>)}
              </select>
            </div>
          )}
        </div>
        <div className="row-gap">
          {busy ? (
            <>
              <button type="button" className="btn" onClick={() => abort.current?.abort()}><IconPlayerStop size={16} aria-hidden="true" /> {t("playground.stop")}</button>
              <span className="muted small" role="status">{editing ? t.plural("playground.edit.making", n)
                : n === 1 ? t("playground.img.making.one") : t("playground.img.making.other", { count: String(n) })}</span>
            </>
          ) : editing ? (
            <button type="submit" className="btn btn-primary" disabled={!ready}><IconWand size={16} aria-hidden="true" /> {t("playground.edit.edit")}</button>
          ) : (
            <button type="submit" className="btn btn-primary" disabled={!ready}><IconPhotoAi size={16} aria-hidden="true" /> {t("playground.generate")}</button>
          )}
          {editing && !busy && !inputs.length && opts && <span className="muted small">{t("playground.edit.needOne")}</span>}
        </div>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {made && (
        <div className="img-out" aria-label={made.edit ? t("playground.edit.made") : t("playground.img.made")}>
          <span className="msg-meta">{made.meta}</span>
          {made.inputs.length > 0 && (
            <div className="img-given">
              <span className="small muted">{t("playground.edit.given")}</span>
              <ul className="img-given-list" aria-label={t("playground.edit.given")}>
                {made.inputs.map((p, k) => <li key={k}><img src={p.uri} alt={t("playground.edit.givenAlt", { n: String(k + 1), name: p.name })} /></li>)}
              </ul>
            </div>
          )}
          <div className="img-grid">
            {made.images.map((i, k) => (
              <figure key={i.url} className="img-item">
                <img src={i.url} alt={t("playground.img.alt", { n: String(k + 1), prompt: prompt.slice(0, 80) })} />
                <figcaption>
                  <a className="btn btn-quiet btn-small" href={i.url} download={i.name}><IconDownload size={14} aria-hidden="true" /> {t("playground.download")}</a>
                </figcaption>
              </figure>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
