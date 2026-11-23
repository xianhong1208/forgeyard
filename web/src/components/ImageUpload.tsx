// Engine images on a machine (1.4, issue #35): what comes through the server by itself (a pinned image), and,
// for admins, "Upload an image": a file `docker save` wrote (optionally gzip), streamed through the server,
// which keeps none of it, into the machine's container engine. The answer says what was loaded, and which
// recipe's image each one is. 1.4.1 (#104): "Copy from another machine": an image another machine's engine has,
// saved there and passed on by the server the same way. 1.4.2 (#123): the images a recipe names, listed with their
// size and the deployments here that use them, and Remove (admins) for one nothing uses; (#124) the copy dialog
// lists those first, and only those until "Show all images".
import { IconBox, IconCopy, IconTrash, IconUpload, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { api, errorText, notifyUnauthorized } from "../lib/api";
import { userHref } from "../lib/cache";
import {
  UPLOAD_ACCEPT, copyBlocked, copyChoices, copySources, engineDiskLine, engineRemoveQuestion, listedLine, matchLine,
  recipesLine, removedLine, sameLine, shortId, uploadBlocked, uploadLine,
  type CopyResult, type EngineImage, type EngineImagesView, type ImageRemoved, type ListedImage, type UploadResult,
} from "../lib/imageUpload";
import { bytes } from "../lib/machines";
import type { Machine } from "../lib/types";
import { useApi } from "../lib/useApi";
import { Notice } from "./ui";
import { useT } from "../i18n/react";

export function EngineImagesSection({ m, machines, canManage, onChanged, onError }: {
  m: Machine; machines: Machine[]; canManage: boolean; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [copying, setCopying] = useState(false);
  const live = m.status === "online" && (m.features ?? []).includes("image_save");
  const { data, error, reload } = useApi<EngineImagesView>(live ? `/machines/${m.id}/engine-images` : null);
  const [asking, setAsking] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const blocked = uploadBlocked(m);
  const copyNo = copyBlocked(m, machines);
  const loaded = (text: string) => { onChanged(text); reload(); };
  const v = live ? data : null;
  // 1.4.3 (#140): an engine's page links here ("Copy or upload") as /machines/<id>#engine-images.
  const here = useRef<HTMLElement>(null);
  useEffect(() => {
    if (window.location.hash === "#engine-images") here.current?.scrollIntoView?.({ block: "start" });
  }, []);
  const disk = v ? engineDiskLine(v) : null;

  const remove = async (i: EngineImage) => {
    setBusy(i.id);
    try {
      const r = await api<ImageRemoved>(`/machines/${m.id}/engine-images/remove`, { method: "POST", body: { image: i.id } });
      onChanged(removedLine(m.name, r));
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
      setAsking(null);
      reload();
    }
  };

  return (
    <section className="panel mk-section" aria-labelledby="mk-images" id="engine-images" ref={here}>
      <div className="panel-head">
        <h2 id="mk-images">{t("machine.img.title")} {v && v.listed && <span className="muted small">· {v.images.length}</span>}</h2>
        <span className="cache-head-side">
          {disk && <span className="cache-disk small">{disk}</span>}
          {canManage && (
            <span className="img-actions">
              <button type="button" className="btn btn-small" disabled={copyNo !== null} title={copyNo ?? undefined}
                onClick={() => setCopying(true)}>
                <IconCopy size={14} aria-hidden="true" /> {t("machine.img.copyButton")}
              </button>
              <button type="button" className="btn btn-small" disabled={blocked !== null} title={blocked ?? undefined}
                onClick={() => setOpen(true)}>
                <IconUpload size={14} aria-hidden="true" /> {t("machine.img.uploadButton")}
              </button>
            </span>
          )}
        </span>
      </div>
      <p className="small muted img-lead">
        {t.rich("machine.img.lead", { command: <code className="mono"> docker save IMAGE | gzip &gt; image.tar.gz</code> })}
      </p>
      {canManage && blocked && <p className="small muted">{blocked}</p>}
      {!live && m.status === "online" && <p className="field-hint cache-note">{t("machine.img.cannotList")}</p>}
      {live && !v && !error && <p className="muted small">{t("machine.img.asking", { name: m.name })}</p>}
      {live && error && <p className="muted small">{error}</p>}
      {v && !v.listed && <p className="field-hint cache-note">{v.note}</p>}
      {v && v.listed && (
        <>
          {v.can_remove && v.remove_note && <p className="field-hint cache-note">{v.remove_note}</p>}
          {v.images.length === 0 ? (
            <p className="empty-line">{t("machine.img.none")}</p>
          ) : (
            <ul className="cachelist" aria-label={t("machine.img.listLabel", { name: m.name })}>
              {v.images.map((i) => (
                <EngineImageRow key={i.id} i={i} m={m} canRemove={v.can_remove} asking={asking === i.id} busy={busy}
                  onAsk={() => setAsking(i.id)} onCancel={() => setAsking(null)} onRemove={() => void remove(i)} />
              ))}
            </ul>
          )}
          {v.others > 0 && (
            <p className="field-hint cache-note">
              {t.plural("machine.img.others", v.others)}
            </p>
          )}
        </>
      )}
      {open && <UploadDialog m={m} onClose={() => setOpen(false)} onLoaded={loaded} />}
      {copying && <CopyDialog m={m} sources={copySources(m, machines)} onClose={() => setCopying(false)} onLoaded={loaded} />}
    </section>
  );
}

function EngineImageRow({ i, m, canRemove, asking, busy, onAsk, onCancel, onRemove }: {
  i: EngineImage; m: Machine; canRemove: boolean; asking: boolean; busy: string | null;
  onAsk: () => void; onCancel: () => void; onRemove: () => void;
}) {
  const t = useT();
  const name = i.tags.length ? i.tags.join(", ") : t("machine.img.noTag");
  return (
    <li className="cacherow">
      <span className="runlist-k" aria-hidden="true"><IconBox size={16} stroke={1.7} /></span>
      <span className="cacherow-main">
        <strong className="mono">{name}</strong>
        <small>{recipesLine(i)}</small>
        <small className="mono">{listedLine({ ...i, size: null })}</small>
      </span>
      <span className="cacherow-use">
        {i.agent && <span className="chip chip-tone-off" title={t("machine.img.agentTitle")}>{t("machine.img.agent")}</span>}
        {i.used_by.map((u) => (
          userHref(u)
            ? <Link key={`${u.kind}-${u.id}`} to={userHref(u)!} className="chip chip-tone-run" title={t("machine.usedByDeployment", { name: u.name })}>{t("machine.inUse", { name: u.name })}</Link>
            : <span key={`${u.kind}-${u.id}`} className="chip chip-tone-run" title={t("machine.usedBy", { name: u.name })}>{t("machine.inUse", { name: u.name })}</span>
        ))}
      </span>
      <span className="cacherow-size mono">{i.size === null ? t("machine.sizeUnknown") : bytes(i.size)}</span>
      {canRemove && (
        <span className="cacherow-act">
          <button type="button" className="btn btn-danger btn-small" disabled={i.remove_note !== null || busy !== null || asking}
            title={i.remove_note ?? t("machine.img.removeTitle", { name, machine: m.name })} aria-label={t("machine.removeNamed", { name })} onClick={onAsk}>
            <IconTrash size={14} aria-hidden="true" /> {t("machine.remove")}
          </button>
        </span>
      )}
      {asking && (
        <div className="cacherow-confirm" role="group" aria-label={t("machine.img.removeAsk", { name })}>
          <p className="small">{engineRemoveQuestion(m.name, i)}</p>
          <span className="cacherow-confirm-act">
            <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={onCancel}>{t("common.cancel")}</button>
            <button type="button" className="btn btn-danger btn-small" disabled={busy !== null} onClick={onRemove}>
              <IconTrash size={14} aria-hidden="true" /> {busy === i.id ? t("machine.removing") : t("machine.img.removeImage")}
            </button>
          </span>
        </div>
      )}
    </li>
  );
}

type Phase = "pick" | "sending" | "loading" | "done" | "error";

function UploadDialog({ m, onClose, onLoaded }: { m: Machine; onClose: () => void; onLoaded: (text: string) => void }) {
  const t = useT();
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("pick");
  const [sent, setSent] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const xhr = useRef<XMLHttpRequest | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const max = m.image_upload_max_bytes ?? 0;
  const tooLarge = file !== null && max > 0 && file.size > max;
  const busy = phase === "sending" || phase === "loading";

  const close = () => {
    if (busy && !window.confirm(t("machine.img.stopUpload"))) return;
    xhr.current?.abort();
    onClose();
  };
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("input, button")?.focus();
  }, []);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  });

  const start = () => {
    if (!file || tooLarge) return;
    const req = new XMLHttpRequest();
    xhr.current = req;
    setPhase("sending");
    setSent(0);
    setError(null);
    setResult(null);
    req.open("POST", `/api/machines/${m.id}/images`);
    req.setRequestHeader("X-Forgeyard", "1");
    req.setRequestHeader("Content-Type", "application/octet-stream");
    req.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    req.upload.onprogress = (e) => setSent(e.loaded);
    req.upload.onload = () => { setSent(file.size); setPhase("loading"); };
    req.onerror = () => { setPhase("error"); setError(t("machine.img.brokeOff")); };
    req.onabort = () => { setPhase("pick"); setSent(0); };
    req.onload = () => {
      let body: { detail?: { message?: string; reason?: string } } & Partial<UploadResult>;
      try { body = JSON.parse(req.responseText || "{}"); } catch { body = {}; }
      if (req.status === 200 && body.images) {
        const r = body as UploadResult;
        setResult(r);
        setPhase("done");
        onLoaded(t("machine.img.loaded", { images: r.images.flatMap((i) => i.tags).join(", ") || r.images.map((i) => shortId(i.id)).join(", "), name: m.name }));
        return;
      }
      if (req.status === 401) notifyUnauthorized(body.detail?.reason);
      setPhase("error");
      setError(body.detail?.message ?? t("machine.img.failed", { status: String(req.status) }));
    };
    req.send(file);
  };

  const frac = file && file.size ? Math.min(1, sent / file.size) : 0;
  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dlg panel" role="dialog" aria-modal="true" aria-labelledby="upl-title" ref={box}>
        <div className="dlg-head">
          <h2 id="upl-title"><IconUpload size={18} aria-hidden="true" /> {t("machine.img.uploadTitle", { name: m.name })}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label={t("common.close")} onClick={close}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">
          {t.rich("machine.img.uploadLead", { docker: <code className="mono">docker save</code>, podman: <code className="mono">podman save</code>,
            name: m.name, engine: m.runtime_info?.container?.engine ?? t("machine.img.dockerOrPodman"), max: bytes(max) })}
        </p>
        <div className="field">
          <label htmlFor="upl-file">{t("machine.img.file")}</label>
          <input id="upl-file" type="file" accept={UPLOAD_ACCEPT} disabled={busy}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPhase("pick"); setError(null); setResult(null); }} />
          {file && <p className={`field-hint${tooLarge ? " field-error" : ""}`}>{file.name} · {bytes(file.size)}{tooLarge ? t("machine.img.tooLarge", { max: bytes(max) }) : ""}</p>}
        </div>
        {busy && file && (
          <div className="upl-progress">
            <span className="bar bar-dl" role="progressbar" aria-valuemin={0} aria-valuemax={100}
              aria-valuenow={Math.round(frac * 100)} aria-label={t("machine.img.uploadLabel", { file: file.name, name: m.name })}>
              <span style={{ width: `${Math.round(frac * 100)}%` }} />
            </span>
            <small>{uploadLine(phase, sent, file.size, m.name)}</small>
          </div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {result && (
          <ul className="upl-result" aria-label={t("machine.img.loadedList")}>
            {result.images.map((i) => (
              <li key={i.id}>
                <IconBox size={16} aria-hidden="true" />
                <span className="upl-result-main">
                  <strong className="mono">{i.tags.length ? i.tags.join(", ") : t("machine.img.noTag")}</strong>
                  <small className="mono muted">{shortId(i.id)}{i.architecture ? ` · ${i.os || "linux"}/${i.architecture}` : ""}</small>
                  <small>{matchLine(i)}</small>
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={close}>{phase === "done" ? t("common.close") : t("common.cancel")}</button>
          {phase !== "done" && (
            <button type="button" className="btn btn-primary" disabled={!file || tooLarge || busy} onClick={start}>
              <IconUpload size={16} aria-hidden="true" /> {busy ? t("machine.img.uploading") : t("machine.img.upload")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

type CopyPhase = "pick" | "copying" | "done" | "error";

// 1.4.1 (#104): an image another machine's engine has, saved there and passed on by the server, loaded here.
function CopyDialog({ m, sources, onClose, onLoaded }: {
  m: Machine; sources: Machine[]; onClose: () => void; onLoaded: (text: string) => void;
}) {
  const t = useT();
  const [from, setFrom] = useState<number | null>(sources[0]?.id ?? null);
  const [list, setList] = useState<ListedImage[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [image, setImage] = useState<string | null>(null);
  const [phase, setPhase] = useState<CopyPhase>("pick");
  const [result, setResult] = useState<CopyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const source = sources.find((x) => x.id === from) ?? null;
  const busy = phase === "copying";
  const shown = list === null ? null : copyChoices(list, showAll);
  const hidden = list === null ? 0 : list.length - copyChoices(list, false).length;

  useEffect(() => {
    box.current?.querySelector<HTMLElement>("select, button")?.focus();
  }, []);
  useEffect(() => {
    if (from === null) return;
    let live = true;
    api<{ images: ListedImage[] }>(`/machines/${from}/images`)
      .then((r) => { if (live) { setList(r.images); setImage(copyChoices(r.images, false)[0]?.id ?? null); } })
      .catch((e) => { if (live) setListError(errorText(e)); });
    return () => { live = false; };
  }, [from]);
  const close = () => {
    if (busy && !window.confirm(t("machine.img.closeCopy"))) return;
    onClose();
  };
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  });

  const start = async () => {
    if (from === null || image === null || !source) return;
    setPhase("copying");
    setError(null);
    setResult(null);
    try {
      const r = await api<CopyResult>(`/machines/${m.id}/images/copy`, { method: "POST", body: { source_id: from, image } });
      setResult(r);
      setPhase("done");
      onLoaded(t("machine.img.copied", { images: r.images.flatMap((i) => i.tags).join(", ") || r.images.map((i) => shortId(i.id)).join(", "),
        source: source.name, name: m.name }));
    } catch (e) {
      setPhase("error");
      setError(errorText(e));
    }
  };

  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dlg panel" role="dialog" aria-modal="true" aria-labelledby="cpy-title" ref={box}>
        <div className="dlg-head">
          <h2 id="cpy-title"><IconCopy size={18} aria-hidden="true" /> {t("machine.img.copyTitle", { name: m.name })}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label={t("common.close")} onClick={close}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">
          {t("machine.img.copyLead", { name: m.name, engine: m.runtime_info?.container?.engine ?? t("machine.img.dockerOrPodman") })}
        </p>
        <div className="field">
          <label htmlFor="cpy-from">{t("machine.img.from")}</label>
          <select id="cpy-from" value={from ?? ""} disabled={busy || phase === "done"}
            onChange={(e) => {
              setFrom(Number(e.target.value)); setList(null); setListError(null); setImage(null); setShowAll(false);
              setPhase("pick"); setError(null); setResult(null);
            }}>
            {sources.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </div>
        {listError && <Notice tone="error">{listError}</Notice>}
        {!listError && list === null && <p className="small muted">{t("machine.img.askingImages", { name: source?.name ?? t("machine.dl.theMachine") })}</p>}
        {list !== null && list.length === 0 && <p className="small muted">{t("machine.img.noImages", { name: source?.name ?? "" })}</p>}
        {shown !== null && list !== null && list.length > 0 && shown.length === 0 && phase !== "done" && (
          <p className="small muted">{t("machine.img.noneNamed", { name: source?.name ?? "" })}</p>
        )}
        {shown !== null && shown.length > 0 && phase !== "done" && (
          <fieldset className="dlg-machines cpy-images">
            <legend>{showAll ? t("machine.img.imagesOn", { name: source?.name ?? "" }) : t("machine.img.engineImagesOn", { name: source?.name ?? "" })}</legend>
            {shown.map((i) => (
              <label key={i.id} className="dlg-target">
                <input type="radio" name="cpy-image" value={i.id} checked={image === i.id} disabled={busy}
                  onChange={() => setImage(i.id)} />
                <span className="dlg-target-main">
                  <span className="dlg-target-name"><strong className="mono">{i.tags.length ? i.tags.join(", ") : t("machine.img.noTag")}</strong></span>
                  <small className="mono">{listedLine(i)}</small>
                  <small>{matchLine({ ...i, tags: i.tags })}</small>
                </span>
              </label>
            ))}
          </fieldset>
        )}
        {list !== null && hidden > 0 && phase !== "done" && (
          <label className="check cpy-all">
            <input type="checkbox" checked={showAll} disabled={busy} onChange={(e) => {
              const all = e.target.checked;
              setShowAll(all);
              if (!all && image !== null && !copyChoices(list, false).some((i) => i.id === image)) {
                setImage(copyChoices(list, false)[0]?.id ?? null);
              }
            }} />
            <span>{t("machine.img.showAll")} <small className="muted">{t("machine.img.showAllMore", { count: hidden })}</small></span>
          </label>
        )}
        {busy && source && (
          <div className="upl-progress" role="status">
            <span className="bar bar-dl bar-busy" aria-hidden="true"><span /></span>
            <small>{t("machine.img.copying", { source: source.name, name: m.name })}</small>
          </div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {result && (
          <ul className="upl-result" aria-label={t("machine.img.loadedList")}>
            {result.images.map((i) => (
              <li key={i.id}>
                <IconBox size={16} aria-hidden="true" />
                <span className="upl-result-main">
                  <strong className="mono">{i.tags.length ? i.tags.join(", ") : t("machine.img.noTag")}</strong>
                  <small className="mono muted">{shortId(i.id)}{i.architecture ? ` · ${i.os || "linux"}/${i.architecture}` : ""} {t("machine.img.copiedSize", { size: bytes(result.bytes), source: result.source.name })}</small>
                  <small>{sameLine(i, result.source.name)}</small>
                  <small>{matchLine(i)}</small>
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={close}>{phase === "done" ? t("common.close") : t("common.cancel")}</button>
          {phase !== "done" && (
            <button type="button" className="btn btn-primary" disabled={image === null || busy} onClick={() => void start()}>
              <IconCopy size={16} aria-hidden="true" /> {busy ? t("machine.img.copyingShort") : t("machine.img.copy")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
