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

export function EngineImagesSection({ m, machines, canManage, onChanged, onError }: {
  m: Machine; machines: Machine[]; canManage: boolean; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
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
        <h2 id="mk-images">Engine images {v && v.listed && <span className="muted small">· {v.images.length}</span>}</h2>
        <span className="cache-head-side">
          {disk && <span className="cache-disk small">{disk}</span>}
          {canManage && (
            <span className="img-actions">
              <button type="button" className="btn btn-small" disabled={copyNo !== null} title={copyNo ?? undefined}
                onClick={() => setCopying(true)}>
                <IconCopy size={14} aria-hidden="true" /> Copy from another machine
              </button>
              <button type="button" className="btn btn-small" disabled={blocked !== null} title={blocked ?? undefined}
                onClick={() => setOpen(true)}>
                <IconUpload size={14} aria-hidden="true" /> Upload an image
              </button>
            </span>
          )}
        </span>
      </div>
      <p className="small muted img-lead">
        A deployment pulls its engine image itself. A machine that cannot reach the registry gets a pinned image through
        the server instead (Internet access, above). Any other image, such as a custom recipe's image named by a tag only,
        can be copied here from another machine (Copy from another machine), or uploaded: save it on a machine that has it, for example
        <code className="mono"> docker save IMAGE | gzip &gt; image.tar.gz</code>.
      </p>
      {canManage && blocked && <p className="small muted">{blocked}</p>}
      {!live && m.status === "online" && <p className="field-hint cache-note">This machine's agent cannot list its engine images: it has no container engine, or it is older than 1.4.1 (update the agent).</p>}
      {live && !v && !error && <p className="muted small">Asking {m.name} for its engine images…</p>}
      {live && error && <p className="muted small">{error}</p>}
      {v && !v.listed && <p className="field-hint cache-note">{v.note}</p>}
      {v && v.listed && (
        <>
          {v.can_remove && v.remove_note && <p className="field-hint cache-note">{v.remove_note}</p>}
          {v.images.length === 0 ? (
            <p className="empty-line">No image a recipe names is on this machine.</p>
          ) : (
            <ul className="cachelist" aria-label={`Engine images on ${m.name}`}>
              {v.images.map((i) => (
                <EngineImageRow key={i.id} i={i} m={m} canRemove={v.can_remove} asking={asking === i.id} busy={busy}
                  onAsk={() => setAsking(i.id)} onCancel={() => setAsking(null)} onRemove={() => void remove(i)} />
              ))}
            </ul>
          )}
          {v.others > 0 && (
            <p className="field-hint cache-note">
              {v.others} other {v.others === 1 ? "image is" : "images are"} in this machine's engine. No recipe names {v.others === 1 ? "it" : "them"}, so {v.others === 1 ? "it is" : "they are"} not Forgeyard's and {v.others === 1 ? "is" : "are"} not listed here.
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
  const name = i.tags.length ? i.tags.join(", ") : "(no tag)";
  return (
    <li className="cacherow">
      <span className="runlist-k" aria-hidden="true"><IconBox size={16} stroke={1.7} /></span>
      <span className="cacherow-main">
        <strong className="mono">{name}</strong>
        <small>{recipesLine(i)}</small>
        <small className="mono">{listedLine({ ...i, size: null })}</small>
      </span>
      <span className="cacherow-use">
        {i.agent && <span className="chip chip-tone-off" title="The agent runs from this image">agent's own image</span>}
        {i.used_by.map((u) => (
          userHref(u)
            ? <Link key={`${u.kind}-${u.id}`} to={userHref(u)!} className="chip chip-tone-run" title={`Used by the ${u.kind} ${u.name} on this machine`}>in use: {u.name}</Link>
            : <span key={`${u.kind}-${u.id}`} className="chip chip-tone-run" title={`Used by ${u.name} on this machine`}>in use: {u.name}</span>
        ))}
      </span>
      <span className="cacherow-size mono">{i.size === null ? "size unknown" : bytes(i.size)}</span>
      {canRemove && (
        <span className="cacherow-act">
          <button type="button" className="btn btn-danger btn-small" disabled={i.remove_note !== null || busy !== null || asking}
            title={i.remove_note ?? `Remove ${name} from ${m.name}'s engine`} aria-label={`Remove ${name}`} onClick={onAsk}>
            <IconTrash size={14} aria-hidden="true" /> Remove
          </button>
        </span>
      )}
      {asking && (
        <div className="cacherow-confirm" role="group" aria-label={`Remove ${name}?`}>
          <p className="small">{engineRemoveQuestion(m.name, i)}</p>
          <span className="cacherow-confirm-act">
            <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null} onClick={onCancel}>Cancel</button>
            <button type="button" className="btn btn-danger btn-small" disabled={busy !== null} onClick={onRemove}>
              <IconTrash size={14} aria-hidden="true" /> {busy === i.id ? "Removing…" : "Remove the image"}
            </button>
          </span>
        </div>
      )}
    </li>
  );
}

type Phase = "pick" | "sending" | "loading" | "done" | "error";

function UploadDialog({ m, onClose, onLoaded }: { m: Machine; onClose: () => void; onLoaded: (text: string) => void }) {
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
    if (busy && !window.confirm("Stop the upload? Nothing is loaded on the machine.")) return;
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
    req.onerror = () => { setPhase("error"); setError("The upload broke off: the connection to the server ended."); };
    req.onabort = () => { setPhase("pick"); setSent(0); };
    req.onload = () => {
      let body: { detail?: { message?: string; reason?: string } } & Partial<UploadResult>;
      try { body = JSON.parse(req.responseText || "{}"); } catch { body = {}; }
      if (req.status === 200 && body.images) {
        const r = body as UploadResult;
        setResult(r);
        setPhase("done");
        onLoaded(`Loaded ${r.images.flatMap((i) => i.tags).join(", ") || r.images.map((i) => shortId(i.id)).join(", ")} on ${m.name}.`);
        return;
      }
      if (req.status === 401) notifyUnauthorized(body.detail?.reason);
      setPhase("error");
      setError(body.detail?.message ?? `The upload failed (HTTP ${req.status}).`);
    };
    req.send(file);
  };

  const frac = file && file.size ? Math.min(1, sent / file.size) : 0;
  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dlg panel" role="dialog" aria-modal="true" aria-labelledby="upl-title" ref={box}>
        <div className="dlg-head">
          <h2 id="upl-title"><IconUpload size={18} aria-hidden="true" /> Upload an image to {m.name}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label="Close" onClick={close}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">
          A file <code className="mono">docker save</code> or <code className="mono">podman save</code> wrote, gzip-compressed
          or not. It goes through the server, which keeps none of it, straight into {m.name}'s container engine
          ({m.runtime_info?.container?.engine ?? "Docker or Podman"}). Up to {bytes(max)}.
        </p>
        <div className="field">
          <label htmlFor="upl-file">Image file</label>
          <input id="upl-file" type="file" accept={UPLOAD_ACCEPT} disabled={busy}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPhase("pick"); setError(null); setResult(null); }} />
          {file && <p className={`field-hint${tooLarge ? " field-error" : ""}`}>{file.name} · {bytes(file.size)}{tooLarge ? ` · larger than the limit of ${bytes(max)} (IMAGE_UPLOAD_MAX_GB)` : ""}</p>}
        </div>
        {busy && file && (
          <div className="upl-progress">
            <span className="bar bar-dl" role="progressbar" aria-valuemin={0} aria-valuemax={100}
              aria-valuenow={Math.round(frac * 100)} aria-label={`Upload of ${file.name} to ${m.name}`}>
              <span style={{ width: `${Math.round(frac * 100)}%` }} />
            </span>
            <small>{uploadLine(phase, sent, file.size, m.name)}</small>
          </div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {result && (
          <ul className="upl-result" aria-label="Loaded images">
            {result.images.map((i) => (
              <li key={i.id}>
                <IconBox size={16} aria-hidden="true" />
                <span className="upl-result-main">
                  <strong className="mono">{i.tags.length ? i.tags.join(", ") : "(no tag)"}</strong>
                  <small className="mono muted">{shortId(i.id)}{i.architecture ? ` · ${i.os || "linux"}/${i.architecture}` : ""}</small>
                  <small>{matchLine(i)}</small>
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={close}>{phase === "done" ? "Close" : "Cancel"}</button>
          {phase !== "done" && (
            <button type="button" className="btn btn-primary" disabled={!file || tooLarge || busy} onClick={start}>
              <IconUpload size={16} aria-hidden="true" /> {busy ? "Uploading…" : "Upload"}
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
    if (busy && !window.confirm("Close this? The copy goes on until it ends; its result is in the audit log.")) return;
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
      onLoaded(`Copied ${r.images.flatMap((i) => i.tags).join(", ") || r.images.map((i) => shortId(i.id)).join(", ")} from ${source.name} to ${m.name}.`);
    } catch (e) {
      setPhase("error");
      setError(errorText(e));
    }
  };

  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dlg panel" role="dialog" aria-modal="true" aria-labelledby="cpy-title" ref={box}>
        <div className="dlg-head">
          <h2 id="cpy-title"><IconCopy size={18} aria-hidden="true" /> Copy an image to {m.name}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label="Close" onClick={close}><IconX size={16} aria-hidden="true" /></button>
        </div>
        <p className="small muted dlg-lead">
          The other machine saves the image from its container engine; the server passes it on, keeping none of it,
          straight into {m.name}'s ({m.runtime_info?.container?.engine ?? "Docker or Podman"}). An image named by a tag
          only is used as that machine has it: copy only from a machine you trust.
        </p>
        <div className="field">
          <label htmlFor="cpy-from">From</label>
          <select id="cpy-from" value={from ?? ""} disabled={busy || phase === "done"}
            onChange={(e) => {
              setFrom(Number(e.target.value)); setList(null); setListError(null); setImage(null); setShowAll(false);
              setPhase("pick"); setError(null); setResult(null);
            }}>
            {sources.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </div>
        {listError && <Notice tone="error">{listError}</Notice>}
        {!listError && list === null && <p className="small muted">Asking {source?.name ?? "the machine"} for its images…</p>}
        {list !== null && list.length === 0 && <p className="small muted">{source?.name} has no images.</p>}
        {shown !== null && list !== null && list.length > 0 && shown.length === 0 && phase !== "done" && (
          <p className="small muted">No image on {source?.name} is one a recipe names.</p>
        )}
        {shown !== null && shown.length > 0 && phase !== "done" && (
          <fieldset className="dlg-machines cpy-images">
            <legend>{showAll ? `Images on ${source?.name}` : `Engine images on ${source?.name} (named by a recipe)`}</legend>
            {shown.map((i) => (
              <label key={i.id} className="dlg-target">
                <input type="radio" name="cpy-image" value={i.id} checked={image === i.id} disabled={busy}
                  onChange={() => setImage(i.id)} />
                <span className="dlg-target-main">
                  <span className="dlg-target-name"><strong className="mono">{i.tags.length ? i.tags.join(", ") : "(no tag)"}</strong></span>
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
            <span>Show all images <small className="muted">({hidden} more that no recipe names, such as other people's on a shared machine)</small></span>
          </label>
        )}
        {busy && source && (
          <div className="upl-progress" role="status">
            <span className="bar bar-dl bar-busy" aria-hidden="true"><span /></span>
            <small>Copying from {source.name}: it saves the image, the server passes it on, {m.name} loads it. A large image takes minutes.</small>
          </div>
        )}
        {error && <Notice tone="error">{error}</Notice>}
        {result && (
          <ul className="upl-result" aria-label="Loaded images">
            {result.images.map((i) => (
              <li key={i.id}>
                <IconBox size={16} aria-hidden="true" />
                <span className="upl-result-main">
                  <strong className="mono">{i.tags.length ? i.tags.join(", ") : "(no tag)"}</strong>
                  <small className="mono muted">{shortId(i.id)}{i.architecture ? ` · ${i.os || "linux"}/${i.architecture}` : ""} · {bytes(result.bytes)} from {result.source.name}</small>
                  <small>{sameLine(i, result.source.name)}</small>
                  <small>{matchLine(i)}</small>
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="dlg-foot">
          <button type="button" className="btn btn-quiet" onClick={close}>{phase === "done" ? "Close" : "Cancel"}</button>
          {phase !== "done" && (
            <button type="button" className="btn btn-primary" disabled={image === null || busy} onClick={() => void start()}>
              <IconCopy size={16} aria-hidden="true" /> {busy ? "Copying…" : "Copy"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
