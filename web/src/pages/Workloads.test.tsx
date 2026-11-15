// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import machinesFixture from "../test/machines.json";
import type { Replica, Workload } from "../lib/types";

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
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
        <Where />
      </AuthProvider>
    </MemoryRouter>,
  );
}

const replica = (over: Partial<Replica> = {}): Replica => ({
  ordinal: 0, id: "a1b2c3", generation: 1, machine: { id: 1, name: "a100-box", status: "online", connected: true },
  gpus: [{ index: 4, vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", key: "nvidia:GPU-x" }], state: "healthy", reason: null,
  exit_code: null, attempt: 0, restarts: 0, final: false, pid: 4321, container: null,
  ports: [{ name: "http", port: null, host_port: 31000, address: "10.0.0.5" }],
  started_at: "2026-11-09T10:00:00Z", finished_at: null, updated_at: "2026-11-09T10:00:00Z", ...over,
});

const web: Workload = {
  id: 7, name: "web", kind: "service", runtime: "process", desired_state: "running", generation: 1, created_by: "owner",
  created_at: "2026-11-09T10:00:00Z", updated_at: "2026-11-09T10:00:00Z", status: "running", ready: 1, replicas: 1,
  spec: {
    name: "web", kind: "service", runtime: { type: "process", command: "python3", args: ["-m", "http.server", "${PORT}"], workdir: null },
    env: { GREETING: "hi" }, resources: { cpu: null, memory: null, gpus: { count: 1, indices: null, vendor: null } },
    ports: [{ name: "http", port: null }], health: { type: "http", port: "http", path: "/", interval: 10, timeout: 3, failure_threshold: 3, initial_delay: 0 },
    restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null,
  },
  replica_list: [replica()],
};

const job: Workload = {
  ...web, id: 8, name: "job", kind: "task", status: "failed", ready: 0,
  spec: { ...web.spec, name: "job", kind: "task", ports: [], health: null, max_duration: 60, retries: 2 },
  replica_list: [replica({ state: "failed", gpus: [], ports: [], pid: null, exit_code: 3, final: true, attempt: 3,
    reason: "exited with code 3 (attempt 3 of 3)", finished_at: "2026-11-09T10:01:00Z" })],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("workloads", () => {
  it("FY-WEB-039 the list shows status, replicas, machine, GPUs and the address", async () => {
    server((url) => (url === "/api/workloads" ? { status: 200, body: { workloads: [web, job], server_time: "2026-11-09T10:02:00Z", can_manage: true } } : undefined));
    mount("/workloads");
    const card = await screen.findByRole("article", { name: "Workload web" });
    expect(within(card).getByText("running")).toBeTruthy();
    expect(within(card).getByText("healthy")).toBeTruthy();
    expect(within(card).getByText("a100-box")).toBeTruthy();
    expect(within(card).getByText("GPU #4")).toBeTruthy();
    expect(within(card).getByRole("link", { name: "10.0.0.5:31000" }).getAttribute("href")).toBe("http://10.0.0.5:31000/");
    expect(within(card).getByText("python3 -m http.server ${PORT}")).toBeTruthy();
    const failed = screen.getByRole("article", { name: "Workload job" });
    expect(within(failed).getByText("exited with code 3 (attempt 3 of 3)")).toBeTruthy();
    expect(screen.getByRole("link", { name: "New workload" })).toBeTruthy();
  });

  it("FY-WEB-217 a model workload says which deployment created it, in the list and on its page (1.4)", async () => {
    const note = "cpu-01 could not reach huggingface.co; placed on cpu-02 instead";
    const model: Workload = { ...web, id: 9, name: "chat", runtime: "model", deployment_id: 3, deployment: { id: 3, name: "chat-dep" },
      spec: { ...web.spec, name: "chat", runtime: { type: "model", recipe: "llama-cpp", model: "qwen3-1.7b-gguf" } },
      replica_list: [replica({ machine: { id: 2, name: "cpu-02", status: "online", connected: true }, gpus: [], placement_note: note })] };
    server((url) => url === "/api/workloads" ? { status: 200, body: { workloads: [web, model], server_time: "", can_manage: true } }
      : url === "/api/workloads/9" ? { status: 200, body: { workload: model, events: [], server_time: "", can_manage: true } } : undefined);
    mount("/workloads");
    const group = await screen.findByRole("region", { name: /Created by model deployments/ });
    expect(within(group).getByRole("link", { name: "chat" }).getAttribute("href")).toBe("/workloads/9");
    expect(within(group).getByText(/created by deployment/)).toBeTruthy();
    expect(within(group).getByRole("link", { name: "chat-dep" }).getAttribute("href")).toBe("/models/deployments/3");
    expect(within(screen.getByRole("article", { name: "Workload web" })).queryByText(/created by deployment/)).toBeNull();
    cleanup();
    mount("/workloads/9");
    expect(await screen.findByRole("heading", { name: "chat" })).toBeTruthy();
    const links = screen.getAllByRole("link", { name: "chat-dep" });
    expect(links.length).toBe(2);  // the banner and the spec's "Created by"
    expect(links.every((a) => a.getAttribute("href") === "/models/deployments/3")).toBe(true);
    expect(screen.getByRole("link", { name: "Open the deployment" }).getAttribute("href")).toBe("/models/deployments/3");
    expect(screen.getByText("Created by")).toBeTruthy();
    expect(within(screen.getByRole("table")).getByText(note)).toBeTruthy();
  });

  it("FY-WEB-040 users can look but not start; the old Deployments link lands on Models (M3)", async () => {
    server((url) => (url === "/api/workloads" ? { status: 200, body: { workloads: [], server_time: "", can_manage: false } }
      : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: false } } : undefined),
      { ...OWNER, role: "user", is_owner: false });
    mount("/deployments");
    expect(await screen.findByText("No deployments yet")).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/models");
    cleanup();
    mount("/workloads");
    expect(await screen.findByText("No workloads yet")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "New workload" })).toBeNull();
    expect(screen.getByRole("link", { name: "Workloads" })).toBeTruthy();
  });

  it("FY-WEB-041 the form sends the spec it shows and opens the new workload", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/machines") return { status: 200, body: machinesFixture };
      if (url === "/api/workloads" && init?.method === "POST") return { status: 201, body: { workload: { ...web, id: 9 }, events: [] } };
      if (url === "/api/workloads/9") return { status: 200, body: { workload: { ...web, id: 9 }, events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    mount("/workloads/new");
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "web-demo" } });
    fireEvent.change(screen.getByLabelText("Image"), { target: { value: "python:3.13-slim" } });
    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: "python\n-m\nhttp.server\n8000\n" } });
    fireEvent.click(screen.getByRole("radio", { name: "How many" }));
    fireEvent.change(screen.getByLabelText("GPU count"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.change(screen.getByLabelText("Variable 1 name"), { target: { value: "MODE" } });
    fireEvent.change(screen.getByLabelText("Variable 1 value"), { target: { value: "demo" } });
    fireEvent.click(screen.getByRole("radio", { name: "HTTP" }));
    const expected = {
      name: "web-demo", kind: "service", runtime: { type: "container", image: "python:3.13-slim", args: ["python", "-m", "http.server", "8000"] },
      env: { MODE: "demo" }, resources: { gpus: { count: 1 } }, replicas: 1, ports: [{ name: "http", port: 8000 }],
      health: { type: "http", port: "http", interval: 10, path: "/" }, restart: "always",
    };
    expect(JSON.parse(screen.getByLabelText("Spec JSON").textContent ?? "")).toEqual(expected);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create and start" })));
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/workloads" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual(expected);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workloads/9"));
  });

  it("FY-WEB-042 picking cards on a machine: only allowed, free ones; a task has limits, no ports", async () => {
    const restricted = structuredClone(machinesFixture);
    const box = restricted.machines[0] as Record<string, unknown> & { accelerators: Record<string, unknown>[] };
    box.features = ["workloads"];
    box.gpu_allowlist = [String(box.accelerators[4].key)];
    box.accelerators.forEach((a, i) => { a.allowed = a.vendor !== "cpu" && i === 4; });
    const fetch = server((url, init) => {
      if (url === "/api/machines") return { status: 200, body: restricted };
      if (url === "/api/workloads" && init?.method === "POST") return { status: 422, body: { detail: { code: "invalid_spec", message: "max_duration: Input should be greater than or equal to 1" } } };
      return undefined;
    });
    mount("/workloads/new");
    fireEvent.click(await screen.findByRole("radio", { name: "Task" }));
    fireEvent.click(screen.getByRole("radio", { name: "Process" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "probe" } });
    fireEvent.change(screen.getByLabelText("Command"), { target: { value: "python3" } });
    await waitFor(() => expect(screen.getByRole("option", { name: /a100-box · online · Forgeyard may use only #4/ })).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Machine"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("radio", { name: "These cards" }));
    const boxes = within(screen.getByRole("group", { name: "Cards on a100-box" })).getAllByRole("checkbox");
    expect(boxes.map((b) => (b as HTMLInputElement).disabled)).toEqual([true, true, true, true, false, true, true, true]);
    expect(screen.getAllByText("not allowed for Forgeyard")).toHaveLength(7);
    fireEvent.click(boxes[4]);
    fireEvent.change(screen.getByLabelText("Max duration (s)"), { target: { value: "0" } });
    expect(screen.queryByText("Ports and health")).toBeNull();
    const spec = JSON.parse(screen.getByLabelText("Spec JSON").textContent ?? "");
    expect(spec).toEqual({ name: "probe", kind: "task", runtime: { type: "process", command: "python3", args: [] },
      resources: { gpus: { indices: [4] } }, replicas: 1, machine_id: 1, max_duration: 0, retries: 0, retry_backoff: 5 });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create and start" })));
    expect(await screen.findByText(/max_duration: Input should be greater/)).toBeTruthy();
    expect(fetch.mock.calls.filter(([u, i]) => u === "/api/workloads" && i?.method === "POST")).toHaveLength(1);
  });

  it("FY-WEB-043 the detail page shows replicas, events and live output; stop asks first", async () => {
    let stopped = false;
    const fetch = server((url, init) => {
      if (url === "/api/workloads/7/stop" && init?.method === "POST") { stopped = true; return { status: 200, body: {} }; }
      if (url === "/api/workloads/7") return { status: 200, body: { workload: stopped ? { ...web, desired_state: "stopped", status: "stopping" } : web,
        events: [{ at: "2026-11-09T10:00:01Z", kind: "placed", replica: 0, message: "Replica 0 placed on a100-box with GPU #4." }],
        server_time: "", can_manage: true } };
      if (url.startsWith("/api/workloads/7/replicas/0/logs")) return { status: 200, body: { source: "live", text: "Serving HTTP on 0.0.0.0 port 31000", truncated: false, state: "healthy" } };
      return undefined;
    });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mount("/workloads/7");
    expect(await screen.findByRole("heading", { name: "web" })).toBeTruthy();
    const table = screen.getByRole("table");
    expect(within(table).getByText("healthy")).toBeTruthy();
    expect(within(table).getByText("pid 4321")).toBeTruthy();
    expect(within(table).getByRole("link", { name: "http://10.0.0.5:31000/" })).toBeTruthy();
    expect(screen.getByText("Replica 0 placed on a100-box with GPU #4.")).toBeTruthy();
    expect((await screen.findByLabelText("Output of replica 0")).textContent).toBe("Serving HTTP on 0.0.0.0 port 31000");
    expect(screen.getByText(/Live from the agent/)).toBeTruthy();
    expect(screen.getByText("GREETING")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(fetch.mock.calls.some(([u]) => u === "/api/workloads/7/stop")).toBe(false);
    confirm.mockReturnValue(true);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Stop" })));
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/workloads/7/stop" && i?.method === "POST")).toBe(true);
    expect(await screen.findByText("Stopping web.")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Start" })).toBeTruthy();
  });

  it("FY-WEB-207 the detail page shows an image pull's progress bar, and the reason while sizes are unknown", async () => {
    const pulling = (download: Replica["download"], reason: string) => ({ ...web, status: "starting",
      replica_list: [replica({ state: "starting", pid: null, reason, download })] });
    let body = pulling({ phase: "image", done: 858993459, total: 2040109466, layers: 7, layers_done: 2 },
                       "pulling the image: 0.80 of 1.90 GiB (42%)");
    server((url) => url === "/api/workloads/7"
      ? { status: 200, body: { workload: body, events: [], server_time: "", can_manage: false } } : undefined);
    const { unmount } = mount("/workloads/7");
    const bar = await screen.findByLabelText("Download progress");
    expect(bar.textContent).toBe("Pulling the image: 819 MiB of 1.9 GiB (42%)");
    expect(screen.queryByText("pulling the image: 0.80 of 1.90 GiB (42%)")).toBeNull();
    unmount();
    cleanup();
    body = pulling({ phase: "image", done: 0, total: 0, layers: 7, layers_done: 3 }, "pulling the image: 3 of 7 layers");
    mount("/workloads/7");
    expect(await screen.findByText("pulling the image: 3 of 7 layers")).toBeTruthy();
    expect(screen.queryByLabelText("Download progress")).toBeNull();
  });

  it("FY-WEB-044 delete asks, then goes back to the list; users see no output or actions", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/workloads/8" && init?.method === "DELETE") return { status: 200, body: { ok: true } };
      if (url === "/api/workloads/8") return { status: 200, body: { workload: job, events: [], server_time: "", can_manage: true } };
      if (url === "/api/workloads") return { status: 200, body: { workloads: [], server_time: "", can_manage: true } };
      if (url.startsWith("/api/workloads/8/replicas/0/logs")) return { status: 200, body: { source: "stored", text: "try", truncated: false, state: "failed", note: "Live output is not available." } };
      return undefined;
    });
    vi.stubGlobal("confirm", vi.fn(() => true));
    mount("/workloads/8");
    expect(await screen.findByText("exit 3")).toBeTruthy();
    expect(await screen.findByText("Live output is not available.")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Delete" })));
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/workloads/8" && i?.method === "DELETE")).toBe(true);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/workloads"));
  });

  it("FY-WEB-045 users get no stop, delete or output on the detail page", async () => {
    const fetch = server((url) => (url === "/api/workloads/7" ? { status: 200, body: { workload: { ...web, spec: { ...web.spec, env: { GREETING: "••••" } } }, events: [], server_time: "", can_manage: false } } : undefined),
      { ...OWNER, role: "user", is_owner: false });
    mount("/workloads/7");
    expect(await screen.findByRole("heading", { name: "web" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByText("Output")).toBeNull();
    expect(fetch.mock.calls.some(([u]) => String(u).includes("/logs"))).toBe(false);
  });
});

describe("workloads and deployments (1.4, #36)", () => {
  const model = (n: number): Workload => ({ ...web, id: 100 + n, name: `m${n}`, runtime: "model", deployment_id: 200 + n,
    deployment: { id: 200 + n, name: `dep${n}` }, spec: { ...web.spec, name: `m${n}`, runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" } } });

  it("FY-WEB-227 the list has your workloads, then the ones deployments created: eight, then '… and N more'", async () => {
    const models = Array.from({ length: 10 }, (_, i) => model(i));
    server((url) => (url === "/api/workloads" ? { status: 200, body: { workloads: [web, ...models, job], server_time: "", can_manage: true } } : undefined));
    mount("/workloads");
    const own = await screen.findByRole("region", { name: "Your workloads · 2" });
    expect(within(own).getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["Workload web", "Workload job"]);
    const made = screen.getByRole("region", { name: "Created by model deployments · 10" });
    expect(within(made).getAllByRole("listitem")).toHaveLength(8);
    expect(within(made).getAllByRole("link", { name: /^dep\d$/ }).map((a) => a.getAttribute("href"))[0]).toBe("/models/deployments/200");
    const more = within(made).getByRole("button", { name: "… and 2 more" });
    fireEvent.click(more);
    expect(within(made).getAllByRole("listitem")).toHaveLength(10);
    fireEvent.click(within(made).getByRole("button", { name: "Show fewer" }));
    expect(within(made).getAllByRole("listitem")).toHaveLength(8);
  });

  it("FY-WEB-228 a deployment's workload has no Stop, Start or Delete: its banner sends you to the deployment", async () => {
    const m = model(1);
    const stopped = { ...model(2), desired_state: "stopped" as const, status: "stopped" as const };
    server((url) => url === "/api/workloads/101" ? { status: 200, body: { workload: m, events: [], server_time: "", can_manage: true } }
      : url === "/api/workloads/102" ? { status: 200, body: { workload: stopped, events: [], server_time: "", can_manage: true } }
        : url === "/api/workloads/7" ? { status: 200, body: { workload: web, events: [], server_time: "", can_manage: true } } : undefined);
    const view = mount("/workloads/101");
    const banner = await screen.findByRole("region", { name: "Created by a deployment" });
    expect(within(banner).getByRole("link", { name: "dep1" }).getAttribute("href")).toBe("/models/deployments/201");
    expect(screen.getByText("Stop and delete are on the deployment")).toBeTruthy();
    for (const name of ["Stop", "Start", "Delete"]) expect(screen.queryByRole("button", { name })).toBeNull();
    view.unmount();
    const again = mount("/workloads/102");
    await screen.findByRole("region", { name: "Created by a deployment" });
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    again.unmount();
    mount("/workloads/7"); // a plain workload keeps its buttons
    expect(await screen.findByRole("button", { name: "Stop" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Created by a deployment" })).toBeNull();
  });

  it("FY-WEB-232 a model workload's spec gives the GPUs its deployment chose, not 'none'", async () => {
    const onGpu = { ...model(1), deployment: { id: 201, name: "dep1", gpus: 1 } };
    const onCpu = { ...model(2), deployment: { id: 202, name: "dep2", gpus: 0 }, replica_list: [replica({ gpus: [] })] };
    const old = { ...model(3), deployment: { id: 203, name: "dep3" } }; // a server before this field: from the replicas
    server((url) => url === "/api/workloads/101" ? { status: 200, body: { workload: onGpu, events: [], server_time: "", can_manage: true } }
      : url === "/api/workloads/102" ? { status: 200, body: { workload: onCpu, events: [], server_time: "", can_manage: true } }
        : url === "/api/workloads/103" ? { status: 200, body: { workload: old, events: [], server_time: "", can_manage: true } }
          : url === "/api/workloads/7" ? { status: 200, body: { workload: web, events: [], server_time: "", can_manage: true } } : undefined);
    const fact = async () => (await screen.findByText("GPUs", { selector: "dt" })).nextElementSibling?.textContent;
    for (const [path, want] of [["/workloads/101", "1 per replica, from the deployment; holding #4"],
      ["/workloads/102", "none: the deployment runs it on the CPU"], ["/workloads/103", "from the deployment; holding #4"],
      ["/workloads/7", "1 per replica"]] as const) {
      const view = mount(path);
      expect(await fact()).toBe(want);
      view.unmount();
    }
  });

  it("FY-WEB-229 'New workload here' opens the form with the machine chosen (Deploy here: FY-WEB-230)", async () => {
    const data = structuredClone(machinesFixture);
    (data.machines[0] as Record<string, unknown>).features = ["workloads", "models"];
    server((url) => url === "/api/machines" ? { status: 200, body: data } : undefined);
    mount("/workloads/new?machine=1");
    const pick = await screen.findByLabelText(/^Machine/) as HTMLSelectElement;
    await waitFor(() => expect(pick.value).toBe("1"));
  });
});

describe("GPU access on a machine's page", () => {
  it("FY-WEB-046 an admin restricts a machine to GPU #4, after confirming", async () => {
    const data = structuredClone(machinesFixture);
    const box = data.machines[0] as Record<string, unknown> & { accelerators: Record<string, unknown>[] };
    box.features = ["workloads"];
    box.gpu_allowlist = null;
    box.runtime_info = { process: { ok: true, user: "nobody", limits: "rlimit" }, container: { ok: false, reason: "docker not found" } };
    box.accelerators.forEach((a) => { a.allowed = a.vendor !== "cpu"; });
    const key4 = String(box.accelerators[4].key);
    const fetch = server((url, init) => {
      if (url === "/api/machines/1/gpu-allowlist" && init?.method === "PUT") return { status: 200, body: {} };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return url === "/api/machines" ? { status: 200, body: data } : undefined;
    });
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    mount("/machines/1");
    const card = await screen.findByRole("region", { name: "GPU access and runtimes" });
    expect(within(card).getByText("all 8 GPUs")).toBeTruthy();
    expect(within(card).getByText(/process as nobody \(rlimits\) · container: docker not found/)).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "Change GPU access" }));
    fireEvent.click(within(card).getByRole("button", { name: "None" }));
    const group = within(card).getByRole("group", { name: "GPU access on a100-box: the GPUs Forgeyard may assign" });
    fireEvent.click(within(group).getAllByRole("checkbox")[4]);
    await act(async () => fireEvent.click(within(card).getByRole("button", { name: "Save" })));
    expect(confirm.mock.calls[0]).toEqual(["Let Forgeyard assign only GPU #4 on a100-box? No workload will ever get another card of this machine."]);
    const put = fetch.mock.calls.find(([u, i]) => u === "/api/machines/1/gpu-allowlist" && i?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ keys: [key4] });
    expect(await screen.findByText("Forgeyard may assign only #4 on a100-box.")).toBeTruthy();
  });

  it("FY-WEB-047 a restricted machine marks the other cards; operators cannot change it", async () => {
    const data = structuredClone(machinesFixture);
    const box = data.machines[0] as Record<string, unknown> & { accelerators: Record<string, unknown>[] };
    box.features = ["workloads"];
    box.gpu_allowlist = [String(box.accelerators[4].key)];
    box.accelerators.forEach((a, i) => { a.allowed = a.vendor !== "cpu" && i === 4; a.assigned_to = i === 4 ? "gpu-probe" : null; });
    server((url) => (url === "/api/machines" ? { status: 200, body: { ...data, can_manage: false } } : undefined), { ...OWNER, role: "operator", is_owner: false });
    mount("/machines/1");
    const card = await screen.findByRole("region", { name: "GPU access and runtimes" });
    expect(within(card).getByText("only #4")).toBeTruthy();
    expect(screen.getAllByText("not for Forgeyard")).toHaveLength(7);
    expect(within(screen.getByRole("group", { name: "GPU #4" })).getByText("in use: gpu-probe")).toBeTruthy();
    expect(within(card).queryByRole("button", { name: "Change GPU access" })).toBeNull();
  });
});
