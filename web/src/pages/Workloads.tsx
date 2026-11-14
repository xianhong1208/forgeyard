import { IconBox, IconPlayerPlay, IconPlus, IconTemplate } from "@tabler/icons-react";
import { useEffect } from "react";
import { Link } from "react-router";

import { HelpLink } from "../components/HelpLink";
import { Notice, PageHeader } from "../components/ui";
import { when, exact } from "../lib/format";
import type { Replica, Workload, WorkloadsResponse, WorkloadTemplate } from "../lib/types";
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

export function WorkloadsPage() {
  const { data, error, reload } = useApi<WorkloadsResponse>("/workloads");
  usePoll(reload, WORKLOADS_POLL_MS);
  const list = data?.workloads ?? null;
  const canManage = data?.can_manage ?? false;

  return (
    <>
      <PageHeader
        index="02 · Workloads"
        title="Workloads"
        lead="Services and tasks on your machines. Each replica runs on one machine, with only the GPUs it was given and an environment that starts empty."
        actions={canManage && (
          <Link to="/workloads/new" className="btn btn-primary">
            <IconPlus size={16} aria-hidden="true" /> New workload
          </Link>
        )}
      />
      {error && <Notice tone="error">{error}</Notice>}
      {canManage && <Templates />}
      {list && list.length === 0 && (
        <section className="empty">
          <div className="empty-art empty-art-lanes" aria-hidden="true">
            <span className="lane" />
            <span className="lane lane-hot" />
            <span className="lane" />
          </div>
          <div className="empty-copy">
            <h2>No workloads yet</h2>
            <p>
              A workload is a program you run on your machines: a container or a command, kept running as a service or run
              once as a task. To serve a model, use Models instead: it sets up the engine for you.{" "}
              {canManage ? "" : "Admins and operators start workloads; they show up here with their status."}
            </p>
            {canManage && (
              <p className="empty-actions">
                <Link to="/workloads/new" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> New workload</Link>
                <Link to="/models" className="btn btn-quiet">Serve a model</Link>
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
          <div className="workloads">
            {list.map((w) => <WorkloadCard key={w.id} w={w} />)}
          </div>
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
    <dl className="fleet" aria-label="Workloads">
      <div><dt>Workloads</dt><dd>{list.length}</dd></div>
      <div><dt>Running</dt><dd className="fleet-online">{running}</dd></div>
      <div><dt>Waiting</dt><dd>{waiting}</dd></div>
      <div><dt>Failing</dt><dd className={failed ? "fleet-bad" : undefined}>{failed}</dd></div>
      <div><dt>GPUs in use</dt><dd>{gpus}</dd></div>
    </dl>
  );
}

function WorkloadCard({ w }: { w: Workload }) {
  return (
    <article className={`wl wl-${STATUS_TONE[w.status]}`} aria-label={`Workload ${w.name}`}>
      <header className="wl-head">
        <div className="wl-title">
          <span className={`dot dot-tone-${STATUS_TONE[w.status]}`} aria-hidden="true" />
          <h2 className="mono"><Link to={`/workloads/${w.id}`}>{w.name}</Link></h2>
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
    </li>
  );
}

// Ready-made templates (1.3): vision services exposed at /x/<name>, on the CPU or an NVIDIA GPU.
function Templates() {
  const { data } = useApi<{ templates: WorkloadTemplate[] }>("/workloads/templates");
  if (!data?.templates?.length) return null;
  return (
    <section className="panel tpl-panel" aria-labelledby="tpl-title">
      <div className="panel-head">
        <h2 id="tpl-title"><IconTemplate size={18} stroke={1.7} aria-hidden="true" /> Start from a template</h2>
        <HelpLink page="how-to" anchor="start-a-workload-from-a-template">What they are</HelpLink>
      </div>
      <p className="muted small">A vision API on Forgeyard's own image, exposed at <code>/x/&lt;name&gt;</code> for API keys you allow.
        Permissively licensed models only; the model is in the image, so it also runs without the network.</p>
      <ul className="tpl-list">
        {data.templates.map((t) => (
          <li key={t.id} className="tpl" aria-label={`Template ${t.title}`}>
            <div className="tpl-head">
              <span className="strong">{t.title}</span>
              <span className="chip mono" title="The model's licence">{t.licence}</span>
            </div>
            <p className="small">{t.summary}</p>
            <p className="small muted"><a href={t.model_url} target="_blank" rel="noreferrer" className="mono">{t.model}</a> · <code>POST /x/{t.name}{t.endpoint}</code></p>
            <div className="row-gap">
              <Link className="btn btn-small" to={`/workloads/new?template=${t.id}&gpu=1`}>On an NVIDIA GPU</Link>
              <Link className="btn btn-quiet btn-small" to={`/workloads/new?template=${t.id}`}>On the CPU</Link>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
