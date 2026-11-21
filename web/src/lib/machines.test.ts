import { describe, expect, it } from "vitest";

import { ageSeconds, ago, bytes, cardSummary, isLoopback, replicaAddressNote } from "./machines";
import type { Machine } from "./types";

describe("machine helpers", () => {
  it.each([
    [85899345920, "80 GiB"],
    [4023779328, "3.7 GiB"],
    [536870912, "512 MiB"],
    [1081822150656, "1008 GiB"],
    [1024 ** 4 * 2, "2 TiB"],
    [null, "—"],
  ])("FY-WEB-030 bytes(%s) = %s", (n, s) => expect(bytes(n)).toBe(s));

  it("FY-WEB-031 ages come from the server's clock", () => {
    expect(ageSeconds("2026-11-09T10:00:41Z", "2026-11-09T10:00:00Z")).toBe(41);
    expect(ageSeconds("2026-11-09T10:00:41+00:00", "2026-11-09T10:00:00")).toBe(41);
    expect(ageSeconds("2026-11-09T10:00:00Z", null)).toBeNull();
    expect(ago(41)).toBe("41 s ago");
    expect(ago(null)).toBe("never");
  });

  it("FY-WEB-032 identical cards are counted once", () => {
    const card = (model: string, vendor: "nvidia" | "amd" | "cpu" = "nvidia") => ({ model, vendor }) as Machine["accelerators"][number];
    const m = { accelerators: [...Array(8)].map(() => card("NVIDIA A100 80GB PCIe")).concat(card("Xeon", "cpu")) } as Machine;
    expect(cardSummary(m)).toBe("8 × NVIDIA A100 80GB PCIe");
    expect(cardSummary({ accelerators: [card("Xeon", "cpu")] } as Machine)).toBe("CPU only");
  });

  it("FY-WEB-520 a loopback replica address is named, and where the gateway dials it", () => {
    const m = (runtime_info: Machine["runtime_info"]) => ({ runtime_info }) as Machine;
    expect(["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"].every(isLoopback)).toBe(true);
    expect(["192.168.1.20", "10.0.0.127", "0.0.0.0", "fe80::1", "127.example"].some(isLoopback)).toBe(false);
    expect(replicaAddressNote(m({ address: "127.0.0.1", same_host: false }))).toBe("loopback: the gateway dials it on the server's host");
    expect(replicaAddressNote(m({ address: "127.0.0.1", same_host: true }))).toBe("loopback, on the server's host");
    expect(replicaAddressNote(m({ address: "192.168.1.20", same_host: false }))).toBeNull();
    expect(replicaAddressNote(m({}))).toBeNull();
    expect(replicaAddressNote(m(undefined))).toBeNull();
  });
});
