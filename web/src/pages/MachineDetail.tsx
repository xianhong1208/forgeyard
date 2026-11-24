// A machine's own page (1.4): its kind, a memory map of each card split by what holds it, what runs
// there, its GPU access, and the agent's details. Admins revoke or remove it here.
import { IconArrowLeft, IconLock, IconPlus } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { AgentUpdateSection, BehindChip, TlsChip } from "../components/AgentUpdate";
import { HolderChips, HolderIcon, KindMark, MemBar, MemLegend, StatusDot, usePlacement } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { EngineImagesSection } from "../components/ImageUpload";
import { ModelCacheSection } from "../components/ModelCache";
import { Notice } from "../components/ui";
import { t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import {
  MACHINE_KIND, appTitle, cardLine, cardMemory, cards, machineKind, machineMemory, memoryRows, uniqueHolders, whereOnMachine, type Placement,
} from "../lib/fleet";
import { exact } from "../lib/format";
import { ageSeconds, ago, archLabel, bytes, replicaAddressNote } from "../lib/machines";
import { tlsLine } from "../lib/replicaTls";
import { useDocumentTitle } from "../lib/title";
import type { Accelerator, Machine, MachinesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { AsleepHereLine } from "../components/Sleep";
import { allowlistSummary, canRunWorkloads, gpuCards, runtimeLine } from "../lib/workloads";
import { POLL_MS } from "./Machines";

// The facts line's heartbeat, after the status word: "heartbeat 41 s ago", "last heartbeat 5 min ago" (offline) or
// "revoked 2 d ago". The tile's text (Machines' heartbeatText) starts with the status, which the page shows already.
export function heartbeatDetail(m: Machine, serverTime: string): string {
  if (m.status === "revoked") return tNow("machines.revokedAgo", { ago: ago(ageSeconds(serverTime, m.revoked_at)) });
  const age = ago(ageSeconds(serverTime, m.last_heartbeat_at));
  return m.status === "offline" ? tNow("machine.lastHeartbeat", { ago: age }) : tNow("machines.heartbeatAgo", { ago: age });
}

export function MachineDetailPage() {
  const t = useT();
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, reload } = useApi<MachinesResponse>("/machines");
  const p = usePlacement(POLL_MS);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") reload();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  const m = data?.machines.find((x) => String(x.id) === id);
  useDocumentTitle(m?.name ?? t("machine.title"));
  const canManage = data?.can_manage ?? false;

  const run = async (fn: () => Promise<unknown>, ok: string, after?: () => void) => {
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      if (after) after(); else reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };
  const revoke = (x: Machine) => {
    if (!window.confirm(t("machine.revokeQuestion", { name: x.name }))) return;
    void run(() => api(`/machines/${x.id}/revoke`, { method: "POST" }), t("machine.revoked", { name: x.name }));
  };
  const remove = (x: Machine) => {
    const extra = x.status === "revoked" ? "" : t("machine.removeExtra");
    if (!window.confirm(t("machine.removeQuestion", { name: x.name, extra }))) return;
    void run(() => api(`/machines/${x.id}`, { method: "DELETE" }), t("machine.removed", { name: x.name }), () => navigate("/machines", { replace: true }));
  };

  return (
    <>
      <p className="crumb"><Link to="/machines"><IconArrowLeft size={14} aria-hidden="true" /> {t("shell.nav.machines")}</Link></p>
      {error && <Notice tone="error">{error}</Notice>}
      {data && !m && <Notice tone="error">{t("machine.notFound")}</Notice>}
      {!data && !error && <p className="muted">{t("common.loading")}</p>}
      {m && data && (
        <MachineView m={m} machines={data.machines} serverTime={data.server_time} p={p} canManage={canManage} message={message}
          onRevoke={revoke} onRemove={remove}
          onChanged={(text) => { setMessage({ tone: "ok", text }); reload(); }}
          onError={(text) => setMessage({ tone: "error", text })} />
      )}
    </>
  );
}

function MachineView({ m, machines, serverTime, p, canManage, message, onRevoke, onRemove, onChanged, onError }: {
  m: Machine; machines: Machine[]; serverTime: string; p: Placement & { loaded: boolean; canRun: boolean }; canManage: boolean;
  message: { tone: "ok" | "error"; text: string } | null;
  onRevoke: (m: Machine) => void; onRemove: (m: Machine) => void; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const t = useT();
  const kind = machineKind(m);
  const mem = machineMemory(m, p);
  const here = p.onMachine(m.id);
  const models = here.filter((h) => h.kind !== "workload");
  const platform = [m.platform.distro || m.platform.os, m.platform.arch].filter(Boolean).join(" · ");
  const drivers = [...new Set(cards(m).map((a) => a.driver).filter(Boolean))];
  const failed = Object.entries(m.collectors).filter(([, s]) => !s.ok && s.reason);
  const canPlace = p.canRun && m.status === "online";
  return (
    <>
      <header className="page-header">
        <div className="mk-title">
          <KindMark kind={kind} size={52} />
          <div>
            <p className="eyebrow">01 · {MACHINE_KIND[kind].label}</p>
            <h1 className="mono">{m.name}</h1>
          </div>
        </div>
        {canManage && (
          <div className="page-actions">
            {m.status !== "revoked" && <button type="button" className="btn btn-quiet" onClick={() => onRevoke(m)}>{t("machine.revoke")}</button>}
            <button type="button" className="btn btn-danger" onClick={() => onRemove(m)}>{t("machine.remove")}</button>
          </div>
        )}
      </header>
      <AdvisorBanner pages={[`machine:${m.id}`]} />
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <p className="mk-facts">
        <span><StatusDot status={m.status} /> <strong className={`machine-state-${m.status}`}>{t.maybe(`common.status.${m.status}`, m.status)}</strong> · <span title={exact(m.last_heartbeat_at)}>{heartbeatDetail(m, serverTime)}</span></span>
        <span><strong>{cardLine(m)}</strong></span>
        <span>{t.rich("machine.facts.free", { free: <strong>{bytes(mem.free)}</strong>, total: bytes(mem.total),
          what: kind === "cpu" ? " RAM" : kind === "amd-apu" ? t("machine.facts.shared") : "" })}</span>
        {m.agent_version && <span>{t("machines.agent", { version: m.agent_version })}{platform ? ` · ${platform}` : ""}</span>}
        <BehindChip m={m} />
        <TlsChip m={m} />
        {m.simulated && (
          <span className="chip chip-simulated" title={t("machine.simulatedTitle")}>{t("machine.simulated")}</span>
        )}
        {Object.entries(m.labels).map(([k, v]) => <span key={k} className="chip">{k}={v}</span>)}
      </p>

      <section className="panel mk-section" aria-labelledby="mk-models">
        <div className="panel-head">
          <h2 id="mk-models">{t("shell.nav.models")}</h2>
          {canPlace && <Link to={`/serving/deploy?machine=${m.id}${kind === "cpu" ? "&device=cpu" : ""}`} className="btn btn-small"><IconPlus size={14} aria-hidden="true" /> {t("machine.deployHere")}</Link>}
        </div>
        {p.loaded ? <HolderChips list={models} empty={t("machine.noModel")} /> : <p className="muted small">{t("common.loading")}</p>}
        {p.loaded && <AsleepHereLine list={p.asleep(m.id)} />}
      </section>

      <section className="panel mk-section" aria-labelledby="mk-map">
        <div className="panel-head"><h2 id="mk-map">{t("machine.memoryMap")}</h2><MemLegend /></div>
        {memoryRows(m).length === 0 ? <p className="muted small">{t("machine.noMemory")}</p> : (
          <div className="memmap">
            {memoryRows(m).map((a) => <MapRow key={a.key} m={m} a={a} p={p} restricted={m.gpu_allowlist !== null && m.gpu_allowlist !== undefined} />)}
          </div>
        )}
      </section>

      <section className="panel mk-section" aria-labelledby="mk-running">
        <div className="panel-head">
          <h2 id="mk-running">{t("machine.runningHere")} <span className="muted small">· {uniqueHolders(here).length}</span></h2>
        </div>
        {!p.loaded ? <p className="muted small">{t("common.loading")}</p> : here.length === 0 ? <p className="empty-line">{t("machine.nothingHere")}</p> : (
          <ul className="runlist">
            {uniqueHolders(here).map((h) => {
              const all = here.filter((x) => x.key === h.key);
              return (
                <li key={h.key}>
                  <span className="runlist-k"><HolderIcon h={h} size={16} /></span>
                  <span className="runlist-name">
                    {h.href ? <Link to={h.href}>{h.name}</Link> : <span className="strong" title={appTitle(h.name)}>{h.name}</span>}
                    <small>{h.trial ? h.trial.model : h.kind === "workload" ? t("machine.appLine", { kind: String(h.workload.kind), runtime: String(h.workload.runtime) }) : h.deployment?.model.name}</small>
                  </span>
                  <span className="runlist-where">{all.map((x) => whereOnMachine(x.replica)).join(" ")}</span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {m.status !== "revoked" && <ModelCacheSection m={m} poll={POLL_MS} canManage={canManage} onChanged={onChanged} onError={onError} />}

      {m.status !== "revoked" && <EngineImagesSection m={m} machines={machines} canManage={canManage} onChanged={onChanged} onError={onError} />}

      {m.status !== "revoked" && <GpuAccess m={m} canManage={canManage} onChanged={onChanged} onError={onError} />}

      <section className="panel mk-section" aria-labelledby="mk-agent">
        <h2 id="mk-agent">{t("machine.agent.title")}</h2>
        <dl className="facts">
          <div className="fact"><dt>{t("machine.agent.version")}</dt><dd>{m.agent_version ?? t("machine.agent.unknown")}{m.protocol ? <span className="muted">{t("machine.agent.protocol", { protocol: m.protocol })}</span> : null}</dd></div>
          {platform && <div className="fact"><dt>{t("machine.agent.platform")}</dt><dd>{platform}</dd></div>}
          {m.hostname && <div className="fact"><dt>{t("machine.agent.hostname")}</dt><dd className="mono">{m.hostname}</dd></div>}
          {m.ip && <div className="fact"><dt>{t("machine.agent.address")}</dt><dd className="mono">{m.ip}</dd></div>}
          {m.runtime_info?.address && (
            <div className="fact">
              <dt>{t("machine.agent.replicasAt")}</dt>
              <dd><span className="mono">{m.runtime_info.address}</span>{replicaAddressNote(m) && <span className="muted"> · {replicaAddressNote(m)}</span>}</dd>
            </div>
          )}
          {m.status !== "revoked" && tlsLine(m.replica_tls) && (
            <div className="fact"><dt>{t("machine.agent.replicaTraffic")}</dt><dd className={`tls-${m.replica_tls?.state}`}>{tlsLine(m.replica_tls)}</dd></div>
          )}
          {drivers.length > 0 && <div className="fact"><dt>{kind === "nvidia" ? t("machine.agent.driver") : "ROCm"}</dt><dd>{drivers.map((d) => (kind === "nvidia" ? t("machine.agent.driverVersion", { version: String(d) }) : `ROCm ${d}`)).join(", ")}</dd></div>}
          <div className="fact"><dt>{t("machine.agent.joined")}</dt><dd title={exact(m.joined_at)}>{exact(m.joined_at)}</dd></div>
        </dl>
        {failed.length > 0 && (
          <details className="detection">
            <summary>{t("machine.agent.detection")}</summary>
            <ul>{failed.map(([name, s]) => <li key={name}><span className="mono">{name}</span>: {s.reason}</li>)}</ul>
          </details>
        )}
      </section>

      <AgentUpdateSection m={m} canManage={canManage} />
      <HelpLinks>
        <HelpLink page="how-to" anchor="restrict-gpus-on-a-shared-machine">{t("machine.help.restrict")}</HelpLink>
        <HelpLink page="how-to" anchor="free-disk-space-on-a-machine">{t("machine.help.disk")}</HelpLink>
        <HelpLink page="how-to" anchor="upload-an-engine-image-to-a-machine">{t("machine.help.upload")}</HelpLink>
        <HelpLink page="troubleshooting" anchor="an-agent-shows-offline">{t("machines.help.offline")}</HelpLink>
      </HelpLinks>
    </>
  );
}

// One card (or the RAM): its name and architecture, a bar split by what holds it, the holders with
// their reservation, and what is free; load and temperature on the right.
function MapRow({ m, a, p, restricted }: { m: Machine; a: Accelerator; p: Placement; restricted: boolean }) {
  const t = useT();
  const held = p.onRow(m.id, a);
  const c = cardMemory(a, held);
  const cpu = a.vendor === "cpu";
  const arch = archLabel(a);
  const label = cpu ? "RAM" : `GPU #${a.index}`;
  const sub = cpu
    ? [a.detail.cores ? t("machine.map.cores", { n: String(a.detail.cores) }) : null,
      a.detail.threads ? t("machine.map.threads", { n: String(a.detail.threads) }) : null].filter(Boolean).join(" · ") || a.model
    : `${a.model.replace(/^(NVIDIA|AMD) /, "")} · ${arch.main}${arch.sub ? ` ${arch.sub}` : ""}`;
  return (
    <div className={`maprow${!cpu && restricted && a.allowed === false ? " maprow-off" : ""}`} role="group" aria-label={label}>
      <span className="maprow-id">
        <strong>{label}</strong>
        <span title={cpu ? a.model : a.model}>{sub}</span>
        <span className="maprow-tags">
          {a.unified_memory && <span className="chip chip-unified" title={t("machine.map.unifiedTitle")}>{t("machine.map.unified")}</span>}
          {!cpu && a.assigned_to && held.length === 0 && <span className="chip chip-tone-run">{t("machine.inUse", { name: a.assigned_to })}</span>}
          {!cpu && restricted && a.allowed === false && <span className="chip chip-off" title={t("machine.map.offTitle")}><IconLock size={11} aria-hidden="true" /> {t("machine.map.off")}</span>}
        </span>
      </span>
      <span className="maprow-bar">
        <MemBar mem={c} held={held} size="lg" />
        {held.length > 0 && (
          <span className="maprow-holders">
            {held.map((h, i) => {
              const inner = <><HolderIcon h={h} /><span>{h.name}</span>{h.reserved ? <em>{bytes(h.reserved)}</em> : null}</>;
              return h.href
                ? <Link key={`${h.key}-${h.replica.ordinal}-${i}`} to={h.href} className="holder-chip">{inner}</Link>
                : <span key={`${h.key}-${h.replica.ordinal}-${i}`} className="holder-chip" title={appTitle(h.name)}>{inner}</span>;
            })}
          </span>
        )}
      </span>
      <span className="maprow-free">
        <span>{t.rich("machine.map.free", { free: <strong>{bytes(c.free)}</strong> })}</span>
        <span className="muted">{t("machine.map.of", { total: bytes(c.total) })}</span>
        <span className="mono muted" title={t("machine.map.loadTitle")}>
          {a.utilization === null ? "" : `${a.utilization}%`}{a.temperature === null ? "" : ` · ${a.temperature}°C`}
        </span>
      </span>
    </div>
  );
}

// Which GPUs Forgeyard may hand to workloads on this machine, and (for admins) the editor.
function GpuAccess({ m, canManage, onChanged, onError }: {
  m: Machine; canManage: boolean; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const t = useT();
  const cards = gpuCards(m);
  const [editing, setEditing] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const runtimes = m.runtime_info ?? {};
  const workloads = canRunWorkloads(m);

  const open = () => {
    setPicked(m.gpu_allowlist ?? cards.map((a) => a.key));
    setEditing(true);
  };
  const save = async () => {
    const all = picked.length === cards.length;
    const names = cards.filter((a) => picked.includes(a.key)).map((a) => `#${a.index}`).join(", ");
    const text = all
      ? t("machine.gpu.askAll", { name: m.name })
      : picked.length
        ? t("machine.gpu.askSome", { names, name: m.name })
        : t("machine.gpu.askNone", { name: m.name });
    if (!window.confirm(text)) return;
    setBusy(true);
    try {
      await api(`/machines/${m.id}/gpu-allowlist`, { method: "PUT", body: { keys: all ? null : picked } });
      setEditing(false);
      onChanged(all ? t("machine.gpu.savedAll", { name: m.name })
        : picked.length ? t("machine.gpu.savedSome", { names, name: m.name }) : t("machine.gpu.savedNone", { name: m.name }));
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel mk-section gpu-access" aria-labelledby="mk-access">
      <h2 id="mk-access">{t("machine.gpu.title")}</h2>
      <p className="gpu-access-line">
        {cards.length > 0 && (
          <span className={m.gpu_allowlist ? "gpu-access-limited" : undefined}>
            {m.gpu_allowlist && <IconLock size={14} aria-hidden="true" />} {t.rich("machine.gpu.may", { what: <strong>{allowlistSummary(m)}</strong> })}
          </span>
        )}
        <span className="muted">
          {workloads
            ? t("machine.gpu.runs", { process: runtimeLine(runtimes.process, "process"), container: runtimeLine(runtimes.container, "container") })
            : t("machine.gpu.noApps")}
        </span>
        {canManage && cards.length > 0 && !editing && (
          <button type="button" className="btn btn-quiet btn-small" onClick={open}>{t("machine.gpu.change")}</button>
        )}
      </p>
      {editing && (
        <fieldset className="allowlist">
          <legend>{t("machine.gpu.legend", { name: m.name })}</legend>
          <div className="allowlist-cards">
            {cards.map((a) => (
              <label key={a.key} className="card-pick">
                <input type="checkbox" checked={picked.includes(a.key)}
                  onChange={() => setPicked((p) => (p.includes(a.key) ? p.filter((k) => k !== a.key) : [...p, a.key]))} />
                <span className="mono">#{a.index}</span>
                <span className="small">{a.model}</span>
                {a.assigned_to && <span className="chip chip-tone-run">{t("machine.inUse", { name: a.assigned_to })}</span>}
              </label>
            ))}
          </div>
          <div className="row-gap">
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setPicked(cards.map((a) => a.key))}>{t("machine.gpu.all")}</button>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setPicked([])}>{t("machine.gpu.none")}</button>
            <span className="grow" />
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setEditing(false)}>{t("common.cancel")}</button>
            <button type="button" className="btn btn-primary btn-small" disabled={busy} onClick={() => void save()}>{t("machine.save")}</button>
          </div>
          <p className="field-hint">{t("machine.gpu.hint")}</p>
        </fieldset>
      )}
    </section>
  );
}
