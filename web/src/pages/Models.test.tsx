// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { CatalogResponse, Deployment, EstimateResponse, Replica, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const GIB = 1024 ** 3;
const rep = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 2, name: "a100", status: "online", connected: true },
  gpus: [{ index: 4, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: "nvidia:GPU-x" }], state: "starting",
  reason: "downloading the model: 2.00 of 7.51 GiB (26%)", exit_code: null, attempt: 0, restarts: 0, final: false, pid: null,
  container: null, ports: [{ name: "http", port: 8000, host_port: 31800, address: "10.0.0.5" }], started_at: "2026-11-10T10:00:00Z",
  finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 15443688448,
  download: { phase: "model", done: 2 * GIB, total: 8 * GIB, file: "model-00001-of-00003.safetensors" },
  runs: { type: "container", image: "vllm/vllm-openai:v0.32.0", command: ["vllm", "serve"] }, ...over,
});

const workload = (r: Replica): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: r.state === "healthy" ? "running" : "starting",
  ready: r.state === "healthy" ? 1 : 0, replicas: 1,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null },
  replica_list: [r],
});

const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15443688448, ram: 0, amount: 15443688448, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8044936192, detail: "4.02 B parameters" }, { label: "KV cache", bytes: 4831838208, detail: "2 x 36 layers" },
    { label: "Engine overhead", bytes: 2 * GIB, detail: "" }, { label: "CUDA context", bytes: 419430400, detail: "" }] };

const dep = (r: Replica): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192 },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" },
  endpoints: [{ replica: 0, machine: "a100", state: r.state, ready: r.state === "healthy", url: "http://10.0.0.5:31800/" }],
  workload: workload(r),
  cards: [{ replica: 0, machine: "a100", key: "nvidia:GPU-x", index: 4, vendor: "nvidia", model: "A100", total: 80 * GIB, free: 30 * GIB, used: 50 * GIB, reserved: 15443688448, updated_at: "2026-11-10T10:00:00Z" }],
});

const catalog: CatalogResponse = {
  can_manage: true, can_deploy: true,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa9a72", file: null,
    format: "safetensors", task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm"], deployments: 0,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 4022468096, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" },
    gguf_files: [] }],
  recipes: [{ name: "vllm", title: "vLLM", description: "Chat models", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"],
    api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"],
    params: { context: { type: "int", default: 8192, min: 256, max: 1048576, label: "Context length (tokens)" },
      concurrency: { type: "int", default: 4, min: 1, label: "Concurrent requests" } },
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: ">=7.0", driver: ">=580", engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("models", () => {
  it("FY-WEB-048 deployments list a replica's download progress, machine, GPU and address", async () => {
    server((url) => url === "/api/deployments" ? { status: 200, body: { deployments: [dep(rep())], server_time: "", can_manage: true } }
      : url === "/api/models" ? { status: 200, body: catalog } : undefined);
    mount("/models");
    const card = await screen.findByRole("article", { name: "Deployment chat" });
    expect(within(card).getByText("Downloading the model: 2 GiB of 8 GiB")).toBeTruthy();
    expect(within(card).getByText("GPU #4")).toBeTruthy();
    expect(within(card).getByRole("link", { name: "10.0.0.5:31800" })).toBeTruthy();
    expect(within(card).getByText(/14.38 GiB per GPU/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Deploy a model" })).toBeTruthy();
  });

  it("FY-WEB-049 the deploy form shows the live estimate and refuses what does not fit, by how much", async () => {
    const tooBig: EstimateResponse = { estimate: { ...estimate, amount: 81.88 * GIB, per_gpu: 81.88 * GIB }, params: {}, recipe: "vllm", model: "qwen3-4b",
      placement: { fits: false, placed: [], summary: "Nothing can take it",
        machines: [{ machine_id: 2, name: "a100", status: "online", fits: false, short_by: 60.38 * GIB,
          reason: "needs 81.88 GiB per GPU; available: #4 21.50 GiB (60.38 GiB short)", cards: [] },
        { machine_id: 3, name: "rtx", status: "online", fits: false, short_by: null, reason: "cuda: driver 550.54.14 is not >=580", cards: [] }] } };
    const ok: EstimateResponse = { estimate, params: {}, recipe: "vllm", model: "qwen3-4b",
      placement: { fits: true, machines: [], placed: [{ replica: 0, machine: "a100", machine_id: 2, variant: "cuda", runtime: "container",
        image: "vllm/vllm-openai:v0.32.0", gpus: [4], cached: true, leftover: 30 * GIB, online: true, gpu_memory_utilization: "0.174" }] } };
    const fetch = server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } };
      if (url === "/api/deployments/estimate") {
        const body = JSON.parse(String(init?.body));
        return { status: 200, body: body.params.concurrency === 16 ? tooBig : ok };
      }
      if (url === "/api/deployments" && init?.method === "POST") return { status: 201, body: { deployment: { id: 3 } } };
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep(rep({ state: "healthy", reason: null, download: null })), events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    mount("/models/deploy?model=qwen3-4b");
    expect(await screen.findByText("14.38 GiB", {}, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByText("--gpu-memory-utilization 0.174")).toBeTruthy();
    expect((screen.getByLabelText("Deployment name") as HTMLInputElement).value).toBe("qwen3-4b");
    fireEvent.change(screen.getByLabelText("Concurrent requests"), { target: { value: "16" } });
    expect(await screen.findByText("60.38 GiB short", {}, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByText("It does not fit anywhere right now.")).toBeTruthy();
    expect(screen.getByText("cuda: driver 550.54.14 is not >=580")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Deploy" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Concurrent requests"), { target: { value: "4" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "Deploy" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Deploy" })));
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/deployments" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ name: "qwen3-4b", model: "qwen3-4b", recipe: "vllm", replicas: 1,
      params: { context: 8192, concurrency: 4, gpus: 1 } });
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/models/deployments/3"));
  });

  it("FY-WEB-050 the detail page: estimate vs live memory, the gateway, try it", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/deployments/3/try" && init?.method === "POST") return { status: 200, body: { status: 200, ok: true, api: "openai-chat",
        endpoint: "http://10.0.0.5:31800/v1/chat/completions", request: {}, summary: "A GPU is a processor.", answer: {}, ms: 812 } };
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep(rep({ state: "healthy", reason: null, download: null })), server_time: "",
        can_manage: true, events: [{ at: "2026-11-10T10:00:01Z", kind: "placed", replica: 0, message: "Replica 0 placed on a100 with GPU #4 (cuda; reserves 14.38 GiB, gpu_memory_utilization 0.174)." }] } };
      if (url.startsWith("/api/workloads/11/replicas/0/logs")) return { status: 200, body: { source: "live", text: "INFO: Uvicorn running", truncated: false, state: "healthy" } };
      return undefined;
    });
    mount("/models/deployments/3");
    expect(await screen.findByRole("heading", { name: "chat" })).toBeTruthy();
    const gw = screen.getByRole("region", { name: "Through the gateway" });
    expect(within(gw).getByText("qwen3-4b").tagName).toBe("CODE");
    expect(within(gw).getByText(`${window.location.origin}/v1/chat/completions`)).toBeTruthy();
    expect(within(gw).getByText("#0 on a100")).toBeTruthy();
    expect(within(gw).getByText("http://10.0.0.5:31800/")).toBeTruthy();
    expect(screen.getByText(/this replica reserves 14.38 GiB/)).toBeTruthy();
    expect(screen.getByText(/50 GiB used of 80 GiB/)).toBeTruthy();
    expect(screen.getByText(/gpu_memory_utilization 0.174/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Input"), { target: { value: "Hi" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send" })));
    expect((await screen.findByLabelText("Answer")).textContent).toBe("A GPU is a processor.");
    const call = fetch.mock.calls.find(([u]) => u === "/api/deployments/3/try");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ input: "Hi" });
  });

  it("FY-WEB-051 users see deployments and try them, but cannot deploy, stop or delete", async () => {
    server((url) => url === "/api/deployments/3" ? { status: 200, body: { deployment: dep(rep({ state: "healthy", reason: null, download: null })), events: [], server_time: "", can_manage: false } }
      : url === "/api/models" ? { status: 200, body: { ...catalog, can_manage: false, can_deploy: false, sources: undefined } } : undefined,
    { ...OWNER, role: "user", is_owner: false });
    mount("/models/deployments/3");
    expect(await screen.findByRole("heading", { name: "chat" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    cleanup();
    mount("/models/deploy");
    expect(await screen.findByText(/Only admins and operators deploy models/)).toBeTruthy();
  });

  it("FY-WEB-052 an admin inspects a model, then adds it with the task it suggests", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/models" && init?.method === "POST") return { status: 201, body: { model: catalog.models[0] } };
      if (url === "/api/models") return { status: 200, body: { ...catalog, models: [] } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
      if (url === "/api/models/inspect") return { status: 200, body: { recipes: ["vllm"], meta: { format: "safetensors", revision: "1cfa9a7208912126459214e8b04321603b3df60c",
        task_hint: "chat", size_bytes: 8 * GIB, file_count: 12, config: { layers: 36, kv_heads: 8, max_context: 40960, dtype: "bfloat16" }, weights: { params: { BF16: 4022468096 } } } } };
      return undefined;
    });
    mount("/models?tab=catalog");
    // An empty catalog opens Add a model by itself; a source can still be typed in.
    fireEvent.click(await screen.findByRole("button", { name: "Enter a source" }));
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "Qwen/Qwen3-4B" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Look it up" })));
    expect(await screen.findByText("4.02 B")).toBeTruthy();
    expect(screen.getByText("36 layers, 8 KV heads, up to 40,960 tokens, bfloat16")).toBeTruthy();
    expect((screen.getByLabelText("Name in the catalog") as HTMLInputElement).value).toBe("qwen3-4b");
    expect((screen.getByLabelText("Task") as HTMLSelectElement).value).toBe("chat");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Add to the catalog" })));
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/models" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ source: "hf://Qwen/Qwen3-4B", name: "qwen3-4b", task: "chat" });
  });

  it("FY-WEB-170 a recipe lists the commands that carry its images to a machine without registry access", async () => {
    const digest = "sha256:" + "a".repeat(64);
    const pinned = `vllm/vllm-openai:v0.32.0@${digest}`;
    const own = "registry.example/team/vllm-rocm:7@sha256:" + "b".repeat(64);
    const recipe = { ...catalog.recipes[0], variants: [
      { ...catalog.recipes[0].variants[0], image: pinned, image_check: true },
      { ...catalog.recipes[0].variants[0], name: "pooling", image: pinned, image_check: true },
      { ...catalog.recipes[0].variants[0], name: "rocm", vendor: "amd" as const, image: own, image_check: false },
      { ...catalog.recipes[0].variants[0], name: "cpu-process", vendor: "cpu" as const, runtime: "process" as const, image: null,
        engine: "https://example.org/x.tar.gz" },
    ] };
    server((url) => url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: true } }
      : url === "/api/models" ? { status: 200, body: { ...catalog, recipes: [recipe] } } : undefined);
    mount("/models?tab=catalog");
    const card = await screen.findByRole("article", { name: "Recipe vllm" });
    fireEvent.click(within(card).getByText("Machine without registry access: load the image"));
    const save = within(card).getByLabelText("Save command for cuda, pooling");
    expect(save.textContent).toBe(`sudo docker pull vllm/vllm-openai@${digest} && \\\n` +
      `  sudo docker tag vllm/vllm-openai@${digest} vllm/vllm-openai:v0.32.0 && \\\n` +
      "  sudo docker save vllm/vllm-openai:v0.32.0 | gzip > vllm-openai-v0.32.0.tar.gz");
    expect(within(card).getByLabelText("Load command for cuda, pooling").textContent).toBe(
      "gunzip -c vllm-openai-v0.32.0.tar.gz | sudo docker load");
    expect(within(card).getByText(/so this recipe works offline as it is/)).toBeTruthy();
    // One image at a time; the process variant has none to carry.
    const pick = within(card).getByLabelText("Variant") as HTMLSelectElement;
    expect([...pick.options].map((o) => o.text)).toEqual(["cuda, pooling · vllm-openai:v0.32.0", "rocm · vllm-rocm:7"]);
    fireEvent.change(pick, { target: { value: "1" } });
    expect(within(card).queryByLabelText("Save command for cuda, pooling")).toBeNull();
    expect(within(card).getByLabelText("Save command for rocm").textContent).toContain(
      "sudo docker save registry.example/team/vllm-rocm:7 | gzip > vllm-rocm-7.tar.gz");
    expect(within(card).getByText(/No ids are recorded for this image/)).toBeTruthy();
    expect(within(card).queryByText(/so this recipe works offline as it is/)).toBeNull();
    expect(within(card).getByRole("link", { name: "Offline machines" }).getAttribute("href")).toBe("/guide/en/how-to#offline-machines");
  });
});
