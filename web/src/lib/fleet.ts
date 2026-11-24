// What runs where (1.4): machines, deployments and workloads joined, so a page can ask "what holds
// this card" and "where does this model run". Pure functions; the icons are in components/Fleet.tsx.
import { labels, t, tPlural } from "../i18n/core";
import { bytes } from "./machines";
import { isVisionTask } from "./models";
import { asleepOn, type AsleepHere } from "./sleep";
import type { Accelerator, Deployment, Machine, Replica, TrialBrief, Workload } from "./types";

// ---------------------------------------------------------------- machine kinds

export type MachineKind = "nvidia" | "amd-gpu" | "amd-apu" | "cpu";

// The labels are read in the reader's language each time (i18n labels()).
const KIND_LABEL = labels({ nvidia: "fleet.machineKind.nvidia", "amd-gpu": "fleet.machineKind.amd-gpu", "amd-apu": "fleet.machineKind.amd-apu",
  cpu: "fleet.machineKind.cpu" });
export const MACHINE_KIND: Record<MachineKind, { label: string; short: string }> = {
  nvidia: { get label() { return KIND_LABEL.nvidia; }, short: "NVIDIA" },
  "amd-gpu": { get label() { return KIND_LABEL["amd-gpu"]; }, short: "AMD" },
  "amd-apu": { get label() { return KIND_LABEL["amd-apu"]; }, short: "APU" },
  cpu: { get label() { return KIND_LABEL.cpu; }, short: "CPU" },
};
export const MACHINE_KIND_ORDER: MachineKind[] = ["nvidia", "amd-gpu", "amd-apu", "cpu"];

// The summary above the machine tiles counts machines, not cards (1.4): "1 NVIDIA machine",
// "2 CPU-only machines". AMD GPU and APU machines are both "AMD" unless both kinds are there.
const MACHINE_NOUN = { nvidia: "fleet.machineNoun.nvidia", "amd-gpu": "fleet.machineNoun.amd", "amd-apu": "fleet.machineNoun.amd",
  cpu: "fleet.machineNoun.cpu" } as const;
export function machineCount(kind: MachineKind, n: number, kinds: MachineKind[] = [kind]): string {
  const bothAmd = kinds.includes("amd-gpu") && kinds.includes("amd-apu");
  const noun = bothAmd && kind === "amd-gpu" ? "fleet.machineNoun.amd-gpu" : bothAmd && kind === "amd-apu" ? "fleet.machineNoun.amd-apu"
    : MACHINE_NOUN[kind];
  return tPlural(noun, n);
}

export function cards(m: Machine): Accelerator[] {
  return m.accelerators.filter((a) => a.vendor !== "cpu");
}

export function cpuOf(m: Machine): Accelerator | undefined {
  return m.accelerators.find((a) => a.vendor === "cpu");
}

// A machine with any NVIDIA card is an NVIDIA machine; an AMD card sharing system memory makes an APU.
export function machineKind(m: Machine): MachineKind {
  const g = cards(m);
  if (g.some((a) => a.vendor === "nvidia")) return "nvidia";
  if (g.some((a) => a.vendor === "amd" && a.unified_memory)) return "amd-apu";
  if (g.some((a) => a.vendor === "amd")) return "amd-gpu";
  return "cpu";
}

export function byKind(list: Machine[]): Machine[] {
  return [...list].sort((a, b) => MACHINE_KIND_ORDER.indexOf(machineKind(a)) - MACHINE_KIND_ORDER.indexOf(machineKind(b)));
}

// "8 × A100 80GB PCIe": the card names without the vendor, counted; the CPU's name without GPUs.
export function cardLine(m: Machine): string {
  const counts = new Map<string, number>();
  for (const a of cards(m)) {
    const n = a.model.replace(/^(NVIDIA|AMD) /, "");
    counts.set(n, (counts.get(n) ?? 0) + 1);
  }
  if (!counts.size) return cpuOf(m)?.model.replace(/\((R|TM)\)/g, "") ?? "CPU";
  return [...counts].map(([n, c]) => (c > 1 ? `${c} × ${n}` : n)).join(", ");
}

// The rows of a machine's memory: each card, then the CPU's RAM (the only row without cards).
export function memoryRows(m: Machine): Accelerator[] {
  const cpu = cpuOf(m);
  return [...cards(m), ...(cpu ? [cpu] : [])];
}

// ---------------------------------------------------------------- model kinds

export type ModelKind = "chat" | "multimodal" | "embed" | "rerank" | "classify" | "asr" | "tts" | "image" | "video" | "vision";

const MODEL_API: Record<ModelKind, string> = {
  chat: "/v1/chat/completions", multimodal: "/v1/chat/completions", embed: "/v1/embeddings", rerank: "/v1/rerank", classify: "/v1/classify",
  asr: "/v1/audio/transcriptions", tts: "/v1/audio/speech", image: "/v1/images/generations", video: "/v1/videos", vision: "/v1/vision/…",
};
function modelKindEntry(k: ModelKind): { label: string; api: string; hint: string } {
  return {
    get label() { return t(`fleet.modelKind.${k}`); },
    api: MODEL_API[k],
    get hint() { return t(`fleet.modelHint.${k}`); },
  };
}
export const MODEL_KIND: Record<ModelKind, { label: string; api: string; hint: string }> = {
  chat: modelKindEntry("chat"), multimodal: modelKindEntry("multimodal"), embed: modelKindEntry("embed"), rerank: modelKindEntry("rerank"),
  classify: modelKindEntry("classify"), asr: modelKindEntry("asr"), tts: modelKindEntry("tts"), image: modelKindEntry("image"),
  video: modelKindEntry("video"), vision: modelKindEntry("vision"),
};
export const MODEL_KIND_ORDER: ModelKind[] = ["chat", "multimodal", "embed", "rerank", "classify", "asr", "tts", "image", "video", "vision"];

// A chat model that takes images, audio or video is multimodal chat.
export function modelKind(d: Deployment): ModelKind {
  if (d.model.task === "chat") return (d.model.modalities ?? []).length > 0 ? "multimodal" : "chat";
  // 1.4: detection, segmentation and depth share one section, each row saying its task.
  if (isVisionTask(d.model.task)) return "vision";
  return d.model.task;
}

export function groupByKind(list: Deployment[]): [ModelKind, Deployment[]][] {
  const out = new Map<ModelKind, Deployment[]>(MODEL_KIND_ORDER.map((k) => [k, []]));
  for (const d of list) out.get(modelKind(d))?.push(d);
  return [...out];
}

// ---------------------------------------------------------------- what holds a card

export type Holder = {
  name: string;                  // the deployment's name, or the workload's
  key: string;                   // one per deployment (or workload)
  // Its page: the deployment's. A workload no deployment lists (apps, until 1.5.3 removed them (#89); now only
  // for a moment, before the deployments list catches up) has none: it is only named, so that a card it holds is
  // never shown free.
  href: string | null;
  kind: ModelKind | "workload";
  deployment?: Deployment;
  workload: Workload;
  replica: Replica;
  // Bytes it holds on each of its cards (or of RAM on the CPU); null: not declared (a plain workload's card).
  reserved: number | null;
  // 1.5.2 (#95, PR 4): the engine trial whose temporary deployment this is.
  trial?: TrialBrief;
};

export type Placement = {
  holders: Holder[];
  onMachine: (machineId: number) => Holder[];
  onRow: (machineId: number, a: Accelerator) => Holder[];
  // 1.5.0 (#37): asleep deployments that would run on the machine (they hold nothing there now).
  asleep: (machineId: number) => AsleepHere[];
};

// Every replica that sits on a machine and is not over: a model's under its deployment, the rest as
// workloads. A deployment's workload counts once, under the deployment.
export function placement(workloads: Workload[], deployments: Deployment[], trials: TrialBrief[] = []): Placement {
  const byWorkload = new Map(deployments.map((d) => [d.workload.id, d]));
  // 1.5.2 (#95, PR 4): an engine trial's temporary deployment holds a card too: named as the trial, linked to the
  // engine's page (it has no deployment page).
  const byTrial = new Map(trials.filter((x) => x.workload).map((x) => [x.workload as string, x]));
  const holders: Holder[] = [];
  for (const w of workloads) {
    const dep = byWorkload.get(w.id);
    const trial = dep ? undefined : byTrial.get(w.name);
    for (const r of w.replica_list) {
      if (!r.machine || r.final || r.state === "stopped") continue;
      if (trial) {
        holders.push({ name: t("enginetrial.holder", { recipe: trial.recipe }), key: `trial:${trial.id}`,
          href: `/models/engines/${encodeURIComponent(trial.recipe)}#trials`, kind: "workload", workload: w, replica: r,
          reserved: r.vram ?? null, trial });
        continue;
      }
      let reserved: number | null = r.vram ?? null;
      if (reserved === null && dep) reserved = (dep.estimate.device === "gpu" ? dep.estimate.per_gpu : dep.estimate.ram) ?? null;
      if (reserved === null && !dep && r.gpus.length === 0) reserved = w.spec.resources.memory ?? null;
      holders.push({
        name: dep?.name ?? w.name,
        key: dep ? `deployment:${dep.id}` : `workload:${w.id}`,
        href: dep ? `/serving/${dep.id}` : null,
        kind: dep ? modelKind(dep) : "workload",
        deployment: dep,
        workload: w,
        replica: r,
        reserved,
      });
    }
  }
  const onMachine = (id: number) => holders.filter((h) => h.replica.machine?.id === id);
  const onRow = (id: number, a: Accelerator) =>
    onMachine(id).filter((h) => (a.vendor === "cpu" ? h.replica.gpus.length === 0 : h.replica.gpus.some((g) => g.key === a.key)));
  return { holders, onMachine, onRow, asleep: (id: number) => asleepOn(deployments, id) };
}

// A workload no deployment lists is named where it holds a card, so the card is never shown free, but not linked.
export function workloadTitle(name: string): string {
  return t("fleet.workloadTitle", { name });
}

// One entry per name: a deployment with two replicas on a machine counts once.
export function uniqueHolders(list: Holder[]): Holder[] {
  const seen = new Set<string>();
  return list.filter((h) => (seen.has(h.key) ? false : (seen.add(h.key), true)));
}

// ---------------------------------------------------------------- memory of one card

export type CardMemory = { total: number; forgeyard: number; other: number; free: number };

// What Forgeyard reserved on the card, what else uses it (the agent's reading minus those
// reservations: other programs, and Forgeyard workloads that declared no size), and what is left.
export function cardMemory(a: Accelerator, held: Holder[]): CardMemory {
  const total = a.vram_total ?? 0;
  const used = a.vram_free === null ? 0 : Math.max(0, total - a.vram_free);
  const forgeyard = Math.min(total, held.reduce((s, h) => s + (h.reserved ?? 0), 0));
  const other = Math.max(0, used - forgeyard);
  return { total, forgeyard, other, free: Math.max(0, total - forgeyard - other) };
}

// A machine's memory: its cards, or its RAM when it has none.
export function machineMemory(m: Machine, p: Placement): CardMemory {
  const list = cards(m).length ? cards(m) : ([cpuOf(m)].filter(Boolean) as Accelerator[]);
  return list.map((a) => cardMemory(a, p.onRow(m.id, a))).reduce(
    (s, x) => ({ total: s.total + x.total, forgeyard: s.forgeyard + x.forgeyard, other: s.other + x.other, free: s.free + x.free }),
    { total: 0, forgeyard: 0, other: 0, free: 0 },
  );
}

export function share(n: number, total: number): string {
  return total > 0 ? `${Math.max(0, Math.min(100, (n / total) * 100))}%` : "0%";
}

export function memoryLabel(mem: CardMemory): string {
  return t("fleet.memory", { forgeyard: bytes(mem.forgeyard), other: bytes(mem.other), free: bytes(mem.free), total: bytes(mem.total) });
}

// "GPU #0 #1", "CPU", or "" for a replica not placed.
export function whereOnMachine(r: Replica): string {
  if (!r.machine) return "";
  return r.gpus.length ? r.gpus.map((g) => `#${g.index}`).join(" ") : "CPU";
}
