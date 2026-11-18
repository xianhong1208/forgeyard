// 1.4.3 (#140): the pieces a catalog model's page and an engine's page share (layout "A+", chosen by Tony): a
// header, a line of facts, jump links to the sections, and sections that a long list (Files, Parameters) folds to one
// line with Show / Hide.
import { IconArrowLeft, IconChevronDown, IconCpu, IconEngine } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { KindMark, ModelKindIcon } from "./Fleet";
import { HARDWARE_LABEL, engineHardware } from "../lib/engines";
import { machineKind } from "../lib/fleet";
import { bytes } from "../lib/machines";
import { shortSource } from "../lib/models";
import { catalogKind, engineHref, modelHref } from "../lib/pages";
import type { CatalogModel, Machine, Recipe } from "../lib/types";

export function Crumb({ to, label }: { to: string; label: string }) {
  return <p className="crumb"><Link to={to}><IconArrowLeft size={14} aria-hidden="true" /> {label}</Link></p>;
}

export function Section({ id, title, count, actions, children, note }: {
  id: string; title: string; count?: number | string; actions?: ReactNode; children: ReactNode; note?: ReactNode;
}) {
  return (
    <section className="panel mk-section lp-sec" id={id} aria-labelledby={`${id}-h`}>
      <div className="panel-head">
        <h2 id={`${id}-h`}>{title}{count !== undefined && <span className="muted small"> · {count}</span>}</h2>
        {actions}
      </div>
      {note && <p className="muted small lp-note">{note}</p>}
      {children}
    </section>
  );
}

// Links that scroll to a section of the page (the address keeps no hash).
export function Jump({ links }: { links: [string, string][] }) {
  return (
    <nav className="lp-jump" aria-label="On this page">
      {links.map(([id, label]) => (
        <a key={id} href={`#${id}`} onClick={(e) => {
          e.preventDefault();
          document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
        }}>{label}</a>
      ))}
    </nav>
  );
}

export function FoldButton({ open, setOpen, what, controls }: { open: boolean; setOpen: (o: boolean) => void; what: string; controls: string }) {
  return (
    <button type="button" className="btn btn-quiet btn-small" aria-expanded={open} aria-controls={controls} onClick={() => setOpen(!open)}>
      <IconChevronDown size={14} aria-hidden="true" className={open ? "lp-rot" : undefined} /> {open ? `Hide ${what}` : `Show ${what}`}
    </button>
  );
}

export function MachineCell({ id, name, status, machines }: { id: number; name: string; status: string; machines: Machine[] | null }) {
  const m = machines?.find((x) => x.id === id);
  return (
    <>
      <Link to={`/machines/${id}`} className="lp-machine">
        {m && <KindMark kind={machineKind(m)} size={24} />}
        <span className="mono">{name}</span>
      </Link>
      <div className={`small muted lp-mstate machine-state-${status}`}>{status}</div>
    </>
  );
}

export function HardwareChips({ r }: { r: Recipe }) {
  return (
    <>
      {engineHardware(r).map((h) => (
        <span key={h} className={`vendor vendor-${h}`}>{h === "cpu" ? <IconCpu size={11} aria-hidden="true" /> : null}{HARDWARE_LABEL[h]}</span>
      ))}
    </>
  );
}

// Cards that open an engine's page (on a model's page) …
export function EngineCards({ list, empty }: { list: Recipe[]; empty: ReactNode }) {
  if (!list.length) return <p className="empty-line">{empty}</p>;
  return (
    <ul className="lp-cards">
      {list.map((r) => (
        <li key={r.name}>
          <Link to={engineHref(r.name)} className="lp-card">
            <span className="lp-card-top"><IconEngine size={16} aria-hidden="true" /><span className="mono strong">{r.name}</span>
              <span className="chip">{r.engine} {r.version}</span>{!r.builtin && <span className="chip chip-tone-run">custom</span>}</span>
            <span className="lp-card-hw"><HardwareChips r={r} /></span>
            <span className="small muted lp-clamp">{r.description}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

// … and a model's page (on an engine's page).
export function ModelCards({ list, empty }: { list: CatalogModel[]; empty: ReactNode }) {
  if (!list.length) return <p className="empty-line">{empty}</p>;
  return (
    <ul className="lp-cards">
      {list.map((m) => (
        <li key={m.id}>
          <Link to={modelHref(m.name)} className="lp-card">
            <span className="lp-card-top"><ModelKindIcon kind={catalogKind(m)} size={16} /><span className="strong">{m.title ?? m.name}</span></span>
            <span className="mono small muted lp-break">{shortSource(m).text}</span>
            <span className="small muted">{m.task_label} · {m.format} · {bytes(m.size_bytes)}{m.deployments ? ` · ${m.deployments} deployed` : ""}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
