// @vitest-environment jsdom
// 1.5.2 (#95 PR 4): trying out an engine. The engine page's Try it out (operators and admins): the dialog (model,
// machine, parameters taken from a deployment, the estimate and where it fits, the image to pull, the time to start)
// and what it sends; the Trials section (a running trial with Cancel, the history, a trial's checks and the comparison
// with the built-in engine) in both languages; Serving's trials in progress; a machine's holder named as the trial;
// Edit's line about the chosen engine's last trial; an engine card's chip. FY-WEB-887 to 895.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LastTrialLine, trialSummary } from "../components/EngineTrials";
import { LANG_KEY } from "../i18n/lang";
import { makeTranslator } from "../i18n/react";
import { placement } from "../lib/fleet";
import type { EnginePage, EngineTrial, Recipe, TrialBrief, Workload } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 3, username: "ursa", role: "user", is_owner: false };
type Reply = { status: number; body?: unknown };
const GIB = 1024 ** 3;

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});

const D2 = "sha256:" + "cd".repeat(32);
const COPY = {
  name: "vllm-0-34-0", title: "vLLM 0.34.0", description: "", engine: "vllm", version: "0.34.0", tasks: ["chat"], formats: ["safetensors"],
  api: { chat: "openai-chat" }, share: "exclusive", builtin: false, based_on: "vllm", estimate: "safetensors-kv", devices: ["gpu"],
  params: { context: { type: "int", default: 8192, min: 256, label: "Context length (tokens)" },
    concurrency: { type: "int", default: 4, min: 1, label: "Concurrency" },
    enforce_eager: { type: "choice", default: "off", choices: ["off", "on"], label: "Eager mode", advanced: true } },
  variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: `vllm/vllm-openai:v0.34.0@${D2}`, arch: null,
    compute_capability: ">=8.0", driver: ">=580", engine: null, image_check: true }],
  copy: { from_version: "0.32.0", kind: "release", made_at: "2026-11-23T10:00:00+00:00", made_by: "owner", left_out: [], archives: [] },
} as unknown as Recipe;

const machine = (id: number, name: string, status = "online", present: boolean | null = true) => ({
  machine_id: id, name, status, connected: status === "online", can_list: true, variant: "cuda", runtime: "container" as const,
  kind: "image" as const, engine_name: COPY.variants[0].image, present, listed_at: null, note: null, download: null });

function enginePage(over: Partial<EnginePage> = {}): EnginePage {
  return { recipe: { ...COPY, yaml: "name: vllm-0-34-0\n" }, can_manage: true, can_download: true,
    machines: [machine(2, "gpu-01", "online", false), machine(5, "gpu-02", "offline")], deployments: [],
    models: [{ id: 1, name: "qwen3-0.6b", title: "Qwen3 0.6B" }, { id: 4, name: "qwen3-4b", title: null }],
    pins: {}, downloads: [], trial_running: null, last_trial: null, ...over };
}

const CHECKS = [
  { id: "started", label: "Starts", value: 95.2, expected: "the built-in engine: 112 s", ok: true, note: "The first start of a new version can take longer (it may compile first).",
    codes: [{ code: "slower" }] },
  { id: "memory", label: "Memory", value: Math.round(5.17 * GIB), expected: "at most 5.54 GiB", ok: true,
    note: "Approximate: other work uses this card too, and its use changes the readings.", codes: [{ code: "approximate" }] },
  { id: "answers", label: "Answers", value: "6/6", expected: "6/6", ok: true, note: "First answer: A GPU is a processor.",
    codes: [{ code: "first_answer", text: "A GPU is a processor." }] },
  { id: "latency", label: "Latency", value: { p50_ms: 120, p95_ms: 180, warmup_ms: 900, count: 5 }, expected: null, ok: null, note: null, codes: [] },
  { id: "same_answer", label: "Same answer as the built-in engine", value: "A GPU is a processor.", expected: "chat (vllm)", ok: null,
    note: "The same.", codes: [{ code: "same" }] },
];

function trial(over: Partial<EngineTrial> & { why?: unknown } = {}): EngineTrial {
  return {
    id: 7, recipe: "vllm-0-34-0", builtin: "vllm", engine: "vllm", engine_version: "0.34.0",
    model: { id: 1, name: "qwen3-0.6b", task: "chat" }, machine: { id: 2, name: "gpu-01" }, gpus: [{ key: "nvidia:GPU-x", index: 4, model: "A100" }],
    params: { context: 4096, concurrency: 4, gpus: 1, share: "vram" }, estimate: Math.round(5.54 * GIB), device: "gpu", reserved: Math.round(5.54 * GIB),
    requested_by: "owner", created_at: "2026-11-23T15:00:00+00:00", ready_at: "2026-11-23T15:01:35+00:00", finished_at: "2026-11-23T15:02:30+00:00",
    deadline_at: "2026-11-23T15:30:00+00:00", state: "passed", phase: null,
    summary: "Passed: ready in 95 s, 6/6 answers, p50 120 ms, +5.17 GiB of 5.54 GiB reserved.", params_from: "chat",
    start: { seconds: 95.2, phases: { image: 2.1, engine: 93.1 }, source: "agent" }, measured: Math.round(5.17 * GIB), measured_peak: null,
    p50_ms: 120, p95_ms: 180, first_answer: "A GPU is a processor.", checks: CHECKS,
    comparison: { builtin: "vllm", deployment: { id: 3, name: "chat", reserved: Math.round(5.54 * GIB), start_seconds: 112, same_machine: true }, trial: null },
    events: [{ at: "2026-11-23T15:00:00+00:00", kind: "created", message: "Trial of vllm-0-34-0 with qwen3-0.6b by owner, on gpu-01." }], log_tail: null,
    ...over,
  } as EngineTrial;
}

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

const DRY = {
  estimate: { method: "safetensors-kv", device: "gpu", gpus: 1, per_gpu: Math.round(5.54 * GIB), ram: 0, amount: Math.round(5.54 * GIB), parts: [], notes: [], kv_tokens: 16384 },
  placement: { fits: true, machines: [], placed: [{ gpus: [4], variant: "cuda" }] },
  params: { context: 4096, concurrency: 4, enforce_eager: "off", gpus: 1, share: "vram" }, params_from: "chat",
  engine_on: { variant: "cuda", runtime: "container", kind: "image", engine_name: "x", present: false, listed_at: null, note: null },
  online: true, running: 0, limit: 4,
};

function engineServer(opts: { page?: EnginePage; trials?: EngineTrial[]; posts?: Record<string, unknown>[]; user?: typeof OWNER;
  detail?: EngineTrial } = {}) {
  return server((url, init) => {
    if (url === "/api/recipes/vllm-0-34-0/trials" && init?.method === "POST") {
      const b = JSON.parse(String(init.body));
      opts.posts?.push(b);
      return b.dry_run ? { status: 200, body: DRY } : { status: 201, body: { trial: trial({ state: "running", phase: "starting" }) } };
    }
    if (url === "/api/recipes/vllm-0-34-0/trials") return { status: 200, body: { trials: opts.trials ?? [], can_run: true, limit: 4 } };
    if (url.startsWith("/api/engine-trials/") && url.endsWith("/cancel")) {
      opts.posts?.push({ cancel: url });
      return { status: 200, body: { trial: trial({ state: "cancelled" }) } };
    }
    if (url.startsWith("/api/engine-trials/")) return { status: 200, body: { trial: opts.detail ?? trial(), can_run: true } };
    if (url.startsWith("/api/recipes/vllm-0-34-0")) return { status: 200, body: opts.page ?? enginePage() };
    if (url === "/api/models") return { status: 200, body: { models: [], recipes: [COPY], can_manage: true, can_deploy: true } };
    if (url === "/api/machines") return { status: 200, body: { machines: [] } };
    if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
    return undefined;
  }, opts.user ?? OWNER);
}

describe("Try it out (1.5.2, #95 PR 4)", () => {
  it("FY-WEB-887 the dialog takes the model, the machine and the deployment's parameters, shows the estimate and the image to pull, and starts the trial", async () => {
    const posts: Record<string, unknown>[] = [];
    engineServer({ posts });
    mount("/models/engines/vllm-0-34-0");
    fireEvent.click(await screen.findByRole("button", { name: "Try it out" }));
    const dlg = screen.getByRole("dialog", { name: "Try out vllm-0-34-0" });
    expect(dlg.textContent).toContain("compares it with vllm");
    expect(dlg.textContent).toContain("no API key or Playground can call it");
    await within(dlg).findByText("Taken from the deployment chat of this model, so the trial runs it as it is served now.");
    expect(posts[0]).toEqual({ model: "qwen3-0.6b", machine_id: 2, dry_run: true });
    // An offline machine is listed but cannot be chosen.
    const opt = within(dlg).getByRole("option", { name: /gpu-02/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
    expect(within(dlg).getByLabelText("Context length (tokens)")).toHaveProperty("value", "4096");
    expect(within(dlg).queryByLabelText("Eager mode")).toBeNull();          // advanced: kept as it is
    expect(within(dlg).getByLabelText("GPU")).toHaveProperty("value", "vram");
    expect(dlg.textContent).toContain("Fits on gpu-01 now, on GPU 4.");
    expect(dlg.textContent).toContain("Its image is not on gpu-01 yet");
    fireEvent.change(within(dlg).getByLabelText("Time to start"), { target: { value: "45" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Start the trial" }));
    await screen.findByText("The trial of vllm-0-34-0 started. Its progress is under Trials.");
    const sent = posts.find((p) => !p.dry_run && !("cancel" in p));
    expect(sent).toEqual({ model: "qwen3-0.6b", machine_id: 2, timeout_minutes: 45,
      params: { context: 4096, concurrency: 4, enforce_eager: "off", gpus: 1, share: "vram" } });
  });

  it("FY-WEB-888 a changed parameter is checked again; a time out of range keeps Start off; a refusal is said", async () => {
    const posts: Record<string, unknown>[] = [];
    engineServer({ posts });
    mount("/models/engines/vllm-0-34-0");
    fireEvent.click(await screen.findByRole("button", { name: "Try it out" }));
    const dlg = screen.getByRole("dialog");
    await within(dlg).findByText(/Fits on gpu-01/);
    fireEvent.change(within(dlg).getByLabelText("Context length (tokens)"), { target: { value: "8192" } });
    await waitFor(() => expect(posts.some((p) => p.dry_run && (p.params as Record<string, unknown>)?.context === 8192)).toBe(true));
    fireEvent.change(within(dlg).getByLabelText("Time to start"), { target: { value: "2" } });
    expect((within(dlg).getByRole("button", { name: "Start the trial" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("FY-WEB-889 zh-TW: the dialog and the section read in Chinese (試跑, 推論引擎, 映像檔)", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    engineServer({ trials: [trial()] });
    mount("/models/engines/vllm-0-34-0");
    fireEvent.click(await screen.findByRole("button", { name: "試跑" }));
    const dlg = screen.getByRole("dialog", { name: "試跑 vllm-0-34-0" });
    await within(dlg).findByText(/沿用這個模型的部署 chat 的參數/);
    expect(dlg.textContent).toContain("gpu-01 上還沒有它的映像檔");
    expect(dlg.textContent!.replace(/推論引擎|容器引擎/g, "")).not.toContain("引擎");
    expect(within(dlg).getByRole("button", { name: "開始試跑" })).toBeTruthy();
    fireEvent.click(within(dlg).getByRole("button", { name: "取消" }));
    expect(await screen.findByRole("heading", { name: "試跑紀錄" })).toBeTruthy();
    const row = (await screen.findByText(/95 秒就緒/)).closest("tr")!;
    expect(row.textContent).toContain("通過：95 秒就緒，回應 6/6，p50 120 毫秒，記憶體 +5.17 GiB（預留 5.54 GiB）。");
  });

  it("FY-WEB-890 the history opens a trial's checks and its comparison with the built-in engine", async () => {
    engineServer({ trials: [trial(), trial({ id: 6, state: "failed", summary: "Failed: it did not start: exited with code 1",
      checks: [{ id: "started", label: "Starts", value: null, expected: "ready", ok: false, note: "error: unknown argument" }] })] });
    mount("/models/engines/vllm-0-34-0");
    const first = (await screen.findByText("Passed: ready in 95 s, 6/6 answers, p50 120 ms, +5.17 GiB of 5.54 GiB reserved.")).closest("tr")!;
    expect(screen.getByText("Failed: it did not start: exited with code 1")).toBeTruthy();
    fireEvent.click(within(first).getByRole("button", { name: /Checks/ }));
    const checks = await screen.findByText("Same answer as the built-in");
    const t = checks.closest("table")!;
    expect(t.textContent).toContain("95 s");
    expect(t.textContent).toContain("+5.17 GiB");
    expect(t.textContent).toContain("at most 5.54 GiB reserved");
    expect(t.textContent).toContain("vllm: 112 s");
    expect(t.textContent).toContain("p50 120 ms · p95 180 ms · first 900 ms");
    expect(t.textContent).toContain("image 2.1 s · engine 93 s");
    expect(screen.getByText("Its deployment chat (on this machine): last start 112 s, reserves 5.54 GiB.")).toBeTruthy();
  });

  it("FY-WEB-891 a running trial is followed and can be cancelled; a user sees the history but no Try it out or Cancel", async () => {
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);
    const posts: Record<string, unknown>[] = [];
    const running = trial({ state: "running", phase: "asking", finished_at: null, summary: null, checks: CHECKS.slice(0, 2) });
    engineServer({ posts, trials: [running], page: enginePage({ trial_running: { id: 7, recipe: "vllm-0-34-0", model: "qwen3-0.6b",
      machine: "gpu-01", machine_id: 2, state: "running", phase: "asking", created_at: running.created_at, finished_at: null, summary: null,
      deployment_id: 9, task: "chat", workload: "trial-7-ab12" } }) });
    mount("/models/engines/vllm-0-34-0");
    await screen.findByText("Trial in progress: qwen3-0.6b on gpu-01 · sending it requests");
    expect((screen.getByRole("button", { name: "Try it out" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel the trial" }));
    await screen.findByText("The trial was cancelled.");
    expect(posts).toContainEqual({ cancel: "/api/engine-trials/7/cancel" });
    cleanup();
    engineServer({ trials: [trial()], user: USER, page: enginePage({ can_manage: false, can_download: false }) });
    mount("/models/engines/vllm-0-34-0");
    await screen.findByText(/Passed: ready in 95 s/);
    expect(screen.queryByRole("button", { name: "Try it out" })).toBeNull();
  });

  it("FY-WEB-892 the summary of a trial that stopped early, in both languages, from its reason code", () => {
    const en = makeTranslator("en");
    const zh = makeTranslator("zh-TW");
    const failed = trial({ state: "failed", summary: "Failed: out of time while it started (the limit was 30 min).", start: null, measured: null,
      p50_ms: null, checks: [] }) as EngineTrial;
    failed.why = { code: "timeout", phase: "starting", minutes: 30 };
    expect(trialSummary(en, failed)).toBe("Failed: out of time while it started (the limit was 30 min).");
    expect(trialSummary(zh, failed)).toBe("未通過：啟動時超過時限（30 分鐘）");
    const cancelled = trial({ state: "cancelled", summary: "Cancelled by olive." }) as EngineTrial;
    cancelled.why = { code: "cancelled", who: "olive" };
    expect(trialSummary(zh, cancelled)).toBe("已取消：由 olive 取消");
    const memory = trial({ state: "failed", summary: "x", checks: [{ ...CHECKS[1], ok: false }] });
    expect(trialSummary(zh, memory)).toBe("未通過（記憶體未通過）：95 秒就緒，p50 120 毫秒，記憶體 +5.17 GiB（預留 5.54 GiB）。");
  });

  it("FY-WEB-893 Serving lists the trials in progress, linked to their engine's page", async () => {
    server((url) => {
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true,
        trials: [{ id: 7, recipe: "vllm-0-34-0", model: "qwen3-0.6b", machine: "gpu-01", machine_id: 2, state: "running", phase: "starting",
          created_at: "2026-11-23T15:00:00+00:00", finished_at: null, summary: null, deployment_id: 9, task: "chat", workload: "trial-7-ab12" }] } };
      if (url === "/api/models") return { status: 200, body: { models: [], recipes: [], can_manage: true, can_deploy: true } };
      if (url === "/api/machines") return { status: 200, body: { machines: [] } };
      if (url === "/api/routes") return { status: 200, body: { routes: [] } };
      return undefined;
    });
    mount("/serving");
    const link = await screen.findByRole("link", { name: "vllm-0-34-0 with qwen3-0.6b on gpu-01 · starting" });
    expect(link.getAttribute("href")).toBe("/models/engines/vllm-0-34-0#trials");
    expect(screen.getByText("A trial's temporary deployment is not served: no API key or Playground can call it.")).toBeTruthy();
  });

  it("FY-WEB-894 a machine names a trial's replica as the trial (linked to the engine), never as an app", () => {
    const w = { id: 21, name: "trial-7-ab12", kind: "service", runtime: "model", replica_list: [{ ordinal: 0, machine: { id: 2, name: "gpu-01" },
      final: false, state: "healthy", gpus: [{ key: "nvidia:GPU-x", index: 4 }], vram: 5 * GIB }] } as unknown as Workload;
    const brief: TrialBrief = { id: 7, recipe: "vllm-0-34-0", model: "qwen3-0.6b", machine: "gpu-01", machine_id: 2, state: "running",
      phase: "asking", created_at: "", finished_at: null, summary: null, deployment_id: 9, task: "chat", workload: "trial-7-ab12" };
    const p = placement([w], [], [brief]);
    expect(p.holders).toHaveLength(1);
    const h = p.holders[0];
    expect([h.name, h.href, h.key, h.reserved, h.trial?.id]).toEqual(["Trial of vllm-0-34-0", "/models/engines/vllm-0-34-0#trials", "trial:7", 5 * GIB, 7]);
    expect(placement([w], [], []).holders[0].key).toBe("app:21");
  });

  it("FY-WEB-895 Edit names the chosen engine's last trial (or that it was never tried), never as a gate", () => {
    const last: TrialBrief = { id: 7, recipe: "vllm-0-34-0", model: "qwen3-0.6b", machine: "gpu-01", machine_id: 2, state: "passed", phase: null,
      created_at: "2026-11-23T15:00:00+00:00", finished_at: "2026-11-23T15:02:30+00:00", summary: "Passed", deployment_id: null, task: "chat", workload: null };
    const r = render(<MemoryRouter><LastTrialLine name="vllm-0-34-0" builtin={false} last={last} /></MemoryRouter>);
    expect(r.container.textContent).toMatch(/^Last tried out on gpu-01 with qwen3-0.6b \(.+\): Passed\.$/);
    expect(screen.getByRole("link", { name: "Passed" }).getAttribute("href")).toBe("/models/engines/vllm-0-34-0#trials");
    cleanup();
    const n = render(<MemoryRouter><LastTrialLine name="vllm-0-34-0" builtin={false} last={null} /></MemoryRouter>);
    expect(n.container.textContent).toBe("vllm-0-34-0 has not been tried out yet. You can try it out on its engine page first.");
    cleanup();
    const b = render(<MemoryRouter><LastTrialLine name="vllm" builtin last={null} /></MemoryRouter>);
    expect(b.container.textContent).toBe("");
  });

  it("FY-WEB-896 memory that could not be measured is said in words, never as a number, in both languages", () => {
    const en = makeTranslator("en");
    const zh = makeTranslator("zh-TW");
    const tr = trial({ measured: null, memory_unmeasured: "went_down", summary: null,
      checks: [CHECKS[0], { id: "memory", label: "Memory", value: null, expected: "x", ok: null, note: "n", codes: [{ code: "went_down" }] }, CHECKS[2]],
      comparison: { builtin: "vllm", deployment: null, trial: { id: 2, machine: "gpu-01", finished_at: null, start_seconds: 105, measured: null,
        memory_unmeasured: "went_down", reserved: 5 * GIB, p50_ms: 30, p95_ms: 40 } } });
    expect(trialSummary(en, tr)).toBe("Passed: ready in 95 s, 6/6 answers, p50 120 ms, memory not measured: other work on the card changed during the trial.");
    expect(trialSummary(zh, tr)).toBe("通過：95 秒就緒，回應 6/6，p50 120 毫秒，記憶體未量測：試跑期間這張卡上其他工作的用量有變化。");
    const ram = trial({ measured: null, memory_unmeasured: "no_readings", summary: null, checks: [] });
    expect(trialSummary(zh, ram)).toBe("通過：95 秒就緒，p50 120 毫秒，記憶體未量測：機器沒有及時傳來讀數。");
    for (const s of [trialSummary(en, tr), trialSummary(zh, tr)]) expect(s).not.toMatch(/\+-|GiB/);
  });

  it("FY-WEB-897 the checks table and the comparison show no number for memory that was not measured", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    const detail = trial({ measured: null, memory_unmeasured: "went_down",
      checks: [CHECKS[0], { id: "memory", label: "Memory", value: null, expected: "x", ok: null, note: "Not measured: …", codes: [{ code: "went_down" }] }],
      comparison: { builtin: "vllm", deployment: null, trial: { id: 2, machine: "gpu-01", finished_at: "2026-11-23T15:00:00+00:00", start_seconds: 105,
        measured: null, memory_unmeasured: "went_down", reserved: 5 * GIB, p50_ms: 30, p95_ms: 40 } } });
    engineServer({ trials: [detail], detail });
    mount("/models/engines/vllm-0-34-0");
    const row = (await screen.findByText(/記憶體未量測/)).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /檢查項目/ }));
    const note = await screen.findByText("無法量測：試跑期間，這張卡上其他工作的用量有變化（反而下降），所以這個差值無法反映這個推論引擎的用量。");
    const memRow = note.closest("tr")!;
    expect(memRow.textContent).not.toMatch(/\+-|-0\.|GiB預留|^\+/);
    expect(within(memRow).getByText("未檢查")).toBeTruthy();
    expect(screen.getByText(/它在 gpu-01 上的試跑（.+）：105 秒就緒，記憶體未量測，p50 30 毫秒。/)).toBeTruthy();
  });

  it("FY-WEB-898 a trial recorded before this rule (a negative difference stored as measured) still shows no number", async () => {
    const zh = makeTranslator("zh-TW");
    const old = trial({ measured: -27262976, summary: "Passed: ready in 92 s, 6/6 answers, p50 120 ms, +-0.03 GiB of 5.54 GiB reserved.",
      checks: [CHECKS[0], { id: "memory", label: "Memory", value: -27262976, expected: "x", ok: null, note: "n", codes: [{ code: "went_down" }] }] });
    expect(trialSummary(zh, old)).toBe("通過：95 秒就緒，p50 120 毫秒。");
    localStorage.setItem(LANG_KEY, "zh-TW");
    engineServer({ trials: [old], detail: old });
    mount("/models/engines/vllm-0-34-0");
    const row = (await screen.findByText("通過：95 秒就緒，p50 120 毫秒。")).closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: /檢查項目/ }));
    const memRow = (await screen.findByText("記憶體")).closest("tr")!;
    expect(memRow.textContent).not.toMatch(/-26|-0\.0|MiB/);
  });
});
