import { IconCpu, IconPlus, IconServer2 } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { AddMachineWays } from "../components/AddMachine";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { HolderChips, KindMark, MemLegend, StatusDot, usePlacement } from "../components/Fleet";
import { CopyBox, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact } from "../lib/format";
import {
  MACHINE_KIND, MACHINE_KIND_ORDER, byKind, cardLine, cardMemory, cards, machineKind, machineMemory, memoryLabel, memoryRows,
  share, uniqueHolders, type Placement,
} from "../lib/fleet";
import { ageSeconds, ago, bytes, toMs } from "../lib/machines";
import type { JoinToken, Machine, MachinesResponse, NewJoinToken, TlsInfo } from "../lib/types";
import { useApi } from "../lib/useApi";

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

  const closeAdd = () => {
    setAdding(false);
    setIssued(null);
  };

  const machines = data?.machines ?? null;
  const p = usePlacement(POLL_MS);
  const joinedSince = issued && machines ? machines.filter((m) => toMs(m.joined_at) >= toMs(issued.created_at)) : [];
  const waiting = canManage ? tokens.filter((t) => t.id !== issued?.id) : [];

  return (
    <>
      <PageHeader
        index="01 · Machines"
        title="Machines"
        lead="Every computer Forgeyard can run work on: what kind it is, how much memory each card has left, and what runs there. Each one runs the Forgeyard agent, which keeps a connection to this server."
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => (adding ? closeAdd() : setAdding(true))} aria-expanded={adding}>
              <IconPlus size={16} aria-hidden="true" /> Add a machine
            </button>
          )
        }
      />
      <AdvisorBanner pages={["machines"]} />
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
              <AddMachineWays issued={issued} tls={tlsInfo} />
              {isLoopback(issued.server_url) && (
                <Notice>
                  <strong>{issued.server_url}</strong> only works on this computer. For other machines, start the server with{" "}
                  <code>HOST=0.0.0.0</code> and set <code>PUBLIC_URL</code> to an address they can reach (a LAN IP or a Tailscale name),
                  or replace the address in the command (the Docker tab has a field for it).
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
          <KindSummary machines={machines} />
          <div className="tiles">
            {byKind(machines).map((m) => <Tile key={m.id} m={m} serverTime={data.server_time} p={p} />)}
          </div>
          <p className="small muted tiles-legend"><MemLegend /> · each slot is one card (or the RAM), filled from the bottom.</p>
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


// How many machines of each kind, above the tiles.
function KindSummary({ machines }: { machines: Machine[] }) {
  const offline = machines.filter((m) => m.status === "offline").length;
  const revoked = machines.filter((m) => m.status === "revoked").length;
  return (
    <ul className="kind-summary" aria-label="Kinds of machine">
      {MACHINE_KIND_ORDER.map((k) => {
        const n = machines.filter((m) => machineKind(m) === k).length;
        return n ? <li key={k}><KindMark kind={k} size={26} /> <span><strong>{n}</strong> {MACHINE_KIND[k].label}</span></li> : null;
      })}
      {offline > 0 && <li className="kind-summary-off"><StatusDot status="offline" /> <span><strong>{offline}</strong> offline</span></li>}
      {revoked > 0 && <li className="kind-summary-off"><StatusDot status="revoked" /> <span><strong>{revoked}</strong> revoked</span></li>}
    </ul>
  );
}

// One machine: its kind, its cards as slots filled from the bottom, free memory and what runs there.
function Tile({ m, serverTime, p }: { m: Machine; serverTime: string; p: Placement & { loaded: boolean } }) {
  const kind = machineKind(m);
  const here = p.onMachine(m.id);
  const rows = memoryRows(m);
  const mem = machineMemory(m, p);
  const gpuCount = cards(m).length;
  return (
    <article className={`tile tile-${m.status}`} aria-label={`Machine ${m.name}`}>
      <div className="tile-head">
        <KindMark kind={kind} size={44} />
        <div className="tile-name">
          <h2><StatusDot status={m.status} /><Link to={`/machines/${m.id}`} className="tile-link">{m.name}</Link>
            {m.status !== "online" && <span className={`chip chip-machine-${m.status}`}>{m.status}</span>}</h2>
          <span>{MACHINE_KIND[kind].label} · {cardLine(m)}</span>
        </div>
      </div>
      {rows.length > 0 && (
        <div className="slots" role="list" aria-label="Memory per card">
          {rows.map((a) => {
            const c = cardMemory(a, p.onRow(m.id, a));
            const label = a.vendor === "cpu" ? "RAM" : `#${a.index}`;
            return (
              <span key={a.key} role="listitem" className={`cslot${a.vendor === "cpu" && gpuCount ? " cslot-ram" : ""}${a.vendor !== "cpu" && a.allowed === false ? " cslot-off" : ""}`}
                title={`${label}: ${bytes(c.free)} free of ${bytes(c.total)}`}>
                <span className="slot-col" role="img" aria-label={`${label}: ${memoryLabel(c)}`}>
                  <span className="s-fy" style={{ height: share(c.forgeyard, c.total) }} />
                  <span className="s-other" style={{ height: share(c.other, c.total) }} />
                </span>
                <span className="slot-n">{label}</span>
              </span>
            );
          })}
        </div>
      )}
      <div className="tile-mem">
        <span><strong>{bytes(mem.free)}</strong> free of {bytes(mem.total)}{kind === "cpu" ? " RAM" : kind === "amd-apu" ? " (shared)" : ""}</span>
        {p.loaded && <span>{uniqueHolders(here).length} running</span>}
      </div>
      {p.loaded && <HolderChips list={here} max={5} />}
      <div className="tile-foot">
        <span title={exact(m.last_heartbeat_at)}>{heartbeatText(m, serverTime)}</span>
        {m.agent_version && <span>agent {m.agent_version}</span>}
      </div>
    </article>
  );
}

export function heartbeatText(m: Machine, serverTime: string): string {
  if (m.status === "revoked") return `revoked ${ago(ageSeconds(serverTime, m.revoked_at))}`;
  const age = ago(ageSeconds(serverTime, m.last_heartbeat_at));
  return m.status === "offline" ? `offline · last heartbeat ${age}` : `heartbeat ${age}`;
}
