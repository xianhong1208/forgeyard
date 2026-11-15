import { describe, expect, it } from "vitest";

import { CLOUD_POLICIES, policyOf, policyValue } from "./cloudPolicy";
import type { CloudLevel } from "./types";

describe("cloud policy (D3)", () => {
  it("FY-WEB-208 each preset is one pair of stored values, both ways; any other pair is Custom", () => {
    const table: [string, CloudLevel, CloudLevel][] = [
      ["none", "off", "off"], ["opt-in", "off", "fallback"], ["spill", "fallback", "fallback"], ["allowed", "fallback", "on"]];
    expect(CLOUD_POLICIES.map((p) => p.id)).toEqual(table.map((t) => t[0]));
    for (const [id, d, m] of table) {
      expect(policyValue(id as never, { default: "on", max: "on" })).toEqual({ default: d, max: m });
      expect(policyOf({ default: d, max: m })).toBe(id);
    }
    for (const [d, m] of [["off", "on"], ["on", "on"], ["fallback", "off"], ["on", "fallback"]] as [CloudLevel, CloudLevel][]) {
      expect(policyOf({ default: d, max: m })).toBe("custom");
    }
    // Custom keeps what is there, so switching to it never changes the stored values.
    expect(policyValue("custom", { default: "off", max: "on" })).toEqual({ default: "off", max: "on" });
    // Labels the admin sees.
    expect(CLOUD_POLICIES.map((p) => p.label)).toEqual(["No cloud", "People may opt in to spill", "Spill by default", "Cloud allowed"]);
  });
});
