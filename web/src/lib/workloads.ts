import type { Accelerator, Machine, PortStatus, Replica, ReplicaState, RuntimeInfo, Workload, WorkloadStatus } from "./types";

// ---------------------------------------------------------------- the create form
export type EnvRow = { key: string; value: string };
export type PortRow = { name: string; port: string };

export type WorkloadForm = {
  kind: "service" | "task";
  name: string;
  runtime: "container" | "process";
  image: string;
  entrypoint: string; // container: replaces the image's entrypoint, one item per line
  command: string; // process: the program
  args: string; // one argument per line
  workdir: string;
  machine: string; // "" = any online machine with room
  gpuMode: "none" | "count" | "indices";
  gpuCount: string;
  gpuIndices: number[];
  vendor: "" | "nvidia" | "amd";
  cpu: string;
  memory: string;
  replicas: string;
  env: EnvRow[];
  ports: PortRow[];
  health: "none" | "http" | "tcp";
  healthPath: string;
  healthPort: string;
  healthInterval: string;
  restart: "always" | "on-failure" | "never";
  maxDuration: string;
  retries: string;
  retryBackoff: string;
};

export function emptyForm(): WorkloadForm {
  return {
    kind: "service", name: "", runtime: "container", image: "", entrypoint: "", command: "", args: "", workdir: "",
    machine: "", gpuMode: "none", gpuCount: "1", gpuIndices: [], vendor: "", cpu: "", memory: "", replicas: "1",
    env: [], ports: [{ name: "http", port: "8000" }], health: "none", healthPath: "/", healthPort: "http",
    healthInterval: "10", restart: "always", maxDuration: "", retries: "0", retryBackoff: "5",
  };
}

export function lines(text: string): string[] {
  return text.split("\n").map((l) => l.replace(/\r$/, "")).filter((l) => l.trim() !== "");
}

function num(text: string): number | null {
  const t = text.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : NaN;
}

// The JSON the API takes. Validation stays on the server (one set of rules); this only shapes
// the form into the spec, leaving out what does not apply to the chosen kind and runtime.
export function buildSpec(f: WorkloadForm): Record<string, unknown> {
  const spec: Record<string, unknown> = { name: f.name.trim(), kind: f.kind };
  const args = lines(f.args);
  if (f.runtime === "container") {
    const entry = lines(f.entrypoint);
    spec.runtime = { type: "container", image: f.image.trim(), ...(entry.length ? { command: entry } : {}), args };
  } else {
    spec.runtime = { type: "process", command: f.command.trim(), args, ...(f.workdir.trim() ? { workdir: f.workdir.trim() } : {}) };
  }
  const env: Record<string, string> = {};
  for (const row of f.env) if (row.key.trim()) env[row.key.trim()] = row.value;
  if (Object.keys(env).length) spec.env = env;

  const resources: Record<string, unknown> = {};
  const cpu = num(f.cpu);
  if (cpu !== null) resources.cpu = cpu;
  if (f.memory.trim()) resources.memory = f.memory.trim();
  if (f.gpuMode === "count") resources.gpus = { count: num(f.gpuCount) ?? 0, ...(f.vendor ? { vendor: f.vendor } : {}) };
  if (f.gpuMode === "indices") resources.gpus = { indices: [...f.gpuIndices].sort((a, b) => a - b), ...(f.vendor ? { vendor: f.vendor } : {}) };
  if (Object.keys(resources).length) spec.resources = resources;

  spec.replicas = num(f.replicas) ?? 1;
  if (f.machine) spec.machine_id = Number(f.machine);

  if (f.kind === "service") {
    spec.ports = f.ports
      .filter((p) => p.name.trim())
      .map((p) => ({ name: p.name.trim(), ...(f.runtime === "container" ? { port: num(p.port) } : {}) }));
    if (f.health !== "none") {
      spec.health = {
        type: f.health, port: f.healthPort || undefined, interval: num(f.healthInterval) ?? 10,
        ...(f.health === "http" ? { path: f.healthPath.trim() || "/" } : {}),
      };
    }
    spec.restart = f.restart;
  } else {
    const max = num(f.maxDuration);
    if (max !== null) spec.max_duration = max;
    spec.retries = num(f.retries) ?? 0;
    spec.retry_backoff = num(f.retryBackoff) ?? 5;
  }
  return spec;
}

// ---------------------------------------------------------------- display
export const STATE_TONE: Record<ReplicaState, "ok" | "run" | "wait" | "bad" | "off"> = {
  healthy: "ok", running: "run", starting: "wait", pending: "wait", backoff: "wait", stopping: "wait",
  unhealthy: "bad", failed: "bad", exited: "off", stopped: "off",
};

export const STATUS_TONE: Record<WorkloadStatus, "ok" | "run" | "wait" | "bad" | "off"> = {
  running: "ok", starting: "wait", pending: "wait", degraded: "bad", failed: "bad", succeeded: "ok",
  stopping: "wait", stopped: "off",
};

export function stateLabel(r: Replica): string {
  if (r.state === "exited" && r.exit_code !== null) return `exited ${r.exit_code}`;
  return r.state;
}

export function runtimeSummary(w: Workload): string {
  const rt = w.spec.runtime;
  if (rt.type === "container") return [rt.image, ...(rt.command ?? []), ...rt.args].join(" ");
  if (rt.type === "model") {
    const runs = w.replica_list.find((r) => r.runs)?.runs;
    const what = runs ? (runs.image ?? (Array.isArray(runs.command) ? runs.command.join(" ") : runs.command)) : null;
    return `model ${rt.model} with ${rt.recipe}${what ? ` · ${what}` : ""}`;
  }
  return [rt.command, ...rt.args].join(" ");
}

export function portUrl(p: PortStatus, kind: "http" | "tcp" = "http"): string | null {
  if (!p.host_port || !p.address) return null;
  const host = p.address.includes(":") ? `[${p.address}]` : p.address;
  return kind === "http" ? `http://${host}:${p.host_port}/` : `${host}:${p.host_port}`;
}

export function gpuLabel(r: Replica): string {
  if (!r.gpus.length) return "—";
  return r.gpus.map((g) => `#${g.index}`).join(", ");
}

export function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 120) return `${seconds} s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

// ---------------------------------------------------------------- machines and GPUs
export function gpuCards(m: Machine): Accelerator[] {
  return m.accelerators.filter((a) => a.vendor !== "cpu");
}

export function allowedCards(m: Machine): Accelerator[] {
  return gpuCards(m).filter((a) => a.allowed !== false);
}

// "all 8 GPUs", "only #4", "no GPU", "#2, #3 of 8"
export function allowlistSummary(m: Machine): string {
  const cards = gpuCards(m);
  if (!cards.length) return "no GPUs";
  const allowed = allowedCards(m);
  if (m.gpu_allowlist === null || m.gpu_allowlist === undefined) return cards.length === 1 ? "its GPU" : `all ${cards.length} GPUs`;
  if (!allowed.length) return "no GPU";
  const names = allowed.map((a) => `#${a.index}`).join(", ");
  return allowed.length === 1 ? `only ${names}` : `${names} of ${cards.length}`;
}

export function canRunWorkloads(m: Machine): boolean {
  return (m.features ?? []).includes("workloads");
}

export function runtimeLine(info: RuntimeInfo | undefined, name: "process" | "container"): string {
  if (!info) return `${name}: unknown`;
  if (!info.ok) return `${name}: ${info.reason ?? "not available"}`;
  if (name === "process") return `process as ${info.user ?? "?"} (${info.limits === "systemd" ? "systemd limits" : "rlimits"})`;
  return `container: ${info.engine ?? "?"}${info.version ? ` ${info.version}` : ""}${info.nvidia ? " with NVIDIA GPUs" : ""}`;
}
