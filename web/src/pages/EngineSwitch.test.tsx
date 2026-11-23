// @vitest-environment jsdom
// 1.5.2 (#95 PR 3): moving a deployment to another version of its engine in Edit (the select, what saving does, the
// notice before restarting the only replica, in both languages), Download to… for an engine alone from its page, the
// hint on a copy whose built-in engine caught up, a deployment that runs a copy, and an engine-only download on a
// machine's list. FY-WEB-881 to 886.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { DownloadList } from "../components/DownloadTo";
import { LANG_KEY } from "../i18n/lang";
import type { CatalogResponse, Deployment, DownloadTargets, EditOutage, EditResponse, EnginePage, ModelDownload, Recipe, Replica, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };
const GIB = 1024 ** 3;

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const D1 = "sha256:" + "ab".repeat(32);
const D2 = "sha256:" + "cd".repeat(32);
const VLLM = {
  name: "vllm", title: "vLLM", description: "", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"], api: { chat: "openai-chat" },
  share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"], based_on: null, copy: null,
  params: { context: { type: "int", default: 8192, min: 256, label: "Context length (tokens)" },
    enforce_eager: { type: "choice", default: "off", choices: ["off", "on"], label: "Eager mode" } },
  variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: `vllm/vllm-openai:v0.32.0@${D1}`, arch: null, compute_capability: ">=8.0",
    driver: ">=580", engine: null, image_check: true }],
} as unknown as Recipe;
const COPY = { ...VLLM, name: "vllm-0-34-0", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm",
  params: { context: VLLM.params.context },
  variants: [{ ...VLLM.variants[0], image: `vllm/vllm-openai:v0.34.0@${D2}` }],
  copy: { from_version: "0.32.0", kind: "release", made_at: "2026-11-23T10:00:00+00:00", made_by: "owner", left_out: [], archives: [] } } as unknown as Recipe;

const rep = (): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 2, name: "gpu-01", status: "online", connected: true },
  gpus: [{ index: 0, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: "nvidia:GPU-x" }], state: "healthy", reason: null,
  exit_code: null, attempt: 0, restarts: 0, final: false, pid: null, container: "fy-chat-0", ports: [], started_at: "2026-11-10T10:00:00Z",
  finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 15 * GIB, download: null,
  runs: { type: "container", image: `vllm/vllm-openai:v0.32.0@${D1}`, command: ["vllm", "serve"] },
}) as Replica;
const workload = (reps: Replica[]): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: reps.length, replicas: reps.length,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: reps.length, machine_id: null },
  replica_list: reps,
}) as Workload;
const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15 * GIB, ram: 0, amount: 15 * GIB, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8 * GIB, detail: "" }] };
const ENGINES = [
  { name: "vllm", title: "vLLM", version: "0.32.0", builtin: true, based_on: null, current: true },
  { name: "vllm-0-34-0", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm", current: false },
];
const dep = (over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192, enforce_eager: "off", gpus: 1, share: "exclusive" },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" }, endpoints: [], workload: workload([rep()]), cards: [],
  pinned: null, rollout: null, engine: { name: "vllm", title: "vLLM", version: "0.32.0", builtin: true, based_on: null }, engines: ENGINES, ...over,
});
const catalog = {
  can_manage: true, can_deploy: true,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa9a72", file: null,
    format: "safetensors", task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm", "vllm-0-34-0"], deployments: 1,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 4022468096, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" },
    gguf_files: [] }],
  recipes: [VLLM, COPY],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
} as unknown as CatalogResponse;
const placement = { fits: true, machines: [], share_hint: null, placed: [{ replica: 0, machine: "gpu-01", machine_id: 2, variant: "cuda",
  runtime: "container", image: null, gpus: [0], cached: true, leftover: 30 * GIB, online: true, previous: true }] };
const OUTAGE: EditOutage = { seconds: 118, source: "last_start", first_start: true, image_missing: ["gpu-01"],
  avoid: ["add_replica", "autoscale", "cloud"], cloud: true, text: "server text" };

function editServer(bodies: Record<string, unknown>[]) {
  return server((url, init) => {
    if (url === "/api/deployments/3" && init?.method === "PATCH") {
      const b = JSON.parse(String(init.body));
      bodies.push(b);
      const switched = b.recipe === "vllm-0-34-0";
      const changes: EditResponse["changes"] = switched ? { recipe: { before: "vllm", after: "vllm-0-34-0" }, enforce_eager: { before: "off", after: null } } : {};
      const out: EditResponse = { estimate, params: b.params, changes, placement: placement as EditResponse["placement"],
        restart: switched ? "all" : "none", restart_text: switched ? "Its replica restarts: the model does not answer until the new one is ready." : "Nothing restarts.",
        recipe: b.recipe ?? "vllm", dropped: switched ? ["enforce_eager"] : [], outage: switched ? OUTAGE : null };
      return { status: 200, body: b.dry_run ? out : { ...out, deployment: dep() } };
    }
    if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep(), events: [], server_time: "", can_manage: true } };
    if (url === "/api/models") return { status: 200, body: catalog };
    if (url === "/api/machines") return { status: 200, body: { machines: [{ id: 2, name: "gpu-01", status: "online" }], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } };
    return undefined;
  });
}

describe("another version of the engine in Edit (1.5.2)", () => {
  it("FY-WEB-881: the Engine select lists the built-in and its copy; choosing the copy previews it, warns about the one replica and saves recipe", async () => {
    const bodies: Record<string, unknown>[] = [];
    editServer(bodies);
    mount("/serving/3/edit");
    const select = await screen.findByLabelText("Engine version") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["vllm · vLLM 0.32.0 · built-in", "vllm-0-34-0 · vLLM 0.34.0 · copy"]);
    expect(select.value).toBe("vllm");
    expect(screen.getByText(/The built-in version, accepted on real hardware/)).toBeTruthy();
    expect(screen.getByLabelText("Eager mode")).toBeTruthy();
    fireEvent.change(select, { target: { value: "vllm-0-34-0" } });
    expect(screen.getByText(/A copy is not accepted by Forgeyard: a newer version may change flags, defaults and memory use\. .*choose vllm again to go back\./)).toBeTruthy();
    expect(screen.queryByLabelText("Eager mode")).toBeNull();   // the copy's parameters
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    expect(within(changes).getByText("Engine")).toBeTruthy();
    expect(within(changes).getByText("vllm-0-34-0")).toBeTruthy();
    expect(screen.getByText("vllm-0-34-0 does not take these parameters, so they are dropped: enforce_eager.")).toBeTruthy();
    expect(screen.getByText("It has one replica: the model will not answer for about 118 s while it restarts.")).toBeTruthy();
    expect(screen.getByText("(Its last start took that long.)")).toBeTruthy();
    expect(screen.getByText("A new engine version can take longer on its first start: it may compile first.")).toBeTruthy();
    expect(screen.getByText(/Its image is not on gpu-01 yet, so it is downloaded first\./)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download to…" }).getAttribute("href")).toBe("/models/engines/vllm-0-34-0");
    expect(screen.getByText(/Raise Replicas to 2 in this change when there is room for two/)).toBeTruthy();
    expect(screen.getByText("Or turn on automatic replicas, with at least 2.")).toBeTruthy();
    expect(screen.getByText("Meanwhile, keys that may use the cloud are answered by this deployment's cloud tier.")).toBeTruthy();
    expect(bodies.at(-1)).toMatchObject({ recipe: "vllm-0-34-0", dry_run: true });
    fireEvent.click(screen.getByRole("button", { name: "Save and restart" }));
    await waitFor(() => expect(bodies.some((b) => !b.dry_run)).toBe(true));
    const saved = bodies.find((b) => !b.dry_run)!;
    expect(saved.recipe).toBe("vllm-0-34-0");
    expect(saved.params).not.toHaveProperty("enforce_eager");
  });

  it("FY-WEB-882: zh-TW: the select, what saving does (the restart translated) and the one-replica notice read in Chinese", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    editServer([]);
    mount("/serving/3/edit");
    const select = await screen.findByLabelText("推論引擎版本") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["vllm · vLLM 0.32.0 · 內建", "vllm-0-34-0 · vLLM 0.34.0 · 複本"]);
    fireEvent.change(select, { target: { value: "vllm-0-34-0" } });
    expect(screen.getByText(/複本沒有經過 Forgeyard 驗收/)).toBeTruthy();
    await screen.findByRole("list", { name: "變更內容" }, { timeout: 3000 });
    expect(screen.getByText("它的副本會重新啟動：新的副本就緒之前，模型不會回應。")).toBeTruthy();
    expect(screen.getByText("它只有一個副本：重新啟動期間，模型大約有 118 秒不會回應。")).toBeTruthy();
    expect(screen.getByText("新版本的推論引擎第一次啟動可能比較久：它可能要先編譯。")).toBeTruthy();
    expect(screen.getByText("這段期間，可以使用雲端的 API 金鑰會改由這個部署的雲端層回應。")).toBeTruthy();
    expect(screen.getByText("vllm-0-34-0 不接受這些參數，所以會拿掉：enforce_eager。")).toBeTruthy();
    expect(screen.queryByText(/Its replica restarts/)).toBeNull();
  });

  it("FY-WEB-883: an engine with no other version says where to try one, and a deployment on a copy says so on its page", async () => {
    server((url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep({ engines: [ENGINES[0]] }), events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: { machines: [] } };
      return undefined;
    });
    mount("/serving/3/edit");
    const select = await screen.findByLabelText("Engine version") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(screen.getByRole("link", { name: "Its page" }).getAttribute("href")).toBe("/models/engines/vllm");
    cleanup();
    server((url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep({ recipe: "vllm-0-34-0",
        engine: { name: "vllm-0-34-0", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm" } }), events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      return { status: 200, body: { machines: [], deployments: [], keys: [] } };
    });
    mount("/serving/3");
    const line = await screen.findByText(/Runs a copy:/);
    expect(line.textContent).toBe("Runs a copy: vLLM 0.34.0 (based on vllm).");
    expect(within(line).getByRole("link", { name: "vllm" }).getAttribute("href")).toBe("/models/engines/vllm");
  });
});

// ------------------------------------------------------------------ the engine page
const target = (machine_id: number, name: string, state: string, over: Record<string, unknown> = {}) => ({
  machine_id, name, status: "online", connected: true, state, note: "", model_cached: false, need_bytes: 0, free_bytes: 40 * GIB, variant: "cuda",
  engine: { kind: "image", name: `vllm/vllm-openai:v0.34.0@${D2}`, cached: false }, engine_note: null, download: null, via: "direct", from_machine: null, ...over,
});
const enginePage = (r: Recipe, over: Partial<EnginePage> = {}): EnginePage => ({
  recipe: { ...r, yaml: `name: ${r.name}\n` } as Recipe, can_manage: true, can_download: true,
  machines: [{ machine_id: 2, name: "gpu-01", status: "online", connected: true, can_list: true, variant: "cuda", runtime: "container", kind: "image",
    engine_name: COPY.variants[0].image, present: false, listed_at: null, note: null, download: null }],
  deployments: [], models: [], pins: {}, downloads: [], caught_up: null, ...over,
}) as EnginePage;
const engineDl = (over: Partial<ModelDownload> = {}): ModelDownload => ({
  id: 9, model: null, engine_only: true, machine: { id: 2, name: "gpu-01", connected: true }, file: null, recipe: "vllm-0-34-0", state: "downloading",
  via: "server", download: { phase: "image", done: 2 * GIB, total: 9 * GIB, layers: 40, layers_done: 10 }, reason: "pulling the image", requested_by: "owner",
  created_at: "2026-11-23T10:00:00Z", finished_at: null, waiting: null, source: null, ...over,
});

describe("Download to… for an engine alone (1.5.2)", () => {
  it("FY-WEB-884: the engine page's Download to… lists the machines (one refused, saying why), starts it with the recipe alone, and lists the downloads", async () => {
    const posts: Record<string, unknown>[] = [];
    let started = false;
    const targets: DownloadTargets = { model: null, engine: { name: "vllm-0-34-0", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm" },
      recipes: ["vllm-0-34-0"], recipe: "vllm-0-34-0", can_download: true,
      machines: [target(2, "gpu-01", "ok", { via: "server" }), target(4, "cpu-box", "no_engine", { variant: null, engine: null,
        engine_note: "no variant of vllm-0-34-0 for this machine's hardware", note: "No variant of vllm-0-34-0 runs on cpu-box: no variant…" })] } as DownloadTargets;
    server((url, init) => {
      if (url.startsWith("/api/recipes/vllm-0-34-0")) return { status: 200, body: enginePage(COPY, started ? { downloads: [engineDl()],
        machines: [{ ...enginePage(COPY).machines[0], download: engineDl() }] } : {}) };
      if (url === "/api/downloads/targets?recipe=vllm-0-34-0") return { status: 200, body: targets };
      if (url === "/api/downloads" && init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        started = true;
        return { status: 201, body: { downloads: [engineDl({ state: "queued", download: null })], skipped: [] } };
      }
      if (url === "/api/models") return { status: 200, body: catalog };
      return { status: 200, body: { machines: [], deployments: [] } };
    });
    mount("/models/engines/vllm-0-34-0");
    fireEvent.click(await screen.findByRole("button", { name: "Download to…" }));
    const dlg = await screen.findByRole("dialog");
    expect(within(dlg).getByText(/Only the engine is fetched: the image or program of the variant each machine would run/)).toBeTruthy();
    await within(dlg).findByText("gpu-01");
    expect(within(dlg).getByText("No variant")).toBeTruthy();
    expect(within(dlg).getByText("No variant of this engine runs here: no variant of vllm-0-34-0 for this machine's hardware")).toBeTruthy();
    expect(within(dlg).getByText(/through the server/)).toBeTruthy();
    const boxes = within(dlg).getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes[1].disabled).toBe(true);
    fireEvent.click(boxes[0]);
    fireEvent.click(within(dlg).getByRole("button", { name: "Download to 1 machine" }));
    expect(await screen.findByText("Downloading vllm-0-34-0 to gpu-01.")).toBeTruthy();
    expect(posts).toEqual([{ recipe: "vllm-0-34-0", machine_ids: [2] }]);
    const list = await screen.findByRole("list", { name: "Downloads" });
    expect(within(list).getByText(/2.00 of 9.00 GiB|22%/)).toBeTruthy();
    expect(screen.getAllByText("downloading")).toHaveLength(2);   // the machine's row and the list
  });

  it("FY-WEB-885: a copy whose built-in caught up says the deployments can switch back (en and zh-TW); operators see Download to… but not Edit", async () => {
    const page = enginePage(COPY, { can_manage: false, caught_up: { builtin: "vllm", title: "vLLM", version: "0.34.0", same: true } });
    server((url) => {
      if (url.startsWith("/api/recipes/vllm-0-34-0")) return { status: 200, body: page };
      if (url === "/api/models") return { status: 200, body: catalog };
      return { status: 200, body: { machines: [], deployments: [] } };
    });
    mount("/models/engines/vllm-0-34-0");
    const hint = await screen.findByText(/now runs 0\.34\.0, the same version as this copy/);
    expect(hint.textContent).toBe("The built-in vllm now runs 0.34.0, the same version as this copy: deployments on this copy can switch back to it in Edit, and then this copy can be deleted.");
    expect(screen.getByRole("button", { name: "Download to…" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    cleanup();
    localStorage.setItem(LANG_KEY, "zh-TW");
    mount("/models/engines/vllm-0-34-0");
    const zh = await screen.findByText(/現在已經是 0\.34\.0/);
    expect(zh.textContent).toBe("內建的 vllm 現在已經是 0.34.0，和這個複本同一版：用這個複本的部署可以在「編輯」裡改回內建版本，之後就能刪除這個複本。");
    expect(screen.getByRole("button", { name: "下載到…" })).toBeTruthy();
  });

  it("FY-WEB-886: on a machine's list an engine-only download names the engine, links to its page and cancels by its name", async () => {
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    render(<MemoryRouter><DownloadList list={[engineDl(), engineDl({ id: 10, state: "done", finished_at: new Date().toISOString(), download: null, via: "direct" })]}
      show="model" canCancel onChanged={() => {}} onError={() => {}} /></MemoryRouter>);
    const links = screen.getAllByRole("link", { name: "Engine vllm-0-34-0" });
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/models/engines/vllm-0-34-0");
    expect(screen.queryByText(/with engine/)).toBeNull();   // the title names it already
    fireEvent.click(screen.getByRole("button", { name: /Cancel/ }));
    expect(String((confirm.mock.calls[0] as unknown[])[0])).toContain("vllm-0-34-0");
  });
});
