import { describe, expect, it } from "vitest";

import { parsePrice, perMillion, priceText, share, usd } from "./prices";

describe("prices", () => {
  it("shows costs in micro-dollars as dollars with the digits that matter", () => {
    expect(usd(null)).toBe("—");
    expect(usd(0)).toBe("$0.00");
    expect(usd(185)).toBe("$0.000185");
    expect(usd(1_850)).toBe("$0.00185");
    expect(usd(123_400)).toBe("$0.1234");
    expect(usd(1_234_567_890)).toBe("$1,234.57");
  });

  it("reads and shows prices per million tokens exactly", () => {
    expect(perMillion(2_500_000)).toBe("$2.50");
    expect(perMillion(75_000)).toBe("$0.075");
    expect(perMillion(0)).toBe("free");
    expect(parsePrice("2.5")).toBe(2_500_000);
    expect(parsePrice("$0.075")).toBe(75_000);
    expect(parsePrice("0.000001")).toBe(1);
    expect(parsePrice("")).toBeNull();
    for (const bad of ["-1", "1e3", "abc", "0.0000001", "1,5"]) expect(parsePrice(bad)).toBe("bad");
    expect(priceText(2_500_000)).toBe("2.5");
    expect(priceText(null)).toBe("");
    expect(share(1, 400)).toBe("<1%");
    expect(share(3, 4)).toBe("75%");
    expect(share(1, 0)).toBe("—");
  });
});
