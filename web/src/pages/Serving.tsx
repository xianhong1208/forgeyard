import { IconBrain, IconCloud, IconMicrophone, IconMovie, IconPhotoAi, IconPlus, IconScan, IconSortDescending, IconSpeakerphone, IconVectorBezier2 } from "@tabler/icons-react";
import { Link, Navigate, useLocation, useParams } from "react-router";

import { AdvisorBanner } from "../components/Advisor";
import { KindMark, ModelKindIcon } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Notice, PageHeader } from "../components/ui";
import { MODEL_KIND, groupByKind, machineKind, whereOnMachine } from "../lib/fleet";
import { TASK_LABEL, VISION_TASKS, downloadFraction, downloadText, gib } from "../lib/models";
import { FALLBACK_ANCHOR, fallbackProviders } from "../lib/routing";
import type { CatalogResponse, Deployment, DeploymentsResponse, Machine, MachinesResponse, RouteInfo, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATUS_TONE, stateLabel } from "../lib/workloads";
import { usePoll, WORKLOADS_POLL_MS } from "./Workloads";

const KINDS = [
  { icon: IconBrain, name: "Chat", detail: "vLLM for safetensors, llama.cpp for GGUF (GPU or CPU)" },
  { icon: IconVectorBezier2, name: "Embeddings", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconSortDescending, name: "Rerank", detail: "vLLM's pooling runner; small, shares a GPU" },
  { icon: IconMicrophone, name: "Speech to text", detail: "faster-whisper behind speaches" },
  { icon: IconSpeakerphone, name: "Text to speech", detail: "Qwen3-TTS on vLLM-Omni, llama.cpp TTS GGUFs, Kokoro on speaches" },
  { icon: IconPhotoAi, name: "Images", detail: "diffusers pipelines on vLLM-Omni; stable-diffusion.cpp on AMD (Vulkan), NVIDIA (CUDA) and CPUs" },
  { icon: IconMovie, name: "Video", detail: "Wan text-to-video pipelines on vLLM-Omni (NVIDIA), as jobs at /v1/videos" },
  { icon: IconScan, name: "Vision", detail: "Object detection, segmentation and depth on Forgeyard's vision server (CPU or NVIDIA), at /v1/vision/…" },
];

// 1.3's addresses under Models (/models/deploy, /models/deployments/<id>[/edit]) open the same page
// under Serving, keeping the query (?model=, ?machine=) and the anchor (#cloud-fallback).
export function OldDeploy() {
  const { search } = useLocation();
  return <Navigate to={`/serving/deploy${search}`} replace />;
}

export function OldDeployment() {
  const rest = useParams()["*"] ?? "";
  const { search, hash } = useLocation();
  const m = /^(\d+)(\/edit)?\/?$/.exec(rest);
  return <Navigate to={m ? `/serving/${m[1]}${m[2] ?? ""}${search}${hash}` : "/serving"} replace />;
}

// 1.4 (#92): Serving, the models running now (the Deployments tab of Models before), grouped by what
// they do, with Deploy a model. A deployment's page is /serving/<id>; the library is under Models.
export function ServingPage() {
  const deps = useApi<DeploymentsResponse>("/deployments");
  const { data: machines } = useApi<MachinesResponse>("/machines");
  const cat = useApi<CatalogResponse>("/models");
  // For the "→ provider" mark of a deployment with a cloud fallback.
  const routes = useApi<RoutesResponse>("/routes");
  usePoll(deps.reload, WORKLOADS_POLL_MS);
  const canDeploy = cat.data?.can_deploy ?? false;

  return (
    <>
      <PageHeader
        index="02 · Serving"
        title="Serving"
        lead="The models running now, grouped by what they do; a deployment's page also holds its cloud fallback. Every ready deployment answers at /v1 with an API key, and in the Playground. What you can deploy is under Models."
        actions={canDeploy && (
          <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>
        )}
      />
      <AdvisorBanner pages={["serving"]} />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="5-add-a-recommended-model">How do I deploy my first model?</HelpLink>
        <HelpLink page="concepts" anchor="deployments-and-the-gateway">Deployments and the gateway</HelpLink>
        <HelpLink page="troubleshooting" anchor="a-deployment-does-not-fit">A deployment does not fit</HelpLink>
      </HelpLinks>
      {deps.error && <Notice tone="error">{deps.error}</Notice>}
      {deps.data && deps.data.deployments.length === 0 && (
        <section className="empty">
          <div className="empty-art empty-art-lanes" aria-hidden="true">
            <span className="lane" /><span className="lane lane-hot" /><span className="lane" />
          </div>
          <div className="empty-copy">
            <h2>No deployments yet</h2>
            <p>
              A deployment runs a model on one of your machines and makes it callable at <code>/v1</code>.{" "}
              {!canDeploy ? "Admins and operators deploy models; once one is ready, try it in the Playground or call it with an API key."
                : cat.data && cat.data.models.length === 0 ? "Start by adding a model to the catalog, then deploy it."
                  : "Choose a model from the catalog; Forgeyard estimates its memory and picks a machine where it fits."}
            </p>
            {canDeploy && (
              <p className="empty-actions">
                {cat.data && cat.data.models.length === 0
                  ? (cat.data.can_manage
                    ? <Link to="/models" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Add a model</Link>
                    : <span className="muted">The catalog is empty: an admin adds models.</span>)
                  : <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> Deploy a model</Link>}
              </p>
            )}
            <ul className="kinds">
              {KINDS.map(({ icon: Icon, name, detail }) => (
                <li key={name}><Icon size={18} stroke={1.6} aria-hidden="true" /><span className="kind-name">{name}</span><span className="kind-detail">{detail}</span></li>
              ))}
            </ul>
          </div>
        </section>
      )}
      {deps.data && deps.data.deployments.length > 0 && (
        <ByKind list={deps.data.deployments} machines={machines?.machines ?? []} canDeploy={canDeploy} routes={routes.data?.routes ?? []} />
      )}
    </>
  );
}

// 1.4 (#34): the deployments in one section per kind, each with its icon, what it does and its path
// at /v1; one row per deployment with where it runs, its memory and its state. Kinds with none are
// named once at the end.
function ByKind({ list, machines, canDeploy, routes }: { list: Deployment[]; machines: Machine[]; canDeploy: boolean; routes: RouteInfo[] }) {
  const groups = groupByKind(list);
  const none = groups.filter(([, l]) => l.length === 0).map(([k]) => k);
  return (
    <>
      {groups.filter(([, l]) => l.length > 0).map(([k, l]) => {
        const K = MODEL_KIND[k];
        return (
          <section key={k} className="panel kindsec" aria-labelledby={`kind-${k}`}>
            <div className="kindsec-head">
              <span className="kindicon"><ModelKindIcon kind={k} size={18} /></span>
              <div className="kindsec-title">
                <h2 id={`kind-${k}`}>{K.label} <span className="muted small">· {l.length}</span></h2>
                <span className="muted">{K.hint}</span>
              </div>
              <span className="kindsec-api mono">{K.api}</span>
            </div>
            {l.map((d) => <DeploymentRow key={d.id} d={d} machines={machines} cloud={fallbackProviders(routes, d.id)} />)}
          </section>
        );
      })}
      {none.length > 0 && (
        <p className="kinds-none small">
          <span className="muted">None deployed yet:</span>
          {none.map((k) => <span key={k} className="kinds-none-item"><ModelKindIcon kind={k} size={14} /> {MODEL_KIND[k].label}</span>)}
          {canDeploy && <Link to="/serving/deploy">Deploy a model</Link>}
        </p>
      )}
    </>
  );
}

function DeploymentRow({ d, machines, cloud }: { d: Deployment; machines: Machine[]; cloud: string[] }) {
  const w = d.workload;
  const tone = STATUS_TONE[w.status];
  const placed = w.replica_list.filter((r) => r.machine);
  // What needs saying about a replica: a download in progress, why it waits, why it is where it is.
  const notes = w.replica_list.flatMap((r) => {
    const frac = downloadFraction(r);
    const tag = w.replica_list.length > 1 ? `#${r.ordinal} ` : "";
    if (frac !== null) return [<span key={r.ordinal} className="deprow-note"><span className="bar bar-dl" aria-label={downloadText(r) ?? ""}><span style={{ width: `${Math.round(frac * 100)}%` }} /></span> {tag}{downloadText(r)}</span>];
    const text = r.not_picked_up ?? r.reason ?? r.placement_note;
    return text && w.status !== "stopped" ? [<span key={r.ordinal} className={`deprow-note${r.not_picked_up ? " rep-stuck" : ""}`}>{tag}{text}</span>] : [];
  });
  return (
    <div className="deprow" role="group" aria-label={`Deployment ${d.name}`}>
      <div className="deprow-main">
        <div className="deprow-name">
          <span className={`dot dot-tone-${tone}`} aria-hidden="true" />
          <Link to={`/serving/${d.id}`}>{d.name}</Link>
          {cloud.length > 0 && (
            <Link className="fallback-mark" to={`/serving/${d.id}#${FALLBACK_ANCHOR}`}
              title={`When ${d.name} is busy or down, requests may move on to ${cloud.join(", ")}`}>
              <IconCloud size={12} aria-hidden="true" /> → {cloud.join(", ")}
            </Link>
          )}
        </div>
        <div className="deprow-model">{VISION_TASKS.includes(d.model.task) && <><span className="deprow-task">{TASK_LABEL[d.model.task]}</span> at <code>/v1/vision/{d.model.task}</code> · </>}{d.model.name} · {d.recipe}{d.served_name !== d.name ? <> · as “{d.served_name}”</> : null}</div>
      </div>
      <div className="where">
        {placed.length ? placed.map((r) => {
          const m = machines.find((x) => x.id === r.machine!.id);
          return (
            <Link key={r.ordinal} to={`/machines/${r.machine!.id}`} className="where-chip" title={`Replica #${r.ordinal}: ${stateLabel(r)}`}>
              {m && <KindMark kind={machineKind(m)} size={20} />}
              <span className="mono">{r.machine!.name}</span>
              <span>{whereOnMachine(r)}</span>
            </Link>
          );
        }) : <span className="small muted">{w.status === "stopped" ? "stopped" : "waiting for a machine"}</span>}
      </div>
      <span className="deprow-mem">{d.estimate.device === "gpu" ? `${gib(d.estimate.per_gpu)} / GPU` : `${gib(d.estimate.ram)} RAM`}</span>
      <span className={`chip chip-tone-${tone} deprow-state`}>{w.status === "running" ? `${w.ready}/${w.replicas} ready` : w.status}</span>
      {notes.length > 0 && <div className="deprow-notes">{notes}</div>}
    </div>
  );
}
