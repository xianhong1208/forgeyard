import { IconArrowLeft, IconPlayerPlay, IconPlayerStop, IconSend, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { CopyBox, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { bytes } from "../lib/machines";
import { API_LABEL, TASK_LABEL, downloadFraction, downloadText, gib } from "../lib/models";
import type { Deployment, DeploymentResponse, LiveCard, TryResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATE_TONE, STATUS_TONE, gpuLabel, stateLabel } from "../lib/workloads";
import { Output } from "./WorkloadDetail";
import { usePoll, WORKLOADS_POLL_MS } from "./Workloads";

export function DeploymentDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { data, error, reload } = useApi<DeploymentResponse>(`/deployments/${id}`);
  usePoll(reload, WORKLOADS_POLL_MS);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const d = data?.deployment;
  const canManage = data?.can_manage ?? false;

  const act = async (what: "stop" | "start" | "delete") => {
    if (!d) return;
    const text = {
      stop: `Stop ${d.name}? Its replicas are stopped and their memory freed; the model stays in the machines' caches.`,
      start: `Start ${d.name} again? Its replicas are placed again.`,
      delete: `Delete ${d.name}? Its replicas are stopped and its route removed. The cached model files stay on the machines.`,
    }[what];
    if (!window.confirm(text)) return;
    setBusy(true);
    setMessage(null);
    try {
      if (what === "delete") {
        await api(`/deployments/${d.id}`, { method: "DELETE" });
        navigate("/models", { replace: true });
        return;
      }
      await api(`/deployments/${d.id}/${what}`, { method: "POST" });
      setMessage({ tone: "ok", text: what === "stop" ? `Stopping ${d.name}.` : `Starting ${d.name} again.` });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const w = d?.workload;
  return (
    <>
      <p className="crumb"><Link to="/models"><IconArrowLeft size={14} aria-hidden="true" /> Models</Link></p>
      <PageHeader index={`03 · ${d ? TASK_LABEL[d.model.task] : "deployment"}`} title={d?.name ?? "Deployment"}
        lead={d ? `${d.model.name} with ${d.recipe}, served as “${d.served_name}” · created by ${d.created_by ?? "?"} ${when(d.created_at)}` : undefined}
        actions={d && w && canManage && (
          <>
            {w.desired_state === "running" ? (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("stop")}><IconPlayerStop size={16} aria-hidden="true" /> Stop</button>
            ) : (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("start")}><IconPlayerPlay size={16} aria-hidden="true" /> Start</button>
            )}
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void act("delete")}><IconTrash size={16} aria-hidden="true" /> Delete</button>
          </>
        )}
      />
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {d && w && (
        <>
          <div className="wl-status">
            <span className={`chip chip-big chip-tone-${STATUS_TONE[w.status]}`}>{w.status}</span>
            <span>{w.ready} of {w.replicas} ready</span>
            {d.route && <span className="muted small">{API_LABEL[d.route.api] ?? d.route.api}</span>}
          </div>
          <Endpoints d={d} />
          <section className="panel" aria-labelledby="replicas-title">
            <h2 id="replicas-title">Replicas</h2>
            <div className="table-wrap">
              <table className="table reps-table">
                <thead><tr><th>#</th><th>State</th><th>Machine</th><th>Device</th><th>Runs</th><th>Reserved</th><th>Since</th></tr></thead>
                <tbody>
                  {w.replica_list.map((r) => {
                    const frac = downloadFraction(r);
                    return (
                      <tr key={r.ordinal}>
                        <td data-label="Replica" className="mono">#{r.ordinal}</td>
                        <td data-label="State">
                          <span className={`chip chip-tone-${STATE_TONE[r.state]}`}>{stateLabel(r)}</span>
                          {frac !== null ? (
                            <div className="dl" aria-label="Download progress">
                              <span className="bar bar-dl"><span style={{ width: `${Math.round(frac * 100)}%` }} /></span>
                              <span className="small">{downloadText(r)} ({Math.round(frac * 100)}%)</span>
                            </div>
                          ) : r.reason && <div className="cell-reason">{r.reason}</div>}
                        </td>
                        <td data-label="Machine">{r.machine ? <span className="mono">{r.machine.name}</span> : <span className="muted">not placed</span>}</td>
                        <td data-label="Device" className="mono">{r.gpus.length ? `GPU ${gpuLabel(r)}` : r.machine ? "CPU" : "—"}</td>
                        <td data-label="Runs" className="mono small">{r.runs ? (r.runs.image ?? (Array.isArray(r.runs.command) ? r.runs.command.join(" ") : r.runs.command)) : "—"}
                          {r.pid ? <div className="muted">pid {r.pid}</div> : r.container ? <div className="muted">{r.container}</div> : null}</td>
                        <td data-label="Reserved" className="small">{r.vram ? gib(r.vram) : "—"}</td>
                        <td data-label="Since" className="small" title={exact(r.started_at)}>{r.started_at ? when(r.started_at) : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
          <div className="columns">
            <Memory d={d} />
            <TryIt d={d} />
          </div>
          {canManage && <Output w={w} />}
          <section className="panel" aria-labelledby="events-title">
            <h2 id="events-title">Events</h2>
            <ol className="events">
              {data.events.map((e, i) => (
                <li key={i} className={`event event-${e.kind}`}><time title={exact(e.at)}>{when(e.at)}</time><span>{e.message}</span></li>
              ))}
            </ol>
          </section>
        </>
      )}
    </>
  );
}

function Endpoints({ d }: { d: Deployment }) {
  if (!d.endpoints.length) return null;
  const path = d.route ? API_LABEL[d.route.api] ?? "" : "";
  return (
    <section className="panel" aria-labelledby="ep-title">
      <h2 id="ep-title">Endpoint{d.endpoints.length > 1 ? "s" : ""}</h2>
      <p className="muted small">Each replica answers on its own port until the gateway (M4) puts them behind one URL. Send <code>"model": "{d.route?.upstream_model ?? d.served_name}"</code>.</p>
      {d.endpoints.map((e) => (
        <div key={e.replica} className="endpoint">
          <span className={`chip chip-tone-${e.ready ? "ok" : "wait"}`}>{e.ready ? "ready" : e.state}</span>
          <CopyBox value={`${e.url.replace(/\/$/, "")}${path}`} label={`Endpoint of replica ${e.replica}`} />
        </div>
      ))}
    </section>
  );
}

function Memory({ d }: { d: Deployment }) {
  const cards = d.cards ?? [];
  const e = d.estimate;
  return (
    <section className="panel" aria-labelledby="mem-title">
      <h2 id="mem-title">Memory: estimated and live</h2>
      <p className="small">Estimated <strong>{gib(e.amount)}</strong> {e.device === "gpu" ? "per GPU" : "of RAM"} ({e.method}):</p>
      <ul className="parts">
        {e.parts.map((p, i) => <li key={p.label}><span className={`swatch seg-${i}`} aria-hidden="true" /><span>{p.label}</span><span className="mono small">{gib(p.bytes)}</span></li>)}
      </ul>
      <h3 className="sub">On the machine now</h3>
      {cards.length === 0 ? <p className="muted small">Not placed yet.</p> : (
        <ul className="live-cards">
          {cards.map((c: LiveCard) => {
            const used = c.total && c.free !== null ? c.total - c.free : null;
            return (
              <li key={`${c.replica}-${c.key}`}>
                <span className="mono small">{c.machine} {c.index === null ? "RAM" : `GPU #${c.index}`}</span>
                <span className="bar"><span style={{ width: `${used && c.total ? Math.round((used / c.total) * 100) : 0}%` }} /></span>
                <span className="small">{bytes(used)} used of {bytes(c.total)} · {bytes(c.free)} free · this replica reserves {gib(c.reserved)}</span>
                <span className="muted small">reported {when(c.updated_at)}; the card may be shared with work outside Forgeyard</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function TryIt({ d }: { d: Deployment }) {
  const [input, setInput] = useState("");
  const [out, setOut] = useState<TryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = d.endpoints.some((e) => e.ready);
  const api_ = d.route?.api;
  const go = async () => {
    setBusy(true); setError(null); setOut(null);
    try {
      setOut(await api<TryResponse>(`/deployments/${d.id}/try`, { method: "POST", body: input.trim() ? { input: input.trim() } : {} }));
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const placeholder = api_ === "openai-chat" ? "In one short sentence: what is a GPU?" : api_ === "openai-embeddings"
    ? "Forgeyard runs models on your own machines." : api_ === "rerank" ? "What is the capital of France?" : "";
  return (
    <section className="panel" aria-labelledby="try-title">
      <h2 id="try-title">Try it</h2>
      <p className="muted small">One small request to a ready replica's own endpoint{api_ === "openai-audio" ? ": an 11-second public-domain clip (JFK, 1961) to transcribe" : ""}.</p>
      {api_ !== "openai-audio" && (
        <div className="field"><label htmlFor="try-input">{api_ === "rerank" ? "Query" : "Input"}</label>
          <input id="try-input" value={input} onChange={(e) => setInput(e.target.value)} placeholder={placeholder} maxLength={500} /></div>
      )}
      <div className="row-gap" style={{ marginTop: 10 }}>
        <button type="button" className="btn" disabled={!ready || busy} onClick={() => void go()}><IconSend size={15} aria-hidden="true" /> {busy ? "Waiting…" : "Send"}</button>
        {!ready && <span className="muted small">No replica is ready yet.</span>}
      </div>
      {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
      {out && (
        <div className="try-out">
          <p className="small muted">{out.ok ? "200" : out.status} in {out.ms} ms · <span className="mono">{out.endpoint}</span></p>
          <pre className="logs try-answer" aria-label="Answer">{out.summary}</pre>
        </div>
      )}
    </section>
  );
}
