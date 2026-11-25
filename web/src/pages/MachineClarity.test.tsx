// @vitest-environment jsdom
// 1.5.6 (#282): engine images as current state, the CPU fit on the model page, and where each machine command runs.
// - the model page's download line says when and how the model came, not which engine came with it (downloads.ts);
// - presence of an engine image or program: here, not here and how it comes (pulled, through the server, or both) with
//   its size, on the model page, the engine page and the deploy form;
// - "Fits?" judges a machine with no GPU the engine can use on its CPU, names the system memory it uses (an APU's GPU
//   memory apart) and offers "Deploy here (CPU)";
// - every Add a machine / Update the agent command says where it runs; a Windows (WSL2) variant of the Docker command.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY, setLang } from "../i18n/lang";
import { NVIDIA_DEVICES, emptyForm, onWsl, runCommand, windowsDriveDir } from "../lib/dockerAgent";
import { cpuMemory, engineLine, machineFit, presenceText, whereText } from "../lib/pages";
import fixture from "../test/machines.json";
import type { Accelerator, CatalogModel, MachineVerdict, ModelPage, Recipe } from "../lib/types";

const GIB = 1024 ** 3;
const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

const WHISPER: Recipe = {
  name: "faster-whisper", title: "faster-whisper", description: "Speech to text.", engine: "speaches", version: "0.8.3",
  tasks: ["asr"], formats: ["ctranslate2"], api: { asr: "openai-transcriptions" }, share: "exclusive", builtin: true, estimate: "ct2",
  devices: ["gpu", "cpu"], params: {},
  variants: [
    { name: "cuda", vendor: "nvidia", runtime: "container", image: "ghcr.io/speaches-ai/speaches:0.8.3-cuda@sha256:" + "a".repeat(64), arch: null, compute_capability: null, driver: null, engine: null },
    { name: "cpu", vendor: "cpu", runtime: "container", image: "ghcr.io/speaches-ai/speaches:0.8.3-cpu@sha256:" + "b".repeat(64), arch: null, compute_capability: null, driver: null, engine: null },
  ],
} as unknown as Recipe;

const MODEL = {
  id: 9, name: "faster-whisper-small", title: null, source: "hf://Systran/faster-whisper-small", kind: "hf", ref: "main", revision: "abc",
  file: null, format: "ctranslate2", task: "asr", task_label: "Speech to text", size_bytes: 480 * 1024 ** 2, engine_args: {}, recipes: ["faster-whisper"],
  deployments: 0, created_by: "owner", created_at: "2026-11-10T00:00:00", summary: {}, license: "mit", modalities: [],
} as unknown as CatalogModel;

const EST = (device: "gpu" | "cpu", amount: number) => ({ method: "ct2", device, gpus: device === "gpu" ? 1 : 0, per_gpu: device === "gpu" ? amount : 0,
  ram: device === "cpu" ? amount : 0, amount, parts: [], notes: [], kv_tokens: 0 });
const verdict = (machine_id: number, name: string, fits: boolean, extra: Partial<MachineVerdict> = {}): MachineVerdict =>
  ({ machine_id, name, status: "online", fits, reason: "", short_by: null, cards: [], ...extra });

// The fixture's machine 2 ("strix") is an APU: its GPU 110 GiB set aside, the CPU row 127 GiB. Here its CPU row reports
// 31 GiB, as on an APU whose BIOS gives 96 GiB to the GPU.
const APU_CPU = { index: null, available: 13 * GIB, total: 31 * GIB, shared_with: [] };

function page(): ModelPage {
  const imageEngine = (variant: string, present: boolean | null, via: "direct" | "server" | "auto", size: number | null) => ({
    variant, runtime: "container" as const, kind: "image" as const, engine_name: WHISPER.variants.find((v) => v.name === variant)!.image,
    present, listed_at: present === null ? null : "2026-11-25T07:00:00+00:00", note: null, via, size });
  return {
    model: MODEL, recipes: [WHISPER], can_manage: true, can_deploy: true,
    files: { recorded: true, downloaded_count: 1, downloaded_bytes: MODEL.size_bytes, files: [{ path: "model.bin", size: MODEL.size_bytes, role: "", downloaded: true }] },
    machines: [
      { machine_id: 1, name: "a100-box", status: "online", connected: true, download: null,
        cache: { reported: true, cached: false, bytes: null, files: null, used_by: [] },
        engines: { "faster-whisper": imageEngine("cuda", false, "direct", 3.24 * GIB) } },
      { machine_id: 2, name: "strix", status: "online", connected: true, download: null,
        cache: { reported: true, cached: true, bytes: MODEL.size_bytes, files: 4, used_by: [] },
        engines: { "faster-whisper": imageEngine("cpu", false, "server", 0.64 * GIB) } },
    ],
    fits: { "faster-whisper": {
      gpu: { estimate: EST("gpu", 1.84 * GIB), fits: true, machines: [
        verdict(1, "a100-box", true, { leftover: 60 * GIB, gpus: [4], cards: [{ index: 4, available: 62 * GIB, total: 80 * GIB, shared_with: [] }] }),
        verdict(2, "strix", false, { reason: "no allowed GPU that faster-whisper supports" })] },
      cpu: { estimate: EST("cpu", 1.45 * GIB), fits: true, machines: [
        verdict(1, "a100-box", true, { cards: [{ index: null, available: 600 * GIB, total: 1000 * GIB, shared_with: [] }] }),
        verdict(2, "strix", true, { cards: [APU_CPU] })] },
    } },
    deployments: [],
  };
}

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? (url === "/api/models" ? { status: 200, body: { models: [MODEL], recipes: [WHISPER], can_manage: true, can_deploy: true } }
        : url === "/api/machines" ? { status: 200, body: fixture }
          : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: true } }
            : url === "/api/downloads" ? { status: 200, body: { downloads: [], can_download: true } }
              : { status: 404, body: {} });
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const mount = (path: string) => render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);

afterEach(() => {
  cleanup();
  setLang("en");
  localStorage.removeItem(LANG_KEY);
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("engine images as current state (1.5.6, #282)", () => {
  it("FY-WEB-985 presence says how a missing image or program comes, with its size, in both languages", () => {
    expect(presenceText("image", false, "direct", 3.24 * GIB)).toBe("image not here: pulled on deploy (3.2 GiB)");
    expect(presenceText("image", false, "server", null)).toBe("image not here: comes through the server on deploy");
    expect(presenceText("image", false, "auto", 0)).toBe("image not here: pulled on deploy, through the server if that fails");
    expect(presenceText("program", false, "server")).toBe("program not here: comes through the server on deploy");
    expect(presenceText("image", true, "server", 5 * GIB)).toBe("image here");
    expect(whereText("image", false, "server", 0.64 * GIB)).toBe("comes through the server when a deployment starts there (655 MiB)");
    expect(whereText("program", false, "auto")).toBe("downloaded when a deployment starts there, through the server if that fails");
    const e = { kind: "image" as const, present: false, via: "server" as const, size: 0.64 * GIB };
    expect(engineLine("apu-01", e)).toBe("apu-01 does not have the engine image yet: it comes through the server when the replica starts (655 MiB).");
    expect(engineLine("gpu-01", { ...e, via: "direct" })).toBe("gpu-01 does not have the engine image yet: it is pulled when the replica starts (655 MiB).");
    expect(engineLine("gpu-01", { ...e, present: true })).toBe("gpu-01 has the engine image.");
    expect(engineLine("gpu-01", { ...e, present: null })).toMatch(/^Whether gpu-01 has the engine image is not known yet/);
    expect(engineLine("cpu-01", { kind: "command", present: true, via: null, size: null })).toBeNull();
    setLang("zh-TW");
    expect(presenceText("image", false, "server", 0.64 * GIB)).toBe("映像檔不在這裡：部署時經由伺服器取得（655 MiB）");
    expect(whereText("image", false, "direct")).toBe("部署在那裡啟動時拉取");
    expect(engineLine("apu-01", e)).toBe("apu-01 還沒有這個推論引擎的映像檔：副本啟動時會經由伺服器取得（655 MiB）。");
    expect(engineLine("gpu-01", { ...e, present: true })).toBe("gpu-01 已經有這個推論引擎的映像檔。");
  });

  it("FY-WEB-986 a machine with no GPU the engine can use is judged on its CPU, and the CPU's memory is the system memory", () => {
    const fit = page().fits["faster-whisper"];
    expect(machineFit(fit, 2)).toMatchObject({ device: "cpu", verdict: { fits: true } });
    // Neither fits: the CPU's reason when no GPU card was usable, the GPU's when one was (short of memory).
    const noCpu = { machines: [verdict(2, "strix", false, { reason: "needs 1.45 GiB of RAM, 0.5 GiB available" })] };
    expect(machineFit({ gpu: fit.gpu, cpu: noCpu }, 2)).toMatchObject({ device: "cpu", verdict: { reason: /RAM/ } });
    const short = { machines: [verdict(2, "strix", false, { cards: [{ index: 0, available: GIB, total: 8 * GIB, shared_with: [] }] })] };
    expect(machineFit({ gpu: short, cpu: noCpu }, 2)?.device).toBe("gpu");
    const shared = { machines: [verdict(2, "strix", false, { share_fits: { gpus: [0], holders: ["x"], exclusive: ["x"], both: false } })] };
    expect(machineFit({ gpu: shared, cpu: noCpu }, 2)?.device).toBe("gpu");
    const apu = fixture.machines[1].accelerators as unknown as Accelerator[];
    expect(cpuMemory(verdict(2, "strix", true, { cards: [APU_CPU] }), 1.45 * GIB, apu))
      .toEqual({ need: 1.45 * GIB, free: 13 * GIB, total: 31 * GIB, apuGpu: 118111600640 });
    const a100 = fixture.machines[0].accelerators as unknown as Accelerator[];
    expect(cpuMemory(verdict(1, "a100-box", true, { cards: [APU_CPU] }), GIB, a100).apuGpu).toBeNull();
  });

  it("FY-WEB-987 the model page: fits on the CPU with the system memory it uses, Deploy here (CPU), and how a missing image comes", async () => {
    server((url) => url === "/api/models/faster-whisper-small" ? { status: 200, body: page() } : undefined);
    mount("/models/faster-whisper-small");
    await screen.findByRole("heading", { level: 1, name: "faster-whisper-small" });
    const table = screen.getByRole("table");
    const strix = within(table).getByText("strix").closest("tr")!;
    await waitFor(() => expect(within(strix).getByText("fits on the CPU")).toBeTruthy());
    // The APU's CPU side (31 GiB), not the 110 GiB its GPU has; the GPU's reason is not shown.
    expect(within(strix).getByText("uses 1.45 GiB of its 31.00 GiB system memory (13.00 GiB free), not the GPU's 110.00 GiB")).toBeTruthy();
    expect(within(strix).queryByText(/no allowed GPU/)).toBeNull();
    expect(within(strix).getByRole("link", { name: "Deploy here (CPU)" }).getAttribute("href"))
      .toBe("/serving/deploy?model=faster-whisper-small&recipe=faster-whisper&machine=2&device=cpu");
    expect(within(strix).getByText("image not here: comes through the server on deploy (655 MiB)")).toBeTruthy();
    const a100 = within(table).getByText("a100-box").closest("tr")!;
    expect(within(a100).getByRole("link", { name: "Deploy here" })).toBeTruthy();
    expect(within(a100).queryByText(/system memory/)).toBeNull();
    expect(within(a100).getByText("image not here: pulled on deploy (3.2 GiB)")).toBeTruthy();
    cleanup();
    localStorage.setItem(LANG_KEY, "zh-TW");
    mount("/models/faster-whisper-small");
    const zh = await screen.findByRole("table");
    const row = (await within(zh).findByText("strix")).closest("tr")!;
    await waitFor(() => expect(within(row).getByText("放得下（用 CPU）")).toBeTruthy());
    expect(within(row).getByText("使用 31.00 GiB 系統記憶體中的 1.45 GiB（剩 13.00 GiB）；GPU 的 110.00 GiB 不算在內")).toBeTruthy();
    expect(within(row).getByRole("link", { name: "部署到這裡（用 CPU）" })).toBeTruthy();
  });

  it("FY-WEB-988 the deploy form says whether the chosen machine has the engine image and how it comes", async () => {
    const placed = (machine_id: number, machine: string, engine: unknown) => ({ replica: 0, machine, machine_id, variant: "cpu", runtime: "container",
      image: WHISPER.variants[1].image, gpus: [], cached: true, leftover: 10 * GIB, online: true, engine });
    const engine = { variant: "cpu", runtime: "container", kind: "image", engine_name: WHISPER.variants[1].image, present: false, listed_at: null, note: null, via: "server", size: 0.64 * GIB };
    server((url, init) => url === "/api/deployments/estimate" && init?.method === "POST" ? { status: 200, body: {
      estimate: EST("cpu", 1.45 * GIB), recipe: "faster-whisper", model: MODEL.name, params: {},
      placement: { fits: true, placed: [placed(2, "strix", engine)], machines: [] } } } : undefined);
    mount("/serving/deploy?model=faster-whisper-small&recipe=faster-whisper&machine=2&device=cpu");
    const line = "strix does not have the engine image yet: it comes through the server when the replica starts (655 MiB).";
    // Under the machine field, and on the placement's line.
    await waitFor(() => expect(document.querySelector(".deploy-engine-there")?.textContent).toBe(line), { timeout: 3000 });
    expect(document.querySelector(".placed-engine")?.textContent).toBe(line);
    expect((screen.getByLabelText("Machine") as HTMLSelectElement).value).toBe("2");
    // Another machine chosen: nothing until an estimate places a replica there.
    fireEvent.change(screen.getByLabelText("Machine"), { target: { value: "1" } });
    expect(document.querySelector(".deploy-engine-there")).toBeNull();
  });

  it("FY-WEB-989 a done download's line: when and how the model came, never which engine came with it", async () => {
    const { downloadLine } = await import("../lib/downloads");
    const done = { state: "done", via: "server", recipe: "faster-whisper", engine_only: false, finished_at: null } as never;
    expect(downloadLine(done)).toBe("Downloaded — via the server");
    setLang("zh-TW");
    expect(downloadLine(done)).toBe("—下載完成，經由伺服器");
  });
});

describe("where the commands run, and Windows (WSL2) (1.5.6, #282)", () => {
  const JOIN = { server: "https://192.168.1.20:7860", fingerprint: "sha256:" + "3f".repeat(32), token: "fyj_example", image: "forgeyard-agent:1.5.6" };

  it("FY-WEB-990 the WSL2 command keeps --gpus all without the /dev/nvidia* loop; a Windows drive and a WSL kernel are recognised", () => {
    const wsl = runCommand("wsl", emptyForm(JOIN.server), JOIN)!;
    expect(wsl).toContain("--gpus all \\\n");
    expect(wsl).not.toContain(NVIDIA_DEVICES);
    expect(wsl).not.toContain("/dev/nvidia");
    expect(runCommand("nvidia", emptyForm(JOIN.server), JOIN)).toContain(NVIDIA_DEVICES);
    // Apart from the GPU lines it is the NVIDIA command.
    expect(wsl).toBe(runCommand("nvidia", emptyForm(JOIN.server), JOIN)!.replace(` \\\n  ${NVIDIA_DEVICES}`, ""));
    expect(windowsDriveDir("/mnt/c/models")).toBe(true);
    expect(windowsDriveDir("/mnt/D")).toBe(true);
    expect(windowsDriveDir("/mnt/data/models")).toBe(false);
    expect(windowsDriveDir("/srv/forgeyard-models")).toBe(false);
    expect(onWsl("5.15.167.4-microsoft-standard-WSL2")).toBe(true);
    expect(onWsl("6.6.87.2-microsoft-standard-WSL2+")).toBe(true);
    expect(onWsl("5.15.0-134-generic")).toBe(false);
    expect(onWsl(undefined)).toBe(false);
  });

  it("FY-WEB-991 Add a machine: every way says where to run it; Windows (WSL2) says what it needs and warns about /mnt/c", async () => {
    const created = { id: "t2", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner", token: "fyj_secret",
      server_url: "https://forge.lan:7860", valid_minutes: 15, ca_fingerprint: null, trust_command: null,
      command: "uv run --no-sync python -m forgeyard agent --server https://forge.lan:7860 --join-token fyj_secret",
      install_command: "curl -fsSk https://forge.lan:7860/install.sh | sudo sh -s -- --join fyj_secret",
      config_file: "server = \"https://forge.lan:7860\"\n", manual_commands: ["join", "start"], version: "1.5.6" };
    server((url, init) => url === "/api/machines/join-tokens" && init?.method === "POST" ? { status: 201, body: created }
      : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } }
        : url === "/api/tls" ? { status: 200, body: { mode: "forgeyard-ca", https: true } } : undefined);
    mount("/machines");
    await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(screen.getByRole("button", { name: "Add a machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    const here = "Run it on the machine you are adding, in a Linux shell (bash). SSH in first if that is not this computer.";
    expect(await screen.findByText(here)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Docker" }));
    expect(screen.getByText(here)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Windows (WSL2)" }));
    expect(screen.getByText(/^Run it on the Windows machine, in the shell of its WSL2 Linux distribution/)).toBeTruthy();
    expect(screen.getByText("Not verified on real hardware yet.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Add a Windows machine (WSL2)" }).getAttribute("href")).toMatch(/how-to#add-a-windows-machine-wsl2$/);
    const docker = () => screen.getByLabelText("Docker command").textContent ?? "";
    expect(docker()).toContain("--gpus all");
    expect(docker()).not.toContain("/dev/nvidia");
    expect(screen.queryByText(/is on a Windows drive/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Model directory on that machine"), { target: { value: "/mnt/c/models" } });
    expect(screen.getByText(/is on a Windows drive: every file goes through WSL's file sharing/)).toBeTruthy();
    // The warning is for Windows only.
    fireEvent.click(screen.getByRole("button", { name: "NVIDIA" }));
    expect(screen.queryByText(/is on a Windows drive/)).toBeNull();
    expect(screen.getByText(here)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Manual (config file)" }));
    expect(screen.getByText(here)).toBeTruthy();
  });

  it("FY-WEB-992 Update the agent says where its commands run: a Linux shell on that machine, or its WSL2 distribution on Windows", async () => {
    const update = { machine_id: 1, name: "a100-box", agent_version: "1.5.5", server_version: "1.5.6", behind: null, method: "container", known: true,
      reason: null, image: "x", offline: false, steps: [{ title: "On a100-box", command: "sudo docker pull x" }], alternative: null, notes: [], guide: "" };
    const withKernel = (kernel: string) => {
      const data = structuredClone(fixture) as typeof fixture & { machines: Record<string, unknown>[] };
      (data.machines[0] as Record<string, unknown>).platform = { os: "Linux", arch: "x86_64", kernel };
      return data;
    };
    let kernel = "5.15.0-134-generic";
    server((url) => url === "/api/machines" ? { status: 200, body: withKernel(kernel) }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: update } : undefined);
    mount("/machines/1");
    expect(await screen.findByText("Run each command in a Linux shell (bash) on the machine its step names. For a100-box, SSH in first if it is not this computer.")).toBeTruthy();
    cleanup();
    kernel = "5.15.167.4-microsoft-standard-WSL2";
    localStorage.setItem(LANG_KEY, "zh-TW");
    mount("/machines/1");
    expect(await screen.findByText(/^每個指令都在步驟寫的那台機器上執行。a100-box 是 Windows 機器，要在它的 WSL2 發行版 shell 裡執行/)).toBeTruthy();
  });
});
