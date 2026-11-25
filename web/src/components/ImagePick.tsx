// 1.5.5 (#143): images to give a model in the Playground (the images to edit, a video's start image): picked, dropped
// or pasted, shown as thumbnails in order, each removable. The files are read in the browser only to show them; they
// are sent as files.
import { IconPhotoPlus, IconX } from "@tabler/icons-react";
import { useRef, useState, type DragEvent } from "react";

import { t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { readImage } from "../lib/gateway";
import { INPUT_ACCEPT, pickProblem, type Picked } from "../lib/imageEdit";

export function ImagePick({ id, label, max, images, onChange, hint, disabled }: {
  id: string; label: string; max: number; images: Picked[]; onChange: (next: Picked[]) => void; hint?: string; disabled?: boolean;
}) {
  const t = useT();
  const [err, setErr] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const full = images.length >= max;

  const add = async (files: File[]) => {
    if (!files.length) return;
    const why = pickProblem(files, images.length, max);
    setErr(why);
    if (why) return;
    try {
      const read = await Promise.all(files.map((f) => readImage(f).then((r) => ({ name: f.name || r.name, uri: r.uri, file: f }))
        .catch(() => { throw new Error(tNow("playground.image.unreadable", { name: f.name })); })));
      onChange([...images, ...read]);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (!disabled) void add(Array.from(e.dataTransfer?.files ?? []));
  };

  return (
    <div className="field">
      <span className="label-like" id={`${id}-label`}>{label}</span>
      <div className={`vis-drop emb-drop${over ? " on" : ""}`} data-testid={`${id}-drop`} aria-labelledby={`${id}-label`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}
        onPaste={(e) => {
          const files = Array.from(e.clipboardData?.files ?? []).filter((f) => f.type.startsWith("image/"));
          if (!files.length || disabled) return;
          e.preventDefault();
          void add(files.map((f, i) => (f.name ? f : new File([f], `pasted-${i + 1}.png`, { type: f.type }))));
        }}>
        <input ref={picker} id={`${id}-file`} type="file" accept={INPUT_ACCEPT} multiple={max > 1} className="sr-only"
          onChange={(e) => { const f = Array.from(e.target.files ?? []); e.target.value = ""; void add(f); }} />
        <IconPhotoPlus size={22} stroke={1.6} aria-hidden="true" />
        <span>{t(max > 1 ? "playground.edit.dropMany" : "playground.edit.dropOne")}{" "}
          <button type="button" className="btn btn-small" disabled={disabled || full} onClick={() => picker.current?.click()}>{t("playground.edit.choose")}</button>
        </span>
        <label htmlFor={`${id}-file`} className="sr-only">{t("playground.edit.chooseLabel", { what: label })}</label>
      </div>
      {hint && <p className="field-hint">{hint}</p>}
      {images.length > 0 && (
        <ul className="emb-pics" aria-label={t("playground.edit.chosen", { what: label })}>
          {images.map((p, i) => (
            <li key={`${i}-${p.name}`}>
              <img className="emb-thumb" src={p.uri} alt="" />
              <span className="mono small emb-pic-name" title={p.name}>{max > 1 ? `${i + 1}. ${p.name}` : p.name}</span>
              <button type="button" className="emb-x" aria-label={t("playground.edit.remove", { name: p.name })} disabled={disabled}
                onClick={() => { setErr(null); onChange(images.filter((_, j) => j !== i)); }}><IconX size={14} aria-hidden="true" /></button>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="field-error" role="alert">{err}</p>}
    </div>
  );
}
