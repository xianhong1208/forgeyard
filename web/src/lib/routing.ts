// Routes with tiers and cloud providers (1.2): labels shared by the routes, providers, keys and
// playground pages.

import { labels, t } from "../i18n/core";

const tr = t;
import type { CloudLevel, RouteInfo, RouteKind, RouteTarget } from "./types";

export const CLOUD_LEVELS: CloudLevel[] = ["off", "fallback", "on"];

export const CLOUD_LABEL: Record<CloudLevel, string> = labels({
  off: "keys.cloud.off",
  fallback: "keys.cloud.fallback",
  on: "keys.cloud.on",
});

export const CLOUD_HELP: Record<CloudLevel, string> = labels({
  off: "keys.cloud.off.help",
  fallback: "keys.cloud.fallback.help",
  on: "keys.cloud.on.help",
});

export const ROUTE_KIND_LABEL: Record<RouteKind, string> = labels({ local: "cloud.route.local", mixed: "cloud.route.mixed", cloud: "cloud.route.cloud" });

export const ROUTE_KIND_TONE: Record<RouteKind, string> = { local: "ok", mixed: "run", cloud: "wait" };

export function cloudAbove(a: CloudLevel, b: CloudLevel): boolean {
  return CLOUD_LEVELS.indexOf(a) > CLOUD_LEVELS.indexOf(b);
}

export function targetName(t: RouteTarget): string {
  if (t.deployment) return t.deployment.name;
  if (t.provider_model) return `${t.provider_model.provider.name}/${t.provider_model.upstream_model}`;
  return tr("cloud.route.removed");
}

export function targetWhere(t: RouteTarget): string {
  if (t.deployment) {
    const d = t.deployment;
    const counts = { ready: String(d.ready), replicas: String(d.replicas) };
    return d.concurrency ? tr("cloud.route.readyEach", { ...counts, n: String(d.concurrency) }) : tr("cloud.route.ready", counts);
  }
  if (t.provider_model) {
    const p = t.provider_model.provider;
    const where = tr(p.external ? "cloud.route.providerCloud" : "cloud.route.providerInside");
    return p.enabled ? where : tr("cloud.route.withDisabled", { where });
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
  if (route.local_only) return t("cloud.spill.localOnly");
  if (tiers(route).length < 2) return t("cloud.spill.oneTier");
  const wait = route.wait_ms ? t("cloud.spill.wait", { ms: String(route.wait_ms) }) : "";
  return t(route.spill_on_long_prompt ? "cloud.spill.rulesLong" : "cloud.spill.rules", { wait });
}

export function targetHeader(target: string | null): string | null {
  if (!target) return null;
  if (target.startsWith("local:")) return t("cloud.route.targetLocal", { name: target.slice(6) });
  if (target.startsWith("provider:")) return target.slice(9);
  return target;
}

// 1.4 (#81): where each route lives in the console. A deployment's own route is its cloud fallback,
// on the deployment's page. A route an admin made under a name of its own lives on Cloud → Cloud
// models: with a provider's model in it it is a cloud model; with deployments only it is "another
// name" for them (several deployments behind one name, or one under a second name).
export const FALLBACK_ANCHOR = "cloud-fallback";

export function routeHome(r: Pick<RouteInfo, "id" | "deployment_id">): string {
  return r.deployment_id !== null ? `/serving/${r.deployment_id}#${FALLBACK_ANCHOR}` : `/cloud/models/${r.id}`;
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
