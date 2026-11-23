import { t, tMaybe } from "../i18n/core";
import type { Accelerator, Machine, Replica, ReplicaState, RuntimeInfo, WorkloadStatus } from "./types";

// ---------------------------------------------------------------- display
export const STATE_TONE: Record<ReplicaState, "ok" | "run" | "wait" | "bad" | "off"> = {
  healthy: "ok", running: "run", starting: "wait", pending: "wait", backoff: "wait", stopping: "wait",
  unhealthy: "bad", failed: "bad", exited: "off", stopped: "off",
};

export const STATUS_TONE: Record<WorkloadStatus, "ok" | "run" | "wait" | "bad" | "off"> = {
  running: "ok", starting: "wait", pending: "wait", degraded: "bad", failed: "bad", succeeded: "ok",
  stopping: "wait", stopped: "off", asleep: "off",
};

export function stateLabel(r: Replica): string {
  if (r.state === "exited" && r.exit_code !== null) return t("fleet.state.exitedCode", { code: String(r.exit_code) });
  return statusLabel(r.state);
}

// A replica's state or a workload's status ("running", "asleep") in the reader's language.
export function statusLabel(s: string): string {
  return tMaybe(`fleet.state.${s}`, s);
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
  if (!cards.length) return t("machine.allow.noGpus");
  const allowed = allowedCards(m);
  if (m.gpu_allowlist === null || m.gpu_allowlist === undefined) return cards.length === 1 ? t("machine.allow.its") : t("machine.allow.all", { count: cards.length });
  if (!allowed.length) return t("machine.allow.none");
  const names = allowed.map((a) => `#${a.index}`).join(", ");
  return allowed.length === 1 ? t("machine.allow.only", { names }) : t("machine.allow.of", { names, count: cards.length });
}

export function canRunWorkloads(m: Machine): boolean {
  return (m.features ?? []).includes("workloads");
}

export function runtimeLine(info: RuntimeInfo | undefined, name: "process" | "container"): string {
  const label = name === "process" ? t("machine.runtime.process") : t("machine.runtime.container");
  if (!info) return t("machine.runtime.unknown", { name: label });
  if (!info.ok) return t("machine.runtime.notAvailable", { name: label, reason: info.reason ?? t("machine.runtime.notAvailableReason") });
  if (name === "process") {
    return t("machine.runtime.processAs", { user: info.user ?? "?", limits: info.limits === "systemd" ? t("machine.runtime.systemd") : t("machine.runtime.rlimits") });
  }
  return t("machine.runtime.containerEngine", { engine: info.engine ?? "?", version: info.version ? ` ${info.version}` : "",
    nvidia: info.nvidia ? t("machine.runtime.nvidia") : "" });
}
