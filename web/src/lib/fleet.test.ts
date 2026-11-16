import { describe, expect, it } from "vitest";

import fixture from "../test/machines.json";
import { cardLine, cardMemory, machineKind, modelKind, placement, uniqueHolders } from "./fleet";
import type { Accelerator, Deployment, Machine, Replica, Workload } from "./types";

const GIB = 1024 ** 3;
const [box, strix] = fixture.machines as unknown as Machine[];

const card = (over: Partial<Accelerator> = {}): Accelerator => ({
  key: "nvidia:a", vendor: "nvidia", index: 0, model: "NVIDIA A100 80GB PCIe", arch: "8.0", uuid: null, vram_total: 80 * GIB,
  vram_free: 50 * GIB, vram_used: 30 * GIB, unified_memory: false, driver: null, utilization: null, temperature: null, detail: {},
  updated_at: "", ...over,
});

const rep = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "r", generation: 1, machine: { id: 1, name: "a100-box", status: "online", connected: true },
  gpus: [{ index: 0, vendor: "nvidia", model: null, key: "nvidia:a" }], state: "healthy", reason: null, exit_code: null, attempt: 0,
  restarts: 0, final: false, pid: null, container: null, ports: [], started_at: null, finished_at: null, updated_at: "", ...over,
});

const wl = (id: number, name: string, replicas: Replica[], memory: number | null = null): Workload => ({
  id, name, kind: "service", runtime: "container", desired_state: "running", generation: 1, created_by: null, created_at: "",
  updated_at: "", status: "running", ready: 1, replicas: replicas.length, replica_list: replicas,
  spec: { name, kind: "service", runtime: { type: "container", image: "x", command: null, args: [] }, env: {},
    resources: { cpu: null, memory, gpus: { count: 0, indices: null, vendor: null } }, ports: [], health: null, restart: "always",
    max_duration: null, retries: 0, retry_backoff: 5, replicas: replicas.length, machine_id: null },
});

const dep = (id: number, name: string, w: Workload, task: Deployment["model"]["task"] = "chat", modalities: string[] | null = null): Deployment => ({
  id, name, recipe: "vllm", served_name: name, params: {},
  model: { id, name: `${name}-model`, source: "", task, format: "safetensors", revision: null, file: null, size_bytes: 0,
    modalities: modalities as Deployment["model"]["modalities"] },
  estimate: { method: "", device: "gpu", gpus: 1, per_gpu: 9 * GIB, ram: 0, amount: 9 * GIB, kv_tokens: null, notes: [], parts: [] },
  has_token: false, created_by: null, created_at: "", route: null, endpoints: [], workload: w,
} as unknown as Deployment);

describe("fleet: what runs where (1.4)", () => {
  it("FY-WEB-219 a machine's kind: NVIDIA, AMD GPU, AMD APU (unified memory) or CPU only", () => {
    expect(machineKind(box)).toBe("nvidia");
    expect(machineKind(strix)).toBe("amd-apu");
    const amd = { ...strix, accelerators: strix.accelerators.map((a) => ({ ...a, unified_memory: false })) };
    expect(machineKind(amd)).toBe("amd-gpu");
    expect(machineKind({ ...box, accelerators: box.accelerators.filter((a) => a.vendor === "cpu") })).toBe("cpu");
    expect(cardLine(box)).toBe("8 × A100 80GB PCIe");
    expect(cardLine(strix)).toBe("Radeon 8060S");
  });

  it("FY-WEB-220 a chat model that takes images, audio or video is multimodal chat; other tasks keep their kind", () => {
    const w = wl(1, "w", []);
    expect(modelKind(dep(1, "a", w))).toBe("chat");
    expect(modelKind(dep(1, "a", w, "chat", []))).toBe("chat");
    expect(modelKind(dep(1, "a", w, "chat", ["image"]))).toBe("multimodal");
    expect(modelKind(dep(1, "a", w, "embed", ["image"]))).toBe("embed");
    expect(modelKind(dep(1, "a", w, "video"))).toBe("video");
  });

  it("FY-WEB-221 holders: a deployment's workload counts under the deployment; stopped and finished replicas hold nothing", () => {
    const model = wl(10, "chat-wl", [rep({ vram: 12 * GIB }), rep({ ordinal: 1, gpus: [{ index: 1, vendor: "nvidia", model: null, key: "nvidia:b" }], vram: null })]);
    const plain = wl(11, "notebook", [rep({ gpus: [{ index: 0, vendor: "nvidia", model: null, key: "nvidia:a" }] })], 4 * GIB);
    const cpuJob = wl(12, "etl", [rep({ gpus: [] })], 2 * GIB);
    const gone = wl(13, "old", [rep({ state: "stopped" }), rep({ ordinal: 1, final: true, state: "failed" }), rep({ ordinal: 2, machine: null })]);
    const d = dep(5, "chat", model);
    const p = placement([model, plain, cpuJob, gone], [d]);
    expect(p.holders.map((h) => [h.name, h.href, h.kind, h.reserved])).toEqual([
      ["chat", "/models/deployments/5", "chat", 12 * GIB],
      ["chat", "/models/deployments/5", "chat", 9 * GIB],    // no reading yet: the estimate per GPU
      ["notebook", "/apps/11", "workload", null],      // a GPU app declares no card memory
      ["etl", "/apps/12", "workload", 2 * GIB],        // a CPU app: its declared RAM
    ]);
    expect(uniqueHolders(p.onMachine(1)).map((h) => h.name)).toEqual(["chat", "notebook", "etl"]);
    expect(p.onRow(1, card()).map((h) => h.name)).toEqual(["chat", "notebook"]);
    expect(p.onRow(1, card({ key: "cpu:0", vendor: "cpu" })).map((h) => h.name)).toEqual(["etl"]);
    expect(p.onMachine(2)).toEqual([]);
  });

  it("FY-WEB-222 a card's memory: Forgeyard's reservations, other use (the reading minus them), and free", () => {
    const held = placement([wl(1, "a", [rep({ vram: 20 * GIB })])], []).holders;
    expect(cardMemory(card(), held)).toEqual({ total: 80 * GIB, forgeyard: 20 * GIB, other: 10 * GIB, free: 50 * GIB });
    // the agent's reading is older than the reservation: nothing is counted twice, free never goes below 0
    expect(cardMemory(card({ vram_free: 75 * GIB }), held)).toEqual({ total: 80 * GIB, forgeyard: 20 * GIB, other: 0, free: 60 * GIB });
    const big = placement([wl(1, "a", [rep({ vram: 100 * GIB })])], []).holders;
    expect(cardMemory(card({ vram_free: 0 }), big)).toEqual({ total: 80 * GIB, forgeyard: 80 * GIB, other: 0, free: 0 });
    // no reading at all: only the reservations count
    expect(cardMemory(card({ vram_free: null }), held)).toEqual({ total: 80 * GIB, forgeyard: 20 * GIB, other: 0, free: 60 * GIB });
  });
});
