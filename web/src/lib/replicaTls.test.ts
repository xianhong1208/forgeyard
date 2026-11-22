// 1.4.5 (#163): how a machine's page says the gateway reaches its replicas.
import { describe, expect, it } from "vitest";

import { tlsChip, tlsLine } from "./replicaTls";
import type { ReplicaTls } from "./types";

const t = (state: ReplicaTls["state"], over: Partial<ReplicaTls> = {}): ReplicaTls => ({ state, not_after: null, reason: null, ...over });

describe("replica traffic", () => {
  it("FY-WEB-540 a chip only when it needs attention: plain links to the update, a TLS machine without a valid certificate is an error", () => {
    expect(tlsChip({ status: "online", replica_tls: t("plain") })).toEqual({ text: "not encrypted: update the agent", tone: "warn", update: true });
    expect(tlsChip({ status: "online", replica_tls: t("missing") })).toEqual({ text: "no replica certificate", tone: "bad", update: false });
    expect(tlsChip({ status: "offline", replica_tls: t("expired") })?.text).toBe("replica certificate expired");
    expect(tlsChip({ status: "online", replica_tls: t("expiring") })?.text).toBe("replica certificate not renewed");
    expect(tlsChip({ status: "online", replica_tls: t("ok", { not_after: "2026-12-21T12:00:00+00:00" }) })).toBeNull();
    expect(tlsChip({ status: "online", replica_tls: t("pending") })).toBeNull();
    expect(tlsChip({ status: "revoked", replica_tls: t("plain") })).toBeNull();
    expect(tlsChip({ status: "online", replica_tls: null })).toBeNull();
    expect(tlsChip({ status: "online" })).toBeNull();
  });

  it("FY-WEB-541 the Agent section's line names the state, the expiry and the reason", () => {
    expect(tlsLine(t("ok", { not_after: "2026-12-21T12:00:00+00:00" }))).toBe("Encrypted (TLS), certificate valid until 2026-12-21");
    expect(tlsLine(t("pending"))).toBe("Encrypted (TLS), getting its certificate");
    expect(tlsLine(t("plain"))).toBe("Not encrypted (plain HTTP): its agent is older than 1.4.5. Update the agent.");
    expect(tlsLine(t("missing", { reason: "the CSR cannot be read" }))).toBe("TLS, but no certificate from this server (the CSR cannot be read): its replicas cannot be reached");
    expect(tlsLine(t("expiring", { not_after: "2026-11-23T00:00:00+00:00" }))).toBe("TLS, certificate valid until 2026-11-23 and not being renewed");
    expect(tlsLine(t("expired", { not_after: "2026-11-01T00:00:00+00:00" }))).toBe("TLS, certificate expired on 2026-11-01: its replicas cannot be reached");
    expect(tlsLine(null)).toBeNull();
  });
});
