import { t, tMaybe } from "../i18n/core";
import type { CloudSettings } from "./types";

// 1.3.1 (D3): the two stored cloud values (what new keys get, the most people may give their own
// keys) under one choice. The stored data is unchanged; a pair that is no preset shows as Custom.
export type CloudPolicy = "none" | "opt-in" | "spill" | "allowed" | "custom";

type Preset = { id: Exclude<CloudPolicy, "custom">; label: string; value: CloudSettings; help: string };

// The label and help are read in the reader's language each time (keys.policy.<id>, keys.policy.<id>.help).
function preset(id: Preset["id"], value: CloudSettings): Preset {
  return {
    id, value,
    get label() { return tMaybe(`keys.policy.${id}`, id); },
    get help() { return tMaybe(`keys.policy.${id}.help`, ""); },
  } as Preset;
}

export const CLOUD_POLICIES: Preset[] = [
  preset("none", { default: "off", max: "off" }),
  preset("opt-in", { default: "off", max: "fallback" }),
  preset("spill", { default: "fallback", max: "fallback" }),
  preset("allowed", { default: "fallback", max: "on" }),
];

export function customHelp(): string {
  return t("keys.policy.customHelp");
}

export function policyOf(c: CloudSettings): CloudPolicy {
  return CLOUD_POLICIES.find((p) => p.value.default === c.default && p.value.max === c.max)?.id ?? "custom";
}

export function policyValue(id: CloudPolicy, current: CloudSettings): CloudSettings {
  if (id === "custom") return current;
  const p = CLOUD_POLICIES.find((x) => x.id === id);
  return p ? { ...p.value } : current;
}

export function policyLabel(id: CloudPolicy): string {
  return id === "custom" ? t("keys.policy.custom") : CLOUD_POLICIES.find((p) => p.id === id)?.label ?? t("keys.policy.custom");
}

