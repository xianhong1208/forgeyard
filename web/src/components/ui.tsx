import { IconAlertTriangle, IconCheck, IconCopy, IconInfoCircle } from "@tabler/icons-react";
import { useState, type InputHTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router";

import { useT } from "../i18n/react";
import { useDocumentTitle } from "../lib/title";

export function PageHeader({ index, title, lead, actions }: { index: string; title: string; lead?: ReactNode; actions?: ReactNode }) {
  useDocumentTitle(title);
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{index}</p>
        <h1>{title}</h1>
        {lead && <p className="lead">{lead}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

type FieldProps = InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: ReactNode; error?: string | null };

export function Field({ label, hint, error, id, ...input }: FieldProps) {
  const fid = id ?? `f-${input.name}`;
  return (
    <div className={`field${error ? " has-error" : ""}`}>
      <label htmlFor={fid}>{label}</label>
      <input id={fid} {...input} aria-invalid={error ? true : undefined} aria-describedby={hint ? `${fid}-hint` : undefined} />
      {hint && !error && <p className="field-hint" id={`${fid}-hint`}>{hint}</p>}
      {error && <p className="field-error">{error}</p>}
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "error" | "ok" | "warn"; children: ReactNode }) {
  const Icon = tone === "error" || tone === "warn" ? IconAlertTriangle : tone === "ok" ? IconCheck : IconInfoCircle;
  return (
    <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={18} stroke={1.8} aria-hidden="true" />
      <div>{children}</div>
    </div>
  );
}

export function CopyBox({ value, label }: { value: string; label: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="copybox">
      <code aria-label={label}>{value}</code>
      <button type="button" className="btn btn-quiet" onClick={copy}>
        {copied ? <IconCheck size={16} aria-hidden="true" /> : <IconCopy size={16} aria-hidden="true" />}
        {copied ? t("common.copied") : t("common.copy")}
      </button>
    </div>
  );
}

// A command over several lines (a long one-liner broken with backslashes), copied as it is shown.
export function CopyBlock({ value, label }: { value: string; label: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="copyblock">
      <pre aria-label={label}>{value}</pre>
      <button type="button" className="btn btn-quiet btn-small" onClick={copy}>
        {copied ? <IconCheck size={16} aria-hidden="true" /> : <IconCopy size={16} aria-hidden="true" />}
        {copied ? t("common.copied") : t("common.copy")}
      </button>
    </div>
  );
}

export function RoleChip({ role }: { role: string }) {
  const t = useT();
  return <span className={`chip chip-role-${role}`}>{t.maybe(`common.role.${role}`, role)}</span>;
}

export function StatusChip({ status }: { status: string }) {
  const t = useT();
  return <span className={`chip chip-status-${status}`}>{t.maybe(`common.status.${status}`, status)}</span>;
}

// Next to the gateway address: programs must trust this server's own certificate authority.
export function TrustHint({ url }: { url: string }) {
  const t = useT();
  return (
    <p className="muted small trust-hint" data-url={url}>
      {t.rich("common.trustHint", { file: <code>forgeyard-ca.pem</code>, link: (text) => <Link to="/trust">{text}</Link> })}
    </p>
  );
}
