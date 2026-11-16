import type { Accelerator, Machine, Replica, ReplicaState, RuntimeInfo, WorkloadStatus } from "./types";

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

export function gpuLabel(r: Replica): string {
  if (!r.gpus.length) return "—";
  return r.gpus.map((g) => `#${g.index}`).join(", ");
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
