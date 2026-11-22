// @vitest-environment jsdom
// 1.5.0 (#37): automatic replicas on the deploy and edit forms and the deployment page (FY-WEB-650–656).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { canAutoscale, emptyScale, scaleDeployParams, scaleEditParams, scaleForm } from "../lib/scale";
import type { CatalogResponse, Deployment, DeploymentAutoscale, EditResponse, EstimateResponse, Recipe, Replica, Workload } from "../lib/types";

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
  ordinal: 0, id: "r1", generation: 1, machine: { id: 2, name: "cpu-01", status: "online", connected: true },
  gpus: [], state: "healthy", reason: null, exit_code: null, attempt: 0, restarts: 0, final: false, pid: 41, container: null, ports: [],
  started_at: "2026-11-10T10:00:00Z", finished_at: null, updated_at: "2026-11-10T10:00:00Z", vram: 3 * GIB, download: null,
  runs: { type: "process", image: null, command: ["llama-server"] }, ...over,
});

const workload = (reps: Replica[]): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: reps.length, replicas: reps.length,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "llama-cpp", model: "qwen3-1.7b-gguf" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: reps.length, machine_id: null },
  replica_list: reps,
});

const estimate = { method: "gguf-kv", device: "cpu" as const, gpus: 0, per_gpu: 0, ram: 3 * GIB, amount: 3 * GIB, kv_tokens: 4096, notes: [],
  parts: [{ label: "Weights", bytes: GIB, detail: "" }] };

const OFF: DeploymentAutoscale = { on: false, min: 1, max: 2, target: 80, scale_up_seconds: 60, scale_down_minutes: 10, capacity: 1,
  replicas: 1, ready: 1, load: null, per_replica: null, wanted: null, draining: null, room: null, paused: null, last: null };

const dep = (reps: Replica[], over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "llama-cpp", served_name: "chat", params: { context: 4096, concurrency: 1, gpus: 0, share: "exclusive" },
  model: { id: 1, name: "qwen3-1.7b-gguf", source: "hf://ggml-org/Qwen3-1.7B-GGUF", task: "chat", format: "gguf", revision: "a1", file: "q.gguf", size_bytes: GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "chat", api: "openai-chat", upstream_model: "chat" }, endpoints: [], workload: workload(reps), cards: [],
  pinned: null, rollout: null, autoscale: OFF, ...over,
});

const llama: Recipe = { name: "llama-cpp", title: "llama.cpp", description: "GGUF", engine: "llama.cpp", version: "1.0.0", tasks: ["chat"], formats: ["gguf"],
  api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "gguf-kv", devices: ["cpu"],
  params: { context: { type: "int", default: 4096, min: 256, label: "Context length (tokens)" },
    concurrency: { type: "int", default: 2, min: 1, label: "Concurrent requests" } },
  variants: [{ name: "cpu", vendor: "cpu", runtime: "process", image: null, arch: null, compute_capability: null, driver: null, engine: null }] };

const catalog = (recipe: Recipe = llama, task: CatalogResponse["models"][number]["task"] = "chat"): CatalogResponse => ({
  can_manage: true, can_deploy: true,
  models: [{ id: 1, name: "qwen3-1.7b-gguf", title: "Qwen3 1.7B", source: "hf://ggml-org/Qwen3-1.7B-GGUF", kind: "hf", ref: "main", revision: "a1", file: "q.gguf",
    format: "gguf", task, task_label: "Chat", size_bytes: GIB, engine_args: {}, recipes: [recipe.name], deployments: 1,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 1, layers: 28, kv_heads: 8, max_context: 40960, architecture: "Qwen3" },
    gguf_files: [] }],
  recipes: [recipe],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
});

const machines = { machines: [{ id: 2, name: "cpu-01", status: "online" }], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true };
const placement = { fits: true, machines: [], share_hint: null, placed: [{ replica: 0, machine: "cpu-01", machine_id: 2, variant: "cpu", runtime: "process",
  image: null, gpus: [], cached: true, leftover: 30 * GIB, online: true, previous: true }] };

function editServer(d: Deployment, seen: Record<string, unknown>[]) {
  return server((url, init) => {
    if (url === "/api/deployments/3" && init?.method === "PATCH") {
      const b = JSON.parse(String(init.body));
      seen.push(b);
      const changes: EditResponse["changes"] = {};
      for (const k of ["autoscale", "min_replicas", "max_replicas", "autoscale_target"]) {
        if (b.params[k] !== undefined && b.params[k] !== (d.params as Record<string, unknown>)[k]) changes[k] = { before: (d.params as Record<string, unknown>)[k] ?? null, after: b.params[k] };
      }
      const out: EditResponse = { estimate, params: b.params, changes, placement, restart: "none", restart_text: "Nothing restarts." };
      return { status: 200, body: b.dry_run ? out : { ...out, deployment: d } };
    }
    if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
    if (url === "/api/models") return { status: 200, body: catalog() };
    if (url === "/api/machines") return { status: 200, body: machines };
    return undefined;
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("automatic replicas (1.5.0)", () => {
  it("FY-WEB-650: what the forms send: nothing for a fixed number, the bounds and settings for automatic, off when changed back", () => {
    expect(scaleDeployParams(emptyScale())).toEqual({ params: {}, replicas: null });
    expect(scaleDeployParams({ ...emptyScale(), mode: "auto" })).toEqual({ params: { autoscale: true, min_replicas: 1, max_replicas: 2 }, replicas: 1 });
    expect(scaleDeployParams({ ...emptyScale(), mode: "auto", min: "0", max: "4", target: "60", upSeconds: "30", downMinutes: "" }))
      .toEqual({ params: { autoscale: true, min_replicas: 0, max_replicas: 4, autoscale_target: 60, scale_up_seconds: 30 }, replicas: 1 });
    expect(scaleDeployParams({ ...emptyScale(), mode: "auto", min: "3" }).replicas).toBe(3);
    expect(scaleEditParams(emptyScale())).toEqual({});
    expect(scaleEditParams({ ...emptyScale(), was: "auto" })).toEqual({ autoscale: null });
    expect(scaleEditParams({ ...emptyScale(), mode: "auto", target: "" })).toEqual({ autoscale: true, min_replicas: 1, max_replicas: 2,
      autoscale_target: null, scale_up_seconds: null, scale_down_minutes: null });
    expect(scaleForm({ autoscale: true, min_replicas: 2, max_replicas: 5, scale_down_minutes: 20 })).toEqual({ mode: "auto", was: "auto",
      min: "2", max: "5", target: "", upSeconds: "", downMinutes: "20" });
    expect(scaleForm({ max_replicas: 3 })).toMatchObject({ mode: "fixed", was: "fixed", min: "1", max: "3" });
    expect(canAutoscale(llama)).toBe(true);
    expect(canAutoscale({ ...llama, tasks: ["video"] })).toBe(false);
    expect(canAutoscale(undefined)).toBe(false);
  });

  it("FY-WEB-651: the edit form turns automatic replicas on: the bounds in place of the number, target and windows under Advanced, no number sent", async () => {
    const seen: Record<string, unknown>[] = [];
    editServer(dep([rep()]), seen);
    mount("/serving/3/edit");
    const mode = await screen.findByLabelText("Replicas") as HTMLSelectElement;
    expect(mode.value).toBe("fixed");
    expect((screen.getByLabelText("Number of replicas") as HTMLInputElement).value).toBe("1");
    expect(screen.queryByLabelText("Target load per replica (%)")).toBeNull();
    fireEvent.change(mode, { target: { value: "auto" } });
    expect(screen.queryByLabelText("Number of replicas")).toBeNull();
    expect((screen.getByLabelText("Fewest replicas") as HTMLInputElement).value).toBe("1");
    expect((screen.getByLabelText("Most replicas") as HTMLInputElement).value).toBe("2");
    expect(screen.getByText(/1 now\./)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Most replicas"), { target: { value: "4" } });
    expect(screen.getByText(/Advanced: scaling, /)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Target load per replica (%)"), { target: { value: "60" } });
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    await waitFor(() => expect(within(changes).getByText("Target load per replica (%)")).toBeTruthy(), { timeout: 3000 });
    expect(within(changes).getByText("Automatic replicas")).toBeTruthy();
    expect(within(changes).getByText("off")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    const saved = seen.find((b) => !b.dry_run);
    expect(saved).toEqual({ params: { context: 4096, concurrency: 1, gpus: 0, share: "exclusive", vram: null, extra_args: null, first_answer_timeout: null,
      autoscale: true, min_replicas: 1, max_replicas: 4, autoscale_target: 60, scale_up_seconds: null, scale_down_minutes: null }, machine_id: null });
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/serving/3"));
  });

  it("FY-WEB-652: an automatic deployment's form starts automatic; back to a fixed number turns it off and sends the number", async () => {
    const seen: Record<string, unknown>[] = [];
    const d = dep([rep(), rep({ ordinal: 1, id: "r2" })], { params: { context: 4096, concurrency: 1, gpus: 0, share: "exclusive", autoscale: true, max_replicas: 3, sleep_when_idle: true },
      autoscale: { ...OFF, on: true, max: 3, replicas: 2, ready: 2 } });
    editServer(d, seen);
    mount("/serving/3/edit");
    const mode = await screen.findByLabelText("Replicas") as HTMLSelectElement;
    expect(mode.value).toBe("auto");
    expect((screen.getByLabelText("Most replicas") as HTMLInputElement).value).toBe("3");
    expect(screen.getByText(/0 as the fewest: down to none only by sleeping when idle/)).toBeTruthy();
    fireEvent.change(mode, { target: { value: "fixed" } });
    expect((screen.getByLabelText("Number of replicas") as HTMLInputElement).value).toBe("2");
    await waitFor(() => expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    const saved = seen.find((b) => !b.dry_run) as { params: Record<string, unknown>; replicas: number };
    expect(saved.params.autoscale).toBeNull();
    expect(saved.replicas).toBe(2);
  });

  it("FY-WEB-653: the deployment page says how many and why, marks the replica being drained, and says nothing with a fixed number", async () => {
    const auto: DeploymentAutoscale = { ...OFF, on: true, max: 4, replicas: 2, ready: 2, load: 1.9, per_replica: 0.95, wanted: 3,
      draining: { replica: 1, since: "2026-11-10T10:00:00Z", in_flight: 1 },
      room: "Wants 3 replicas, room for 2: Nothing can take a replica that needs 3.00 GiB of RAM.", paused: null,
      last: { at: "2026-11-10T10:00:00Z", text: "Draining replica 1: load 0.3 per replica over 10 min (target 0.8)." } };
    let d = dep([rep(), rep({ ordinal: 1, id: "r2" })], { autoscale: auto });
    server((url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    const view = mount("/serving/3");
    const line = await screen.findByLabelText("Automatic replicas");
    expect(within(line).getByText("Automatic, 1 to 4 · 2 now")).toBeTruthy();
    expect(within(line).getByText(/the load asks for 3/)).toBeTruthy();
    expect(within(line).getByText("Load 1.9 over the last 60 s, 0.95 per ready replica (target 0.8 of 1 concurrent request each).")).toBeTruthy();
    expect(within(line).getByText("Draining replica #1: it takes no new requests and is removed once the 1 in flight finishes.")).toBeTruthy();
    expect(within(line).getByText(/Wants 3 replicas, room for 2/)).toBeTruthy();
    expect(screen.getAllByText("draining")).toHaveLength(1);
    view.unmount();
    d = dep([rep()]);
    mount("/serving/3");
    expect(await screen.findByRole("heading", { name: "Replicas" })).toBeTruthy();
    expect(screen.queryByLabelText("Automatic replicas")).toBeNull();
    expect(screen.queryByText("draining")).toBeNull();
  });

  it("FY-WEB-654: a video deployment has only a number of replicas", async () => {
    const video: Recipe = { ...llama, name: "vllm-omni-video", tasks: ["video"], params: {} };
    server((url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep([rep()], { recipe: "vllm-omni-video" }), events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog(video, "video") };
      if (url === "/api/machines") return { status: 200, body: machines };
      return { status: 200, body: { estimate, params: {}, changes: {}, placement, restart: "none", restart_text: "Nothing restarts." } };
    });
    mount("/serving/3/edit");
    expect(await screen.findByLabelText("Replicas")).toBeTruthy();
    expect((screen.getByLabelText("Replicas") as HTMLElement).tagName).toBe("INPUT");
    expect(screen.queryByRole("option", { name: "Automatic, by load" })).toBeNull();
  });

  it("FY-WEB-655: the deploy form sends automatic replicas with the minimum as the replicas it starts with", async () => {
    const ok: EstimateResponse = { estimate, params: {}, recipe: "llama-cpp", model: "qwen3-1.7b-gguf", placement };
    const fetch = server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog() };
      if (url === "/api/machines") return { status: 200, body: machines };
      if (url === "/api/deployments/estimate") return { status: 200, body: ok };
      if (url === "/api/deployments" && init?.method === "POST") return { status: 201, body: { deployment: { id: 3 } } };
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep([rep()]), events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    mount("/serving/deploy?model=qwen3-1.7b-gguf");
    const mode = await screen.findByLabelText("Replicas", {}, { timeout: 3000 }) as HTMLSelectElement;
    fireEvent.change(mode, { target: { value: "auto" } });
    fireEvent.change(screen.getByLabelText("Fewest replicas"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Most replicas"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("Quiet for (minutes) before one fewer"), { target: { value: "20" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "Deploy" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Deploy" })));
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/deployments" && i?.method === "POST");
    const b = JSON.parse(String(post?.[1]?.body));
    expect(b.replicas).toBe(2);
    expect(b.params).toMatchObject({ autoscale: true, min_replicas: 2, max_replicas: 5, scale_down_minutes: 20 });
    expect(b.params.autoscale_target).toBeUndefined();
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/serving/3"));
  });
});
