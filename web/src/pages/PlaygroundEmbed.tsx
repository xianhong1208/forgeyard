// The Playground's Embeddings panel. 1.5.4 (#141): a model that embeds images (a CLIP-like model on the transformers
// engine: texts and images in one space; DINOv2: images only) takes images too, picked, dropped, pasted or named
// by an address the gateway fetches; the answer is a similarity table (cosine) between the texts and the images,
// or between every item when there is one kind only: the quickest way to see text -> image search work.
import { IconLink, IconPhotoPlus, IconX } from "@tabler/icons-react";
import { useRef, useState, type ClipboardEvent, type DragEvent, type FormEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { Notice } from "../components/ui";
import { t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import {
  embedSummary, embedsImages, embedsText, imageUrl, itemLabel, norm, requestInput, shade, similarityGrid, urlName,
  type EmbedItem, type Grid,
} from "../lib/encoders";
import { ms, readImage } from "../lib/gateway";
import { failure, imageProblem, now, post } from "../lib/playground";
import type { EmbedModel } from "../lib/types";

const TEXT_SAMPLE = "Forgeyard runs models on your own machines.\nYour models run on hardware you own.\nThe cat sleeps on the sofa.";
const CLIP_SAMPLE = "a photo of cats on a sofa\na busy city street\na red square on a grey background";
const DEFAULT_MAX_IMAGES = 32;   // the transformers recipe's max_images

type Answer = { items: EmbedItem[]; vectors: number[][]; took: number; tokens: number | null; images: number | null };
type Image = Extract<EmbedItem, { kind: "image" }>;

export function Embeddings({ model, embed, maxImages }: { model: string; embed: EmbedModel | null; maxImages: number | null }) {
  const t = useT();
  const texts = embedsText(embed);
  const images = embedsImages(embed);
  const clip = texts && images;
  const cap = maxImages ?? DEFAULT_MAX_IMAGES;
  const [input, setInput] = useState(clip ? CLIP_SAMPLE : TEXT_SAMPLE);
  const [pics, setPics] = useState<Image[]>([]);
  const [address, setAddress] = useState("");
  const [pickErr, setPickErr] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [out, setOut] = useState<Answer | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const lines = texts ? input.split("\n").map((x) => x.trim()).filter(Boolean) : [];
  const items: EmbedItem[] = [...lines.map((text) => ({ kind: "text" as const, text })), ...pics];

  const addFiles = async (files: File[]) => {
    setPickErr(null);
    const room = cap - pics.length;
    if (files.length > room) setPickErr(t("playground.emb.capped", { max: cap }));
    try {
      const why = files.slice(0, Math.max(0, room)).map(imageProblem).find(Boolean);
      if (why) throw new Error(why);
      const read = await Promise.all(files.slice(0, Math.max(0, room)).map((f) => readImage(f)
        .catch(() => { throw new Error(tNow("playground.image.unreadable", { name: f.name })); })));
      setPics((cur) => [...cur, ...read.map((r) => ({ kind: "image" as const, name: r.name || "image", uri: r.uri }))].slice(0, cap));
    } catch (e) {
      setPickErr((e as Error).message);
    }
  };

  const addAddress = () => {
    const url = imageUrl(address);
    if (!url) { setPickErr(t("playground.emb.badUrl")); return; }
    if (pics.length >= cap) { setPickErr(t("playground.emb.capped", { max: cap })); return; }
    setPickErr(null);
    setPics((cur) => [...cur, { kind: "image", name: urlName(url), uri: url, url: true }]);
    setAddress("");
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    void addFiles(Array.from(e.dataTransfer?.files ?? []));
  };

  // A pasted image (a screenshot copied to the clipboard); pasted text goes where the cursor is, as usual.
  const onPaste = (e: ClipboardEvent) => {
    if (!images) return;
    const files = Array.from(e.clipboardData?.files ?? []).filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    void addFiles(files.map((f, i) => (f.name ? f : new File([f], `pasted-${i + 1}.png`, { type: f.type }))));
  };

  const go = async (e: FormEvent) => {
    e.preventDefault();
    if (!items.length) return;
    setBusy(true); setErr(null); setOut(null);
    const started = now();
    const sent = items;
    try {
      const res = await post("embeddings", { model, input: requestInput(sent) });
      if (!res.ok) { setErr(await failure(res)); return; }
      const data = await res.json();
      const rows = [...(data.data ?? [])].sort((a: { index: number }, b: { index: number }) => a.index - b.index);
      setOut({ items: sent, vectors: rows.map((d: { embedding: number[] }) => d.embedding ?? []), took: now() - started,
        tokens: data.usage?.prompt_tokens ?? null, images: data.usage?.images ?? null });
    } catch { setErr(t("playground.unreachable")); } finally { setBusy(false); }
  };

  return (
    <section className="panel play" aria-labelledby="emb-title" onPaste={onPaste}>
      <div className="panel-head">
        <h2 id="emb-title">{t("playground.emb.title")}</h2>
        {images && <HelpLink page="how-to" anchor="search-images-by-text-image-embeddings">{t("playground.emb.howTo")}</HelpLink>}
      </div>
      <p className="muted small">
        {t("playground.emb.about", { summary: embedSummary(embed),
          about: clip ? t("playground.emb.aboutBoth") : images ? t("playground.emb.aboutImages") : t("playground.emb.aboutTexts") })}
      </p>
      <form onSubmit={go}>
        {texts && (
          <div className="field">
            <label htmlFor="emb-input">{t("playground.emb.texts")}</label>
            <textarea id="emb-input" rows={3} value={input} onChange={(e) => setInput(e.target.value)} />
          </div>
        )}
        {images && (
          <div className="field">
            <span className="label-like" id="emb-images-label">{t("playground.emb.images", { count: pics.length, max: cap })}</span>
            <div className={`vis-drop emb-drop${dragOver ? " on" : ""}`} data-testid="emb-drop" aria-labelledby="emb-images-label"
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={() => setDragOver(false)} onDrop={onDrop}>
              <input ref={picker} id="emb-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" multiple className="sr-only"
                onChange={(e) => { const f = Array.from(e.target.files ?? []); e.target.value = ""; void addFiles(f); }} />
              <IconPhotoPlus size={22} stroke={1.6} aria-hidden="true" />
              <span>{t("playground.emb.drop")}{" "}
                <button type="button" className="btn btn-small" disabled={pics.length >= cap} onClick={() => picker.current?.click()}>{t("playground.emb.choose")}</button>
              </span>
              <label htmlFor="emb-file" className="sr-only">{t("playground.emb.chooseLabel")}</label>
            </div>
            <div className="emb-url">
              <label htmlFor="emb-url" className="sr-only">{t("playground.emb.urlLabel")}</label>
              <input id="emb-url" type="url" inputMode="url" value={address} placeholder={t("playground.emb.urlPlaceholder")} autoComplete="off"
                onChange={(e) => setAddress(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addAddress(); } }} />
              <button type="button" className="btn btn-small" disabled={!address.trim() || pics.length >= cap} onClick={addAddress}>
                <IconLink size={14} aria-hidden="true" /> {t("playground.emb.addUrl")}
              </button>
            </div>
            <p className="field-hint">{t("playground.emb.urlHint")}</p>
            {pics.length > 0 && (
              <ul className="emb-pics" aria-label={t("playground.emb.chosen")}>
                {pics.map((p, i) => (
                  <li key={`${i}-${p.name}`}>
                    <Thumb p={p} />
                    <span className="mono small emb-pic-name" title={p.url ? p.uri : p.name}>{itemLabel(items, lines.length + i, 22)}</span>
                    <button type="button" className="emb-x" aria-label={t("playground.emb.remove", { name: p.name })}
                      onClick={() => setPics((cur) => cur.filter((_, j) => j !== i))}><IconX size={14} aria-hidden="true" /></button>
                  </li>
                ))}
              </ul>
            )}
            {pickErr && <p className="field-error" role="alert">{pickErr}</p>}
          </div>
        )}
        {!texts && <p className="small muted" role="note">{t("playground.emb.noTexts")}</p>}
        {texts && !images && embed && <p className="small muted" role="note">{t("playground.emb.noImages")}</p>}
        <button type="submit" className="btn btn-primary" disabled={busy || !items.length} style={{ marginTop: 10 }}>
          {busy ? t("playground.waiting") : t("playground.emb.embed")}
        </button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && <Result a={out} />}
    </section>
  );
}

function Thumb({ p }: { p: Image }) {
  // An image by address is not loaded here: the gateway fetches it under the server's rules, not the browser.
  if (p.url) return <span className="emb-thumb emb-thumb-url" aria-hidden="true"><IconLink size={16} /></span>;
  return <img className="emb-thumb" src={p.uri} alt="" />;
}

function Result({ a }: { a: Answer }) {
  const t = useT();
  const dims = a.vectors[0]?.length ?? 0;
  const length = a.vectors[0] ? norm(a.vectors[0]) : 0;
  const grid = similarityGrid(a.items, a.vectors);
  const line = [
    t.plural("playground.emb.vectors", a.vectors.length, { dims }),
    ms(a.took),
    a.tokens ? t.plural("playground.emb.tokens", a.tokens) : null,
    a.images ? t.plural("playground.emb.imagesUsed", a.images) : null,
    Math.abs(length - 1) < 1e-3 ? t("playground.emb.normalised") : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className="emb-result" aria-label={t("playground.emb.answer")}>
      <p className="small emb-line" role="status">{line}</p>
      {grid && <SimilarityTable items={a.items} g={grid} />}
      <details className="emb-values">
        <summary className="small">{t("playground.emb.values")}</summary>
        <pre className="logs try-answer play-out">{a.vectors.map((v, i) => `${itemLabel(a.items, i, 40)}\n[${v.slice(0, 8).map((x) => x.toFixed(4)).join(", ")}, …]`).join("\n")}</pre>
      </details>
    </div>
  );
}

function Head({ items, i }: { items: EmbedItem[]; i: number }) {
  const x = items[i];
  const label = itemLabel(items, i, 40);
  if (x.kind === "image") {
    return <span className="emb-head-img" title={x.url ? x.uri : x.name}><Thumb p={x} /><span className="mono">{label.split(" ")[0]}</span></span>;
  }
  return <span className="emb-head-text" title={x.text}>{label}</span>;
}

function SimilarityTable({ items, g }: { items: EmbedItem[]; g: Grid }) {
  const t = useT();
  const cross = items[g.rows[0]].kind !== items[g.cols[0]].kind;
  return (
    <>
      <div className="emb-sim-wrap">
        <table className="emb-sim" aria-label={t("playground.emb.table")}>
          <thead>
            <tr><th scope="col"><span className="sr-only">{t("playground.emb.item")}</span></th>
              {g.cols.map((c) => <th key={c} scope="col"><Head items={items} i={c} /></th>)}</tr>
          </thead>
          <tbody>
            {g.rows.map((r, i) => (
              <tr key={r}>
                <th scope="row"><Head items={items} i={r} /></th>
                {g.cells[i].map((v, j) => (
                  <td key={g.cols[j]} className={`mono${g.best[i] === j ? " emb-best" : ""}${v === null ? " emb-self" : ""}`}
                    style={{ ["--s" as string]: shade(v, g).toFixed(3) }}
                    aria-label={v === null ? t("playground.emb.self") : g.best[i] === j ? t("playground.emb.bestCell", { value: v.toFixed(3) }) : undefined}>
                    {v === null ? "—" : v.toFixed(3)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">{cross ? t("playground.emb.tableCross") : t("playground.emb.tableSame")}</p>
    </>
  );
}
