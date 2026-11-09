import { IconAlertTriangle, IconCheck, IconCopy, IconInfoCircle } from "@tabler/icons-react";
import { useState, type InputHTMLAttributes, type ReactNode } from "react";

export function PageHeader({ index, title, lead, actions }: { index: string; title: string; lead?: string; actions?: ReactNode }) {
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

export function Notice({ tone = "info", children }: { tone?: "info" | "error" | "ok"; children: ReactNode }) {
  const Icon = tone === "error" ? IconAlertTriangle : tone === "ok" ? IconCheck : IconInfoCircle;
  return (
    <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={18} stroke={1.8} aria-hidden="true" />
      <div>{children}</div>
    </div>
  );
}

export function CopyBox({ value, label }: { value: string; label: string }) {
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
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export function RoleChip({ role }: { role: string }) {
  return <span className={`chip chip-role-${role}`}>{role}</span>;
}

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip chip-status-${status}`}>{status}</span>;
}
