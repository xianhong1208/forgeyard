// A machine's own page (1.4): its kind, a memory map of each card split by what holds it, what runs
// there, its GPU access, and the agent's details. Admins revoke or remove it here.
import { IconArrowLeft, IconLock, IconPlus } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { AgentUpdateSection, BehindChip } from "../components/AgentUpdate";
import { HolderChips, HolderIcon, KindMark, MemBar, MemLegend, StatusDot, usePlacement } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { EngineImagesSection } from "../components/ImageUpload";
import { ModelCacheSection } from "../components/ModelCache";
import { Notice } from "../components/ui";
import { api, errorText } from "../lib/api";
import {
  MACHINE_KIND, appTitle, cardLine, cardMemory, cards, machineKind, machineMemory, memoryRows, uniqueHolders, whereOnMachine, type Placement,
} from "../lib/fleet";
import { exact } from "../lib/format";
import { archLabel, bytes, replicaAddressNote } from "../lib/machines";
import { useDocumentTitle } from "../lib/title";
import type { Accelerator, Machine, MachinesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { allowlistSummary, canRunWorkloads, gpuCards, runtimeLine } from "../lib/workloads";
import { heartbeatText, POLL_MS } from "./Machines";

export function MachineDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, reload } = useApi<MachinesResponse>("/machines");
  const p = usePlacement(POLL_MS);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState !== "hidden") reload();
    }, POLL_MS);
    return () => window.clearInterval(t);
  }, [reload]);
  const m = data?.machines.find((x) => String(x.id) === id);
  useDocumentTitle(m?.name ?? "Machine");
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
    if (!window.confirm(`Revoke ${x.name}? Its agent is disconnected now and its credential stops working. To bring it back it has to join again with a new token.`)) return;
    void run(() => api(`/machines/${x.id}/revoke`, { method: "POST" }), `Revoked ${x.name}; its agent was disconnected.`);
  };
  const remove = (x: Machine) => {
    const extra = x.status === "revoked" ? "" : " Its agent is disconnected and its credential stops working.";
    if (!window.confirm(`Remove ${x.name} from the list?${extra} This cannot be undone.`)) return;
    void run(() => api(`/machines/${x.id}`, { method: "DELETE" }), `Removed ${x.name}.`, () => navigate("/machines", { replace: true }));
  };

  return (
    <>
      <p className="crumb"><Link to="/machines"><IconArrowLeft size={14} aria-hidden="true" /> Machines</Link></p>
      {error && <Notice tone="error">{error}</Notice>}
      {data && !m && <Notice tone="error">No such machine: it may have been removed.</Notice>}
      {!data && !error && <p className="muted">Loading…</p>}
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
            {m.status !== "revoked" && <button type="button" className="btn btn-quiet" onClick={() => onRevoke(m)}>Revoke</button>}
            <button type="button" className="btn btn-danger" onClick={() => onRemove(m)}>Remove</button>
          </div>
        )}
      </header>
      <AdvisorBanner pages={[`machine:${m.id}`]} />
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <p className="mk-facts">
        <span><StatusDot status={m.status} /> <strong className={`machine-state-${m.status}`}>{m.status}</strong> · <span title={exact(m.last_heartbeat_at)}>{heartbeatText(m, serverTime).replace(/^(offline|revoked) · /, "")}</span></span>
        <span><strong>{cardLine(m)}</strong></span>
        <span><strong>{bytes(mem.free)}</strong> free of {bytes(mem.total)}{kind === "cpu" ? " RAM" : kind === "amd-apu" ? " (shared)" : ""}</span>
        {m.agent_version && <span>agent {m.agent_version}{platform ? ` · ${platform}` : ""}</span>}
        <BehindChip m={m} />
        {m.simulated && (
          <span className="chip chip-simulated" title="This agent reports made-up hardware from a fixture (development and tests only).">simulated inventory</span>
        )}
        {Object.entries(m.labels).map(([k, v]) => <span key={k} className="chip">{k}={v}</span>)}
      </p>

      <section className="panel mk-section" aria-labelledby="mk-models">
        <div className="panel-head">
          <h2 id="mk-models">Models</h2>
          {canPlace && <Link to={`/serving/deploy?machine=${m.id}${kind === "cpu" ? "&device=cpu" : ""}`} className="btn btn-small"><IconPlus size={14} aria-hidden="true" /> Deploy here</Link>}
        </div>
        {p.loaded ? <HolderChips list={models} empty="No model runs on this machine." /> : <p className="muted small">Loading…</p>}
      </section>

      <section className="panel mk-section" aria-labelledby="mk-map">
        <div className="panel-head"><h2 id="mk-map">Memory map</h2><MemLegend /></div>
        {memoryRows(m).length === 0 ? <p className="muted small">The agent reported no cards and no memory.</p> : (
          <div className="memmap">
            {memoryRows(m).map((a) => <MapRow key={a.key} m={m} a={a} p={p} restricted={m.gpu_allowlist !== null && m.gpu_allowlist !== undefined} />)}
          </div>
        )}
      </section>

      <section className="panel mk-section" aria-labelledby="mk-running">
        <div className="panel-head">
          <h2 id="mk-running">Running here <span className="muted small">· {uniqueHolders(here).length}</span></h2>
        </div>
        {!p.loaded ? <p className="muted small">Loading…</p> : here.length === 0 ? <p className="empty-line">Nothing runs here yet.</p> : (
          <ul className="runlist">
            {uniqueHolders(here).map((h) => {
              const all = here.filter((x) => x.key === h.key);
              return (
                <li key={h.key}>
                  <span className="runlist-k"><HolderIcon h={h} size={16} /></span>
                  <span className="runlist-name">
                    {h.href ? <Link to={h.href}>{h.name}</Link> : <span className="strong" title={appTitle(h.name)}>{h.name}</span>}
                    <small>{h.kind === "workload" ? `app · ${h.workload.kind} · ${h.workload.runtime}` : h.deployment?.model.name}</small>
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
        <h2 id="mk-agent">Agent</h2>
        <dl className="facts">
          <div className="fact"><dt>Version</dt><dd>{m.agent_version ?? "unknown"}{m.protocol ? <span className="muted"> · protocol {m.protocol}</span> : null}</dd></div>
          {platform && <div className="fact"><dt>Platform</dt><dd>{platform}</dd></div>}
          {m.hostname && <div className="fact"><dt>Host name</dt><dd className="mono">{m.hostname}</dd></div>}
          {m.ip && <div className="fact"><dt>Address</dt><dd className="mono">{m.ip}</dd></div>}
          {m.runtime_info?.address && (
            <div className="fact">
              <dt>Replicas at</dt>
              <dd><span className="mono">{m.runtime_info.address}</span>{replicaAddressNote(m) && <span className="muted"> · {replicaAddressNote(m)}</span>}</dd>
            </div>
          )}
          {drivers.length > 0 && <div className="fact"><dt>{kind === "nvidia" ? "Driver" : "ROCm"}</dt><dd>{drivers.map((d) => (kind === "nvidia" ? `driver ${d}` : `ROCm ${d}`)).join(", ")}</dd></div>}
          <div className="fact"><dt>Joined</dt><dd title={exact(m.joined_at)}>{exact(m.joined_at)}</dd></div>
        </dl>
        {failed.length > 0 && (
          <details className="detection">
            <summary>Detection notes</summary>
            <ul>{failed.map(([name, s]) => <li key={name}><span className="mono">{name}</span>: {s.reason}</li>)}</ul>
          </details>
        )}
      </section>

      <AgentUpdateSection m={m} canManage={canManage} />
      <HelpLinks>
        <HelpLink page="how-to" anchor="restrict-gpus-on-a-shared-machine">Restrict GPUs on a shared machine</HelpLink>
        <HelpLink page="how-to" anchor="free-disk-space-on-a-machine">Free disk space on a machine</HelpLink>
        <HelpLink page="how-to" anchor="upload-an-engine-image-to-a-machine">Upload an engine image to a machine</HelpLink>
        <HelpLink page="troubleshooting" anchor="an-agent-shows-offline">An agent shows offline</HelpLink>
      </HelpLinks>
    </>
  );
}

// One card (or the RAM): its name and architecture, a bar split by what holds it, the holders with
// their reservation, and what is free; load and temperature on the right.
function MapRow({ m, a, p, restricted }: { m: Machine; a: Accelerator; p: Placement; restricted: boolean }) {
  const held = p.onRow(m.id, a);
  const c = cardMemory(a, held);
  const cpu = a.vendor === "cpu";
  const arch = archLabel(a);
  const label = cpu ? "RAM" : `GPU #${a.index}`;
  const sub = cpu
    ? [a.detail.cores ? `${a.detail.cores} cores` : null, a.detail.threads ? `${a.detail.threads} threads` : null].filter(Boolean).join(" · ") || a.model
    : `${a.model.replace(/^(NVIDIA|AMD) /, "")} · ${arch.main}${arch.sub ? ` ${arch.sub}` : ""}`;
  return (
    <div className={`maprow${!cpu && restricted && a.allowed === false ? " maprow-off" : ""}`} role="group" aria-label={label}>
      <span className="maprow-id">
        <strong>{label}</strong>
        <span title={cpu ? a.model : a.model}>{sub}</span>
        <span className="maprow-tags">
          {a.unified_memory && <span className="chip chip-unified" title="Shares system memory with the CPU. The size shown is the larger of the VRAM carve-out and the GTT pool.">unified memory</span>}
          {!cpu && a.assigned_to && held.length === 0 && <span className="chip chip-tone-run">in use: {a.assigned_to}</span>}
          {!cpu && restricted && a.allowed === false && <span className="chip chip-off" title="Not on this machine's allowlist: Forgeyard never assigns it."><IconLock size={11} aria-hidden="true" /> not for Forgeyard</span>}
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
        <span><strong>{bytes(c.free)}</strong> free</span>
        <span className="muted">of {bytes(c.total)}</span>
        <span className="mono muted" title="Utilisation and temperature">
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
      ? `Let Forgeyard assign every GPU of ${m.name}?`
      : picked.length
        ? `Let Forgeyard assign only GPU ${names} on ${m.name}? No model or app will ever get another card of this machine.`
        : `Let Forgeyard assign no GPU on ${m.name}? Only models and apps on the CPU will run there.`;
    if (!window.confirm(text)) return;
    setBusy(true);
    try {
      await api(`/machines/${m.id}/gpu-allowlist`, { method: "PUT", body: { keys: all ? null : picked } });
      setEditing(false);
      onChanged(all ? `Forgeyard may assign every GPU of ${m.name}.` : `Forgeyard may assign ${picked.length ? `only ${names}` : "no GPU"} on ${m.name}.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel mk-section gpu-access" aria-labelledby="mk-access">
      <h2 id="mk-access">GPU access and runtimes</h2>
      <p className="gpu-access-line">
        {cards.length > 0 && (
          <span className={m.gpu_allowlist ? "gpu-access-limited" : undefined}>
            {m.gpu_allowlist && <IconLock size={14} aria-hidden="true" />} Forgeyard may assign <strong>{allowlistSummary(m)}</strong>
          </span>
        )}
        <span className="muted">
          {workloads
            ? `Runs ${runtimeLine(runtimes.process, "process")} · ${runtimeLine(runtimes.container, "container")}`
            : "Its agent does not run apps yet (update it)."}
        </span>
        {canManage && cards.length > 0 && !editing && (
          <button type="button" className="btn btn-quiet btn-small" onClick={open}>Change GPU access</button>
        )}
      </p>
      {editing && (
        <fieldset className="allowlist">
          <legend>GPU access on {m.name}: the GPUs Forgeyard may assign</legend>
          <div className="allowlist-cards">
            {cards.map((a) => (
              <label key={a.key} className="card-pick">
                <input type="checkbox" checked={picked.includes(a.key)}
                  onChange={() => setPicked((p) => (p.includes(a.key) ? p.filter((k) => k !== a.key) : [...p, a.key]))} />
                <span className="mono">#{a.index}</span>
                <span className="small">{a.model}</span>
                {a.assigned_to && <span className="chip chip-tone-run">in use: {a.assigned_to}</span>}
              </label>
            ))}
          </div>
          <div className="row-gap">
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setPicked(cards.map((a) => a.key))}>All</button>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setPicked([])}>None</button>
            <span className="grow" />
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setEditing(false)}>Cancel</button>
            <button type="button" className="btn btn-primary btn-small" disabled={busy} onClick={() => void save()}>Save</button>
          </div>
          <p className="field-hint">A card in use cannot be taken off; stop the model or app on it first. Every change is in the audit log.</p>
        </fieldset>
      )}
    </section>
  );
}
