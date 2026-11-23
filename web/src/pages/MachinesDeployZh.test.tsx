// @vitest-environment jsdom
// Machines and deployments in Traditional Chinese (#189, group 1): a machine's page, a deployment's page, the deploy
// and edit forms and their dialogs, rendered for a zh-TW reader. The server's own words (event messages, reasons,
// machine names) stay as sent; English pages are unchanged (their own tests).
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import type { EngineImagesView } from "../lib/imageUpload";
import type { CatalogResponse, Deployment, DownloadTargets, EditResponse, EstimateResponse, MachineCache, Replica, Workload } from "../lib/types";
import fixture from "../test/machines.json";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };
const GIB = 1024 ** 3;
const HAN = /\p{Script=Han}/u;

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string, lang: "en" | "zh-TW" = "zh-TW") {
  localStorage.setItem(LANG_KEY, lang);
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// Every heading of the page's main area (not the shell, translated in #237) has Chinese in it.
function headingsInChinese() {
  const main = document.querySelector("main") ?? document.body;
  const english = [...main.querySelectorAll("h1, h2, h3")].map((h) => h.textContent ?? "")
    .filter((text) => /[A-Za-z]{3,}/.test(text) && !HAN.test(text) && !/^[\w.-]+$/.test(text.trim()));
  expect(english).toEqual([]);
}

// ---------------------------------------------------------------- a machine's page

const cache: MachineCache = {
  machine_id: 1, connected: true, reported: true, sizes: true, ok: true, reason: null, free_bytes: 50 * GIB, total_bytes: 100 * GIB,
  used_bytes: 7.5 * GIB, partial_bytes: 300 * 1024 ** 2, partial_files: 2, truncated: false, remove_note: null,
  models: [{ key: "url/" + "d".repeat(24), kind: "url", file: "tiny.gguf", bytes: 100 * 1024 ** 2, files: 1, catalog: [], used_by: [] }],
  engines: [],
} as unknown as MachineCache;
const VIEW: EngineImagesView = { machine_id: 1, listed: true, note: null, others: 1, free_bytes: 40 * GIB, total_bytes: 100 * GIB,
  can_remove: true, remove_note: null,
  images: [{ id: "sha256:" + "1".repeat(64), tags: ["ghcr.io/ggml-org/llama.cpp:server-b12359"], content: null, size: 1.9 * GIB, created: null,
    os: "linux", architecture: "amd64", agent: false, used_by: [], remove_note: null,
    matches: [{ recipe: "llama-cpp", variant: "cpu", image: "ghcr.io/ggml-org/llama.cpp:server-b12359@sha256:" + "9".repeat(64), how: "checked" }] }] };
const TARGETS: DownloadTargets = {
  model: { id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF", size_bytes: 1.2 * GIB, kind: "hf", file: "q.gguf" },
  recipes: ["llama-cpp"], recipe: "llama-cpp", can_download: true,
  machines: [{ machine_id: 1, name: "a100-box", state: "ok", connected: true, need_bytes: 1.2 * GIB, free_bytes: 48 * GIB, model_cached: false,
    variant: "cpu-process", engine: { kind: "archive", name: "llama.cpp-1.0.0", cached: false }, engine_note: null, download: null, note: null }],
} as unknown as DownloadTargets;

function machineServer(allOnline = false, features = ["workloads", "models", "cache_admin", "prefetch", "image_load", "image_save", "image_remove"]) {
  const machines = { ...fixture, machines: fixture.machines.map((m) => ({ ...m, ...(allOnline ? { status: "online" } : {}), features, fetch_via: "auto", image_upload_max_bytes: 64 * GIB,
    runtime_info: { process: { ok: true, user: "forgeyard-run", limits: "rlimit" }, container: { ok: true, engine: "docker", version: "27.1" } } })) };
  return server((url) => {
    if (url === "/api/machines") return { status: 200, body: machines };
    if (url === "/api/workloads") return { status: 200, body: { workloads: [], server_time: fixture.server_time, can_manage: true } };
    if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: fixture.server_time, can_manage: true } };
    if (/^\/api\/machines\/\d\/cache$/.test(url)) return { status: 200, body: cache };
    if (url.startsWith("/api/downloads?machine_id=")) return { status: 200, body: { downloads: [], can_download: true } };
    if (url === "/api/machines/1/engine-images") return { status: 200, body: VIEW };
    if (url === "/api/models") return { status: 200, body: { models: [{ id: 5, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B GGUF", kind: "hf", size_bytes: 1.2 * GIB }], recipes: [] } };
    if (url.startsWith("/api/downloads/targets?model_id=5")) return { status: 200, body: TARGETS };
    return undefined;
  });
}

describe("a machine's page in zh-TW", () => {
  it("FY-WEB-709 every section is in Chinese: facts, memory map, running here, cache, engine images, GPU access, agent", async () => {
    machineServer();
    mount("/machines/1");
    expect(await screen.findByRole("heading", { name: "記憶體配置圖" })).toBeTruthy();
    for (const name of ["模型", "在這裡執行 · 0", "GPU 存取和執行環境", "agent 資訊"]) expect(screen.getByRole("heading", { name })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: /^快取中的模型/ })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: /^引擎映像檔/ })).toBeTruthy();
    expect(screen.getByText("線上")).toBeTruthy();
    expect(screen.getByText("心跳 4 秒前")).toBeTruthy();
    expect(screen.getByRole("button", { name: "撤銷" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "移除" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "部署到這裡" })).toBeTruthy();
    expect(screen.getByText("這裡還沒有在執行任何東西。")).toBeTruthy();
    expect(screen.getByText(/^執行方式：行程（以 forgeyard-run 身分，rlimit 限制） · 容器：docker 27\.1$/)).toBeTruthy();
    expect(await screen.findByText("快取中有 7.5 GiB · 可用 50 GiB，共 100 GiB")).toBeTruthy();
    expect(screen.getByText("2 個檔案", { exact: false })).toBeTruthy();
    expect(screen.getByRole("button", { name: "移除未完成的下載" })).toBeTruthy();
    expect(screen.getByLabelText("下載時的網路連線")).toBeTruthy();
    expect(await screen.findByText(/這台機器的容器引擎裡還有 1 個其他映像檔/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "從其他機器複製" })).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-710 an offline machine's facts say the status once, in either language (the old English-only prefix strip)", async () => {
    machineServer();
    const { unmount } = mount("/machines/2");
    const zh = await screen.findByText(/^最後心跳 /);
    expect(zh.textContent).toBe("最後心跳 41 秒前");
    expect(zh.closest("span")?.parentElement?.textContent).toMatch(/^ 離線 · 最後心跳 41 秒前$/);
    unmount();
    mount("/machines/2", "en");
    const en = await screen.findByText(/^last heartbeat /);
    expect(en.textContent).toBe("last heartbeat 41 s ago");
    expect(en.closest("span")?.parentElement?.textContent).toBe(" offline · last heartbeat 41 s ago");
  });

  it("FY-WEB-717 in English the plurals read as before: one other image, two partial files", async () => {
    machineServer();
    mount("/machines/1", "en");
    expect(await screen.findByText("1 other image is in this machine's engine. No recipe names it, so it is not Forgeyard's and is not listed here.")).toBeTruthy();
    expect((await screen.findByText(/^Partial downloads:/)).textContent).toBe(" Partial downloads: 300 MiB in 2 files");
    expect(screen.getByText("heartbeat 4 s ago")).toBeTruthy();
  });

  it("FY-WEB-711 changing GPU access asks and answers in Chinese", async () => {
    machineServer();
    const ask = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount("/machines/1");
    fireEvent.click(await screen.findByRole("button", { name: "變更 GPU 存取" }));
    const box = screen.getByRole("group", { name: "a100-box 的 GPU 存取：Forgeyard 可以指派的 GPU" });
    fireEvent.click(within(box).getByRole("button", { name: "都不選" }));
    fireEvent.click(within(box).getByRole("button", { name: "儲存" }));
    expect(ask).toHaveBeenCalledWith("要讓 Forgeyard 在 a100-box 上不指派任何 GPU 嗎？那裡只會執行使用 CPU 的模型和應用程式。");
    expect(within(box).getByRole("button", { name: "取消" })).toBeTruthy();
  });

  it("FY-WEB-712 the download dialog and the upload and copy dialogs are in Chinese", async () => {
    machineServer(true);
    mount("/machines/1");
    fireEvent.click(await screen.findByRole("button", { name: "下載模型到這裡…" }));
    const dlg = await screen.findByRole("dialog", { name: "下載模型到…" });
    expect(within(dlg).getByText(/^模型會現在就放進每台機器的模型快取/)).toBeTruthy();
    expect(await within(dlg).findByText("放得下")).toBeTruthy();
    expect(within(dlg).getByText("要下載 1.2 GiB · 可用 48 GiB · 引擎：llama.cpp-1.0.0 封存檔 · cpu-process")).toBeTruthy();
    expect((within(dlg).getByRole("checkbox", { name: /a100-box/ }) as HTMLInputElement).checked).toBe(true);
    expect(within(dlg).getByRole("button", { name: "下載到 1 台機器" })).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "上傳映像檔" }));
    const up = await screen.findByRole("dialog", { name: "上傳映像檔到 a100-box" });
    expect(within(up).getByText(/上限 64 GiB。$/)).toBeTruthy();
    expect(within(up).getByLabelText("映像檔檔案")).toBeTruthy();
    fireEvent.click(within(up).getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "從其他機器複製" }));
    const cp = await screen.findByRole("dialog", { name: "複製映像檔到 a100-box" });
    expect(within(cp).getByLabelText("來源")).toBeTruthy();
    expect(within(cp).getByRole("button", { name: "複製" })).toBeTruthy();
  });
});

// ---------------------------------------------------------------- a deployment's page and the forms

const rep = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 1, name: "a100-box", status: "online", connected: true },
  gpus: [{ index: 0, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: "nvidia:GPU-x" }], state: "healthy", reason: null,
  exit_code: null, attempt: 0, restarts: 0, final: false, pid: null, container: "fy-chat-0", ports: [], started_at: "2026-11-10T10:00:00Z",
  finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 15443688448, download: null,
  runs: { type: "container", image: "vllm/vllm-openai:v0.32.0", command: ["vllm", "serve"] }, ...over,
});
const workload = (reps: Replica[]): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: reps.length, replicas: reps.length,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: reps.length, machine_id: null },
  replica_list: reps,
} as Workload);
const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15443688448, ram: 0, amount: 15443688448, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8044936192, detail: "" }, { label: "KV cache", bytes: 4831838208, detail: "" }] };
const dep = (over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive" },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" },
  endpoints: [{ replica: 0, machine: "a100-box", url: "https://10.0.0.5:47001", ready: true, state: "healthy", served: 12, in_flight: 1, protected: true, guarded: true }],
  workload: workload([rep()]), cards: [], pinned: null, rollout: null,
  sleep: { on: true, idle_minutes: 30, wake_wait: 300, asleep: false, asleep_since: null, waking: null, held_by: null, sleeps_at: null,
    last_request_at: null, wakes: 0 },
  ...over,
} as unknown as Deployment);
const EVENTS = [{ at: "2026-11-10T10:00:00Z", kind: "placed", message: "Replica 0 placed on a100-box GPU #0." }];
const catalog = {
  can_manage: true, can_deploy: true, hf_token_set: false,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa9a72", file: null,
    format: "safetensors", task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm"], deployments: 1,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 4022468096, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" },
    gguf_files: [] }],
  recipes: [{ name: "vllm", title: "vLLM", description: "Chat models", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"],
    api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"],
    params: { context: { type: "int", default: 8192, min: 256, max: 1048576, label: "Context length (tokens)" } },
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: ">=7.0", driver: ">=580", engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
} as unknown as CatalogResponse;
const placement = { fits: true, machines: [{ machine_id: 1, name: "a100-box", status: "online", fits: true, variant: "cuda", reason: "",
  cards: [{ index: 0, available: 30 * GIB }] }], share_hint: null,
  placed: [{ replica: 0, machine: "a100-box", machine_id: 1, variant: "cuda", runtime: "container", image: null, gpus: [0], cached: true,
    leftover: 30 * GIB, online: true, previous: false }] };

function deploymentServer(logs: object = { source: "live", text: "ready", note: null }) {
  return server((url, init) => {
    if (url === "/api/deployments/3" && init?.method === "PATCH") {
      const out = { estimate, params: {}, placement, restart: "all", restart_text: "Its replica restarts: the model does not answer until the new one is ready.",
        changes: { share: { before: "exclusive", after: "vram" }, sleep_when_idle: { before: null, after: true } } } as unknown as EditResponse;
      return { status: 200, body: out };
    }
    if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep(), events: EVENTS, server_time: "", can_manage: true } };
    if (url.startsWith("/api/workloads/11/replicas/0/logs")) return { status: 200, body: logs };
    if (url === "/api/models") return { status: 200, body: catalog };
    if (url === "/api/machines") return { status: 200, body: { ...fixture, machines: fixture.machines.slice(0, 1) } };
    if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
    if (url === "/api/deployments/estimate") return { status: 200, body: { estimate, placement, params: {}, recipe: "vllm", model: "qwen3-4b" } as unknown as EstimateResponse };
    return undefined;
  });
}

describe("a deployment's page in zh-TW", () => {
  it("FY-WEB-713 every section is in Chinese; the events name their kind in Chinese before the server's message", async () => {
    deploymentServer();
    mount("/serving/3");
    expect(await screen.findByRole("heading", { name: "副本" })).toBeTruthy();
    for (const name of ["經由閘道", "沒有使用時", "記憶體：估算與即時", "試試看", "事件", "輸出", "背後的副本", "目前在機器上"]) {
      expect(screen.getByRole("heading", { name })).toBeTruthy();
    }
    expect(screen.getByText("1/1 就緒")).toBeTruthy();
    expect(document.querySelector(".wl-status .chip-big")?.textContent).toBe("執行中");
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual(["#", "狀態", "機器", "裝置", "執行內容", "保留", "開始時間"]);
    expect(within(table).getByText("健康")).toBeTruthy();
    expect(screen.getByText("開啟：30 分鐘沒有請求後休眠；請求最多等 300 秒讓它喚醒")).toBeTruthy();
    expect(screen.getByText("12 個請求 · 1 個進行中")).toBeTruthy();
    expect(screen.getByText("金鑰守門員")).toBeTruthy();
    const event = screen.getByText("Replica 0 placed on a100-box GPU #0.", { exact: false });
    expect(event.textContent).toBe("已放置Replica 0 placed on a100-box GPU #0.");
    expect(within(event).getByText("已放置").tagName).toBe("B");
    for (const name of ["編輯", "停止", "刪除"]) expect(screen.getByRole(name === "編輯" ? "link" : "button", { name })).toBeTruthy();
    expect(await screen.findByText(/^即時取自 agent，每 2 秒更新；顯示最後 300 行。$/)).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-714 in English the events show the server's message alone, as before", async () => {
    deploymentServer();
    mount("/serving/3", "en");
    const event = await screen.findByText("Replica 0 placed on a100-box GPU #0.");
    expect(event.tagName).toBe("SPAN");
    expect(event.textContent).toBe("Replica 0 placed on a100-box GPU #0.");
    expect(screen.queryByText("Placed")).toBeNull();
  });

  it("FY-WEB-840 the output panel's note from the server in Chinese when it is a known sentence; English as sent", async () => {
    const note = "Live output is not available: its machine is not connected. Showing the lines kept when it last ended.";
    deploymentServer({ source: "stored", text: "old line", truncated: false, state: "exited", note });
    mount("/serving/3");
    expect(await screen.findByText("無法顯示即時輸出：它的機器沒有連線。下面是它上次結束時保留的輸出。")).toBeTruthy();
    cleanup();
    deploymentServer({ source: "stored", text: "old line", truncated: false, state: "exited", note });
    mount("/serving/3", "en");
    expect(await screen.findByText(note)).toBeTruthy();
  });
});

describe("the deploy and edit forms in zh-TW", () => {
  it("FY-WEB-715 Deploy a model: sections, fields, the estimate and where it would go", async () => {
    deploymentServer();
    mount("/serving/deploy?model=qwen3-4b");
    expect(await screen.findByRole("heading", { name: "部署模型" })).toBeTruthy();
    for (const name of ["模型和引擎", "參數", "沒有使用時"]) expect(await screen.findByRole("heading", { name })).toBeTruthy();
    expect(screen.getByLabelText("部署名稱")).toBeTruthy();
    expect(screen.getByLabelText("對外的模型名稱")).toBeTruthy();
    expect(screen.getByLabelText("GPU 共用")).toBeTruthy();
    expect(screen.getByLabelText("閒置時休眠")).toBeTruthy();
    expect(screen.getByText(/^進階：記憶體覆寫、引擎參數、首次回應等待、喚醒等待、權杖$/)).toBeTruthy();
    expect(screen.getByText(/^只適用閒置時休眠：喚醒時，送給它的請求最多等多久（10 到 1,800）/)).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "估算" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "會放在哪裡" }, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByText(/^副本 0：/)).toBeTruthy();
    expect(screen.getByText("放得下")).toBeTruthy();
    expect(screen.getByRole("button", { name: "部署" })).toBeTruthy();
    headingsInChinese();
  });

  it("FY-WEB-716 Change a deployment: what saving does names each change and its values in Chinese", async () => {
    deploymentServer();
    mount("/serving/3/edit");
    expect(await screen.findByRole("heading", { name: "變更 chat" })).toBeTruthy();
    const changes = await screen.findByRole("list", { name: "變更內容" }, { timeout: 3000 });
    expect([...changes.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["GPU 共用：獨占 → 共用 VRAM", "閒置時休眠：關閉 → 開啟"]);
    expect(screen.getByRole("heading", { name: "GPU 共用和機器" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "儲存後會發生什麼" })).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "儲存並重新啟動" })).toBeTruthy());
    expect((screen.getByLabelText("機器") as HTMLSelectElement).options[1].textContent).toBe("固定在 a100-box · 線上");
    headingsInChinese();
  });
});
