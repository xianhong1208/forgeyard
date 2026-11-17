// Download a backup of the secret key and the CA (1.4.2, issue #127). Admins only: the admin types their
// password again and chooses a passphrase; the server answers the file sealed with it. Neither is kept.
import { IconDownload, IconKey, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError, errorText, notifyUnauthorized, type ErrorDetail } from "../lib/api";
import { BACKUP_PASSPHRASE_MIN, backupFilename, passphraseProblem } from "../lib/keyBackup";
import type { SecretKeyInfo } from "../lib/types";
import { useApi } from "../lib/useApi";
import { CopyBox, Notice } from "./ui";

export type KeyBackupDone = { kid: string; file: string };

// POST the form, then hand the answer to the browser as a file. Not api(): the answer is a file,
// and a 401 here is a wrong password (code wrong_password) unless the session really ended.
export async function downloadKeyBackup(password: string, passphrase: string, confirm: string): Promise<KeyBackupDone> {
  let res: Response;
  try {
    res = await fetch("/api/secret-key/backup", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Forgeyard": "1", "Content-Type": "application/json", Accept: "application/octet-stream" },
      body: JSON.stringify({ password, passphrase, passphrase_confirm: confirm }),
    });
  } catch {
    throw new ApiError(0, { code: "network", message: "Cannot reach the Forgeyard server." });
  }
  if (!res.ok) {
    let detail: ErrorDetail = {};
    try {
      const raw = (JSON.parse(await res.text()) as { detail?: unknown }).detail;
      if (raw && typeof raw === "object" && !Array.isArray(raw)) detail = raw as ErrorDetail;
    } catch {
      detail = {};
    }
    if (res.status === 401 && detail.code !== "wrong_password") notifyUnauthorized(detail.reason);
    const retry = Number(res.headers.get("Retry-After"));
    throw new ApiError(res.status, detail, Number.isFinite(retry) && retry > 0 ? retry : undefined);
  }
  const blob = await res.blob();
  const file = backupFilename(res.headers.get("Content-Disposition")) ?? "forgeyard-key-backup.fykey";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = file;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
  const kid = /forgeyard-key-backup-([0-9a-f]+)-/.exec(file)?.[1] ?? "";
  return { kid, file };
}

// onClose gets the finished download, if there was one: the caller refreshes what depends on it (the
// advisor's reminder) only then, so the dialog is not unmounted while it shows how to restore.
export function KeyBackupDialog({ onClose }: { onClose: (done: KeyBackupDone | null) => void }) {
  const info = useApi<SecretKeyInfo>("/secret-key");
  const [password, setPassword] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<KeyBackupDone | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const close = () => onClose(done);
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("input, button")?.focus();
  }, []);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(done); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, done]);

  const problem = passphraseProblem(passphrase, confirm, password);
  const ready = password.length > 0 && problem === null && !busy;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const d = await downloadKeyBackup(password, passphrase, confirm);
      setPassword("");
      setPassphrase("");
      setConfirm("");
      setDone(d);
    } catch (err) {
      setError(errorText(err));
      if (err instanceof ApiError && err.code === "wrong_password") setPassword("");
    } finally {
      setBusy(false);
    }
  };

  const i = info.data;
  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="dlg panel keybak" role="dialog" aria-modal="true" aria-labelledby="keybak-title" ref={box}>
        <div className="dlg-head">
          <h2 id="keybak-title"><IconKey size={18} aria-hidden="true" /> Back up the secret key</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label="Close" onClick={close}><IconX size={16} aria-hidden="true" /></button>
        </div>
        {done ? (
          <div className="keybak-done">
            <Notice tone="ok">
              <strong>Downloaded {done.file}.</strong> Setup health counts it as the backup of key id {done.kid}.
            </Notice>
            <p className="small">
              Keep the file and its passphrase apart from the database backup, and the passphrase apart from the file: whoever has both can
              read every stored secret and sign certificates your devices trust. Forgeyard keeps neither.
            </p>
            <p className="small">To restore it, stop the server and run, in the Forgeyard folder:</p>
            <CopyBox label="Restore command" value={`uv run --no-sync python -m forgeyard secrets restore ${done.file}`} />
            <p className="small muted">It asks for the passphrase, and never replaces a different key without <code>--force</code>.</p>
            <div className="dlg-foot">
              <button type="button" className="btn btn-primary" onClick={close}>Done</button>
            </div>
          </div>
        ) : (
          <form onSubmit={(e) => void submit(e)} noValidate>
            <p className="small muted dlg-lead">
              One file with the key that encrypts the stored secrets (Hugging Face tokens, provider keys, replica keys)
              {i?.includes_ca ? " and this server's certificate authority (its key and certificate)" : ""}, encrypted with a passphrase you
              choose now. You need the file and the passphrase to restore; Forgeyard keeps neither.
            </p>
            {info.error && <Notice tone="error">{info.error}</Notice>}
            {i && (
              <dl className="facts keybak-facts">
                <dt>Key id</dt>
                <dd className="mono">{i.kid}</dd>
                <dt>Certificate authority</dt>
                <dd>{i.includes_ca ? "included" : i.tls_mode === "own" ? "not included: this server uses your own certificate, back it up yourself" : "not included: this server does not use Forgeyard's own CA"}</dd>
                {i.backed_up && (
                  <>
                    <dt>Last backup</dt>
                    <dd>{i.backed_up.at.slice(0, 10)} by {i.backed_up.by}</dd>
                  </>
                )}
              </dl>
            )}
            {i && !i.has_password ? (
              <Notice tone="error">Your account has no password to confirm this with. Set one first (an admin can send you a link).</Notice>
            ) : (
              <>
                <div className="field">
                  <label htmlFor="keybak-password">Your password</label>
                  <input id="keybak-password" type="password" autoComplete="current-password" value={password}
                    onChange={(e) => setPassword(e.target.value)} />
                </div>
                <div className={`field${touched && problem && !passphrase ? " has-error" : ""}`}>
                  <label htmlFor="keybak-passphrase">Backup passphrase</label>
                  <input id="keybak-passphrase" type="password" autoComplete="off" value={passphrase} aria-describedby="keybak-passphrase-hint"
                    onChange={(e) => setPassphrase(e.target.value)} />
                  <p className="field-hint" id="keybak-passphrase-hint">
                    At least {BACKUP_PASSPHRASE_MIN} characters, not your password. Write it down: a lost passphrase cannot be recovered.
                  </p>
                </div>
                <div className="field">
                  <label htmlFor="keybak-confirm">Passphrase again</label>
                  <input id="keybak-confirm" type="password" autoComplete="off" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
                </div>
                {(touched || (passphrase && confirm)) && problem && <p className="field-error" role="alert">{problem}</p>}
                {error && <Notice tone="error">{error}</Notice>}
              </>
            )}
            <div className="dlg-foot">
              <button type="button" className="btn btn-quiet" onClick={close}>Cancel</button>
              <button type="submit" className="btn btn-primary" disabled={busy || !i || !i.has_password}>
                <IconDownload size={16} aria-hidden="true" /> {busy ? "Encrypting…" : "Download the backup"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// A button that opens the dialog; onDone runs when a dialog that downloaded the file is closed.
export function KeyBackupButton({ className = "btn btn-small", label = "Download a backup…", onDone }: {
  className?: string; label?: string; onDone?: (d: KeyBackupDone) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className={className} onClick={() => setOpen(true)}>
        <IconDownload size={15} aria-hidden="true" /> {label}
      </button>
      {open && <KeyBackupDialog onClose={(d) => { setOpen(false); if (d) onDone?.(d); }} />}
    </>
  );
}
