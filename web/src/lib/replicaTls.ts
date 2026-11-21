// 1.4.5 (#163): how the gateway reaches a machine's replicas, as its page says it. Pure, so each case is tested.
import type { Machine, ReplicaTls } from "./types";

export type TlsTone = "ok" | "warn" | "bad" | "wait";

// The short label of the machine's facts line, its tone, and whether it links to the update command.
export function tlsChip(m: Pick<Machine, "replica_tls" | "status">): { text: string; tone: TlsTone; update: boolean } | null {
  const t = m.replica_tls;
  if (!t || m.status === "revoked") return null;
  switch (t.state) {
    case "plain":
      return { text: "not encrypted: update the agent", tone: "warn", update: true };
    case "missing":
      return { text: "no replica certificate", tone: "bad", update: false };
    case "expired":
      return { text: "replica certificate expired", tone: "bad", update: false };
    case "expiring":
      return { text: "replica certificate not renewed", tone: "bad", update: false };
    default:
      return null; // ok and pending need no chip: the Agent section says it
  }
}

// The Agent section's "Replica traffic" line.
export function tlsLine(t: ReplicaTls | null | undefined): string | null {
  if (!t) return null;
  const until = t.not_after ? t.not_after.slice(0, 10) : null;
  switch (t.state) {
    case "ok":
      return `Encrypted (TLS)${until ? `, certificate valid until ${until}` : ""}`;
    case "pending":
      return "Encrypted (TLS), getting its certificate";
    case "plain":
      return "Not encrypted (plain HTTP): its agent is older than 1.4.5. Update the agent.";
    case "missing":
      return `TLS, but no certificate from this server${t.reason ? ` (${t.reason})` : ""}: its replicas cannot be reached`;
    case "expiring":
      return `TLS, certificate valid until ${until ?? "?"} and not being renewed${t.reason ? ` (${t.reason})` : ""}`;
    case "expired":
      return `TLS, certificate expired${until ? ` on ${until}` : ""}: its replicas cannot be reached`;
    default:
      return null;
  }
}
