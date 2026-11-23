// 1.4.5 (#163): how the gateway reaches a machine's replicas, as its page says it. Pure, so each case is tested.
import { t as tr } from "../i18n/core";
import type { Machine, ReplicaTls } from "./types";

export type TlsTone = "ok" | "warn" | "bad" | "wait";

// The short label of the machine's facts line, its tone, and whether it links to the update command.
export function tlsChip(m: Pick<Machine, "replica_tls" | "status">): { text: string; tone: TlsTone; update: boolean } | null {
  const t = m.replica_tls;
  if (!t || m.status === "revoked") return null;
  switch (t.state) {
    case "plain":
      return { text: tr("machine.tls.chip.plain"), tone: "warn", update: true };
    case "missing":
      return { text: tr("machine.tls.chip.missing"), tone: "bad", update: false };
    case "expired":
      return { text: tr("machine.tls.chip.expired"), tone: "bad", update: false };
    case "expiring":
      return { text: tr("machine.tls.chip.expiring"), tone: "bad", update: false };
    default:
      return null; // ok and pending need no chip: the Agent section says it
  }
}

// The Agent section's "Replica traffic" line. The date stays as the server's ISO date (2026-12-01) in both languages.
export function tlsLine(t: ReplicaTls | null | undefined): string | null {
  if (!t) return null;
  const until = t.not_after ? t.not_after.slice(0, 10) : null;
  const reason = t.reason ? tr("machine.tls.reason", { reason: t.reason }) : "";
  switch (t.state) {
    case "ok":
      return until ? tr("machine.tls.line.okUntil", { until }) : tr("machine.tls.line.ok");
    case "pending":
      return tr("machine.tls.line.pending");
    case "plain":
      return tr("machine.tls.line.plain");
    case "missing":
      return tr("machine.tls.line.missing", { reason });
    case "expiring":
      return tr("machine.tls.line.expiring", { until: until ?? "?", reason });
    case "expired":
      return until ? tr("machine.tls.line.expiredOn", { until }) : tr("machine.tls.line.expired");
    default:
      return null;
  }
}
