import { IconArrowLeft, IconPlayerPause, IconPlayerPlay, IconPlayerStop, IconTrash } from "@tabler/icons-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, imageTag, when } from "../lib/format";
import { bytes } from "../lib/machines";
import type { LogsResponse, Replica, Workload, WorkloadResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATE_TONE, STATUS_TONE, duration, gpuLabel, portUrl, stateLabel } from "../lib/workloads";
import { usePoll, WORKLOADS_POLL_MS } from "./Workloads";

export const LOGS_POLL_MS = 2000;

export function WorkloadDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, reload } = useApi<WorkloadResponse>(`/workloads/${id}`);
  usePoll(reload, WORKLOADS_POLL_MS);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const w = data?.workload;
  const canManage = data?.can_manage ?? false;

  const act = async (what: "stop" | "start" | "delete") => {
    if (!w) return;
    const text = {
      stop: `Stop ${w.name}? Every replica is stopped on its machine (SIGTERM, then SIGKILL after 10 s). Its GPUs are freed once the agents confirm.`,
      start: `Start ${w.name} again? Its replicas are placed again and start fresh.`,
      delete: `Delete ${w.name}? Running replicas are stopped and its history and output are removed. This cannot be undone.`,
    }[what];
    if (!window.confirm(text)) return;
    setBusy(true);
    setMessage(null);
    try {
      if (what === "delete") {
        await api(`/workloads/${w.id}`, { method: "DELETE" });
        navigate("/workloads", { replace: true });
        return;
      }
      await api(`/workloads/${w.id}/${what}`, { method: "POST" });
      setMessage({ tone: "ok", text: what === "stop" ? `Stopping ${w.name}.` : `Starting ${w.name} again.` });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="crumb"><Link to="/workloads"><IconArrowLeft size={14} aria-hidden="true" /> Workloads</Link></p>
      <PageHeader index={`02 · ${w?.kind ?? "workload"}`} title={w?.name ?? "Workload"}
        lead={w ? `${w.kind === "service" ? "Service" : "Task"} · ${w.runtime} · created by ${w.created_by ?? "?"} ${when(w.created_at)}` : undefined}
        actions={w && canManage && (
          <>
            {w.desired_state === "running" ? (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("stop")}>
                <IconPlayerStop size={16} aria-hidden="true" /> Stop
              </button>
            ) : (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("start")}>
                <IconPlayerPlay size={16} aria-hidden="true" /> Start
              </button>
            )}
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void act("delete")}>
              <IconTrash size={16} aria-hidden="true" /> Delete
            </button>
          </>
        )}
      />
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {w && (
        <>
          <div className="wl-status">
            <span className={`chip chip-big chip-tone-${STATUS_TONE[w.status]}`}>{w.status}</span>
            {w.kind === "service" && <span>{w.ready} of {w.replicas} ready</span>}
            {w.generation > 1 && <span className="muted small">generation {w.generation}</span>}
          </div>
          <section className="panel" aria-labelledby="replicas-title">
            <h2 id="replicas-title">Replicas</h2>
            <div className="table-wrap">
              <table className="table reps-table">
                <thead>
                  <tr><th>#</th><th>State</th><th>Machine</th><th>GPUs</th><th>Address</th><th>Process</th><th>Runs</th><th>Since</th></tr>
                </thead>
                <tbody>
                  {w.replica_list.map((r) => <ReplicaRow key={r.ordinal} r={r} w={w} />)}
                </tbody>
              </table>
            </div>
          </section>
          {canManage && <Output w={w} />}
          <div className="columns">
            <Spec w={w} />
            <section className="panel" aria-labelledby="events-title">
              <h2 id="events-title">Events</h2>
              <ol className="events">
                {data.events.map((e, i) => (
                  <li key={i} className={`event event-${e.kind}`}>
                    <time title={exact(e.at)}>{when(e.at)}</time>
                    <span>{e.message}</span>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        </>
      )}
    </>
  );
}

function ReplicaRow({ r, w }: { r: Replica; w: Workload }) {
  const tone = STATE_TONE[r.state];
  const health = w.spec.health;
  return (
    <tr>
      <td data-label="Replica" className="mono">#{r.ordinal}</td>
      <td data-label="State">
        <span className={`chip chip-tone-${tone}`}>{stateLabel(r)}</span>
        {r.reason && <div className="cell-reason">{r.reason}</div>}
      </td>
      <td data-label="Machine">
        {r.machine ? (
          <span className="mono">{r.machine.name}{!r.machine.connected && <span className="muted"> (offline)</span>}</span>
        ) : <span className="muted">not placed</span>}
      </td>
      <td data-label="GPUs" className="mono">{gpuLabel(r)}</td>
      <td data-label="Address">
        {r.ports.length === 0 && <span className="muted">—</span>}
        {r.ports.map((p) => {
          const url = portUrl(p, health?.type === "tcp" && health.port === p.name ? "tcp" : "http");
          return (
            <div key={p.name} className="mono small">
              {url && r.state !== "stopped" && url.startsWith("http") ? <a href={url} target="_blank" rel="noreferrer">{url}</a> : url ?? p.name}
              {p.port ? <span className="muted"> → {p.port}</span> : null}
            </div>
          );
        })}
      </td>
      <td data-label="Process" className="mono">{r.pid ? `pid ${r.pid}` : r.container ?? "—"}</td>
      <td data-label="Runs" className="small">
        {w.kind === "task" ? `attempt ${r.attempt}` : `${r.restarts} restart${r.restarts === 1 ? "" : "s"}`}
        {r.exit_code !== null && <div className="muted">exit {r.exit_code}</div>}
      </td>
      <td data-label="Since" className="small" title={exact(r.finished_at ?? r.started_at)}>
        {r.finished_at ? `ended ${when(r.finished_at)}` : r.started_at ? when(r.started_at) : "—"}
      </td>
    </tr>
  );
}

export function Output({ w }: { w: Workload }) {
  const [ordinal, setOrdinal] = useState(0);
  const [follow, setFollow] = useState(true);
  const [logs, setLogs] = useState<LogsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pre = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api<LogsResponse>(`/workloads/${w.id}/replicas/${ordinal}/logs?lines=300`).then(
        (r) => {
          if (!alive) return;
          setLogs(r);
          setError(null);
        },
        (e) => alive && setError(errorText(e)),
      );
    void load();
    const timer = follow
      ? window.setInterval(() => {
          if (document.visibilityState !== "hidden") void load();
        }, LOGS_POLL_MS)
      : undefined;
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [w.id, ordinal, follow]);

  useEffect(() => {
    if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [logs, follow]);

  return (
    <section className="panel" aria-labelledby="output-title">
      <div className="panel-head">
        <h2 id="output-title">Output</h2>
        <div className="row-gap">
          {w.replica_list.length > 1 && (
            <select aria-label="Replica" value={ordinal} onChange={(e) => setOrdinal(Number(e.target.value))}>
              {w.replica_list.map((r) => <option key={r.ordinal} value={r.ordinal}>Replica #{r.ordinal}</option>)}
            </select>
          )}
          <button type="button" className="btn btn-quiet btn-small" onClick={() => setFollow((v) => !v)} aria-pressed={follow}>
            {follow ? <IconPlayerPause size={14} aria-hidden="true" /> : <IconPlayerPlay size={14} aria-hidden="true" />}
            {follow ? "Pause" : "Follow"}
          </button>
        </div>
      </div>
      <p className="muted small output-source" role="status">
        {logs?.source === "live" ? (
          <><span className={`pulse${follow ? "" : " pulse-off"}`} aria-hidden="true" /> Live from the agent{follow ? `, every ${LOGS_POLL_MS / 1000} s` : " (paused)"}; the last 300 lines.</>
        ) : logs ? (logs.note ?? "Kept from when it ended.") : "Loading…"}
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      <pre className="logs" ref={pre} aria-label={`Output of replica ${ordinal}`}>{logs?.text || (logs ? "(no output yet)" : "")}</pre>
    </section>
  );
}

function Spec({ w }: { w: Workload }) {
  const s = w.spec;
  const rt = s.runtime;
  const gpus = s.resources.gpus;
  const facts: [string, ReactNode][] = [["Runtime", rt.type]];
  if (rt.type === "model") {
    facts.push(["Model", <code key="m">{rt.model}</code>], ["Recipe", <code key="r">{rt.recipe}</code>]);
    if (w.deployment_id) facts.push(["Deployment", <Link key="d" to={`/models/deployments/${w.deployment_id}`}>open the deployment</Link>]);
  } else {
    facts.push(rt.type === "container" ? ["Image", <code key="i" title={rt.image}>{imageTag(rt.image)}</code>] : ["Command", <code key="c">{rt.command}</code>]);
    if (rt.type === "container" && rt.command) facts.push(["Entrypoint", <code key="e">{rt.command.join(" ")}</code>]);
    if (rt.args.length) facts.push(["Arguments", <code key="a">{rt.args.join(" ")}</code>]);
  }
  if (rt.type === "process" && rt.workdir) facts.push(["Working directory", <code key="w">{rt.workdir}</code>]);
  facts.push(["Placement", s.machine_id ? `machine ${w.replica_list[0]?.machine?.name ?? `#${s.machine_id}`}` : "any online machine with room"]);
  facts.push(["GPUs", gpus.indices ? `cards ${gpus.indices.map((i) => `#${i}`).join(", ")}` : gpus.count ? `${gpus.count}${gpus.vendor ? ` ${gpus.vendor}` : ""} per replica` : "none"]);
  facts.push(["CPU and memory", s.resources.cpu === null && s.resources.memory === null ? "not declared"
    : `${s.resources.cpu ?? "—"} core${s.resources.cpu === 1 ? "" : "s"} · ${s.resources.memory ? bytes(s.resources.memory) : "—"}`]);
  facts.push(["Replicas", String(s.replicas)]);
  if (s.kind === "service") {
    facts.push(["Ports", s.ports.length ? s.ports.map((p) => (p.port ? `${p.name} (${p.port})` : p.name)).join(", ") : "none"]);
    facts.push(["Health", s.health ? `${s.health.type}${s.health.type === "http" ? ` ${s.health.path}` : ""} on ${s.health.port} every ${s.health.interval} s` : "none"]);
    facts.push(["Restart", s.restart]);
  } else {
    facts.push(["Max duration", duration(s.max_duration)]);
    facts.push(["Retries", s.retries ? `${s.retries}, backoff ${s.retry_backoff} s doubling` : "none"]);
  }
  const env = Object.entries(s.env);
  return (
    <section className="panel" aria-labelledby="spec-title">
      <h2 id="spec-title">Spec</h2>
      <dl className="facts">
        {facts.map(([k, v]) => <div key={k} className="fact"><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
      <h3 className="sub">Environment</h3>
      {env.length === 0 ? <p className="muted small">Nothing declared: it starts with an empty environment.</p> : (
        <ul className="env-list mono">
          {env.map(([k, v]) => <li key={k}><span>{k}</span>=<span className="muted">{v}</span></li>)}
        </ul>
      )}
    </section>
  );
}
