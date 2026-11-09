import { describe, expect, it } from "vitest";

import { ageSeconds, ago, bytes, cardSummary } from "./machines";
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
});
