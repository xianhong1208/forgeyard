// The Playground's one model picker (1.4, #100): the ready models, grouped by kind the way Serving groups
// deployments (the same kinds, labels and icons), each kind opening its own panel.

import { API_KIND } from "./gateway";
import { MODEL_KIND_ORDER, modelKind, type ModelKind } from "./fleet";
import { ROUTE_KIND_LABEL } from "./routing";
import { sleepState, type SleepState } from "./sleep";
import type { Deployment, RouteInfo } from "./types";

// The kind of a route's protocol. A chat model whose deployment takes images, audio or video is
// multimodal chat, as on Serving; a cloud-only model has no deployment, so it is plain chat.
const API_MODEL_KIND: Record<string, ModelKind> = {
  "openai-chat": "chat", "openai-embeddings": "embed", rerank: "rerank", "openai-audio": "asr", "openai-speech": "tts",
  "openai-images": "image", "openai-videos": "video", "vision-detect": "vision", "vision-segment": "vision", "vision-depth": "vision", "vision-classify": "vision",
  classify: "classify",
};

export function routeKind(r: RouteInfo, deployments: Deployment[]): ModelKind | null {
  const k = API_MODEL_KIND[r.api];
  if (k !== "chat" || r.deployment_id == null) return k ?? null;
  const d = deployments.find((x) => x.id === r.deployment_id);
  return d && modelKind(d) === "multimodal" ? "multimodal" : "chat";
}

// Ready: a replica answers, or a cloud provider does.
export function routeReady(r: RouteInfo): boolean {
  return r.endpoints.length > 0 || r.kind === "cloud";
}

// 1.5.0 (#37): a route whose own deployment is asleep or waking: offered too, with the moon (a request wakes
// it, or Wake now does).
export function routeSleep(r: RouteInfo, deployments: Deployment[]): SleepState {
  if (r.deployment_id == null) return null;
  const d = deployments.find((x) => x.id === r.deployment_id);
  return d ? sleepState(d) : null;
}

function stillWaking(r: RouteInfo, deployments: Deployment[], woken: ReadonlySet<number>): boolean {
  if (r.deployment_id == null || !woken.has(r.deployment_id)) return false;
  return deployments.find((x) => x.id === r.deployment_id)?.workload?.desired_state === "running";
}

// What the picker offers: the models the person may use that are ready now, or asleep or waking (1.5.0), one
// group per kind in Serving's order; kinds with none are left out. A protocol the console has no panel for is
// not offered. `woken`: deployments this page saw asleep or waking; they stay offered while they run, so the
// moment between "no longer waking" and "its replica is listed as ready" (two lists, asked one after the other)
// never takes the model, and the answer it is writing, off the page.
export function pickerGroups(routes: RouteInfo[], deployments: Deployment[], woken: ReadonlySet<number> = new Set()): [ModelKind, RouteInfo[]][] {
  const out = new Map<ModelKind, RouteInfo[]>(MODEL_KIND_ORDER.map((k) => [k, []]));
  for (const r of routes) {
    if (r.usable === false || (!routeReady(r) && !routeSleep(r, deployments) && !stillWaking(r, deployments, woken))) continue;
    const k = routeKind(r, deployments);
    if (k) out.get(k)?.push(r);
  }
  return [...out].filter(([, l]) => l.length > 0);
}

// The small print beside a model in the picker: the vision task, and where a route's answers come from
// when that is not only this server's machines.
export function pickerDetail(r: RouteInfo): string {
  const parts: string[] = [];
  if (r.api.startsWith("vision-")) parts.push(API_KIND[r.api] ?? r.api);
  if (r.kind && r.kind !== "local") parts.push(ROUTE_KIND_LABEL[r.kind]);
  return parts.join(" · ");
}

// 1.3's Vision tab (/playground?tab=vision) is gone: its address opens the first ready vision model, or
// null when there is none (the picker opens instead).
export function firstVision(groups: [ModelKind, RouteInfo[]][]): string | null {
  return groups.find(([k]) => k === "vision")?.[1][0]?.served_name ?? null;
}
