import { IconAdjustments, IconArrowLeft, IconMoon, IconPlayerPlay, IconPlayerStop, IconSend, IconTrash } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth";

import { AdvisorBanner } from "../components/Advisor";
import { EngineImage } from "../components/EngineImage";
import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { deploymentRuns } from "../lib/engines";
import { MODEL_KIND, modelKind } from "../lib/fleet";
import { exact, imageTag, when } from "../lib/format";
import { ms, num } from "../lib/gateway";
import { FALLBACK_ANCHOR, routeHome, targetName, tiers } from "../lib/routing";
import { bytes } from "../lib/machines";
import { API_LABEL, downloadFraction, downloadText, gib } from "../lib/models";
import { engineHref, modelHref } from "../lib/pages";
import type { CatalogResponse, Deployment, DeploymentResponse, LiveCard, RouteInfo, RouteOptions, RoutesResponse, TryResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATE_TONE, STATUS_TONE, gpuLabel, stateLabel, statusLabel } from "../lib/workloads";
import { RoutingEditor } from "./RouteDetail";
import { NotPickedUpNotice, Output } from "../components/ReplicaOutput";
import { AutoscaleLine } from "../components/Replicas";
import { usePoll, WORKLOADS_POLL_MS } from "../lib/poll";
import { elapsed, seconds, sleepState, wakeSeconds } from "../lib/sleep";
import { SleepBadge } from "../components/Sleep";
import { useT } from "../i18n/react";
import { eventKindLabel } from "../i18n/server";

export function DeploymentDetailPage() {
  const t = useT();
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
      stop: t("deployment.stopQuestion", { name: d.name }),
      start: d.workload.desired_state === "asleep" ? t("deployment.wakeQuestion", { name: d.name }) : t("deployment.startQuestion", { name: d.name }),
      delete: t("deployment.deleteQuestion", { name: d.name }),
    }[what];
    if (!window.confirm(text)) return;
    setBusy(true);
    setMessage(null);
    try {
      if (what === "delete") {
        await api(`/deployments/${d.id}`, { method: "DELETE" });
        navigate("/serving", { replace: true });
        return;
      }
      await api(`/deployments/${d.id}/${what}`, { method: "POST" });
      const waking = d.workload.desired_state === "asleep";
      setMessage({ tone: "ok", text: what === "stop" ? t("deployment.stopping", { name: d.name }) : waking ? t("deployment.wakingName", { name: d.name })
        : t("deployment.starting", { name: d.name }) });
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const w = d?.workload;
  // 1.5.2 (#189): the image beside the engine's name: what the replicas run, or (none placed) the engine's own.
  const placed = (w?.replica_list ?? []).map((r) => r.runs?.image).filter((x): x is string => Boolean(x));
  const cat = useApi<CatalogResponse>(d && placed.length === 0 ? "/models" : null);
  const runs = d ? deploymentRuns(placed, cat.data?.recipes.find((r) => r.name === d.recipe), d.estimate.device) : [];
  return (
    <>
      <p className="crumb"><Link to="/serving"><IconArrowLeft size={14} aria-hidden="true" /> {t("shell.nav.serving")}</Link></p>
      <PageHeader index={`02 · ${d ? MODEL_KIND[modelKind(d)].label : t("deployment.eyebrow")}`} title={d?.name ?? t("deployment.title")}
        lead={d ? t.rich("deployment.lead", { model: <Link to={modelHref(d.model.name)}>{d.model.name}</Link>, recipe: <span className="engine-with-image"><Link to={engineHref(d.recipe)}>{d.recipe}</Link><EngineImage runs={runs} /></span>,
          served: d.served_name, by: d.created_by ?? "?", when: when(d.created_at) }) : undefined}
        actions={d && w && canManage && (
          <>
            <Link to={`/serving/${d.id}/edit`} className="btn"><IconAdjustments size={16} aria-hidden="true" /> {t("deployment.edit")}</Link>
            {w.desired_state === "asleep" ? (
              <>
                <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void act("start")}><IconPlayerPlay size={16} aria-hidden="true" /> {t("deployment.wakeNow")}</button>
                <button type="button" className="btn" disabled={busy} onClick={() => void act("stop")}><IconPlayerStop size={16} aria-hidden="true" /> {t("deployment.stop")}</button>
              </>
            ) : w.desired_state === "running" ? (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("stop")}><IconPlayerStop size={16} aria-hidden="true" /> {t("deployment.stop")}</button>
            ) : (
              <button type="button" className="btn" disabled={busy} onClick={() => void act("start")}><IconPlayerPlay size={16} aria-hidden="true" /> {t("deployment.start")}</button>
            )}
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void act("delete")}><IconTrash size={16} aria-hidden="true" /> {t("deployment.delete")}</button>
          </>
        )}
      />
      {d?.engine?.based_on && (
        <p className="small muted dep-copy-line">{t.rich("deployment.runsCopy", {
          engine: <Link to={engineHref(d.engine.name)}>{d.engine.title.endsWith(` ${d.engine.version}`) ? d.engine.title : `${d.engine.title} ${d.engine.version}`}</Link>,
          builtin: <Link to={engineHref(d.engine.based_on)} className="mono">{d.engine.based_on}</Link> })}</p>
      )}
      <AdvisorBanner pages={[`deployment:${id}`]} />
      {error && <Notice tone="error">{error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {w && <NotPickedUpNotice w={w} />}
      {d?.sleep?.asleep && (
        <Notice tone="info">
          {t("deployment.asleepNotice", { since: d.sleep.asleep_since ? t("deployment.since", { when: when(d.sleep.asleep_since) }) : "",
            minutes: d.sleep.idle_minutes, orWake: canManage ? t("deployment.orWakeNow") : "",
            last: d.last_start ? t("deployment.lastStartTook", { took: seconds(d.last_start.seconds) }) : t("deployment.period") })}
        </Notice>
      )}
      {d && sleepState(d) === "waking" && (
        <Notice tone="info">
          {t("deployment.wakingNotice", { by: d.sleep?.waking?.by ?? "", elapsed: elapsed(d.sleep?.waking?.since), about: seconds(wakeSeconds(d) ?? 30),
            wait: String(d.sleep?.wake_wait ?? 300), waiting: d.sleep?.waking?.waiting ? t("deployment.waitingNow", { count: d.sleep.waking.waiting }) : "" })}
        </Notice>
      )}
      {d && w && (
        <>
          <div className="wl-status">
            <SleepBadge d={d} big fallback={<span className={`chip chip-big chip-tone-${STATUS_TONE[w.status]}`}>{statusLabel(w.status)}</span>} />
            <span>{t("deployment.ready", { ready: w.ready, total: w.replicas })}</span>
            {d.rollout && <span className="chip chip-tone-wait" title={t("deployment.rolloutTitle")}>
              {t("deployment.rollout", { updated: d.rollout.updated, total: d.rollout.total })}</span>}
            <span className="small">{d.pinned ? t.rich("deployment.pinnedTo", { name: <span className="mono">{d.pinned.name ?? t("deployment.machineN", { id: d.pinned.id })}</span> })
              : <span className="muted">{t("deployment.machineAuto")}</span>}</span>
            {d.estimate.device === "gpu" && <span className="small">{d.params.share === "vram" ? t("deployment.sharesVram") : <span className="muted">{t("deployment.exclusiveGpu")}</span>}</span>}
            {d.route && <span className="muted small">{API_LABEL[d.route.api] ?? d.route.api}</span>}
          </div>
          <Gateway d={d} />
          <WhenNotInUse d={d} canManage={canManage} />
          <section className="panel" aria-labelledby="replicas-title">
            <h2 id="replicas-title">{t("deployment.replicas")}</h2>
            <AutoscaleLine d={d} />
            <div className="table-wrap">
              <table className="table reps-table">
                <thead><tr><th>#</th><th>{t("deployment.col.state")}</th><th>{t("deployment.col.machine")}</th><th>{t("deployment.col.device")}</th>
                  <th>{t("deployment.col.runs")}</th><th>{t("deployment.col.reserved")}</th><th>{t("deployment.col.since")}</th></tr></thead>
                <tbody>
                  {w.replica_list.map((r) => {
                    const frac = downloadFraction(r);
                    return (
                      <tr key={r.ordinal}>
                        <td data-label={t("deployment.col.replica")} className="mono">#{r.ordinal}</td>
                        <td data-label={t("deployment.col.state")}>
                          <span className={`chip chip-tone-${STATE_TONE[r.state]}`}>{stateLabel(r)}</span>
                          {d.autoscale?.draining?.replica === r.ordinal && <span className="chip chip-tone-wait chip-draining" title={t("deployment.drainingTitle")}>{t("deployment.draining")}</span>}
                          {frac !== null ? (
                            <div className="dl" aria-label={t("deployment.downloadProgress")}>
                              <span className="bar bar-dl"><span style={{ width: `${Math.round(frac * 100)}%` }} /></span>
                              <span className="small">{downloadText(r)} ({Math.round(frac * 100)}%)</span>
                            </div>
                          ) : r.not_picked_up ? <div className="cell-stuck">{r.not_picked_up}</div> : r.reason && <div className="cell-reason">{r.reason}</div>}
                        </td>
                        <td data-label={t("deployment.col.machine")}><div>{r.machine ? <span className="mono">{r.machine.name}</span> : <span className="muted">{t("deployment.notPlaced")}</span>}
                          {r.placement_note && <div className="cell-note">{r.placement_note}</div>}</div></td>
                        <td data-label={t("deployment.col.device")} className="mono">{r.gpus.length ? `GPU ${gpuLabel(r)}` : r.machine ? "CPU" : "—"}</td>
                        <td data-label={t("deployment.col.runs")} className="mono small" title={r.runs?.image ?? undefined}>{r.runs ? (r.runs.image ? imageTag(r.runs.image) : (Array.isArray(r.runs.command) ? r.runs.command.join(" ") : r.runs.command)) : "—"}
                          {r.pid ? <div className="muted">pid {r.pid}</div> : r.container ? <div className="muted">{r.container}</div> : null}</td>
                        <td data-label={t("deployment.col.reserved")} className="small">{r.vram ? gib(r.vram) : "—"}</td>
                        <td data-label={t("deployment.col.since")} className="small" title={exact(r.started_at)}>{r.started_at ? when(r.started_at) : "—"}</td>
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
          {d.routing && <Fallback key={d.routing.id} routing={d.routing} reload={reload} />}
          {canManage && <Output w={w} />}
          <section className="panel" aria-labelledby="events-title">
            <h2 id="events-title">{t("deployment.events")}</h2>
            <ol className="events">
              {data.events.map((e, i) => (
                <li key={i} className={`event event-${e.kind}`}><time title={exact(e.at)}>{when(e.at)}</time>
                  {/* zh-TW: the kind in Chinese before the server's own words; English shows the server's message alone, as before. */}
                  <span>{t.lang !== "en" && <b className="event-kind">{eventKindLabel(e.kind, t.lang)}</b>}{e.message}</span></li>
              ))}
            </ol>
          </section>
        </>
      )}
    </>
  );
}

// 1.5.0 (#37): sleep when idle, where it stands, and the start a wake waits for.
function WhenNotInUse({ d, canManage }: { d: Deployment; canManage: boolean }) {
  const t = useT();
  const s = d.sleep;
  if (!s) return null;
  const state = sleepState(d);
  return (
    <section className="panel sleep-panel" aria-labelledby="sleep-title">
      <div className="panel-head">
        <h2 id="sleep-title"><IconMoon size={16} aria-hidden="true" /> {t("deployment.sleep.title")}</h2>
        {canManage && <Link to={`/serving/${d.id}/edit`} className="small">{t("deployment.change")}</Link>}
      </div>
      <dl className="facts">
        <dt>{t("deployment.sleep.label")}</dt>
        <dd>{s.on ? t("deployment.sleep.on", { minutes: s.idle_minutes, wait: String(s.wake_wait ?? 300) })
          : <span className="muted">{t("deployment.sleep.off")}</span>}</dd>
        <dt>{t("deployment.sleep.now")}</dt>
        <dd>{state === "asleep" ? t.rich("deployment.sleep.asleep", { since: s.asleep_since
            ? t.rich("deployment.since", { when: <span title={exact(s.asleep_since)}>{when(s.asleep_since)}</span> }) : "" })
          : state === "waking" ? t("deployment.sleep.waking", { elapsed: elapsed(s.waking?.since), by: s.waking?.by ?? "" })
            : d.workload.desired_state !== "running" ? <span className="muted">{t("deployment.sleep.stopped")}</span>
              : s.on && s.held_by ? t("deployment.sleep.heldBy", { reason: s.held_by })
                : s.on && s.sleeps_at ? t.rich("deployment.sleep.sleepsAt", { when: <span title={exact(s.sleeps_at)}>{when(s.sleeps_at)}</span> })
                  : t("deployment.sleep.awake")}</dd>
        <dt>{t("deployment.sleep.lastRequest")}</dt>
        <dd>{s.last_request_at ? <span title={exact(s.last_request_at)}>{when(s.last_request_at)}</span>
          : <span className="muted">{t("deployment.sleep.noRequest")}</span>}</dd>
        <dt>{t("deployment.sleep.lastStart")}</dt>
        <dd>{d.last_start ? <>{seconds(d.last_start.seconds)} <span className="muted small">{t("deployment.sleep.lastStartAt", { when: when(d.last_start.at) })}</span></>
          : <span className="muted">{t("deployment.sleep.notMeasured")}</span>}</dd>
        {(s.on || (s.wakes ?? 0) > 0) && <><dt>{t("deployment.sleep.woken")}</dt><dd>{s.wakes ?? 0}</dd></>}
      </dl>
    </section>
  );
}

function Gateway({ d }: { d: Deployment }) {
  const t = useT();
  const base = `${window.location.origin}/v1`;
  const path = d.route ? (API_LABEL[d.route.api] ?? "/v1").replace(/^\/v1/, "") : "";
  const u = d.usage?.total;
  return (
    <section className="panel gw-panel" aria-labelledby="gw-title">
      <h2 id="gw-title">{t("deployment.gw.title")}</h2>
      <dl className="facts">
        <dt>{t("deployment.gw.modelName")}</dt><dd><code className="gw-model">{d.served_name}</code></dd>
        <dt>{t("deployment.gw.endpoint")}</dt><dd className="mono small">{base}{path}</dd>
        {d.routing && (
          <>
            <dt>{t("deployment.gw.fallback")}</dt>
            <dd>
              {tiers(d.routing).length > 1 ? (
                <span className="small">{tiers(d.routing).slice(1).map((tier, i) => (
                  <span key={i}>{i === 0 ? t("deployment.gw.then") : t("deployment.gw.thenNext")}<span className="mono">{tier.map(targetName).join(" + ")}</span></span>
                ))}</span>
              ) : <span className="muted small">{t("deployment.gw.none")}</span>}{" "}
              <a className="small" href={`#${FALLBACK_ANCHOR}`}>{d.routing.kind === "local" ? t("deployment.gw.addOne") : t("deployment.change")}</a>
            </dd>
          </>
        )}
        {d.also_in && d.also_in.length > 0 && (
          <>
            <dt>{t("deployment.gw.alsoAs")}</dt>
            <dd><AlsoIn names={d.also_in} /></dd>
          </>
        )}
      </dl>
      <p className="muted small gw-how">
        {t.rich("deployment.gw.how", { key: (text) => <Link to="/keys">{text}</Link>, play: (text) => <Link to="/playground">{text}</Link>,
          baseUrl: <code>base_url</code>, base: <code>{base}</code> })}
      </p>
      {u && (
        <dl className="fleet gw-usage" aria-label={t("deployment.gw.lastDays", { days: d.usage?.days ?? 7 })}>
          <div><dt>{t("deployment.gw.requestsDays", { days: d.usage?.days ?? 7 })}</dt><dd>{num(u.requests)}</dd></div>
          <div><dt>{t("deployment.gw.errors")}</dt><dd>{num(u.errors)}</dd></div>
          <div><dt>{t("deployment.gw.tokens")}</dt><dd>{num(u.tokens)}</dd></div>
          <div><dt>p50 / p95</dt><dd className="small-dd">{ms(u.latency_p50_ms)} / {ms(u.latency_p95_ms)}</dd></div>
        </dl>
      )}
      {d.endpoints.length > 0 && (
        <>
          <h3 className="sub">{t("deployment.gw.behind")}</h3>
          <ul className="gw-replicas">
            {d.endpoints.map((e) => (
              <li key={e.replica}>
                <span className={`chip chip-tone-${e.ready ? "ok" : "wait"}`}>{e.ready ? t("deployment.gw.ready") : statusLabel(e.state)}</span>
                <span className="mono small">#{e.replica}{e.machine ? t("deployment.gw.on", { machine: e.machine }) : ""}</span>
                <span className="small">{t("deployment.gw.served", { count: num(e.served ?? 0) })}{e.in_flight ? t("deployment.gw.inFlight", { count: e.in_flight }) : ""}</span>
                <span className={`chip ${e.protected ? "chip-tone-ok" : "chip-tone-wait"}`} title={e.guarded
                  ? t("deployment.gw.guardedTitle") : e.protected ? t("deployment.gw.protectedTitle") : t("deployment.gw.openTitle")}>
                  {e.guarded ? t("deployment.gw.guarded") : e.protected ? t("deployment.gw.protected") : t("deployment.gw.open")}</span>
                <span className="muted small mono gw-addr">{e.url}</span>
              </li>
            ))}
          </ul>
          <p className="muted small">{t("deployment.gw.note")}</p>
        </>
      )}
    </section>
  );
}

// The deployment's own route, as its cloud fallback (1.4, #81: it was on the Serving tab of Models). Admins
// change it here; everyone else sees it.
function Fallback({ routing, reload }: { routing: RouteInfo; reload: () => void }) {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const opts = useApi<RouteOptions>(admin ? "/route-options" : null);
  const { hash } = useLocation();
  // A link to #cloud-fallback (the Deployments list's mark, the advisor) opens the page at it, once
  // the section is there.
  const scrolled = useRef(false);
  useEffect(() => {
    if (hash !== `#${FALLBACK_ANCHOR}` || scrolled.current) return;
    scrolled.current = true;
    document.getElementById(FALLBACK_ANCHOR)?.scrollIntoView?.({ block: "start" });
  }, [hash]);
  return <RoutingEditor r={routing} admin={admin} opts={admin ? opts.data : null} reload={reload} embedded />;
}

// Other names an admin made that this deployment answers under: links to them where they live.
function AlsoIn({ names }: { names: string[] }) {
  const { data } = useApi<RoutesResponse>("/routes");
  return (
    <span className="also-in">
      {names.map((n) => {
        const r = data?.routes.find((x) => x.served_name === n);
        return r ? <Link key={n} className="mono small" to={routeHome(r)}>{n}</Link> : <code key={n}>{n}</code>;
      })}
    </span>
  );
}

function Memory({ d }: { d: Deployment }) {
  const t = useT();
  const cards = d.cards ?? [];
  const e = d.estimate;
  return (
    <section className="panel" aria-labelledby="mem-title">
      <h2 id="mem-title">{t("deployment.mem.title")}</h2>
      <p className="small">{t.rich("deployment.mem.estimated", { amount: <strong>{gib(e.amount)}</strong>,
        per: e.device === "gpu" ? t("deployment.mem.perGpu") : t("deployment.mem.ofRam"), method: e.method })}</p>
      <ul className="parts">
        {e.parts.map((p, i) => <li key={p.label}><span className={`swatch seg-${i}`} aria-hidden="true" /><span>{p.label}</span><span className="mono small">{gib(p.bytes)}</span></li>)}
      </ul>
      <h3 className="sub">{t("deployment.mem.now")}</h3>
      {cards.length === 0 ? <p className="muted small">{t("deployment.mem.notPlaced")}</p> : (
        <ul className="live-cards">
          {cards.map((c: LiveCard) => {
            const used = c.total && c.free !== null ? c.total - c.free : null;
            return (
              <li key={`${c.replica}-${c.key}`}>
                <span className="mono small">{c.machine} {c.index === null ? "RAM" : `GPU #${c.index}`}</span>
                <span className="bar"><span style={{ width: `${used && c.total ? Math.round((used / c.total) * 100) : 0}%` }} /></span>
                <span className="small">{t("deployment.mem.card", { used: bytes(used), total: bytes(c.total), free: bytes(c.free), reserved: gib(c.reserved) })}</span>
                <span className="muted small">{t("deployment.mem.reported", { when: when(c.updated_at) })}</span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function TryIt({ d }: { d: Deployment }) {
  const t = useT();
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
  const placeholder = api_ === "openai-chat" ? t("deployment.try.phChat") : api_ === "openai-embeddings"
    ? t("deployment.try.phEmbed") : api_ === "rerank" ? t("deployment.try.phRerank")
    : api_ === "classify" ? t("deployment.try.phClassify") : "";
  return (
    <section className="panel" aria-labelledby="try-title">
      <h2 id="try-title">{t("deployment.try.title")}</h2>
      <p className="muted small">{t("deployment.try.lead", { what: api_ === "openai-audio" ? t("deployment.try.audio")
        : api_ === "openai-speech" ? t("deployment.try.speech")
        : api_ === "openai-images" ? t("deployment.try.images")
        : api_ === "openai-videos" ? t("deployment.try.videos")
        : api_?.startsWith("vision-") ? t("deployment.try.vision", { box: api_ === "vision-segment" ? t("deployment.try.visionBox") : "" }) : "" })}</p>
      {api_ !== "openai-audio" && api_ !== "openai-videos" && !api_?.startsWith("vision-") && (
        <div className="field"><label htmlFor="try-input">{api_ === "rerank" ? t("deployment.try.query") : t("deployment.try.input")}</label>
          <input id="try-input" value={input} onChange={(e) => setInput(e.target.value)} placeholder={placeholder} maxLength={500} /></div>
      )}
      <div className="row-gap" style={{ marginTop: 10 }}>
        <button type="button" className="btn" disabled={!ready || busy} onClick={() => void go()}><IconSend size={15} aria-hidden="true" /> {busy ? t("deployment.try.waiting") : t("deployment.try.send")}</button>
        {!ready && <span className="muted small">{t("deployment.try.notReady")}</span>}
      </div>
      {error && <div style={{ marginTop: 12 }}><Notice tone="error">{error}</Notice></div>}
      {out && (
        <div className="try-out">
          <p className="small muted">{t("deployment.try.took", { status: out.ok ? "200" : String(out.status), ms: String(out.ms) })} · <span className="mono">{out.endpoint}</span></p>
          <pre className="logs try-answer" aria-label={t("deployment.try.answer")}>{out.summary}</pre>
        </div>
      )}
    </section>
  );
}
