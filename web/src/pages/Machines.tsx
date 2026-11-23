import { IconCpu, IconPlus, IconServer2 } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";

import { BehindChip } from "../components/AgentUpdate";
import { AdvisorBanner } from "../components/Advisor";
import { AddMachineWays } from "../components/AddMachine";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { HolderChips, KindMark, MemLegend, StatusDot, usePlacement } from "../components/Fleet";
import { CopyBox, Notice, PageHeader } from "../components/ui";
import { t as tNow } from "../i18n/core";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { exact } from "../lib/format";
import {
  MACHINE_KIND, MACHINE_KIND_ORDER, byKind, machineCount, cardLine, cardMemory, cards, machineKind, machineMemory, memoryLabel, memoryRows,
  share, uniqueHolders, type Placement,
} from "../lib/fleet";
import { ageSeconds, ago, bytes, toMs } from "../lib/machines";
import type { JoinToken, Machine, MachinesResponse, NewJoinToken, TlsInfo } from "../lib/types";
import { useApi } from "../lib/useApi";
import { AsleepHereLine } from "../components/Sleep";

// Machines change state on their own (heartbeats, a dropped network): poll so the page follows.
export const POLL_MS = 5000;

const KINDS = [
  { icon: IconServer2, name: "NVIDIA", kind: "nvidia" },
  { icon: IconServer2, name: "AMD", kind: "amd" },
  { icon: IconCpu, name: "", kind: "cpu" },
] as const;

function isLoopback(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host === "[::1]" || host.startsWith("127.");
  } catch {
    return false;
  }
}

export function MachinesPage() {
  const t = useT();
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
        index={t("machines.index")}
        title={t("machines.title")}
        lead={t("machines.lead")}
        actions={
          canManage && (
            <button type="button" className="btn btn-primary" onClick={() => (adding ? closeAdd() : setAdding(true))} aria-expanded={adding}>
              <IconPlus size={16} aria-hidden="true" /> {t("machines.add")}
            </button>
          )
        }
      />
      <AdvisorBanner pages={["machines"]} />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="3-add-a-machine">{t("machines.help.add")}</HelpLink>
        <HelpLink page="how-to" anchor="restrict-gpus-on-a-shared-machine">{t("machines.help.restrict")}</HelpLink>
        <HelpLink page="troubleshooting" anchor="an-agent-shows-offline">{t("machines.help.offline")}</HelpLink>
      </HelpLinks>
      {loadError && <Notice tone="error">{loadError}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      {adding && (
        <section className="panel panel-link add-machine" aria-labelledby="add-machine-title">
          <h2 id="add-machine-title">{issued ? t("machines.runThis") : t("machines.add")}</h2>
          {!issued ? (
            <>
              <ol className="steps">
                <li>{t("machines.step1")}</li>
                <li>{t.rich("machines.step2", { strong: (text) => <strong>{text}</strong> })}</li>
                <li>{t("machines.step3")}</li>
              </ol>
              <div className="row-gap">
                <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void createToken()}>
                  {busy ? t("machines.creating") : t("machines.create")}
                </button>
                <button type="button" className="btn btn-quiet" onClick={closeAdd}>{t("common.cancel")}</button>
              </div>
            </>
          ) : (
            <>
              <p>{t.rich("machines.expires", { time: <strong>{t.time(issued.expires_at)}</strong>, minutes: issued.valid_minutes })}</p>
              <AddMachineWays issued={issued} tls={tlsInfo} />
              {isLoopback(issued.server_url) && (
                <Notice>
                  {t.rich("machines.loopback", { url: <strong>{issued.server_url}</strong>, host: <code>HOST=0.0.0.0</code>, public: <code>PUBLIC_URL</code> })}
                </Notice>
              )}
              {issued.ca_fingerprint ? (
                <p className="muted small">
                  {t.rich("machines.pinned", { wss: <code>wss://</code>, fingerprint: <code className="fingerprint">{issued.ca_fingerprint.slice(0, 23)}…</code> })}
                </p>
              ) : issued.server_url.startsWith("http://") ? (
                <p className="muted small">{t.rich("machines.plain", { off: <code>TLS=off</code>, public: <code>PUBLIC_URL</code> })}</p>
              ) : null}
              {issued.trust_command && (
                <details className="trust-upgrade">
                  <summary className="small">{t("machines.joinedBefore")}</summary>
                  <p className="small">{t("machines.joinedBeforeDetail")}</p>
                  <CopyBox value={issued.trust_command} label={t("machines.trustCommand")} />
                </details>
              )}
              {joinedSince.length > 0 ? (
                <Notice tone="ok">{t("machines.joined", { names: joinedSince.map((m) => m.name).join(", ") })}</Notice>
              ) : (
                <p className="waiting small" role="status">
                  <span className="pulse" aria-hidden="true" /> {t("machines.waiting")}
                </p>
              )}
              <button type="button" className="btn btn-quiet" onClick={closeAdd}>{t("common.done")}</button>
            </>
          )}
        </section>
      )}

      {canManage && waiting.length > 0 && (
        <div className="tokens-line small">
          {t.plural("machines.unused", waiting.length)}
          {waiting.map((tok) => (
            <span key={tok.id} className="token-chip">
              {t("machines.tokenExpires", { time: t.time(tok.expires_at) })}
              <button type="button" className="btn btn-quiet btn-small"
                onClick={() => void run(() => api(`/machines/join-tokens/${tok.id}`, { method: "DELETE" }), t("machines.tokenCancelled"))}>
                {t("common.cancel")}
              </button>
            </span>
          ))}
        </div>
      )}

      {canManage && tlsInfo?.admin?.trust_command && (machines ?? []).some((m) => m.status === "offline") && (
        <details className="trust-upgrade trust-offline">
          <summary className="small">{t("machines.offlineSinceHttps")}</summary>
          <p className="small">{t.rich("machines.offlineSinceHttpsDetail", { http: <code>http://</code> })}</p>
          <CopyBox value={tlsInfo.admin.trust_command} label={t("machines.trustCommand")} />
        </details>
      )}

      {machines && machines.length === 0 && <EmptyMachines canManage={canManage} adding={adding} onAdd={() => setAdding(true)} />}
      {machines && machines.length > 0 && data && (
        <>
          <KindSummary machines={machines} />
          <div className="tiles">
            {byKind(machines).map((m) => <Tile key={m.id} m={m} serverTime={data.server_time} p={p} />)}
          </div>
          <p className="small muted tiles-legend"><MemLegend /> · {t("machines.legend")}</p>
        </>
      )}
    </>
  );
}

function EmptyMachines({ canManage, adding, onAdd }: { canManage: boolean; adding: boolean; onAdd: () => void }) {
  const t = useT();
  return (
    <section className="empty">
      <div className="empty-art" aria-hidden="true">
        {Array.from({ length: 12 }, (_, i) => (
          <span key={i} className={`slot${i === 5 ? " slot-hot" : ""}`} />
        ))}
      </div>
      <div className="empty-copy">
        <h2>{t("machines.empty.title")}</h2>
        <p>
          {t("machines.empty.body")}{" "}
          {canManage ? t("machines.empty.admin") : t("machines.empty.user")}
        </p>
        {canManage && !adding && (
          <p className="empty-actions">
            <button type="button" className="btn btn-primary" onClick={onAdd}><IconPlus size={16} aria-hidden="true" /> {t("machines.add")}</button>
          </p>
        )}
        <ul className="kinds">
          {KINDS.map(({ icon: Icon, name, kind }) => (
            <li key={kind}>
              <Icon size={18} stroke={1.6} aria-hidden="true" />
              <span className="kind-name">{name || t("machines.docker.cpuOnly")}</span>
              <span className="kind-detail">{t(`machines.kind.${kind}`)}</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}


// How many machines of each kind, above the tiles.
function KindSummary({ machines }: { machines: Machine[] }) {
  const t = useT();
  const offline = machines.filter((m) => m.status === "offline").length;
  const revoked = machines.filter((m) => m.status === "revoked").length;
  const kinds = machines.map(machineKind);
  return (
    <ul className="kind-summary" aria-label={t("machines.kinds")}>
      {MACHINE_KIND_ORDER.map((k) => {
        const n = kinds.filter((x) => x === k).length;
        return n ? <li key={k}><KindMark kind={k} size={26} /> <span><strong>{t.num(n)}</strong> {machineCount(k, n, kinds)}</span></li> : null;
      })}
      {offline > 0 && <li className="kind-summary-off"><StatusDot status="offline" /> <span><strong>{t.num(offline)}</strong> {t("machines.offlineCount")}</span></li>}
      {revoked > 0 && <li className="kind-summary-off"><StatusDot status="revoked" /> <span><strong>{t.num(revoked)}</strong> {t("machines.revokedCount")}</span></li>}
    </ul>
  );
}

// One machine: its kind, its cards as slots filled from the bottom, free memory and what runs there.
function Tile({ m, serverTime, p }: { m: Machine; serverTime: string; p: Placement & { loaded: boolean } }) {
  const t = useT();
  const kind = machineKind(m);
  const here = p.onMachine(m.id);
  const rows = memoryRows(m);
  const mem = machineMemory(m, p);
  const gpuCount = cards(m).length;
  return (
    <article className={`tile tile-${m.status}`} aria-label={t("machines.tile", { name: m.name })}>
      <div className="tile-head">
        <KindMark kind={kind} size={44} />
        <div className="tile-name">
          <h2><StatusDot status={m.status} /><Link to={`/machines/${m.id}`} className="tile-link">{m.name}</Link>
            {m.status !== "online" && <span className={`chip chip-machine-${m.status}`}>{t.maybe(`fleet.machineStatus.${m.status}`, m.status)}</span>}</h2>
          <span>{MACHINE_KIND[kind].label} · {cardLine(m)}</span>
        </div>
      </div>
      {rows.length > 0 && (
        <div className="slots" role="list" aria-label={t("machines.memoryPerCard")}>
          {rows.map((a) => {
            const c = cardMemory(a, p.onRow(m.id, a));
            const label = a.vendor === "cpu" ? t("machines.ram") : `#${a.index}`;
            return (
              <span key={a.key} role="listitem" className={`cslot${a.vendor === "cpu" && gpuCount ? " cslot-ram" : ""}${a.vendor !== "cpu" && a.allowed === false ? " cslot-off" : ""}`}
                title={t("machines.slotTitle", { label, free: bytes(c.free), total: bytes(c.total) })}>
                <span className="slot-col" role="img" aria-label={t("machines.slotLabel", { label, memory: memoryLabel(c) })}>
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
        <span>{t.rich(kind === "cpu" ? "machines.freeRam" : kind === "amd-apu" ? "machines.freeShared" : "machines.free",
          { free: bytes(mem.free), total: bytes(mem.total), strong: (text) => <strong>{text}</strong> })}</span>
        {p.loaded && <span>{t("machines.running", { count: uniqueHolders(here).length })}</span>}
      </div>
      {p.loaded && <HolderChips list={here} max={5} />}
      {p.loaded && <AsleepHereLine list={p.asleep(m.id)} compact />}
      <div className="tile-foot">
        <span title={exact(m.last_heartbeat_at)}>{heartbeatText(m, serverTime)}</span>
        {m.agent_behind ? <BehindChip m={m} /> : m.agent_version && <span>{t("machines.agent", { version: m.agent_version })}</span>}
      </div>
    </article>
  );
}

export function heartbeatText(m: Machine, serverTime: string): string {
  if (m.status === "revoked") return tNow("machines.revokedAgo", { ago: ago(ageSeconds(serverTime, m.revoked_at)) });
  const age = ago(ageSeconds(serverTime, m.last_heartbeat_at));
  return m.status === "offline" ? tNow("machines.offlineAgo", { ago: age }) : tNow("machines.heartbeatAgo", { ago: age });
}
