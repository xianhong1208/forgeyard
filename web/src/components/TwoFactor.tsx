// My account's two-factor sign-in (1.6.0, #39): authenticator apps (QR code, setup key, first code and password),
// recovery codes shown once, and the password or a current code before anything is removed or replaced.
import { IconCheck, IconCopy, IconDownload, IconPlus, IconShieldCheck } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { api, ApiError, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { groupKey, reauthBody, recoveryFile, type Enrolment, type Factor, type MfaStatus } from "../lib/mfa";
import { useApi } from "../lib/useApi";
import { HelpLink, HelpLinks } from "./HelpLink";
import { QrCode } from "./QrCode";
import { CopyBox, Field, Notice } from "./ui";

type Say = (m: { tone: "ok" | "error"; text: string }) => void;
type Pending = { kind: "remove"; factor: Factor } | { kind: "codes" };

function downloadText(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function RecoveryCodes({ codes, username, onDone }: { codes: string[]; username: string; onDone: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="mfa-codes">
      <h3>{t("mfa.codes.title")}</h3>
      <Notice tone="warn">{t("mfa.codes.body")}</Notice>
      <ol className="recovery-list mono" aria-label={t("mfa.codes.label")}>
        {codes.map((c) => <li key={c}>{c}</li>)}
      </ol>
      <div className="row-gap">
        <button type="button" className="btn btn-quiet" onClick={() => void copy()}>
          {copied ? <IconCheck size={16} aria-hidden="true" /> : <IconCopy size={16} aria-hidden="true" />} {copied ? t("common.copied") : t("common.copy")}
        </button>
        <button type="button" className="btn btn-quiet"
          onClick={() => downloadText(`forgeyard-recovery-codes-${username}.txt`, recoveryFile(t("mfa.codes.fileHeader", { name: username }), codes))}>
          <IconDownload size={16} aria-hidden="true" /> {t("mfa.codes.download")}
        </button>
        <button type="button" className="btn btn-primary" onClick={onDone}>{t("mfa.codes.done")}</button>
      </div>
    </div>
  );
}

function EnrolForm({ enrolment, first, onDone, onCancel }: {
  enrolment: Enrolment; first: boolean; onDone: (r: MfaStatus & { factor: Factor; recovery_codes: string[] | null }) => void; onCancel: () => void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [auth, setAuth] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<MfaStatus & { factor: Factor; recovery_codes: string[] | null }>("/auth/mfa/totp", {
        method: "POST",
        body: { enrolment: enrolment.enrolment, new_code: code.replace(/\s+/g, ""), name: name || null, ...reauthBody(auth) },
      });
      onDone(r);
    } catch (err) {
      setError(errorText(err));
      if (err instanceof ApiError && (err.code === "wrong_password" || err.code === "mfa_code_invalid")) setAuth("");
      if (err instanceof ApiError && err.code === "mfa_new_code_invalid") setCode("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="mfa-enrol" onSubmit={(e) => void submit(e)}>
      <h3>{t("mfa.enrol.title")}</h3>
      <div className="mfa-enrol-grid">
        <div className="mfa-qr">
          <p className="small">{t("mfa.enrol.scan")}</p>
          <QrCode text={enrolment.uri} label={t("mfa.enrol.qr")} />
        </div>
        <div className="mfa-key">
          <p className="small muted">{t("mfa.enrol.cantScan")}</p>
          <CopyBox label={t("mfa.enrol.key")} value={groupKey(enrolment.secret)} />
          <details className="small">
            <summary>{t("mfa.enrol.uriShow")}</summary>
            <CopyBox label={t("mfa.enrol.uri")} value={enrolment.uri} />
          </details>
        </div>
      </div>
      <div className="stack">
        <Field label={t("mfa.enrol.code")} name="mfa-new-code" inputMode="numeric" autoComplete="one-time-code" required
          pattern="[0-9 ]{6,7}" maxLength={7} value={code} onChange={(e) => setCode(e.target.value)} hint={t("mfa.enrol.codeHint")} />
        <Field label={t("mfa.enrol.name")} name="mfa-name" maxLength={64} placeholder={t("mfa.enrol.namePlaceholder")}
          value={name} onChange={(e) => setName(e.target.value)} hint={t("mfa.enrol.nameHint")} />
        <Field label={first ? t("mfa.enrol.password") : t("mfa.enrol.passwordOrCode")} name="mfa-auth" type="password"
          autoComplete="current-password" required value={auth} onChange={(e) => setAuth(e.target.value)} />
        {error && <Notice tone="error">{error}</Notice>}
        <div className="row-gap">
          <button className="btn btn-primary" disabled={busy}>
            {busy ? t("mfa.enrol.busy") : first ? t("mfa.enrol.submitFirst") : t("mfa.enrol.submit")}
          </button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
        </div>
      </div>
    </form>
  );
}

function ConfirmForm({ pending, last, onDone, onCancel }: {
  pending: Pending; last: boolean; onDone: (r: MfaStatus & { recovery_codes?: string[] }) => void; onCancel: () => void;
}) {
  const t = useT();
  const [auth, setAuth] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const path = pending.kind === "remove" ? `/auth/mfa/factors/${pending.factor.id}/remove` : "/auth/mfa/recovery-codes";
      onDone(await api<MfaStatus & { recovery_codes?: string[] }>(path, { method: "POST", body: reauthBody(auth) }));
    } catch (err) {
      setError(errorText(err));
      setAuth("");
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="mfa-confirm stack" onSubmit={(e) => void submit(e)}>
      <p className="strong">{pending.kind === "remove" ? t("mfa.confirm.remove", { name: pending.factor.name }) : t("mfa.confirm.codes")}</p>
      {pending.kind === "remove" && last && <p className="small muted">{t("mfa.confirm.removeLast")}</p>}
      <Field label={t("mfa.confirm.label")} name="mfa-confirm" type="password" autoComplete="current-password" required autoFocus
        value={auth} onChange={(e) => setAuth(e.target.value)} />
      {error && <Notice tone="error">{error}</Notice>}
      <div className="row-gap">
        <button className={`btn ${pending.kind === "remove" ? "btn-danger" : "btn-primary"}`} disabled={busy}>
          {busy ? t("mfa.confirm.busy") : pending.kind === "remove" ? t("mfa.remove") : t("mfa.newCodes")}
        </button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </form>
  );
}

// `onChange`: after an authenticator is added or removed (My account then reloads its sessions, whose
// "two-factor" chip changes with it).
export function TwoFactorPanel({ say, onChange }: { say: Say; onChange?: () => void }) {
  const t = useT();
  const { user, setUser, reload: reloadAuth } = useAuth();
  const { data, error, reload } = useApi<MfaStatus>("/auth/mfa/factors");
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null);
  const [starting, setStarting] = useState(false);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const factors = data?.factors ?? [];
  const on = factors.length > 0;

  // After the first authenticator, the account counts as having one only once its recovery codes are put away: a
  // required setup that was overdue (MfaRequired.tsx) would otherwise leave this page, and the codes with it.
  const [markAfterCodes, setMarkAfterCodes] = useState(false);
  const markUser = () => {
    if (user && !user.has_mfa) setUser({ ...user, has_mfa: true, mfa_enrolment: null });
  };

  const start = async () => {
    setStarting(true);
    setPending(null);
    try {
      setEnrolment(await api<Enrolment>("/auth/mfa/totp/start", { method: "POST" }));
    } catch (err) {
      say({ tone: "error", text: errorText(err) });
    } finally {
      setStarting(false);
    }
  };

  return (
    <section className="panel two-factor">
      <div className="panel-head">
        <h2><IconShieldCheck size={18} aria-hidden="true" /> {t("mfa.title")}</h2>
        {data && <span className={`chip ${on ? "chip-status-active" : ""}`}>{on ? t("mfa.on") : t("mfa.off")}</span>}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {data && <p className="muted small">{on ? t("mfa.leadOn") : t("mfa.leadOff")}</p>}
      <HelpLinks>
        <HelpLink page="how-to" anchor="turn-on-two-factor-sign-in">{t("mfa.help")}</HelpLink>
      </HelpLinks>
      {on && (
        <ul className="mfa-factors">
          {factors.map((f) => (
            <li key={f.id}>
              <div className="mfa-factor-main">
                <span className="strong">{f.name}</span>
                <span className="muted small">
                  <span title={exact(f.created_at)}>{t("mfa.added", { when: when(f.created_at) })}</span>
                  {f.last_used_at && <> · <span title={exact(f.last_used_at)}>{t("mfa.lastUsed", { when: when(f.last_used_at) })}</span></>}
                </span>
              </div>
              <button type="button" className="btn btn-quiet btn-small" onClick={() => { setEnrolment(null); setPending({ kind: "remove", factor: f }); }}>
                {t("mfa.remove")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {on && data && (
        <div className="mfa-recovery">
          <p className="small">
            <span className={data.recovery_codes_left <= 2 ? "strong" : ""}>{t.plural("mfa.recoveryLeft", data.recovery_codes_left)}</span>{" "}
            <span className="muted">{t("mfa.recoveryHint")}</span>
          </p>
          <button type="button" className="btn btn-quiet btn-small" onClick={() => { setEnrolment(null); setPending({ kind: "codes" }); }}>
            {t("mfa.newCodes")}
          </button>
        </div>
      )}
      {pending && (
        <ConfirmForm
          key={pending.kind === "remove" ? `r${pending.factor.id}` : "codes"}
          pending={pending}
          last={factors.length === 1}
          onCancel={() => setPending(null)}
          onDone={(r) => {
            setPending(null);
            reload();
            if (pending.kind === "remove") {
              const off = r.factors.length === 0;
              onChange?.();
              // Off again: the server says whether it is required of this account, and by when.
              if (off) void reloadAuth();
              say({ tone: "ok", text: off ? t("mfa.done.off", { name: pending.factor.name }) : t("mfa.done.removed", { name: pending.factor.name }) });
            } else {
              setCodes(r.recovery_codes ?? null);
              say({ tone: "ok", text: t("mfa.done.codes") });
            }
          }}
        />
      )}
      {codes && user && (
        <RecoveryCodes codes={codes} username={user.username} onDone={() => {
          setCodes(null);
          if (markAfterCodes) {
            setMarkAfterCodes(false);
            markUser();
          }
        }} />
      )}
      {enrolment ? (
        <EnrolForm
          enrolment={enrolment}
          first={!on}
          onCancel={() => setEnrolment(null)}
          onDone={(r) => {
            setEnrolment(null);
            reload();
            onChange?.();
            if (r.recovery_codes) {
              setCodes(r.recovery_codes);
              setMarkAfterCodes(true);
            } else {
              markUser();
            }
            say({ tone: "ok", text: on ? t("mfa.done.added", { name: r.factor.name }) : t("mfa.done.on") });
          }}
        />
      ) : (
        data && !codes && factors.length < data.max_factors && (
          <div>
            <button type="button" className={`btn ${on ? "btn-quiet" : "btn-primary"}`} onClick={() => void start()} disabled={starting}>
              <IconPlus size={16} aria-hidden="true" /> {starting ? t("mfa.enrol.starting") : on ? t("mfa.addAnother") : t("mfa.add")}
            </button>
          </div>
        )
      )}
    </section>
  );
}
