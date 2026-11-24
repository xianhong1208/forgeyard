// The pieces the Machines and Serving pages share (1.4): kind icons, memory bars, what runs where.
import {
  IconBox,
  IconBrain,
  IconCpu,
  IconCpu2,
  IconGpu,
  IconMicrophone,
  IconMovie,
  IconPhotoAi,
  IconPhotoScan,
  IconScan,
  IconSortDescending,
  IconSpeakerphone,
  IconTags,
  IconVectorBezier2,
  type Icon,
} from "@tabler/icons-react";
import { useEffect, useMemo } from "react";
import { Link } from "react-router";

import {
  MACHINE_KIND, workloadTitle, memoryLabel, placement, share, uniqueHolders,
  type CardMemory, type Holder, type MachineKind, type ModelKind, type Placement,
} from "../lib/fleet";
import { useT } from "../i18n/react";
import { bytes } from "../lib/machines";
import type { DeploymentsResponse, Machine, WorkloadsResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

const MACHINE_ICON: Record<MachineKind, Icon> = { nvidia: IconGpu, "amd-gpu": IconGpu, "amd-apu": IconCpu2, cpu: IconCpu };

const MODEL_ICON: Record<ModelKind, Icon> = {
  chat: IconBrain, multimodal: IconPhotoScan, embed: IconVectorBezier2, rerank: IconSortDescending,
  asr: IconMicrophone, tts: IconSpeakerphone, image: IconPhotoAi, video: IconMovie, vision: IconScan, classify: IconTags,
};

// The machine's kind on a small plate: the shape says GPU, APU or CPU; the tint says NVIDIA, AMD or none.
export function KindMark({ kind, size = 34 }: { kind: MachineKind; size?: number }) {
  useT(); // re-render with the language: the label below is read in it
  const I = MACHINE_ICON[kind];
  const { label } = MACHINE_KIND[kind];
  return (
    <span className={`kindmark kindmark-${kind}`} style={{ width: size, height: size }} title={label} role="img" aria-label={label}>
      <I size={Math.round(size * 0.56)} stroke={1.6} aria-hidden="true" />
    </span>
  );
}

export function ModelKindIcon({ kind, size = 16 }: { kind: ModelKind; size?: number }) {
  const I = MODEL_ICON[kind];
  return <I size={size} stroke={1.7} aria-hidden="true" />;
}

export function HolderIcon({ h, size = 14 }: { h: Holder; size?: number }) {
  return h.kind === "workload" ? <IconBox size={size} stroke={1.7} aria-hidden="true" /> : <ModelKindIcon kind={h.kind} size={size} />;
}

export function StatusDot({ status }: { status: Machine["status"] }) {
  return <span className={`dot dot-${status} fl-dot`} aria-hidden="true" />;
}

// One bar: what Forgeyard reserved in heat, other use in steel, free empty. With `held`, Forgeyard's
// part is split by what holds it, each named when there is room (the memory map).
export function MemBar({ mem, held, size = "md" }: { mem: CardMemory; held?: Holder[]; size?: "sm" | "md" | "lg" }) {
  useT(); // the label follows the language
  const named = (held ?? []).filter((h) => (h.reserved ?? 0) > 0);
  return (
    <span className={`membar membar-${size}`} role="img" aria-label={memoryLabel(mem)}>
      {named.length
        ? named.map((h, i) => (
          <span key={`${h.key}-${h.replica.ordinal}-${i}`} className="membar-fy membar-seg" style={{ width: share(h.reserved ?? 0, mem.total) }}
            title={`${h.name}: ${bytes(h.reserved)}`}>
            {size === "lg" && (h.reserved ?? 0) / Math.max(1, mem.total) > 0.09 && <span className="membar-label">{h.name}</span>}
          </span>
        ))
        : <span className="membar-fy" style={{ width: share(mem.forgeyard, mem.total) }} />}
      <span className="membar-other" style={{ width: share(mem.other, mem.total) }} />
    </span>
  );
}

export function MemLegend() {
  const t = useT();
  return (
    <span className="memlegend small muted">
      <span><i className="sw sw-fy" /> {t("fleet.legend.forgeyard")}</span>
      <span><i className="sw sw-other" /> {t("fleet.legend.other")}</span>
      <span><i className="sw sw-free" /> {t("fleet.legend.free")}</span>
    </span>
  );
}

// What runs on a machine: one chip per deployment (linking to it) or app; "+N more" past `max`.
export function HolderChips({ list, max, empty, link = true }: { list: Holder[]; max?: number; empty?: string; link?: boolean }) {
  const t = useT();
  const u = uniqueHolders(list);
  const shown = max === undefined ? u : u.slice(0, max);
  return (
    <div className="holder-chips">
      {shown.map((h) => {
        const title = h.trial ? `${h.name} · ${h.trial.model}` : h.kind === "workload" ? workloadTitle(h.name) : `${h.deployment?.model.name ?? h.name}`;
        return link && h.href ? (
          <Link key={h.key} to={h.href} className="holder-chip" title={title}><HolderIcon h={h} /><span>{h.name}</span></Link>
        ) : (
          <span key={h.key} className="holder-chip" title={title}><HolderIcon h={h} /><span>{h.name}</span></span>
        );
      })}
      {shown.length < u.length && <span className="holder-chip holder-more">{t("fleet.more", { count: u.length - shown.length })}</span>}
      {u.length === 0 && <span className="empty-line">{empty ?? t("fleet.nothingHere")}</span>}
    </div>
  );
}

// The workloads and deployments behind "what runs where", polled with the page. A failed fetch only
// leaves the lists empty: the machines themselves still show.
export function usePlacement(pollMs: number): Placement & { loaded: boolean; canRun: boolean } {
  const w = useApi<WorkloadsResponse>("/workloads");
  const d = useApi<DeploymentsResponse>("/deployments");
  const { reload: rw } = w;
  const { reload: rd } = d;
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      rw();
      rd();
    }, pollMs);
    return () => window.clearInterval(id);
  }, [rw, rd, pollMs]);
  const p = useMemo(() => placement(w.data?.workloads ?? [], d.data?.deployments ?? [], d.data?.trials ?? []), [w.data, d.data]);
  return { ...p, loaded: Boolean(w.data && d.data), canRun: w.data?.can_manage ?? false };
}
