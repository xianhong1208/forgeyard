// Engine images on a machine (1.4, issue #35): what comes through the server by itself (a pinned image), and,
// for admins, "Upload an image": a file `docker save` wrote (optionally gzip), streamed through the server,
// which keeps none of it, into the machine's container engine. The answer says what was loaded, and which
// recipe's image each one is.
import { IconBox, IconUpload, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

import { notifyUnauthorized } from "../lib/api";
import { UPLOAD_ACCEPT, matchLine, shortId, uploadBlocked, uploadLine, type UploadResult } from "../lib/imageUpload";
import { bytes } from "../lib/machines";
import type { Machine } from "../lib/types";
import { Notice } from "./ui";

export function EngineImagesSection({ m, canManage, onChanged }: { m: Machine; canManage: boolean; onChanged: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const blocked = uploadBlocked(m);
  return (
    <section className="panel mk-section" aria-labelledby="mk-images">
      <div className="panel-head">
        <h2 id="mk-images">Engine images</h2>
        {canManage && (
          <button type="button" className="btn btn-small" disabled={blocked !== null} title={blocked ?? undefined}
            onClick={() => setOpen(true)}>
            <IconUpload size={14} aria-hidden="true" /> Upload an image
          </button>
        )}
      </div>
      <p className="small muted img-lead">
        A deployment pulls its engine image itself. A machine that cannot reach the registry gets a pinned image through
        the server instead (Internet access, above). Any other image, such as a custom recipe's image named by a tag only,
        can be uploaded here: save it on a machine that has it, for example
        <code className="mono"> docker save IMAGE | gzip &gt; image.tar.gz</code>.
      </p>
      {canManage && blocked && <p className="small muted">{blocked}</p>}
      {open && <UploadDialog m={m} onClose={() => setOpen(false)} onLoaded={onChanged} />}
    </section>
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
