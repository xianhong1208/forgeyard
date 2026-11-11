import { IconArrowLeft, IconPlayerPlay, IconPlayerStop, IconSend, IconTrash } from "@tabler/icons-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact, imageTag, when } from "../lib/format";
import { ms, num } from "../lib/gateway";
import { ROUTE_KIND_LABEL, ROUTE_KIND_TONE, spillRules, targetName, tiers } from "../lib/routing";
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
      <AdvisorBanner pages={[`deployment:${id}`]} />
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {d && w && (
        <>
          <div className="wl-status">
            <span className={`chip chip-big chip-tone-${STATUS_TONE[w.status]}`}>{w.status}</span>
            <span>{w.ready} of {w.replicas} ready</span>
            {d.route && <span className="muted small">{API_LABEL[d.route.api] ?? d.route.api}</span>}
          </div>
          <Gateway d={d} />
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
                        <td data-label="Runs" className="mono small" title={r.runs?.image ?? undefined}>{r.runs ? (r.runs.image ? imageTag(r.runs.image) : (Array.isArray(r.runs.command) ? r.runs.command.join(" ") : r.runs.command)) : "—"}
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

function Gateway({ d }: { d: Deployment }) {
  const base = `${window.location.origin}/v1`;
  const path = d.route ? (API_LABEL[d.route.api] ?? "/v1").replace(/^\/v1/, "") : "";
  const u = d.usage?.total;
  return (
    <section className="panel gw-panel" aria-labelledby="gw-title">
      <h2 id="gw-title">Through the gateway</h2>
      <dl className="facts">
        <dt>Model name</dt><dd><code className="gw-model">{d.served_name}</code></dd>
        <dt>Endpoint</dt><dd className="mono small">{base}{path}</dd>
        {d.routing && (
          <>
            <dt>Routing</dt>
            <dd>
              <span className={`chip chip-tone-${ROUTE_KIND_TONE[d.routing.kind]}`}>{ROUTE_KIND_LABEL[d.routing.kind]}</span>{" "}
              {tiers(d.routing).map((tier, i) => (
                <span key={i} className="small">{i === 0 ? "" : " → then "}<span className="mono">{tier.map(targetName).join(" + ")}</span></span>
              ))}
              <div className="muted small">{spillRules(d.routing)} <Link to={`/models/routes/${d.routing.id}`}>Routing and cloud spill</Link></div>
              {d.also_in && d.also_in.length > 0 && <div className="muted small">Also a target of: {d.also_in.map((n) => <code key={n}>{n}</code>)}</div>}
            </dd>
          </>
        )}
      </dl>
      <p className="muted small gw-how">
        Programs call it with an <Link to="/keys">API key</Link> (any OpenAI client, <code>base_url</code> = <code>{base}</code>); you can also
        try it in the <Link to="/playground">playground</Link>. Requests go to the ready replica with the fewest in flight.
      </p>
      {u && (
        <dl className="fleet gw-usage" aria-label={`Last ${d.usage?.days ?? 7} days`}>
          <div><dt>Requests, {d.usage?.days ?? 7} days</dt><dd>{num(u.requests)}</dd></div>
          <div><dt>Errors</dt><dd>{num(u.errors)}</dd></div>
          <div><dt>Tokens</dt><dd>{num(u.tokens)}</dd></div>
          <div><dt>p50 / p95</dt><dd className="small-dd">{ms(u.latency_p50_ms)} / {ms(u.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {d.endpoints.length > 0 && (
        <>
          <h3 className="sub">Replicas behind it</h3>
          <ul className="gw-replicas">
            {d.endpoints.map((e) => (
              <li key={e.replica}>
                <span className={`chip chip-tone-${e.ready ? "ok" : "wait"}`}>{e.ready ? "ready" : e.state}</span>
                <span className="mono small">#{e.replica}{e.machine ? ` on ${e.machine}` : ""}</span>
                <span className="small">{num(e.served ?? 0)} requests{e.in_flight ? ` · ${e.in_flight} in flight` : ""}</span>
                <span className={`chip ${e.protected ? "chip-tone-ok" : "chip-tone-wait"}`} title={e.guarded
                  ? "The agent's key guard holds this port: every path needs the replica's internal key, which only the gateway has. The engine itself listens on loopback only."
                  : e.protected
                    ? "The engine requires the replica's internal key on its /v1 routes; its other routes are open (the agent on that machine has no key guard yet)."
                    : "The engine runs without a key: anyone who can reach its port can use it."}>{e.guarded ? "key guard" : e.protected ? "internal key" : "no key"}</span>
                <span className="muted small mono gw-addr">{e.url}</span>
              </li>
            ))}
          </ul>
          <p className="muted small">Requests sent since the server started. Calls straight to a replica's port without its internal key are refused (401) by the agent's key guard, on every path.</p>
        </>
      )}
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
      <p className="muted small">One small request straight to a ready replica (with its internal key), to check that replica{api_ === "openai-audio" ? ": an 11-second public-domain clip (JFK, 1961) to transcribe" : ""}.</p>
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
