import { IconBox, IconPlayerPlay, IconPlus } from "@tabler/icons-react";
import { useEffect } from "react";
import { Link } from "react-router";

import { HelpLink } from "../components/HelpLink";
import { Notice, PageHeader } from "../components/ui";
import { when, exact } from "../lib/format";
import type { Replica, Workload, WorkloadsResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATE_TONE, STATUS_TONE, gpuLabel, portUrl, runtimeSummary, stateLabel } from "../lib/workloads";

export const WORKLOADS_POLL_MS = 3000;

export function usePoll(reload: () => void, ms: number) {
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState !== "hidden") reload();
    }, ms);
    return () => window.clearInterval(id);
  }, [reload, ms]);
}

const KINDS = [
  { icon: IconBox, name: "Services", detail: "Run until stopped, health-checked, restarted: an API, a web app" },
  { icon: IconPlayerPlay, name: "Tasks", detail: "Run to the end, with retries and a time limit: a batch job" },
];

// Apps (1.4, #81; Workloads before): the programs people run themselves. The workloads model
// deployments create are seen under Models (and on Machines), not here.
export function appsOnly(list: Workload[]): Workload[] {
  return list.filter((w) => !w.deployment && !w.deployment_id);
}

export function WorkloadsPage() {
  const { data, error, reload } = useApi<WorkloadsResponse>("/workloads");
  usePoll(reload, WORKLOADS_POLL_MS);
  const list = data ? appsOnly(data.workloads) : null;
  const canManage = data?.can_manage ?? false;

  return (
    <>
      <PageHeader
        index="Advanced · Apps"
        title="Apps"
        lead="An advanced feature, not in the menu: programs you run on your machines yourself, as services or tasks."
        actions={canManage && (
          <Link to="/apps/new" className="btn btn-primary">
            <IconPlus size={16} aria-hidden="true" /> New app
          </Link>
        )}
      />
      <p className="apps-note">
        Just want to use a model? <Link to="/models">Deploy it under Models</Link>. Apps fit a program that is not a model Forgeyard
        runs: a model's own server image exposed at <code>/x/</code>, or a one-off GPU task. Object detection, segmentation and
        depth are models too (1.4): <Link to="/models?tab=library">add them from the Library</Link>; apps made from the 1.3
        templates keep running as they are.{" "}
        <HelpLink page="concepts" anchor="apps-advanced">About apps</HelpLink>
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      {list && list.length === 0 && (
        <section className="empty">
          <div className="empty-art empty-art-lanes" aria-hidden="true">
            <span className="lane" />
            <span className="lane lane-hot" />
            <span className="lane" />
          </div>
          <div className="empty-copy">
            <h2>No apps yet</h2>
            <p>
              An app is a program you run on your machines: a container or a command, kept running as a service or run
              once as a task.{" "}
              {canManage ? "" : "Admins and operators start apps; they show up here with their status."}
            </p>
            {canManage && (
              <p className="empty-actions">
                <Link to="/apps/new" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> New app</Link>
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
      )}
      {list && list.length > 0 && (
        <>
          <Totals list={list} />
          <div className="workloads">{list.map((w) => <WorkloadCard key={w.id} w={w} />)}</div>
        </>
      )}
    </>
  );
}

function Totals({ list }: { list: Workload[] }) {
  const running = list.filter((w) => ["running", "starting", "degraded"].includes(w.status)).length;
  const waiting = list.filter((w) => w.status === "pending").length;
  const failed = list.filter((w) => ["failed", "degraded"].includes(w.status)).length;
  const gpus = list.flatMap((w) => w.replica_list.filter((r) => !r.final && r.state !== "stopped").flatMap((r) => r.gpus)).length;
  return (
    <dl className="fleet" aria-label="Apps">
      <div><dt>Apps</dt><dd>{list.length}</dd></div>
      <div><dt>Running</dt><dd className="fleet-online">{running}</dd></div>
      <div><dt>Waiting</dt><dd>{waiting}</dd></div>
      <div><dt>Failing</dt><dd className={failed ? "fleet-bad" : undefined}>{failed}</dd></div>
      <div><dt title="GPUs held by apps; models' GPUs are on Models and Machines">GPUs used by apps</dt><dd>{gpus}</dd></div>
    </dl>
  );
}

function WorkloadCard({ w }: { w: Workload }) {
  return (
    <article className={`wl wl-${STATUS_TONE[w.status]}`} aria-label={`App ${w.name}`}>
      <header className="wl-head">
        <div className="wl-title">
          <span className={`dot dot-tone-${STATUS_TONE[w.status]}`} aria-hidden="true" />
          <h2 className="mono"><Link to={`/apps/${w.id}`}>{w.name}</Link></h2>
          <span className={`chip chip-tone-${STATUS_TONE[w.status]}`}>{w.status}</span>
          <span className="chip">{w.kind}</span>
          <span className="chip">{w.runtime}</span>
          {w.gateway?.exposed && <span className="chip chip-tone-run mono" title="Exposed behind the gateway">{w.gateway.path}</span>}
          {w.kind === "service" && <span className="muted small">{w.ready}/{w.replicas} ready</span>}
        </div>
        <span className="muted small" title={exact(w.created_at)}>
          {w.created_by ? `${w.created_by} · ` : ""}{when(w.created_at)}
        </span>
      </header>
      <p className="wl-cmd mono" title={runtimeSummary(w)}>{runtimeSummary(w)}</p>
      <ul className="reps">
        {w.replica_list.map((r) => <ReplicaLine key={r.ordinal} r={r} />)}
      </ul>
    </article>
  );
}

function ReplicaLine({ r }: { r: Replica }) {
  const tone = STATE_TONE[r.state];
  return (
    <li className="rep">
      <span className="rep-id mono">#{r.ordinal}</span>
      <span className={`chip chip-tone-${tone}`}>{stateLabel(r)}</span>
      <span className="rep-machine">{r.machine ? <span className="mono">{r.machine.name}</span> : <span className="muted">not placed</span>}</span>
      <span className="rep-gpus mono" title="GPUs">{r.gpus.length ? `GPU ${gpuLabel(r)}` : "no GPU"}</span>
      <span className="rep-ports">
        {r.ports.map((p) => {
          const url = portUrl(p);
          return url && r.state !== "stopped" ? (
            <a key={p.name} className="mono" href={url} target="_blank" rel="noreferrer">{p.address}:{p.host_port}</a>
          ) : null;
        })}
      </span>
      {r.reason && <span className="rep-reason">{r.reason}</span>}
      {r.placement_note && <span className="rep-reason rep-note">{r.placement_note}</span>}
    </li>
  );
}
