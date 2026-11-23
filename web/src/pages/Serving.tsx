import { IconBrain, IconCloud, IconMicrophone, IconMovie, IconPhotoAi, IconPlus, IconScan, IconSortDescending, IconSpeakerphone, IconVectorBezier2 } from "@tabler/icons-react";
import { Link, Navigate, useLocation, useParams } from "react-router";

import { ServingTrials } from "../components/EngineTrials";
import { AdvisorBanner } from "../components/Advisor";
import { useT } from "../i18n/react";
import { KindMark, ModelKindIcon } from "../components/Fleet";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { Notice, PageHeader } from "../components/ui";
import { MODEL_KIND, groupByKind, machineKind, whereOnMachine } from "../lib/fleet";
import { TASK_LABEL, VISION_TASKS, downloadFraction, downloadText, gib } from "../lib/models";
import { engineHref, modelHref } from "../lib/pages";
import { FALLBACK_ANCHOR, fallbackProviders } from "../lib/routing";
import type { CatalogResponse, Deployment, DeploymentsResponse, Machine, MachinesResponse, RouteInfo, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { STATUS_TONE, stateLabel, statusLabel } from "../lib/workloads";
import { sleepState } from "../lib/sleep";
import { SleepBadge } from "../components/Sleep";
import { when } from "../lib/format";
import { usePoll, WORKLOADS_POLL_MS } from "../lib/poll";

// What each kind runs on, for the empty page: the kind's name (fleet.modelKind.*) and its engines (serving.kind.*).
const KINDS = [
  { icon: IconBrain, kind: "chat" },
  { icon: IconVectorBezier2, kind: "embed" },
  { icon: IconSortDescending, kind: "rerank" },
  { icon: IconMicrophone, kind: "asr" },
  { icon: IconSpeakerphone, kind: "tts" },
  { icon: IconPhotoAi, kind: "image" },
  { icon: IconMovie, kind: "video" },
  { icon: IconScan, kind: "vision" },
] as const;

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
  const t = useT();
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
        index={t("serving.index")}
        title={t("serving.title")}
        lead={t("serving.lead")}
        actions={canDeploy && (
          <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> {t("serving.deploy")}</Link>
        )}
      />
      <AdvisorBanner pages={["serving"]} />
      <HelpLinks>
        <HelpLink page="quick-start" anchor="5-add-a-recommended-model">{t("serving.help.first")}</HelpLink>
        <HelpLink page="concepts" anchor="deployments-and-the-gateway">{t("serving.help.gateway")}</HelpLink>
        <HelpLink page="troubleshooting" anchor="a-deployment-does-not-fit">{t("serving.help.fit")}</HelpLink>
        <HelpLink page="how-to" anchor="let-a-deployment-sleep-when-nobody-uses-it">{t("serving.help.sleep")}</HelpLink>
      </HelpLinks>
      {deps.error && <Notice tone="error">{deps.error}</Notice>}
      {/* 1.5.2 (#95, PR 4): engine trials that run now hold a card or RAM, but are not served. */}
      <ServingTrials list={deps.data?.trials} />
      {deps.data && deps.data.deployments.length === 0 && (
        <section className="empty">
          <div className="empty-art empty-art-lanes" aria-hidden="true">
            <span className="lane" /><span className="lane lane-hot" /><span className="lane" />
          </div>
          <div className="empty-copy">
            <h2>{t("serving.empty.title")}</h2>
            <p>
              {t.rich("serving.empty.body", { v1: <code>/v1</code> })}{" "}
              {!canDeploy ? t("serving.empty.cannot")
                : cat.data && cat.data.models.length === 0 ? t("serving.empty.noCatalog")
                  : t("serving.empty.choose")}
            </p>
            {canDeploy && (
              <p className="empty-actions">
                {cat.data && cat.data.models.length === 0
                  ? (cat.data.can_manage
                    ? <Link to="/models" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> {t("serving.empty.addModel")}</Link>
                    : <span className="muted">{t("serving.empty.catalogEmpty")}</span>)
                  : <Link to="/serving/deploy" className="btn btn-primary"><IconPlus size={16} aria-hidden="true" /> {t("serving.deploy")}</Link>}
              </p>
            )}
            <ul className="kinds">
              {KINDS.map(({ icon: Icon, kind }) => (
                <li key={kind}><Icon size={18} stroke={1.6} aria-hidden="true" /><span className="kind-name">{t(`fleet.modelKind.${kind}`)}</span><span className="kind-detail">{t(`serving.kind.${kind}`)}</span></li>
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
  const t = useT();
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
                <h2 id={`kind-${k}`}>{K.label} <span className="muted small">· {t.num(l.length)}</span></h2>
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
          <span className="muted">{t("serving.noneYet")}</span>
          {none.map((k) => <span key={k} className="kinds-none-item"><ModelKindIcon kind={k} size={14} /> {MODEL_KIND[k].label}</span>)}
          {canDeploy && <Link to="/serving/deploy">{t("serving.deploy")}</Link>}
        </p>
      )}
    </>
  );
}

export function DeploymentRow({ d, machines, cloud }: { d: Deployment; machines: Machine[]; cloud: string[] }) {
  const t = useT();
  const w = d.workload;
  const tone = STATUS_TONE[w.status];
  const sleeping = sleepState(d);   // 1.5.0: asleep, or woken with no replica ready yet
  const placed = w.replica_list.filter((r) => r.machine);
  // What needs saying about a replica: a download in progress, why it waits, why it is where it is.
  const notes = w.replica_list.flatMap((r) => {
    const frac = downloadFraction(r);
    const tag = w.replica_list.length > 1 ? `#${r.ordinal} ` : "";
    if (frac !== null) return [<span key={r.ordinal} className="deprow-note"><span className="bar bar-dl" aria-label={downloadText(r) ?? ""}><span style={{ width: `${Math.round(frac * 100)}%` }} /></span> {tag}{downloadText(r)}</span>];
    const text = r.not_picked_up ?? r.reason ?? r.placement_note;
    return text && w.status !== "stopped" && w.status !== "asleep" && sleeping !== "waking" ? [<span key={r.ordinal} className={`deprow-note${r.not_picked_up ? " rep-stuck" : ""}`}>{tag}{text}</span>] : [];
  });
  return (
    <div className="deprow" role="group" aria-label={t("serving.row", { name: d.name })}>
      <div className="deprow-main">
        <div className="deprow-name">
          <span className={`dot ${sleeping ? `dot-${sleeping}` : `dot-tone-${tone}`}`} aria-hidden="true" />
          <Link to={`/serving/${d.id}`}>{d.name}</Link>
          {cloud.length > 0 && (
            <Link className="fallback-mark" to={`/serving/${d.id}#${FALLBACK_ANCHOR}`}
              title={t("serving.fallbackTitle", { name: d.name, providers: cloud.join(", ") })}>
              <IconCloud size={12} aria-hidden="true" /> → {cloud.join(", ")}
            </Link>
          )}
        </div>
        <div className="deprow-model">{VISION_TASKS.includes(d.model.task) && t.rich("serving.taskAt", {
          task: <span className="deprow-task">{TASK_LABEL[d.model.task]}</span>,
          path: <code>/v1/vision/{d.model.task === "image-classify" ? "classify" : d.model.task}</code>,
        })}<Link to={modelHref(d.model.name)}>{d.model.name}</Link> · <Link to={engineHref(d.recipe)}>{d.recipe}</Link>{d.served_name !== d.name ? t("serving.as", { name: d.served_name }) : null}</div>
      </div>
      <div className="where">
        {sleeping === "asleep" ? <AsleepWhere d={d} /> : placed.length ? placed.map((r) => {
          const m = machines.find((x) => x.id === r.machine!.id);
          return (
            <Link key={r.ordinal} to={`/machines/${r.machine!.id}`} className="where-chip" title={t("serving.replicaTitle", { n: String(r.ordinal), state: stateLabel(r) })}>
              {m && <KindMark kind={machineKind(m)} size={20} />}
              <span className="mono">{r.machine!.name}</span>
              <span>{whereOnMachine(r)}</span>
            </Link>
          );
        }) : <span className="small muted">{w.status === "stopped" ? t("serving.stopped") : t("serving.waitingMachine")}</span>}
      </div>
      <span className="deprow-mem">{d.estimate.device === "gpu" ? t("serving.perGpu", { size: gib(d.estimate.per_gpu) }) : t("serving.ram", { size: gib(d.estimate.ram) })}</span>
      <SleepBadge d={d} fallback={<span className={`chip chip-tone-${tone} deprow-state`}>{w.status === "running"
        ? t("serving.ready", { ready: w.ready, replicas: w.replicas }) : statusLabel(w.status)}</span>} />
      {notes.length > 0 && <div className="deprow-notes">{notes}</div>}
    </div>
  );
}

// Where an asleep deployment is: nowhere (it holds nothing), and where it wakes.
function AsleepWhere({ d }: { d: Deployment }) {
  const t = useT();
  const names = [...new Set(d.workload.replica_list.map((r) => r.machine?.name).filter(Boolean))] as string[];
  const on = d.pinned?.name ?? names.join(", ");
  return (
    <span className="small muted">
      {t("serving.holdsNothing")}{d.sleep?.asleep_since ? t("serving.asleepSince", { when: when(d.sleep.asleep_since) }) : ""}
      {on ? t.rich("serving.wakesOn", { machine: <span className="mono">{on}</span> }) : null}
    </span>
  );
}
