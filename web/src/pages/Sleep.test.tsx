// @vitest-environment jsdom
// Sleep when idle in the console (1.5.0, #37, PR 4): the setting on the deploy and edit forms, the Asleep and Waking
// badges on Serving, the deployment page's "When not in use", Wake now in the Playground, and the line on Machines
// (FY-WEB-600 to 610).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import fixture from "../test/machines.json";
import { deployBody, editBody, editForm, emptyDeploy } from "../lib/models";
import { asleepOn, elapsed, idleProblem, sameModelStart, seconds, sleepState, startHint, wakeSeconds, wakeWaitProblem } from "../lib/sleep";
import { pickerGroups } from "../lib/playground";
import { SleepSetting } from "../components/Sleep";
import type { CatalogResponse, Deployment, DeploymentSleep, EditResponse, Placement, Replica, RouteInfo, StartTime, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
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

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const GIB = 1024 ** 3;
const NOW = Date.parse("2026-11-22T12:00:00Z");
const ago = (s: number) => new Date(NOW - s * 1000).toISOString();

const rep = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 1, name: "a100-box", status: "online", connected: true },
  gpus: [{ index: 0, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: fixture.machines[0].accelerators[0].key }], state: "healthy", reason: null,
  exit_code: null, attempt: 0, restarts: 0, final: false, pid: null, container: "fy-chat-0", ports: [], started_at: ago(3600),
  finished_at: null, updated_at: ago(3600), vram: 15443688448, download: null, runs: null, ...over,
});
const asleepRep = () => rep({ state: "stopped", started_at: null, container: null, reason: null });

const workload = (reps: Replica[], over: Partial<Workload> = {}): Workload => ({
  id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
  created_at: ago(86400), updated_at: ago(3600), status: "running", ready: reps.length, replicas: reps.length,
  spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
    resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
    health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: reps.length, machine_id: null },
  replica_list: reps, ...over,
});

const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15443688448, ram: 0, amount: 15443688448, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8044936192, detail: "" }] };

const sleepOf = (over: Partial<DeploymentSleep> = {}): DeploymentSleep => ({
  on: true, idle_minutes: 30, asleep: false, last_request_at: ago(600), since: ago(600), sleeps_at: new Date(NOW + 20 * 60000).toISOString(),
  held_by: null, wake_wait: 300, waking: null, asleep_since: null, wakes: 0, ...over,
});
const START: StartTime = { seconds: 74.2, phases: { engine: 51.1, model: 20 }, source: "agent", at: ago(1800), replica: 0 };

const dep = (reps: Replica[], over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive", sleep_when_idle: true, idle_minutes: 30 },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: ago(86400),
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" }, endpoints: [], workload: workload(reps), cards: [],
  pinned: null, rollout: null, sleep: sleepOf(), last_start: START, ...over,
});
const asleep = (over: Partial<Deployment> = {}) => dep([asleepRep()], {
  workload: workload([asleepRep()], { desired_state: "asleep", status: "asleep", ready: 0 }),
  sleep: sleepOf({ asleep: true, asleep_since: ago(300), sleeps_at: null, last_request_at: null }), ...over,
});
const waking = (over: Partial<Deployment> = {}) => dep([rep({ state: "starting" })], {
  workload: workload([rep({ state: "starting" })], { status: "starting", ready: 0 }),
  sleep: sleepOf({ waking: { since: ago(42), by: "a request (key laptop)", waiting: 2, expected_seconds: 74 } }), ...over,
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
const recipe = catalog.recipes[0];
const fits: Placement = { fits: true, machines: [], share_hint: null, placed: [{ replica: 0, machine: "a100-box", machine_id: 1, variant: "cuda",
  runtime: "container", image: null, gpus: [0], cached: true, leftover: 30 * GIB, online: true, previous: true }] };
const machinesBody = { ...fixture, can_manage: true };

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("sleep when idle: the words (lib/sleep)", () => {
  it("FY-WEB-600: asleep, waking or neither; elapsed and start times; the hint beside the setting; the idle minutes' range", () => {
    expect(sleepState(asleep())).toBe("asleep");
    expect(sleepState(waking())).toBe("waking");
    expect(sleepState(dep([rep()]))).toBeNull();
    // waking ends once a replica is ready, whatever the flag says (the next poll clears it)
    expect(sleepState(waking({ workload: workload([rep()]) }))).toBeNull();
    // a person's Stop is not asleep
    expect(sleepState(dep([asleepRep()], { workload: workload([asleepRep()], { desired_state: "stopped", status: "stopped", ready: 0 }), sleep: sleepOf({ sleeps_at: null }) }))).toBeNull();
    expect(elapsed(ago(42), NOW)).toBe("0:42");
    expect(elapsed(ago(725), NOW)).toBe("12:05");
    expect(elapsed(new Date(NOW + 5000).toISOString(), NOW)).toBe("0:00");
    expect(elapsed(null, NOW)).toBe("");
    expect([seconds(0.2), seconds(74.2), seconds(90), seconds(200), seconds(240)]).toEqual(["1 s", "74 s", "1 min 30 s", "3 min 20 s", "4 min"]);
    expect(startHint(START)).toBe("The last start took 74 s: the first request after a sleep waits about that long.");
    expect(startHint(START, "chat-a")).toBe("chat-a (the same model and engine) last started in 74 s: the first request after a sleep waits about that long.");
    expect(startHint(null)).toMatch(/^The first request after a sleep waits while the model starts: seconds for a small llama\.cpp model/);
    expect(["", "1", "1440", "30"].map(idleProblem)).toEqual([null, null, null, null]);
    expect(idleProblem("0")).toBe("1 to 1,440 minutes.");
    expect(idleProblem("1441")).toBe("1 to 1,440 minutes.");
    expect(idleProblem("1.5")).toBe("A whole number of minutes.");
    expect(idleProblem("soon")).toBe("A whole number of minutes.");
    expect(["", "10", "1800", "300"].map(wakeWaitProblem)).toEqual([null, null, null, null]);
    expect([wakeWaitProblem("9"), wakeWaitProblem("1801"), wakeWaitProblem("2.5")]).toEqual(["10 to 1,800 seconds.", "10 to 1,800 seconds.", "A whole number of seconds."]);
    // about how long a wake takes: what the server expects for the wake under way, else the last start
    expect(wakeSeconds(waking({ sleep: sleepOf({ waking: { since: ago(1), by: "owner", waiting: 0, expected_seconds: 12 } }) }))).toBe(12);
    expect(wakeSeconds(asleep())).toBe(74.2);
    expect(wakeSeconds(asleep({ last_start: null }))).toBeNull();
  });

  it("FY-WEB-601: the deploy form's hint uses the newest start of the same model and engine; asleep deployments by machine", () => {
    const a = dep([rep()], { id: 4, name: "a", last_start: { ...START, seconds: 30, at: ago(9000) } });
    const b = dep([rep()], { id: 5, name: "b", last_start: { ...START, seconds: 50, at: ago(60) } });
    const other = dep([rep()], { id: 6, name: "c", recipe: "llama-cpp", last_start: { ...START, at: ago(1) } });
    const none = dep([rep()], { id: 7, name: "d", last_start: null });
    expect(sameModelStart([a, b, other, none], "qwen3-4b", "vllm")).toEqual({ name: "b", last: b.last_start });
    expect(sameModelStart([other, none], "qwen3-4b", "vllm")).toBeNull();
    // asleep on machine 1 (where its replica ran), with its reservation per card and the card; nothing on machine 2
    const here = asleepOn([asleep(), dep([rep()], { id: 8, name: "awake" })], 1);
    expect(here.map((h) => [h.deployment.name, h.bytes, h.gpus])).toEqual([["chat", 15443688448, ["#0"]]]);
    expect(asleepOn([asleep()], 2)).toEqual([]);
    // pinned to a machine and never placed: there, with the estimate
    const pinned = asleep({ pinned: { id: 2, name: "strix" }, workload: workload([rep({ machine: null, gpus: [], vram: null, state: "stopped" })], { desired_state: "asleep", status: "asleep", ready: 0 }) });
    expect(asleepOn([pinned], 2).map((h) => [h.bytes, h.gpus])).toEqual([[15443688448, []]]);
    // the Playground offers an asleep or waking model, not one a person stopped or one still starting
    const route = (id: number, endpoints: unknown[] = []) => ({ id, served_name: `m${id}`, api: "openai-chat", deployment_id: id, kind: "local", usable: true, endpoints }) as unknown as RouteInfo;
    const stopped = dep([asleepRep()], { id: 12, workload: workload([asleepRep()], { desired_state: "stopped", status: "stopped", ready: 0 }) });
    const starting = dep([rep({ state: "starting" })], { id: 13, sleep: sleepOf(), workload: workload([rep({ state: "starting" })], { status: "starting", ready: 0 }) });
    const groups = pickerGroups([route(10), route(11), route(12), route(13), route(14, [{}])],
      [asleep({ id: 10 }), waking({ id: 11 }), stopped, starting, dep([rep()], { id: 14 })]);
    expect(groups.flatMap(([, l]) => l.map((r) => r.served_name))).toEqual(["m10", "m11", "m14"]);
  });
});

describe("sleep when idle: the deploy and edit forms", () => {
  it("FY-WEB-602: the deploy body: off sends nothing; on sends sleep_when_idle and its minutes (30 unless changed); ticking fills in 30", () => {
    const base = { ...emptyDeploy(), model: "qwen3-4b", recipe: "vllm", name: "chat" };
    expect(base.sleepOn).toBe(false);
    expect(base.idleMinutes).toBe("30");
    const off = deployBody(base, recipe).params as Record<string, unknown>;
    expect("sleep_when_idle" in off || "idle_minutes" in off).toBe(false);
    expect(deployBody({ ...base, sleepOn: true }, recipe).params).toMatchObject({ sleep_when_idle: true, idle_minutes: 30 });
    expect(deployBody({ ...base, sleepOn: true, idleMinutes: " 90 " }, recipe).params).toMatchObject({ sleep_when_idle: true, idle_minutes: 90 });
    const empty = deployBody({ ...base, sleepOn: true, idleMinutes: "" }, recipe).params as Record<string, unknown>;
    expect(empty.sleep_when_idle).toBe(true);
    expect("idle_minutes" in empty).toBe(false);   // the server's default, 30
    // the wake wait goes with the setting, only when set (else the server's 300)
    expect(deployBody({ ...base, sleepOn: true, wakeWait: " 120 " }, recipe).params).toMatchObject({ wake_wait: 120 });
    expect("wake_wait" in (deployBody({ ...base, sleepOn: true }, recipe).params as Record<string, unknown>)).toBe(false);
    expect("wake_wait" in (deployBody({ ...base, wakeWait: "120" }, recipe).params as Record<string, unknown>)).toBe(false);
    // ticking it with no idle time set (a deployment from before 1.5) fills in 30; unticking keeps what is there
    const changed = vi.fn();
    render(<MemoryRouter><SleepSetting prefix="x" on={false} minutes="" onChange={changed} /></MemoryRouter>);
    expect((screen.getByLabelText("Idle minutes") as HTMLInputElement).placeholder).toBe("30");
    fireEvent.click(screen.getByRole("checkbox", { name: /Sleep when idle/ }));
    expect(changed).toHaveBeenLastCalledWith(true, "30");
    cleanup();
    render(<MemoryRouter><SleepSetting prefix="x" on minutes="45" onChange={changed} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("checkbox", { name: /Sleep when idle/ }));
    expect(changed).toHaveBeenLastCalledWith(false, "45");
  });

  it("FY-WEB-603: the deploy form shows When not in use, off by default, with the same model's last start; ticking it puts it in the request", async () => {
    const other = dep([rep()], { id: 9, name: "chat-a" });
    const posted: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: machinesBody };
      if (url === "/api/deployments" && init?.method === "GET") return { status: 200, body: { deployments: [other], server_time: "", can_manage: true } };
      if (url === "/api/deployments/estimate") {
        posted.push(JSON.parse(String(init?.body)));
        return { status: 200, body: { estimate, placement: fits, params: {}, recipe: "vllm", model: "qwen3-4b" } };
      }
      return undefined;
    });
    mount("/serving/deploy?model=qwen3-4b");
    const panel = (await screen.findByRole("heading", { name: "When not in use" })).closest("section")!;
    const box = within(panel).getByRole("checkbox", { name: /Sleep when idle/ }) as HTMLInputElement;
    const minutes = within(panel).getByLabelText("Idle minutes") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(minutes.disabled).toBe(true);
    expect(minutes.value).toBe("30");
    expect(within(panel).getByText(/^Off: it keeps its memory while nobody uses it/)).toBeTruthy();
    expect(within(panel).queryByText(/its memory may be used by others/)).toBeNull();
    expect(within(panel).getByText("chat-a (the same model and engine) last started in 74 s: the first request after a sleep waits about that long.")).toBeTruthy();
    const json = () => JSON.parse(screen.getByLabelText("Deployment JSON").textContent ?? "{}");
    expect(json().params.sleep_when_idle).toBeUndefined();
    fireEvent.click(box);
    expect(minutes.disabled).toBe(false);
    expect(json().params).toMatchObject({ sleep_when_idle: true, idle_minutes: 30 });
    expect(within(panel).getByText(/^After 30 min without a request its replicas stop/)).toBeTruthy();
    expect(within(panel).getByText(/its memory may be used by others; if none is free when it wakes/)).toBeTruthy();
    fireEvent.change(minutes, { target: { value: "0" } });
    expect(within(panel).getByText("1 to 1,440 minutes.")).toBeTruthy();
    expect(minutes.getAttribute("aria-invalid")).toBe("true");
    fireEvent.change(minutes, { target: { value: "45" } });
    expect(within(panel).queryByText("1 to 1,440 minutes.")).toBeNull();
    expect(json().params.idle_minutes).toBe(45);
    // the estimate is asked with the same parameters
    await waitFor(() => expect(posted.at(-1)?.params).toMatchObject({ sleep_when_idle: true, idle_minutes: 45 }), { timeout: 3000 });
  });

  it("FY-WEB-604: the edit form starts from the deployment's setting, names the change, and sends off as null; ticking an old one sets 30", async () => {
    const d = dep([rep()], { params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive", sleep_when_idle: true, idle_minutes: 45 } });
    const f = editForm(d, recipe);
    expect([f.sleepOn, f.idleMinutes]).toEqual([true, "45"]);
    expect(editBody(f, recipe).params).toMatchObject({ sleep_when_idle: true, idle_minutes: 45 });
    // off: the parameter goes (null); the idle time stays for when it is turned on again
    expect(editBody({ ...f, sleepOn: false }, recipe).params).toMatchObject({ sleep_when_idle: null, idle_minutes: 45 });
    // a deployment from before 1.5 has neither: nothing changes unless someone ticks it
    const old = editForm(dep([rep()], { params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive" } }), recipe);
    expect([old.sleepOn, old.idleMinutes]).toEqual([false, ""]);
    expect(editBody(old, recipe).params).toMatchObject({ sleep_when_idle: null, idle_minutes: null });
    // turned off earlier with an idle time kept: off, and the time is still there for later
    const kept = editForm(dep([rep()], { params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive", idle_minutes: 45, wake_wait: 120 } }), recipe);
    expect([kept.sleepOn, kept.idleMinutes, kept.wakeWait]).toEqual([false, "45", "120"]);
    expect(editBody(kept, recipe).params).toMatchObject({ wake_wait: 120 });
    expect(editBody({ ...kept, wakeWait: "" }, recipe).params).toMatchObject({ wake_wait: null });

    const bodies: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/deployments/3" && init?.method === "PATCH") {
        const b = JSON.parse(String(init.body));
        bodies.push(b);
        const changes: EditResponse["changes"] = {};
        if (b.params.idle_minutes !== 45) changes.idle_minutes = { before: 45, after: b.params.idle_minutes };
        if (b.params.sleep_when_idle !== true) changes.sleep_when_idle = { before: true, after: null };
        const out: EditResponse = { estimate, params: b.params, changes, placement: fits, restart: "none", restart_text: "Nothing restarts." };
        return { status: 200, body: b.dry_run ? out : { ...out, deployment: d } };
      }
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: machinesBody };
      return undefined;
    });
    mount("/serving/3/edit");
    const panel = (await screen.findByRole("heading", { name: "When not in use" })).closest("section")!;
    expect((within(panel).getByRole("checkbox", { name: /Sleep when idle/ }) as HTMLInputElement).checked).toBe(true);
    expect(within(panel).getByText("The last start took 74 s: the first request after a sleep waits about that long.")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Idle minutes"), { target: { value: "60" } });
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    expect(changes.textContent).toBe("Idle minutes: 45 → 60");
    expect(screen.getByText("Nothing restarts.")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("checkbox", { name: /Sleep when idle/ }));
    await waitFor(() => expect(screen.getByRole("list", { name: "Changes" }).textContent).toContain("Sleep when idle: on → off"), { timeout: 3000 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    const saved = bodies.find((b) => !b.dry_run)!;
    expect(saved.params).toMatchObject({ sleep_when_idle: null, idle_minutes: 60 });
  });
});

describe("sleep when idle: Serving and the deployment page", () => {
  it("FY-WEB-605: Serving shows Asleep with the moon, where it wakes and that it holds nothing; Waking with the time since; running as before", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    const sleepy = asleep({ id: 3, name: "chat" });
    const wakingOne = waking({ id: 4, name: "embed-like" });
    const awake = dep([rep()], { id: 5, name: "awake", sleep: sleepOf({ on: false, sleeps_at: null }) });
    server((url) => (url === "/api/deployments" ? { status: 200, body: { deployments: [sleepy, wakingOne, awake], server_time: "", can_manage: true } }
      : url === "/api/models" ? { status: 200, body: catalog } : url === "/api/machines" ? { status: 200, body: machinesBody }
        : url === "/api/routes" ? { status: 200, body: { routes: [], cloud: "off", is_admin: true } } : undefined));
    mount("/serving");
    const row = await screen.findByRole("group", { name: "Deployment chat" });
    const chip = within(row).getByText("Asleep", { selector: ".chip" });
    expect(chip.querySelector("svg")).toBeTruthy();
    expect(chip.className).toContain("chip-asleep");
    expect(within(row).getByText(/Holds nothing, asleep since 5 min ago/).textContent).toBe("Holds nothing, asleep since 5 min ago · wakes on a100-box");
    expect(row.querySelector(".where-chip")).toBeNull();
    expect(row.querySelector(".dot-asleep")).toBeTruthy();
    const w = screen.getByRole("group", { name: "Deployment embed-like" });
    expect(within(w).getByText(/^Waking/, { selector: ".chip" }).textContent).toBe(" Waking 0:42");
    expect(within(screen.getByRole("group", { name: "Deployment awake" })).getByText("1/1 ready")).toBeTruthy();
  });

  it("FY-WEB-606: the deployment page's When not in use: awake and counting down, kept awake, off, and asleep with Wake now", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    let d: Deployment = dep([rep()], { sleep: sleepOf({ wakes: 2 }) });
    const posts: string[] = [];
    server((url, init) => {
      if (init?.method === "POST") { posts.push(url); return { status: 200, body: { deployment: d } }; }
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
      if (url === "/api/routes") return { status: 200, body: { routes: [], cloud: "off", is_admin: true } };
      return undefined;
    });
    vi.stubGlobal("confirm", vi.fn(() => true));
    const view = mount("/serving/3");
    const card = async () => (await screen.findByRole("heading", { name: "When not in use" })).closest("section")!;
    const facts = async () => Object.fromEntries([...(await card()).querySelectorAll("dt")].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]));
    expect(await facts()).toEqual({
      "Sleep when idle": "On: after 30 min without a request; a request waits up to 300 s for it to wake", Now: "Awake; sleeps 20 min from now unless a request comes",
      "Last request": "10 min ago", "Last start": "74 s (30 min ago; a wake waits about that long)", "Woken, last 24 h": "2" });
    expect(within(await card()).getByRole("link", { name: "Change" }).getAttribute("href")).toBe("/serving/3/edit");
    view.unmount();

    d = dep([rep()], { sleep: sleepOf({ held_by: "a request is in flight" }) });
    mount("/serving/3");
    expect((await facts()).Now).toBe("Awake, kept awake: a request is in flight");
    cleanup();

    d = dep([rep()], { sleep: sleepOf({ on: false, sleeps_at: null, last_request_at: null }), last_start: null });
    mount("/serving/3");
    const off = await facts();
    expect([off["Sleep when idle"], off.Now, off["Last request"], off["Last start"], off["Woken, last 24 h"]])
      .toEqual(["Off: it keeps its memory while nobody uses it", "Awake", "none since the server started", "not measured yet", undefined]);
    cleanup();

    d = dep([asleepRep()], { workload: workload([asleepRep()], { desired_state: "stopped", status: "stopped", ready: 0 }), sleep: sleepOf({ sleeps_at: null }) });
    mount("/serving/3");
    expect((await facts()).Now).toBe("Stopped by a person: it does not wake by itself");
    cleanup();

    d = asleep();
    mount("/serving/3");
    expect((await facts()).Now).toBe("Asleep since 5 min ago; it holds no memory");
    expect(screen.getByText(/^Asleep since 5 min ago: nobody used it for 30 min/).textContent).toContain("The next request wakes it, or Wake now; the last start took 74 s.");
    expect(screen.getByText("Asleep", { selector: ".chip" }).className).toContain("chip-big");
    fireEvent.click(screen.getByRole("button", { name: "Wake now" }));
    await waitFor(() => expect(posts).toEqual(["/api/deployments/3/start"]));
    cleanup();

    d = waking();
    d.sleep!.wake_wait = 120;
    mount("/serving/3");
    expect((await facts()).Now).toBe("Waking, for 0:42 (by a request (key laptop))");
    expect(screen.getByText(/^Waking: woken by a request \(key laptop\) 0:42 ago; it should be ready in about 74 s\./).textContent)
      .toMatch(/Requests to it wait until a replica is ready \(up to 120 s\): 2 waiting now\.$/);
  });
});

describe("sleep when idle: the Playground and Machines", () => {
  const routes = [{ id: 1, served_name: "qwen3-4b", api: "openai-chat", deployment_id: 3, kind: "local", usable: true, endpoints: [],
    local_only: false, spill_on_long_prompt: false, wait_ms: 0, has_local: true, has_cloud: false, targets: [], upstream_model: null }];

  it("FY-WEB-607: an asleep model is offered with the moon; a message is simply sent, the gateway holds it while the model wakes, and the page says Waking the model (about N s)… until the answer comes", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    let d: Deployment = asleep();
    let release: (() => void) | null = null;
    const answer = "data: " + JSON.stringify({ choices: [{ delta: { content: "Hello there" } }] }) + "\n\ndata: [DONE]\n\n";
    const asked: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
      if (url === "/api/auth/state") return json({ needs_setup: false, registration: "off", user: OWNER });
      if (url === "/api/deployments") return json({ deployments: [d], server_time: "", can_manage: true });
      if (url === "/api/routes") return json({ routes, cloud: "off", is_admin: true });
      if (url === "/api/playground/chat/completions") {
        asked.push(String(init?.body));
        // the server expects 80 s for this wake (its own measure), not the 74 s last start the page knew before
        d = waking({ sleep: sleepOf({ waking: { since: ago(1), by: "a request from the Playground (owner)", waiting: 1, expected_seconds: 80 } }) });
        await new Promise<void>((ok) => { release = ok; });   // the gateway holds it until a replica is ready
        d = dep([rep()]);
        return new Response(answer, { status: 200, headers: { "Content-Type": "text/event-stream" } });
      }
      return json({}, 404);
    }));
    mount("/playground");
    const bar = (await screen.findByText(/is asleep/, { selector: ".play-wake strong" })).closest(".play-wake")!;
    expect(bar.textContent).toContain("chat is asleep since 5 min ago: nobody used it for 30 min, so it holds no memory. Send a request and it wakes: the request waits until it is ready (about 74 s), up to 300 s, or is answered by a cloud tier meanwhile when you may use one.");
    expect(within(bar as HTMLElement).getByRole("button", { name: "Wake now" })).toBeTruthy();
    expect(screen.getByText("Asleep: a request wakes it.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Model"));
    expect(within(screen.getByRole("listbox")).getByRole("option", { name: /qwen3-4b/ }).textContent).toContain("asleep");
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    // the message goes out at once, as to any model; while the gateway holds it the page follows the wake
    fireEvent.change(screen.getByPlaceholderText("Write a message…"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(asked.length).toBe(1));
    const waitingText = await screen.findByText(/^Waking the model \(about 80 s\)…$/, undefined, { timeout: 4000 });
    expect(waitingText.closest(".play-wake")!.textContent).toMatch(/Waking the model \(about 80 s\)… \d+:\d\d\. A request sent to it waits until a replica is ready \(up to 300 s\); 1 waiting now\./);
    act(() => release!());
    expect(await screen.findByText("Hello there")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/Waking the model/)).toBeNull(), { timeout: 4000 });
  });

  it("FY-WEB-608: someone who cannot start models gets no Wake now (a request still wakes it); Wake now wakes it for operators; the page follows a wake it did not start", async () => {
    let d = asleep();
    server((url) => (url === "/api/deployments" ? { status: 200, body: { deployments: [d], server_time: "", can_manage: false } }
      : url === "/api/routes" ? { status: 200, body: { routes, cloud: "off", is_admin: false } } : undefined), USER);
    mount("/playground");
    const bar = (await screen.findByText(/is asleep/, { selector: ".play-wake strong" })).closest(".play-wake")!;
    expect(bar.textContent).toContain("Send a request and it wakes");
    expect(within(bar as HTMLElement).queryByRole("button")).toBeNull();
    d = waking();   // someone else's request woke it: the page asks again every 2 s
    expect(await screen.findByText(/^Waking the model \(about 74 s\)…$/, undefined, { timeout: 4000 })).toBeTruthy();
    cleanup();

    d = asleep();
    const posts: string[] = [];
    server((url, init) => {
      if (url === "/api/deployments/3/start" && init?.method === "POST") { posts.push(url); d = waking(); return { status: 200, body: { workload: d.workload } }; }
      return url === "/api/deployments" ? { status: 200, body: { deployments: [d], server_time: "", can_manage: true } }
        : url === "/api/routes" ? { status: 200, body: { routes, cloud: "off", is_admin: true } } : undefined;
    });
    mount("/playground");
    const mine = (await screen.findByText(/is asleep/, { selector: ".play-wake strong" })).closest(".play-wake")!;
    fireEvent.click(within(mine as HTMLElement).getByRole("button", { name: "Wake now" }));
    await waitFor(() => expect(posts).toEqual(["/api/deployments/3/start"]));
    expect(await screen.findByText(/^Waking the model \(about 74 s\)…$/)).toBeTruthy();
  });

  it("FY-WEB-609: Machines names an asleep deployment on the machine it would run on (it holds nothing), on the tile and the machine's page", async () => {
    const sleepy = asleep();
    const route = (url: string): Reply | undefined =>
      url === "/api/machines" ? { status: 200, body: machinesBody }
        : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } }
          : url === "/api/workloads" ? { status: 200, body: { workloads: [sleepy.workload], server_time: "", can_manage: true } }
            : url === "/api/deployments" ? { status: 200, body: { deployments: [sleepy], server_time: "", can_manage: true } } : undefined;
    server(route);
    const view = mount("/machines");
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    const line = await within(box).findByLabelText("Asleep here");
    expect(line.textContent).toBe("chat asleep here (14.4 GiB on GPU #0 when awake)");
    expect(within(line).getByRole("link", { name: "chat" }).getAttribute("href")).toBe("/serving/3");
    // it holds nothing: not among what runs there
    expect(box.querySelector(".holder-chip")).toBeNull();
    expect(within(screen.getByRole("article", { name: "Machine strix" })).queryByLabelText("Asleep here")).toBeNull();
    view.unmount();
    mount("/machines/1");
    const full = await screen.findByLabelText("Asleep here");
    expect(full.textContent).toMatch(/^chat asleep here \(14\.4 GiB on GPU #0 when awake\)\. It holds nothing now, so others may use that memory meanwhile/);
  });
});

describe("sleep when idle: the wake wait (1.5.0 PR 3's parameter) on the forms", () => {
  it("FY-WEB-610: Wake wait sits under Advanced on the deploy and edit forms, 300 by default, checked, sent, and named in the changes", async () => {
    const bodies: Record<string, unknown>[] = [];
    const d = dep([rep()], { params: { context: 8192, concurrency: 4, gpus: 1, share: "exclusive", sleep_when_idle: true, idle_minutes: 30 } });
    server((url, init) => {
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/machines") return { status: 200, body: machinesBody };
      if (url === "/api/deployments" && init?.method === "GET") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
      if (url === "/api/deployments/estimate") return { status: 200, body: { estimate, placement: fits, params: {}, recipe: "vllm", model: "qwen3-4b" } };
      if (url === "/api/deployments/3" && init?.method === "PATCH") {
        const b = JSON.parse(String(init.body));
        bodies.push(b);
        const changes: EditResponse["changes"] = b.params.wake_wait !== null ? { wake_wait: { before: null, after: b.params.wake_wait } } : {};
        const out: EditResponse = { estimate, params: b.params, changes, placement: fits, restart: "none", restart_text: "Nothing restarts." };
        return { status: 200, body: b.dry_run ? out : { ...out, deployment: d } };
      }
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: d, events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    const view = mount("/serving/deploy?model=qwen3-4b");
    const field = (await screen.findByLabelText("Wake wait (seconds)")) as HTMLInputElement;
    expect(field.closest("details")?.querySelector("summary")?.textContent).toMatch(/wake wait/);
    expect(field.closest("details")?.open).toBe(false);   // collapsed: it works by default
    expect(field.placeholder).toBe("300");
    fireEvent.change(field, { target: { value: "5" } });
    expect(screen.getByText("10 to 1,800 seconds.")).toBeTruthy();
    fireEvent.change(field, { target: { value: "120" } });
    expect(screen.queryByText("10 to 1,800 seconds.")).toBeNull();
    const json = () => JSON.parse(screen.getByLabelText("Deployment JSON").textContent ?? "{}");
    expect(json().params.wake_wait).toBeUndefined();          // sleep is off: nothing to wait for
    fireEvent.click(screen.getByRole("checkbox", { name: /Sleep when idle/ }));
    expect(json().params).toMatchObject({ sleep_when_idle: true, idle_minutes: 30, wake_wait: 120 });
    view.unmount();

    mount("/serving/3/edit");
    const edit = (await screen.findByLabelText("Wake wait (seconds)")) as HTMLInputElement;
    expect(edit.value).toBe("");
    fireEvent.change(edit, { target: { value: "90" } });
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    expect(changes.textContent).toBe("Wake wait (s): — → 90");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    expect(bodies.find((b) => !b.dry_run)!.params).toMatchObject({ wake_wait: 90, sleep_when_idle: true });
  });
});
