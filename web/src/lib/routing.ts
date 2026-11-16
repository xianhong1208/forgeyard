// Routes with tiers and cloud providers (1.2): labels shared by the routes, providers, keys and
// playground pages.

import type { CloudLevel, RouteInfo, RouteKind, RouteTarget } from "./types";

export const CLOUD_LEVELS: CloudLevel[] = ["off", "fallback", "on"];

export const CLOUD_LABEL: Record<CloudLevel, string> = {
  off: "Never",
  fallback: "Only to spill",
  on: "Allowed",
};

export const CLOUD_HELP: Record<CloudLevel, string> = {
  off: "Requests never leave your network. Cloud-only models do not exist for this key.",
  fallback: "A local model may spill to the cloud when local is busy or down. Cloud-only models do not exist for this key.",
  on: "Also cloud-only models (listed in /v1/models).",
};

export const ROUTE_KIND_LABEL: Record<RouteKind, string> = { local: "local", mixed: "local, then cloud", cloud: "cloud only" };

export const ROUTE_KIND_TONE: Record<RouteKind, string> = { local: "ok", mixed: "run", cloud: "wait" };

export function cloudAbove(a: CloudLevel, b: CloudLevel): boolean {
  return CLOUD_LEVELS.indexOf(a) > CLOUD_LEVELS.indexOf(b);
}

export function targetName(t: RouteTarget): string {
  if (t.deployment) return t.deployment.name;
  if (t.provider_model) return `${t.provider_model.provider.name}/${t.provider_model.upstream_model}`;
  return "(removed)";
}

export function targetWhere(t: RouteTarget): string {
  if (t.deployment) return `${t.deployment.ready}/${t.deployment.replicas} ready${t.deployment.concurrency ? ` · ${t.deployment.concurrency} at once each` : ""}`;
  if (t.provider_model) {
    const p = t.provider_model.provider;
    return `${p.external ? "cloud" : "inside your network"}${p.enabled ? "" : " · provider disabled"}`;
  }
  return "";
}

// Tiers in order, each with its targets.
export function tiers(route: RouteInfo): RouteTarget[][] {
  const out: RouteTarget[][] = [];
  for (const t of route.targets) {
    (out[t.tier] ??= []).push(t);
  }
  return out.filter(Boolean);
}

// "Local: chat (1/1 ready) → then OpenAI/gpt-4o-mini"
export function tierSummary(route: RouteInfo): string {
  return tiers(route)
    .map((tier, i) => `${i === 0 ? "" : "then "}${tier.map(targetName).join(" + ")}`)
    .join(" → ");
}

export function spillRules(route: RouteInfo): string {
  if (route.local_only) return "Never leaves your network (local only).";
  if (tiers(route).length < 2) return "One tier: nothing to spill to.";
  const when = ["no local replica is ready", "every replica is busy", "the engine answers 429 or 503"];
  if (route.spill_on_long_prompt) when.push("the prompt is longer than the local context");
  const wait = route.wait_ms ? ` (after waiting up to ${route.wait_ms} ms for a slot)` : "";
  return `Moves to the next tier when ${when.join(", ")}${wait}. Never once the answer has started.`;
}

export function targetHeader(target: string | null): string | null {
  if (!target) return null;
  if (target.startsWith("local:")) return `local ${target.slice(6)}`;
  if (target.startsWith("provider:")) return target.slice(9);
  return target;
}

// 1.4 (#81): where each route lives in the console. A deployment's own route is its cloud fallback,
// on the deployment's page. A route an admin made under a name of its own lives on Cloud → Cloud
// models: with a provider's model in it it is a cloud model; with deployments only it is "another
// name" for them (several deployments behind one name, or one under a second name).
export const FALLBACK_ANCHOR = "cloud-fallback";

export function routeHome(r: Pick<RouteInfo, "id" | "deployment_id">): string {
  return r.deployment_id !== null ? `/models/deployments/${r.deployment_id}#${FALLBACK_ANCHOR}` : `/cloud/models/${r.id}`;
}

// Among those names: one with a provider's model in it (a provider inside your network counts too).
export function isCloudModel(r: RouteInfo): boolean {
  return r.targets.some((t) => t.kind === "provider");
}

export function ownNames(routes: RouteInfo[]): { cloud: RouteInfo[]; other: RouteInfo[] } {
  const own = routes.filter((r) => r.deployment_id === null);
  return { cloud: own.filter(isCloudModel), other: own.filter((r) => !isCloudModel(r)) };
}

// The providers a deployment's own route sends requests to when it moves on (switched-on provider
// targets), for the "→ Gemini" mark in the Deployments list.
export function fallbackProviders(routes: RouteInfo[], deploymentId: number): string[] {
  const own = routes.find((r) => r.deployment_id === deploymentId);
  if (!own) return [];
  const names = own.targets.filter((t) => t.kind === "provider" && t.enabled && t.provider_model).map((t) => t.provider_model!.provider.name);
  return [...new Set(names)];
}
