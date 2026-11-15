import type { CloudSettings } from "./types";

// 1.3.1 (D3): the two stored cloud values (what new keys get, the most people may give their own
// keys) under one choice. The stored data is unchanged; a pair that is no preset shows as Custom.
export type CloudPolicy = "none" | "opt-in" | "spill" | "allowed" | "custom";

export const CLOUD_POLICIES: { id: Exclude<CloudPolicy, "custom">; label: string; value: CloudSettings; help: string }[] = [
  { id: "none", label: "No cloud", value: { default: "off", max: "off" },
    help: "New keys get Never, and people may not open their own keys to the cloud; only an admin can, key by key." },
  { id: "opt-in", label: "People may opt in to spill", value: { default: "off", max: "fallback" },
    help: "New keys get Never; people may set their own keys to Only to spill." },
  { id: "spill", label: "Spill by default", value: { default: "fallback", max: "fallback" },
    help: "New keys get Only to spill (a request may go to the cloud when local is busy or down); people may not set their own keys to Allowed." },
  { id: "allowed", label: "Cloud allowed", value: { default: "fallback", max: "on" },
    help: "New keys get Only to spill; people may set their own keys to Allowed, cloud-only models too." },
];

export const CUSTOM_HELP = "Choose what new keys get and the most people may give their own keys.";

export function policyOf(c: CloudSettings): CloudPolicy {
  return CLOUD_POLICIES.find((p) => p.value.default === c.default && p.value.max === c.max)?.id ?? "custom";
}

export function policyValue(id: CloudPolicy, current: CloudSettings): CloudSettings {
  if (id === "custom") return current;
  const p = CLOUD_POLICIES.find((x) => x.id === id);
  return p ? { ...p.value } : current;
}

export function policyLabel(id: CloudPolicy): string {
  return id === "custom" ? "Custom" : CLOUD_POLICIES.find((p) => p.id === id)?.label ?? "Custom";
}

