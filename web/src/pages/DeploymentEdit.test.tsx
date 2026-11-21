// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { deployBody, editBody, editForm, emptyDeploy, takesFirstAnswer } from "../lib/models";
import type { CatalogResponse, Deployment, EditResponse, EstimateResponse, Placement, Replica, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

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

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const GIB = 1024 ** 3;
const rep = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 2, name: "gpu-01", status: "online", connected: true },
  gpus: [{ index: 0, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: "nvidia:GPU-x" }], state: "healthy", reason: null,
  exit_code: null, attempt: 0, restarts: 0, final: false, pid: null, container: "fy-chat-0", ports: [], started_at: "2026-11-10T10:00:00Z",
  finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 15443688448, download: null,
  runs: { type: "container", image: "vllm/vllm-openai:v0.32.0", command: ["vllm", "serve"] }, ...over,
});

const workload = (reps: Replica[], over: Partial<Workload> = {}): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: reps.length, replicas: reps.length,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: reps.length, machine_id: null },
  replica_list: reps, ...over,
});

const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15443688448, ram: 0, amount: 15443688448, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8044936192, detail: "" }, { label: "KV cache", bytes: 4831838208, detail: "" }] };

const dep = (reps: Replica[], over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive" },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" }, endpoints: [], workload: workload(reps), cards: [],
  pinned: null, rollout: null, ...over,
});

const catalog: CatalogResponse = {
  can_manage: true, can_deploy: true,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa9a72", file: null,
    format: "safetensors", task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm"], deployments: 1,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 4022468096, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" },
    gguf_files: [] }],
  recipes: [{ name: "vllm", title: "vLLM", description: "Chat models", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"],
    api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"],
    params: { context: { type: "int", default: 8192, min: 256, max: 1048576, label: "Context length (tokens)" },
      concurrency: { type: "int", default: 4, min: 1, label: "Concurrent requests" } },
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: ">=7.0", driver: ">=580", engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
};

const machines = { machines: [{ id: 2, name: "gpu-01", status: "online" }, { id: 5, name: "gpu-02", status: "online" }], server_time: "",
  heartbeat_interval: 10, offline_after: 30, can_manage: true };

const HINT = { need: 15443688448, card_total: 80 * GIB, card_model: "NVIDIA A100 80GB PCIe", machine: "gpu-01", gpu: 0,
  text: "This model needs 14.38 GiB of an 80.00 GiB card; with Share VRAM the rest can hold other models. They will share compute." };

const fits = (over: Partial<Placement> = {}): Placement => ({ fits: true, machines: [], share_hint: null, placed: [{ replica: 0, machine: "gpu-01", machine_id: 2,
  variant: "cuda", runtime: "container", image: null, gpus: [0], cached: true, leftover: 30 * GIB, online: true, previous: true }], ...over });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("changing a deployment (1.3.1)", () => {
  it("FY-WEB-198: the form starts from the running settings, previews each change, offers sharing, and saves a PATCH", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/deployments/3" && init?.method === "PATCH") {
        const b = JSON.parse(String(init.body));
        const changes: EditResponse["changes"] = {};
        if (b.params.context !== 8192) changes.context = { before: 8192, after: b.params.context };
        if (b.params.share !== "exclusive") changes.share = { before: "exclusive", after: b.params.share };
        const out: EditResponse = { estimate, params: b.params, changes, placement: fits({ share_hint: b.params.share === "exclusive" ? HINT : null }),
          restart: Object.keys(changes).length ? "all" : "none",
          restart_text: Object.keys(changes).length ? "Its replica restarts: the model does not answer until the new one is ready." : "Nothing restarts." };
        return { status: 200, body: b.dry_run ? out : { ...out, deployment: dep([rep()]) } };
      }
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep([rep()]), events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: machines };
      return undefined;
    });
    mount("/serving/3/edit");
    expect(await screen.findByRole("heading", { name: "Change chat" })).toBeTruthy();
    expect((screen.getByLabelText("Context length (tokens)") as HTMLInputElement).value).toBe("8192");
    expect((screen.getByLabelText("GPU sharing") as HTMLSelectElement).value).toBe("exclusive");
    expect((screen.getByLabelText("Machine") as HTMLSelectElement).value).toBe("");
    expect(await screen.findByText("Nothing has changed yet.", {}, { timeout: 3000 })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    const hint = await screen.findByRole("note", { name: "GPU sharing hint" });
    expect(within(hint).getByText(/This model needs 14.38 GiB of an 80.00 GiB card/)).toBeTruthy();
    expect(screen.getByText(/where it runs now/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Context length (tokens)"), { target: { value: "16384" } });
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    expect(within(changes).getByText("Context length (tokens)")).toBeTruthy();
    expect(screen.getByText("Its replica restarts: the model does not answer until the new one is ready.")).toBeTruthy();
    fireEvent.click(within(hint).getByRole("button", { name: "Use Share VRAM" }));
    expect((screen.getByLabelText("GPU sharing") as HTMLSelectElement).value).toBe("vram");
    expect(screen.queryByRole("note", { name: "GPU sharing hint" })).toBeNull();  // at once, before the new preview
    await waitFor(() => expect((screen.getByRole("button", { name: "Save and restart" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save and restart" })));
    const saved = fetch.mock.calls.filter(([u, i]) => u === "/api/deployments/3" && i?.method === "PATCH").map(([, i]) => JSON.parse(String(i?.body)))
      .find((b) => !b.dry_run);
    expect(saved).toEqual({ params: { context: 16384, concurrency: 4, gpus: 1, share: "vram", vram: null, extra_args: null, first_answer_timeout: null }, replicas: 1, machine_id: null });
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/serving/3"));
  });

  it("FY-WEB-199: a change that does not fit cannot be saved, and the machines say that it fits if both share VRAM", async () => {
    const nofit: Placement = { fits: false, placed: [], summary: "Nothing can take it", share_hint: null,
      machines: [{ machine_id: 2, name: "gpu-01", status: "online", fits: false, short_by: null, cards: [],
        reason: "needs 1 GPU it can have; in use: #0 (embed) (this deployment does not share cards); fits if both share VRAM: #0 has 60.00 GiB available, held by embed",
        share_fits: { gpus: [0], holders: ["embed"], exclusive: ["embed"], both: true } }] };
    server((url, init) => {
      if (url === "/api/deployments/3" && init?.method === "PATCH") {
        return { status: 200, body: { estimate, params: {}, changes: { machine_id: { before: null, after: 2 } }, placement: nofit, restart: "all",
          restart_text: "Its replica restarts: the model does not answer until the new one is ready." } };
      }
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep([rep({ machine: { id: 5, name: "gpu-02", status: "online", connected: true } })]), events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: machines };
      return undefined;
    });
    mount("/serving/3/edit");
    // The machines come from /api/machines, which may answer after the deployment: pick one once it is listed.
    await screen.findByRole("option", { name: /Pinned to gpu-01/ });
    fireEvent.change(screen.getByLabelText("Machine"), { target: { value: "2" } });
    expect(screen.getByText(/Its replicas run on this machine only/)).toBeTruthy();
    expect(await screen.findByText(/It does not fit with these settings right now/, {}, { timeout: 3000 })).toBeTruthy();
    expect(screen.getByText(/fits if both share VRAM: #0 has 60.00 GiB available, held by embed/)).toBeTruthy();
    expect(screen.getByText(/To use it/).textContent).toBe("To use it: turn on Share VRAM here, and on embed (Edit on its page).");
    expect((screen.getByRole("button", { name: "Save and restart" }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(screen.getByRole("list", { name: "Changes" })).getByRole("listitem").textContent).toBe("Machine: automatic → gpu-01");
  });

  it("FY-WEB-200: the deploy page points out sharing for a small exclusive model and switches it on", async () => {
    const est: EstimateResponse = { estimate, params: {}, recipe: "vllm", model: "qwen3-4b", placement: fits({ share_hint: HINT }) };
    server((url) => url === "/api/models" ? { status: 200, body: catalog } : url === "/api/machines" ? { status: 200, body: machines }
      : url === "/api/deployments/estimate" ? { status: 200, body: est } : undefined);
    mount("/serving/deploy?model=qwen3-4b");
    const hint = await screen.findByRole("note", { name: "GPU sharing hint" }, { timeout: 3000 });
    expect(hint.textContent).toContain("They will share compute.");
    fireEvent.click(within(hint).getByRole("button", { name: "Use Share VRAM" }));
    expect((screen.getByLabelText("GPU sharing") as HTMLSelectElement).value).toBe("vram");
    expect(screen.queryByRole("note", { name: "GPU sharing hint" })).toBeNull();
  });

  it("FY-WEB-230: Deploy here on a machine's page opens the deploy form with that machine chosen (1.4)", async () => {
    const est: EstimateResponse = { estimate, params: {}, recipe: "vllm", model: "qwen3-4b", placement: fits({}) };
    server((url) => url === "/api/models" ? { status: 200, body: catalog } : url === "/api/machines" ? { status: 200, body: machines }
      : url === "/api/deployments/estimate" ? { status: 200, body: est } : undefined);
    mount("/serving/deploy?model=qwen3-4b&machine=2");
    await waitFor(() => expect((screen.getByLabelText("Machine") as HTMLSelectElement).value).toBe("2"), { timeout: 3000 });
  });

  it("FY-WEB-201: a replica its agent never took says so on the deployment, with what to do; its old workload address opens it", async () => {
    const text = "gpu-01's agent (1.2.0) has not taken this replica for 3 minutes; update the agent (the server is 1.3.1)";
    const stuck = rep({ state: "pending", started_at: null, container: null, not_picked_up: text });
    const d = dep([stuck], { pinned: { id: 2, name: "gpu-01" }, rollout: { generation: 2, updated: 1, total: 2, next: 1 } });
    server((url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [d], server_time: "", can_manage: true } };
      if (url === "/api/workloads/11") return { status: 200, body: { workload: d.workload, events: [], server_time: "", can_manage: true } };
      if (url.startsWith("/api/workloads/11/replicas")) return { status: 200, body: { source: "stored", text: "", truncated: false, state: "pending" } };
      return undefined;
    });
    mount("/serving/3");
    const banner = await screen.findByRole("alert");
    expect(within(banner).getByText("Replica #0 was never picked up.")).toBeTruthy();
    expect(within(banner).getByText(`#0: ${text}.`)).toBeTruthy();
    expect(within(banner).getByRole("link", { name: "Why a replica stays pending" })).toBeTruthy();
    expect(screen.getByText(text, { selector: ".cell-stuck" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Edit" }).getAttribute("href")).toBe("/serving/3/edit");
    expect(screen.getByText("updating: 1 of 2 on the new settings")).toBeTruthy();
    expect(screen.getByText(/Pinned to/).textContent).toBe("Pinned to gpu-01");
    cleanup();
    mount("/workloads/11");  // 1.4 (#88): no workload pages; a model's opens its deployment
    expect(within(await screen.findByRole("alert")).getByText("Replica #0 was never picked up.")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: d.name })).toBeTruthy();
    expect(screen.getByText(text, { selector: ".cell-stuck" })).toBeTruthy();
  });

  it("FY-WEB-202: the edit form's start and its PATCH body", () => {
    const d = dep([rep(), rep({ ordinal: 1 })], { params: { context: 4096, concurrency: 2, gpus: 1, share: "vram", vram: 20 * GIB, extra_args: ["--seed", "1"] } });
    d.workload.spec.machine_id = 5;
    const f = editForm(d, catalog.recipes[0]);
    expect(f).toEqual({ device: "gpu", gpus: "1", share: "vram", replicas: "2", machine: "5", vram: "20Gi", extraArgs: "--seed\n1",
      firstAnswer: "", params: { context: "4096", concurrency: "2" } });
    expect(editBody({ ...f, vram: " ", extraArgs: "", machine: "" }, catalog.recipes[0])).toEqual({
      params: { context: 4096, concurrency: 2, gpus: 1, share: "vram", vram: null, extra_args: null, first_answer_timeout: null },
      replicas: 2, machine_id: null });
    expect(editBody({ ...f, device: "cpu" }, catalog.recipes[0]).params).toMatchObject({ gpus: 0, vram: "20Gi", extra_args: ["--seed", "1"] });
  });

  it("FY-WEB-507: the wait for the first answer (1.4.5): kept, sent, cleared, and left out for image recipes", () => {
    const d = dep([rep()], { params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive", first_answer_timeout: 900 } });
    const f = editForm(d, catalog.recipes[0]);
    expect(f.firstAnswer).toBe("900");
    const params = (b: Record<string, unknown>) => b.params as Record<string, unknown>;
    expect(params(editBody(f, catalog.recipes[0])).first_answer_timeout).toBe(900);
    expect(params(editBody({ ...f, firstAnswer: " " }, catalog.recipes[0])).first_answer_timeout).toBeNull();
    const image = { ...catalog.recipes[0], tasks: ["image" as const] };
    expect(takesFirstAnswer(image)).toBe(false);
    expect(takesFirstAnswer(catalog.recipes[0])).toBe(true);
    expect("first_answer_timeout" in params(editBody(f, image))).toBe(false);
    const body = deployBody({ ...emptyDeploy(), model: "m", recipe: "vllm", name: "x", firstAnswer: "300" }, catalog.recipes[0]);
    expect((body.params as Record<string, unknown>).first_answer_timeout).toBe(300);
    const plain = deployBody({ ...emptyDeploy(), model: "m", recipe: "vllm", name: "x" }, catalog.recipes[0]);
    expect("first_answer_timeout" in (plain.params as Record<string, unknown>)).toBe(false);
  });

  it("FY-WEB-508: the edit page shows the first-answer field under Advanced, with the default as its placeholder", async () => {
    server((url) => url === "/api/deployments/3" ? { status: 200, body: { deployment: dep([rep()]), events: [], server_time: "", can_manage: true } }
      : url === "/api/models" ? { status: 200, body: catalog } : url === "/api/machines" ? { status: 200, body: machines } : undefined);
    mount("/serving/3/edit");
    const input = await screen.findByLabelText("Wait for the first answer (seconds)") as HTMLInputElement;
    expect(input.placeholder).toBe("120");
    expect(screen.getByText(/Changing it restarts nothing/)).toBeTruthy();
  });
});
