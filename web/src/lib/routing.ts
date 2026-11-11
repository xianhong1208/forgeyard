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
