// @vitest-environment jsdom
// Vision models under Models (1.4, #86): the Deployments' Vision section, the Library's checks, Try it, the
// Playground's Models tab, the snippets, and the Apps page without its templates (FY-WEB-300 to 306).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { searchLicenceProblem, visionProblem, type SearchResult } from "../lib/addmodel";
import { snippets } from "../lib/gateway";
import type { CatalogResponse, Deployment, Replica } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };
const GIB = 1024 ** 3;

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const rep = (): Replica => ({
  ordinal: 0, id: "r1", generation: 1, machine: { id: 2, name: "cpu-a", status: "online", connected: true }, gpus: [], state: "healthy",
  reason: null, exit_code: null, attempt: 0, restarts: 0, final: false, pid: 4242, container: null,
  ports: [{ name: "http", port: null, host_port: 47851, address: "10.0.0.5" }], started_at: "2026-11-16T04:00:00Z", finished_at: null,
  updated_at: "2026-11-16T04:00:00Z", vram: null, download: null, runs: { type: "process", image: null, command: "python" },
});

const visionDep = (id: number, name: string, task: "detect" | "segment" | "depth", model: string): Deployment => ({
  id, name, recipe: "vision", served_name: name, params: { gpus: 0, threads: 4 },
  model: { id, name: model, source: `hf://x/${model}`, task, format: "safetensors", revision: "5650", file: null, size_bytes: 80 * 1024 ** 2 },
  estimate: { method: "files", device: "cpu", gpus: 0, per_gpu: 0, ram: 1.33 * GIB, amount: 1.33 * GIB, kv_tokens: 0, notes: [],
    parts: [{ label: "Model files", bytes: 80 * 1024 ** 2, detail: "" }, { label: "Engine overhead", bytes: 1.25 * GIB, detail: "recipe" }] },
  has_token: false, created_by: "owner", created_at: "2026-11-16T04:00:00Z",
  route: { served_name: name, api: `vision-${task}`, upstream_model: name },
  endpoints: [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:47851/" }],
  workload: { id: 100 + id, name, kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
    created_at: "2026-11-16T04:00:00Z", updated_at: "2026-11-16T04:00:00Z", status: "running", ready: 1, replicas: 1,
    spec: { name, kind: "service", runtime: { type: "model", recipe: "vision", model }, env: {},
      resources: { cpu: 4, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [{ name: "http", port: null }],
      health: null, restart: "always", max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null },
    replica_list: [rep()] },
  cards: [],
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("vision models (1.4, #86)", () => {
  it("FY-WEB-300 Deployments: detection, segmentation and depth share one Vision section, each row with its task and path", async () => {
    const deps = [visionDep(3, "det", "detect", "rtdetr-v2-r18"), visionDep(4, "seg", "segment", "sam2.1-hiera-tiny"),
      visionDep(5, "dav2", "depth", "depth-anything-v2-small")];
    server((url) => url === "/api/deployments" ? { status: 200, body: { deployments: deps, server_time: "", can_manage: true } }
      : url === "/api/machines" ? { status: 200, body: { machines: [] } } : undefined);
    mount("/models");
    const sec = await screen.findByRole("region", { name: "Vision · 3" });
    expect(within(sec).getByText("/v1/vision/…")).toBeTruthy();
    expect(within(sec).getByText("Object detection, segmentation and depth")).toBeTruthy();
    const det = within(sec).getByRole("group", { name: "Deployment det" });
    expect(within(det).getByText("/v1/vision/detect")).toBeTruthy();
    expect(within(det).getByText(/Object detection ·/)).toBeTruthy();
    expect(within(within(sec).getByRole("group", { name: "Deployment seg" })).getByText("/v1/vision/segment")).toBeTruthy();
    expect(within(within(sec).getByRole("group", { name: "Deployment dav2" })).getByText("/v1/vision/depth")).toBeTruthy();
    expect(within(det).getByText("1.33 GiB RAM")).toBeTruthy();
    expect(screen.queryByRole("region", { name: /^Chat/ })).toBeNull();
  });

  it("FY-WEB-301 Try it on a vision deployment sends the test image, without an input box", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/deployments/3/try" && init?.method === "POST") return { status: 200, body: { status: 200, ok: true, api: "vision-detect",
        endpoint: "http://10.0.0.5:47851/v1/vision/detect", request: {}, summary: "No objects found above the threshold (the test image is two coloured squares).", answer: {}, ms: 389 } };
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: visionDep(3, "det", "detect", "rtdetr-v2-r18"), server_time: "", can_manage: true, events: [] } };
      if (url.startsWith("/api/workloads/103/replicas/0/logs")) return { status: 200, body: { source: "live", text: "", truncated: false, state: "healthy" } };
      return undefined;
    });
    mount("/models/deployments/3");
    const tryIt = (await screen.findByRole("heading", { name: "Try it" })).closest("section") as HTMLElement;
    expect(within(tryIt).getByText(/a small test image \(two coloured squares\)/)).toBeTruthy();
    expect(within(tryIt).queryByLabelText("Input")).toBeNull();
    await act(async () => fireEvent.click(within(tryIt).getByRole("button", { name: "Send" })));
    expect((await screen.findByLabelText("Answer")).textContent).toMatch(/No objects found/);
    const call = fetch.mock.calls.find(([u]) => u === "/api/deployments/3/try");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({});
  });

  it("FY-WEB-302 the Library says before adding why a vision model cannot be added: another task, a licence, timm", () => {
    const ok = { vision: { task: "detect" as const, architecture: "RtDetrV2ForObjectDetection" }, license: "apache-2.0" };
    expect(visionProblem(ok, "detect")).toBeNull();
    expect(visionProblem(ok, "chat")).toBeNull();
    expect(visionProblem(ok, "depth")).toBe("This is a object detection model, not one for depth estimation.");
    expect(visionProblem({ ...ok, license: "cc-by-nc-4.0" }, "detect")).toMatch(/permissive licence only.*cc-by-nc-4.0/);
    expect(visionProblem({ ...ok, license: null }, "detect")).toMatch(/not stated/);
    expect(visionProblem({ vision: { ...ok.vision, problem: "it uses a timm backbone" }, license: "mit" }, "detect")).toMatch(/timm/);
    expect(visionProblem({ license: "apache-2.0" }, "segment")).toMatch(/not a segmentation model/);
    const r = { task: "depth", license: "cc-by-nc-4.0" } as SearchResult;
    expect(searchLicenceProblem(r)).toMatch(/cc-by-nc-4.0/);
    expect(searchLicenceProblem({ ...r, license: "apache-2.0" })).toBeNull();
    expect(searchLicenceProblem({ ...r, task: "chat" })).toBeNull();
    expect(searchLicenceProblem({ ...r, license: null })).toBeNull();
  });

  it("FY-WEB-303 adding a vision model with a licence that is not permissive is said and not sent", async () => {
    const catalog: CatalogResponse = { can_manage: true, can_deploy: true, models: [], recipes: [],
      sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false } };
    const fetch = server((url, init) => {
      if (url === "/api/models" && init?.method === "POST") return { status: 201, body: {} };
      if (url === "/api/models") return { status: 200, body: catalog };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
      if (url === "/api/models/inspect") return { status: 200, body: { recipes: ["vision"], meta: { format: "safetensors", revision: "5" .repeat(40),
        task_hint: "depth", size_bytes: 1.3 * GIB, file_count: 4, license: "cc-by-nc-4.0", modalities: [],
        vision: { task: "depth", architecture: "DepthAnythingForDepthEstimation" } }, suggestion: { recipe: "vision", recipes: ["vision"] } } };
      return undefined;
    });
    mount("/models?tab=library");
    fireEvent.click(await screen.findByRole("button", { name: "Enter a source" }));
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "depth-anything/Depth-Anything-V2-Large-hf" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Look it up" })));
    expect(await screen.findByText(/Depth estimation · DepthAnythingForDepthEstimation/)).toBeTruthy();
    await waitFor(() => expect((screen.getByLabelText("Task") as HTMLSelectElement).value).toBe("depth"));
    expect(screen.getByText(/permissive licence only.*cc-by-nc-4.0/)).toBeTruthy();
    expect(screen.queryByText(/^Text only$/)).toBeNull();
    expect((screen.getByRole("button", { name: "Add to the catalog" }) as HTMLButtonElement).disabled).toBe(true);
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/models" && i?.method === "POST")).toBe(false);
  });

  it("FY-WEB-304 the Playground's Models tab opens the vision panel for a vision model", async () => {
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: { cloud: "off", is_admin: true, routes: [{ id: 1, served_name: "det", api: "vision-detect",
        deployment_id: 3, kind: "local", endpoints: [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "x" }], targets: [] }] } };
      if (url === "/api/playground/vision/options") return { status: 200, body: { models: [{ name: "det", task: "detect", title: "Object detection",
        endpoint: "/v1/vision/detect", deployment_id: 3, model: "PekingU/rtdetr_v2_r18vd", licence: "apache-2.0", ready: 1, running: true, reason: null }],
      workloads: [], tasks: [] } };
      return undefined;
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Object detection" })).closest("section") as HTMLElement;
    expect(within(panel).getByText("POST /v1/vision/detect with model=det")).toBeTruthy();
    expect(within(panel).getByText("PekingU/rtdetr_v2_r18vd")).toBeTruthy();
    expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe("det");
  });

  it("FY-WEB-305 the snippets for a vision model: curl with the image as a file, Python with httpx (no OpenAI SDK)", () => {
    const d = snippets("https://fy.example:7860/v1", "fyk_x", "det", "vision-detect", "https://fy.example:7860/trust");
    expect(d.curl).toContain("https://fy.example:7860/v1/vision/detect");
    expect(d.curl).toContain("-F model=det -F image=@photo.jpg -F threshold=0.5");
    expect(d.curl).toContain("--cacert forgeyard-ca.pem");
    expect(d.python).toContain('httpx.post("https://fy.example:7860/v1/vision/detect"');
    expect(d.python).toContain('files={"image": f}');
    expect(d.python).toContain("verify=ca");
    expect(d.python).not.toContain("from openai");
    const s = snippets("http://h/v1", "k", "seg", "vision-segment");
    expect(s.curl).toContain("-F 'box=[40, 40, 320, 240]'");
    expect(s.python).not.toContain("ssl");
    const z = snippets("http://h/v1", "k", "dav2", "vision-depth");
    expect(z.python).toContain("import base64");
    expect(z.python).toContain('r.json()["depth_png"]');
  });

  it("FY-WEB-306 Apps no longer offers the vision templates, and points vision to Models", async () => {
    const fetch = server((url) => url === "/api/workloads" ? { status: 200, body: { workloads: [], server_time: "x", can_manage: true } } : undefined);
    mount("/apps");
    expect(await screen.findByRole("heading", { name: "No apps yet" })).toBeTruthy();
    expect(screen.queryByText("Start from a template")).toBeNull();
    expect(screen.getByRole("link", { name: "add them from the Library" }).getAttribute("href")).toBe("/models?tab=library");
    expect(fetch.mock.calls.some(([u]) => u === "/api/workloads/templates")).toBe(false);
  });
});
