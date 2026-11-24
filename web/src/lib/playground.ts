// The Playground's one model picker (1.4, #100): the ready models, grouped by kind the way Serving groups
// deployments (the same kinds, labels and icons), each kind opening its own panel.

import { labels, t, translate } from "../i18n/core";
import { getLang } from "../i18n/lang";
import { serverMessage } from "../i18n/server";
import { notifyUnauthorized } from "./api";
import { API_KIND, MAX_IMAGE_BYTES } from "./gateway";
import { MODEL_KIND_ORDER, modelKind, type ModelKind } from "./fleet";
import { sleepState, type SleepState } from "./sleep";
import type { Deployment, RouteInfo, RouteKind } from "./types";

// Where a route's answers come from, in the picker's small print (the same words as routing's ROUTE_KIND_LABEL).
const ROUTE_KIND_TEXT: Record<RouteKind, string> = labels({
  local: "playground.routeKind.local", mixed: "playground.routeKind.mixed", cloud: "playground.routeKind.cloud",
});

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
  if (r.kind && r.kind !== "local") parts.push(ROUTE_KIND_TEXT[r.kind]);
  return parts.join(" · ");
}

// 1.3's Vision tab (/playground?tab=vision) is gone: its address opens the first ready vision model, or
// null when there is none (the picker opens instead).
export function firstVision(groups: [ModelKind, RouteInfo[]][]): string | null {
  return groups.find(([k]) => k === "vision")?.[1][0]?.served_name ?? null;
}

// What went wrong with a request, in plain words (#189): the status and the gateway's message, as sent in English;
// in Chinese a known error code in its translation (the server's specifics kept), else the message as sent.
export function playError(status: number, body: unknown, lang = getLang()): string {
  const b = body as { error?: { message?: string; code?: string }; detail?: { message?: string; code?: string } } | null;
  const message = b?.error?.message ?? b?.detail?.message;
  if (message) {
    const code = b?.error?.message !== undefined ? b?.error?.code : b?.detail?.code;
    return translate(lang, "playground.error.status", { status: String(status), message: lang === "en" ? message : serverMessage(code, message, lang) });
  }
  return translate(lang, status >= 500 ? "playground.error.server" : "playground.error.failed", { status: String(status) });
}

// Why an image cannot be attached to a chat message (null: it can), before it is read (gateway's readImage rules).
export function imageProblem(file: File): string | null {
  if (!file.type.startsWith("image/")) return t("playground.image.notImage", { name: file.name });
  if (file.size > MAX_IMAGE_BYTES) return t("playground.image.tooLarge", { name: file.name, mb: String(MAX_IMAGE_BYTES / 1024 / 1024) });
  return null;
}

// "45 s", "1 min 05 s": a video job's elapsed time (gateway's elapsed, in the reader's words).
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? t("playground.elapsed.s", { s: String(s) })
    : t("playground.elapsed.m", { m: String(Math.floor(s / 60)), s: String(s % 60).padStart(2, "0") });
}

// 1.5.0: a request from the Playground was sent (an asleep model starts waking: the page looks again soon).
export const SENT_EVENT = "forgeyard:playground-sent";

// The playground talks to /api/playground/<endpoint>: the gateway's routing, as the signed-in
// user (the session cookie and the CSRF header), counted as "console" in usage. No API key.
export async function post(endpoint: string, body: unknown, signal?: AbortSignal, form?: FormData, localOnly = false): Promise<Response> {
  window.dispatchEvent(new Event(SENT_EVENT));
  const headers: Record<string, string> = { "X-Forgeyard": "1" };
  if (localOnly) headers["X-Forgeyard-Local-Only"] = "1";
  if (!form) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/playground/${endpoint}`, {
    method: "POST", credentials: "same-origin", headers, signal, body: form ?? JSON.stringify(body),
  });
  if (res.status === 401) {
    const data = await res.json().catch(() => null);
    notifyUnauthorized((data as { detail?: { reason?: string } } | null)?.detail?.reason);
  }
  return res;
}

// A failed answer's text (playError).
export async function failure(res: Response): Promise<string> {
  const data = await res.json().catch(() => null);
  return playError(res.status, data);
}

// The time a request took is measured in its event handler (a clock read outside render).
export function now(): number {
  return performance.now();
}
