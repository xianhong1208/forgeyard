import { IconCpu, IconLock, IconPlus, IconServer2 } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import { AddMachineWays } from "../components/AddMachine";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBox, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact } from "../lib/format";
import {
  VENDOR_NAME,
  ageSeconds,
  ago,
  archLabel,
  bytes,
  cardSummary,
  gpus,
  memoryTotals,
  toMs,
  usedFraction,
} from "../lib/machines";
import type { Accelerator, JoinToken, Machine, MachinesResponse, NewJoinToken, TlsInfo } from "../lib/types";
import { useApi } from "../lib/useApi";
import { allowlistSummary, canRunWorkloads, gpuCards, runtimeLine } from "../lib/workloads";

// Machines change state on their own (heartbeats, a dropped network): poll so the page follows.
export const POLL_MS = 5000;

const KINDS = [
  { icon: IconServer2, name: "NVIDIA", detail: "CUDA cards, read through NVML" },
  { icon: IconServer2, name: "AMD", detail: "ROCm cards and APUs, read through AMD SMI" },
  { icon: IconCpu, name: "CPU only", detail: "Machines without a GPU" },
];

function isLoopback(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host === "[::1]" || host.startsWith("127.");
  } catch {
    return false;
  }
}

export function MachinesPage() {
  const { data, error: loadError, reload } = useApi<MachinesResponse>("/machines");
  const canManage = data?.can_manage ?? false;
  // For admins: the one-time command for agents that joined before the server used HTTPS.
  const { data: tlsInfo } = useApi<TlsInfo>("/tls");
  const [tokens, setTokens] = useState<JoinToken[]>([]);
  const reloadTokens = useCallback(() => {
    if (!canManage) return;
    api<{ tokens: JoinToken[] }>("/machines/join-tokens").then(
      (r) => setTokens(r.tokens),
      () => undefined, // the list of unused tokens is a convenience; the page works without it
    );
  }, [canManage]);
  const [adding, setAdding] = useState(false);
  const [issued, setIssued] = useState<NewJoinToken | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    reloadTokens();
    const id = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      reload();
      reloadTokens();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [reload, reloadTokens]);

  const createToken = async () => {
    setBusy(true);
    setMessage(null);
    try {
      setIssued(await api<NewJoinToken>("/machines/join-tokens", { method: "POST" }));
      reloadTokens();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      reload();
      reloadTokens();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  const revoke = (m: Machine) => {
    if (!window.confirm(`Revoke ${m.name}? Its agent is disconnected now and its credential stops working. To bring it back it has to join again with a new token.`)) return;
    void run(() => api(`/machines/${m.id}/revoke`, { method: "POST" }), `Revoked ${m.name}; its agent was disconnected.`);
  };

  const remove = (m: Machine) => {
    const extra = m.status === "revoked" ? "" : " Its agent is disconnected and its credential stops working.";
    if (!window.confirm(`Remove ${m.name} from the list?${extra} This cannot be undone.`)) return;
    void run(() => api(`/machines/${m.id}`, { method: "DELETE" }), `Removed ${m.name}.`);
  };

  const closeAdd = () => {
    setAdding(false);
    setIssued(null);
  };

  const machines = data?.machines ?? null;
  const joinedSince = issued && machines ? machines.filter((m) => toMs(m.joined_at) >= toMs(issued.created_at)) : [];
  const waiting = canManage ? tokens.filter((t) => t.id !== issued?.id) : [];

  return (
    <>
      <PageHeader
        index="01 · Machines"
        title="Machines"
        lead="Every computer Forgeyard can run work on, with its cards, memory and load. Each one runs the Forgeyard agent, which keeps a connection to this server."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => (adding ? closeAdd() : setAdding(true))} aria-expanded={adding}>
              <IconPlus size={16} aria-hidden="true" /> Add a machine
            </button>
          )
        }
      />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="3-add-a-machine">How do I add a machine?</HelpLink>
        <HelpLink page="how-to" anchor="restrict-gpus-on-a-shared-machine">Restrict GPUs on a shared machine</HelpLink>
        <HelpLink page="troubleshooting" anchor="an-agent-shows-offline">An agent shows offline</HelpLink>
      </HelpLinks>
      {loadError && <Notice tone="error">{loadError}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      {adding && (
        <section className="panel panel-link add-machine" aria-labelledby="add-machine-title">
          <h2 id="add-machine-title">{issued ? "Run this on the new machine" : "Add a machine"}</h2>
          {!issued ? (
            <>
              <ol className="steps">
                <li>Create a join command here. It works once, within 15 minutes, and is shown only once.</li>
                <li>
                  Run it on the new machine: a <strong>one-line install</strong> that sets the agent up as a service, a{" "}
                  <strong>Docker</strong> container, or the agent from a Forgeyard checkout with a config file.
                </li>
                <li>The machine appears below within seconds, and reconnects by itself after a restart or a network drop.</li>
              </ol>
              <div className="row-gap">
                <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void createToken()}>
                  {busy ? "Creating…" : "Create join command"}
                </button>
                <button type="button" className="btn btn-quiet" onClick={closeAdd}>Cancel</button>
              </div>
            </>
          ) : (
            <>
              <p>
                Works once and expires at <strong>{new Date(issued.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</strong>{" "}
                ({issued.valid_minutes} minutes). It is shown only now; the server keeps just a hash.
              </p>
              <AddMachineWays issued={issued} />
              {isLoopback(issued.server_url) && (
                <Notice>
                  <strong>{issued.server_url}</strong> only works on this computer. For other machines, start the server with{" "}
                  <code>HOST=0.0.0.0</code> and set <code>PUBLIC_URL</code> to an address they can reach (a LAN IP or a Tailscale name),
                  or replace the address in the command.
                </Notice>
              )}
              {issued.ca_fingerprint ? (
                <p className="muted small">
                  The agent connects over <code>wss://</code> and trusts only the certificate authority with the fingerprint in the
                  command (<code className="fingerprint">{issued.ca_fingerprint.slice(0, 23)}…</code>): a machine in between with another
                  certificate is refused.
                </p>
              ) : issued.server_url.startsWith("http://") ? (
                <p className="muted small">
                  This server speaks plain HTTP (<code>TLS=off</code>): the agent's connection is not encrypted. Use it only behind a TLS
                  proxy whose address is in <code>PUBLIC_URL</code>, or on a trusted network.
                </p>
              ) : null}
              {issued.trust_command && (
                <details className="trust-upgrade">
                  <summary className="small">A machine joined before this server used HTTPS?</summary>
                  <p className="small">Its agent stays offline until you run this once on it, then restart the agent. It keeps its credential.</p>
                  <CopyBox value={issued.trust_command} label="Trust command" />
                </details>
              )}
              {joinedSince.length > 0 ? (
                <Notice tone="ok">{joinedSince.map((m) => m.name).join(", ")} joined.</Notice>
              ) : (
                <p className="waiting small" role="status">
                  <span className="pulse" aria-hidden="true" /> Waiting for the machine to join…
                </p>
              )}
              <button type="button" className="btn btn-quiet" onClick={closeAdd}>Done</button>
            </>
          )}
        </section>
      )}

      {canManage && waiting.length > 0 && (
        <div className="tokens-line small">
          {waiting.length} unused join token{waiting.length > 1 ? "s" : ""}:
          {waiting.map((t) => (
            <span key={t.id} className="token-chip">
              expires {new Date(t.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              <button type="button" className="btn btn-quiet btn-small"
                onClick={() => void run(() => api(`/machines/join-tokens/${t.id}`, { method: "DELETE" }), "Join token cancelled.")}>
                Cancel
              </button>
            </span>
          ))}
        </div>
      )}

      {canManage && tlsInfo?.admin?.trust_command && (machines ?? []).some((m) => m.status === "offline") && (
        <details className="trust-upgrade trust-offline">
          <summary className="small">Machines offline since this server switched to HTTPS?</summary>
          <p className="small">
            An agent that joined over plain HTTP keeps trying <code>http://</code> and stays offline. Run this once on its machine, in the
            Forgeyard folder, then restart the agent. It keeps its credential: no new join token.
          </p>
          <CopyBox value={tlsInfo.admin.trust_command} label="Trust command" />
        </details>
      )}

      {machines && machines.length === 0 && <EmptyMachines canManage={canManage} adding={adding} onAdd={() => setAdding(true)} />}
      {machines && machines.length > 0 && data && (
        <>
          <Fleet machines={machines} />
          <div className="machines">
            {machines.map((m) => (
              <MachineCard key={m.id} m={m} serverTime={data.server_time} canManage={canManage}
                onRevoke={revoke} onRemove={remove} onChanged={(text) => { setMessage({ tone: "ok", text }); reload(); }}
                onError={(text) => setMessage({ tone: "error", text })} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function EmptyMachines({ canManage, adding, onAdd }: { canManage: boolean; adding: boolean; onAdd: () => void }) {
  return (
    <section className="empty">
      <div className="empty-art" aria-hidden="true">
        {Array.from({ length: 12 }, (_, i) => (
          <span key={i} className={`slot${i === 5 ? " slot-hot" : ""}`} />
        ))}
      </div>
      <div className="empty-copy">
        <h2>No machines yet</h2>
        <p>
          Forgeyard runs models and workloads on machines you add: GPU servers, workstations, or computers with only a CPU.
          Each one runs the Forgeyard agent, which reports its hardware and keeps a connection to this server.{" "}
          {canManage
            ? "To add one, create a join command here and paste it on the machine: one line installs the agent as a service (or run it in Docker)."
            : "An admin adds machines; ask one to add yours."}
        </p>
        {canManage && !adding && (
          <p className="empty-actions">
            <button type="button" className="btn btn-primary" onClick={onAdd}><IconPlus size={16} aria-hidden="true" /> Add a machine</button>
          </p>
        )}
        <ul className="kinds">
          {KINDS.map(({ icon: Icon, name, detail }) => (
            <li key={name}>
              <Icon size={18} stroke={1.6} aria-hidden="true" />
              <span className="kind-name">{name}</span>
              <span className="kind-detail">{detail}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Fleet({ machines }: { machines: Machine[] }) {
  const online = machines.filter((m) => m.status === "online");
  const offline = machines.filter((m) => m.status === "offline").length;
  const revoked = machines.filter((m) => m.status === "revoked").length;
  const cards = online.flatMap(gpus);
  const mem = memoryTotals(cards);
  return (
    <dl className="fleet" aria-label="Fleet">
      <div><dt>Machines</dt><dd>{machines.length}</dd></div>
      <div><dt>Online</dt><dd className="fleet-online">{online.length}</dd></div>
      <div><dt>Offline</dt><dd>{offline}{revoked > 0 && <span className="muted small"> + {revoked} revoked</span>}</dd></div>
      <div><dt>GPUs online</dt><dd>{cards.length}</dd></div>
      <div><dt>GPU memory free</dt><dd>{bytes(mem.free)} <span className="muted small">of {bytes(mem.total)}</span></dd></div>
    </dl>
  );
}

function MachineCard({ m, serverTime, canManage, onRevoke, onRemove, onChanged, onError }: {
  m: Machine; serverTime: string; canManage: boolean;
  onRevoke: (m: Machine) => void; onRemove: (m: Machine) => void;
  onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const age = ageSeconds(serverTime, m.last_heartbeat_at);
  const cards = gpus(m);
  const mem = memoryTotals(cards);
  const platform = [m.platform.distro || m.platform.os, m.platform.arch].filter(Boolean).join(" · ");
  const failed = Object.entries(m.collectors).filter(([, s]) => !s.ok && s.reason);
  return (
    <article className={`machine machine-${m.status}`} aria-label={`Machine ${m.name}`}>
      <header className="machine-head">
        <div className="machine-title">
          <span className={`dot dot-${m.status}`} aria-hidden="true" />
          <h2 className="mono">{m.name}</h2>
          <span className={`chip chip-machine-${m.status}`}>{m.status}</span>
          {m.simulated && (
            <span className="chip chip-simulated" title="This agent reports made-up hardware from a fixture (development and tests only).">
              simulated inventory
            </span>
          )}
          {Object.entries(m.labels).map(([k, v]) => (
            <span key={k} className="chip">{k}={v}</span>
          ))}
        </div>
        {canManage && (
          <div className="machine-actions">
            {m.status !== "revoked" && (
              <button type="button" className="btn btn-quiet btn-small" onClick={() => onRevoke(m)}>Revoke</button>
            )}
            <button type="button" className="btn btn-danger btn-small" onClick={() => onRemove(m)}>Remove</button>
          </div>
        )}
      </header>
      <p className="machine-meta">
        {m.status === "revoked" ? (
          <span title={exact(m.revoked_at)}>Revoked {ago(ageSeconds(serverTime, m.revoked_at))}{m.revoked_by ? ` by ${m.revoked_by}` : ""}</span>
        ) : m.status === "offline" ? (
          <span className="meta-offline" title={exact(m.last_heartbeat_at)}>
            Offline · last heartbeat {ago(age)}
          </span>
        ) : (
          <span title={exact(m.last_heartbeat_at)}>Heartbeat {ago(age)}</span>
        )}
        {m.agent_version && <span>agent {m.agent_version}</span>}
        {platform && <span>{platform}</span>}
        {m.ip && <span className="mono">{m.ip}</span>}
      </p>
      <p className="machine-summary">
        <strong>{cardSummary(m)}</strong>
        {cards.length > 0 && (
          <span className="muted"> · {bytes(mem.free)} free of {bytes(mem.total)}</span>
        )}
      </p>
      {m.status !== "revoked" && (
        <GpuAccess m={m} canManage={canManage} onChanged={onChanged} onError={onError} />
      )}
      <ul className="accels">
        {m.accelerators.map((a) => (
          <AcceleratorRow key={a.key} a={a} restricted={m.gpu_allowlist !== null && m.gpu_allowlist !== undefined} />
        ))}
      </ul>
      {failed.length > 0 && (
        <details className="detection">
          <summary>Detection notes</summary>
          <ul>
            {failed.map(([name, s]) => (
              <li key={name}><span className="mono">{name}</span>: {s.reason}</li>
            ))}
          </ul>
        </details>
      )}
    </article>
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
        ? `Let Forgeyard assign only GPU ${names} on ${m.name}? No workload will ever get another card of this machine.`
        : `Let Forgeyard assign no GPU on ${m.name}? Only CPU workloads will run there.`;
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
    <div className="gpu-access">
      <p className="gpu-access-line">
        {cards.length > 0 && (
          <span className={m.gpu_allowlist ? "gpu-access-limited" : undefined}>
            {m.gpu_allowlist && <IconLock size={14} aria-hidden="true" />} Forgeyard may assign <strong>{allowlistSummary(m)}</strong>
          </span>
        )}
        <span className="muted">
          {workloads
            ? `Runs ${runtimeLine(runtimes.process, "process")} · ${runtimeLine(runtimes.container, "container")}`
            : "Its agent does not run workloads yet (update it)."}
        </span>
        {canManage && cards.length > 0 && !editing && (
          <button type="button" className="btn btn-quiet btn-small" onClick={open}>Change GPU access</button>
        )}
      </p>
      {editing && (
        <fieldset className="allowlist">
          <legend>GPUs Forgeyard may assign on {m.name}</legend>
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
          <p className="field-hint">A card in use cannot be taken off; stop its workload first. Every change is in the audit log.</p>
        </fieldset>
      )}
    </div>
  );
}

function AcceleratorRow({ a, restricted }: { a: Accelerator; restricted: boolean }) {
  const frac = usedFraction(a);
  const arch = archLabel(a);
  const cpu = a.vendor === "cpu";
  const tight = frac !== null && frac >= 0.9;
  const cores = cpu ? [a.detail.cores && `${a.detail.cores} cores`, a.detail.threads && `${a.detail.threads} threads`].filter(Boolean).join(" · ") : "";
  return (
    <li className={`accel accel-${a.vendor}${restricted && !cpu && a.allowed === false ? " accel-off" : ""}`}>
      <span className="accel-id">
        <span className={`vendor vendor-${a.vendor}`}>{VENDOR_NAME[a.vendor] ?? a.vendor}</span>
        {!cpu && <span className="accel-index mono">#{a.index}</span>}
      </span>
      <span className="accel-model">
        <span className="accel-name">{a.model}</span>
        <span className="accel-sub">
          <span className="mono" title={a.vendor === "nvidia" ? "CUDA compute capability" : a.vendor === "amd" ? "gfx target" : "Machine architecture"}>
            {arch.main}{arch.sub && <span className="muted"> {arch.sub}</span>}
          </span>
          {a.unified_memory && (
            <span className="chip chip-unified" title="Shares system memory with the CPU. The size shown is the larger of the VRAM carve-out and the GTT pool.">
              unified memory
            </span>
          )}
          {cpu && cores && <span className="muted">{cores}</span>}
          {!cpu && restricted && a.allowed === false && (
            <span className="chip chip-off" title="Not on this machine's allowlist: Forgeyard never assigns it.">not for Forgeyard</span>
          )}
          {!cpu && a.assigned_to && <span className="chip chip-tone-run">in use: {a.assigned_to}</span>}
          {a.driver && <span className="muted">{a.vendor === "amd" ? "ROCm" : "driver"} {a.driver}</span>}
        </span>
      </span>
      <span className="accel-mem">
        <span className="mem-text">
          <strong>{bytes(a.vram_free)}</strong> free <span className="muted">of {bytes(a.vram_total)}{cpu ? " RAM" : ""}</span>
        </span>
        <span className={`bar${tight ? " bar-tight" : ""}`} role="meter" aria-label={`${a.model} memory used`}
          aria-valuemin={0} aria-valuemax={100} aria-valuenow={frac === null ? undefined : Math.round(frac * 100)}>
          <span style={{ width: `${frac === null ? 0 : Math.round(frac * 100)}%` }} />
        </span>
      </span>
      <span className="accel-live mono">
        <span title="Utilisation">{a.utilization === null ? "—" : `${a.utilization}%`}</span>
        <span title="Temperature">{a.temperature === null ? "" : `${a.temperature}°C`}</span>
      </span>
    </li>
  );
}
