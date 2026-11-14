// The generic passthrough (1.3): service workloads exposed behind the gateway at /x/<name>.

import type { ApiKey, Workload, WorkloadAccess, WorkloadGatewaySettings } from "./types";

export const ACCESS_LEVELS: WorkloadAccess[] = ["none", "listed", "all"];

export const ACCESS_LABEL: Record<WorkloadAccess, string> = {
  none: "None",
  listed: "Listed workloads",
  all: "Every exposed workload",
};

export const ACCESS_HELP: Record<WorkloadAccess, string> = {
  none: "This key cannot call workloads at /x/ (the default).",
  listed: "Only the workloads you tick, at /x/<name>.",
  all: "Every workload an admin or operator exposes at /x/, now and later.",
};

export function keyWorkloads(k: ApiKey): string {
  const level = k.workloads ?? "none";
  if (level === "all") return "every exposed one";
  if (level === "listed") return (k.workload_list ?? []).join(", ") || "none";
  return "none";
}

export const DEFAULT_GATEWAY: WorkloadGatewaySettings = {
  expose: false, port: null, read_timeout: 600, max_body_mb: 32, concurrency: null, key_concurrency: null,
};

// The form's text fields: empty means the default (no limit for the two concurrency fields).
export type GatewayForm = {
  expose: boolean; port: string; readTimeout: string; maxBodyMb: string; concurrency: string; keyConcurrency: string;
};

export function gatewayForm(g?: WorkloadGatewaySettings | null): GatewayForm {
  const v = g ?? DEFAULT_GATEWAY;
  return {
    expose: v.expose, port: v.port ?? "", readTimeout: String(v.read_timeout), maxBodyMb: String(v.max_body_mb),
    concurrency: v.concurrency ? String(v.concurrency) : "", keyConcurrency: v.key_concurrency ? String(v.key_concurrency) : "",
  };
}

function int(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : NaN;
}

// What the API takes: only what differs from the defaults, so a plain "expose" stays {expose: true}.
export function buildGateway(f: GatewayForm): Record<string, unknown> {
  const out: Record<string, unknown> = { expose: f.expose };
  if (f.port) out.port = f.port;
  const rt = int(f.readTimeout);
  if (rt !== null && rt !== DEFAULT_GATEWAY.read_timeout) out.read_timeout = rt;
  const mb = int(f.maxBodyMb);
  if (mb !== null && mb !== DEFAULT_GATEWAY.max_body_mb) out.max_body_mb = mb;
  const c = int(f.concurrency);
  if (c !== null) out.concurrency = c;
  const kc = int(f.keyConcurrency);
  if (kc !== null) out.key_concurrency = kc;
  return out;
}

export function xUrl(base: string, name: string): string {
  return `${base.replace(/\/$/, "")}/x/${name}`;
}

// A curl call to an exposed workload, with the server's CA when it uses its own.
export function xCurl(base: string, name: string, trustUrl?: string | null, path = "/"): string {
  const curl = trustUrl ? `# forgeyard-ca.pem: this server's certificate authority, from ${trustUrl}\ncurl --cacert forgeyard-ca.pem` : "curl";
  return `${curl} ${xUrl(base, name)}${path} \\\n  -H "Authorization: Bearer $FORGEYARD_API_KEY"`;
}

export function isExposed(w: Workload): boolean {
  return !!w.gateway?.exposed;
}
