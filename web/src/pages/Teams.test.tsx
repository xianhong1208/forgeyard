// @vitest-environment jsdom
// Teams (1.6.1, #190): the Teams page and a team's page for admins, managers and members; Teams in the menu only for
// admins and people in a team; team keys on the API keys page; a team's usage; a deployment's team on its Change page,
// and what a team's manager (not an operator) may change there.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import { rowName } from "../lib/charts";
import type { CatalogResponse, Deployment, EditResponse, Team } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const MIA = { ...OWNER, id: 2, username: "mia", role: "user", is_owner: false };
const OLA = { ...OWNER, id: 3, username: "ola", role: "operator", is_owner: false };
type Reply = { status: number; body: unknown };

function server(user: Record<string, unknown>, route: (url: string, init?: RequestInit) => Reply | undefined) {
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
  return <div data-testid="where">{l.pathname}{l.search}</div>;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /><Where /></AuthProvider></MemoryRouter>);
}

const NO_BUDGET = { budget_micros: null, spent_micros: 0, reserved_micros: 0, used: null, level: null };
const team = (over: Partial<Team> = {}): Team => ({
  id: 4, name: "ml", title: "Machine learning", created_by: "owner", created_at: "2026-11-26T08:00:00Z", my_role: "manager", can_manage: true,
  members: 2, managers: 1, keys: 1, deployments: 1, budget: { budget_micros: 5_000_000, spent_micros: 1_250_000, reserved_micros: 0, used: 0.25, level: "ok" },
  ...over,
});
const page = (over: Partial<Team> = {}): Team => team({
  member_list: [{ username: "mia", role: "manager", status: "active", added_by: "owner", added_at: "2026-11-26T08:00:00Z" },
    { username: "max", role: "member", status: "active", added_by: "mia", added_at: "2026-11-26T08:05:00Z" }],
  key_list: [{ id: "k1", name: "ml-service", prefix: "fyk_abcd1234", status: "active", created_by: "mia", created_at: "2026-11-26T08:10:00Z", last_used_at: null },
    { id: "k2", name: "old", prefix: "fyk_zzzz0000", status: "revoked", created_by: "mia", created_at: "2026-11-20T08:10:00Z", last_used_at: null }],
  deployment_list: [{ id: 3, name: "chat", served_name: "qwen3-4b" }],
  ...over,
});
const teams = (list: Team[], admin = false) => ({ teams: list, is_admin: admin, month: "2026-11", resets_at: "2026-12-01T00:00:00Z" });
const sent = (fetch: ReturnType<typeof server>, method: string) =>
  fetch.mock.calls.filter(([, i]) => i?.method === method).map(([u, i]) => ({ url: u as string, body: i?.body ? JSON.parse(String(i.body)) : null }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("teams", () => {
  it("FY-WEB-1036 an admin lists every team and makes a new one; with none the page says keys work as before", async () => {
    let list: Team[] = [];
    const fetch = server(OWNER, (url, init) => {
      if (url === "/api/teams" && init?.method === "POST") {
        list = [team({ my_role: null })];
        return { status: 201, body: { team: list[0] } };
      }
      if (url === "/api/teams") return { status: 200, body: teams(list, true) };
      return undefined;
    });
    mount("/teams");
    expect(await screen.findByText(/No teams yet/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New team" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ml" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Machine learning" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Make team" })));
    expect(sent(fetch, "POST")).toEqual([{ url: "/api/teams", body: { name: "ml", title: "Machine learning" } }]);
    expect(await screen.findByText("Team Machine learning made. Add its people on its page.")).toBeTruthy();
    const row = (await screen.findByRole("link", { name: "Machine learning" })).closest("tr")!;
    expect(within(row).getByText("2 people, 1 managing")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Machine learning" }).getAttribute("href")).toBe("/teams/ml");
  });

  it("FY-WEB-1037 a manager's team page: members, adding someone as a member, removing members (not managers), its keys", async () => {
    const fetch = server(MIA, (url, init) => {
      if (url.startsWith("/api/teams/ml/members/") && init?.method) return { status: 200, body: { ok: true } };
      if (url === "/api/teams/ml") return { status: 200, body: { team: page(), month: "2026-11", resets_at: "2026-12-01T00:00:00Z" } };
      if (url === "/api/teams") return { status: 200, body: teams([team()]) };
      return undefined;
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/teams/ml");
    expect(await screen.findByRole("heading", { name: "Machine learning" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete team" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change team" })).toBeNull();
    const members = screen.getByRole("region", { name: "Members" });
    const mia = within(members).getByText("mia").closest("tr")!;
    const max = within(members).getByText("max").closest("tr")!;
    expect(within(mia).queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.queryByLabelText("Team role of max")).toBeNull();          // only admins change roles
    await act(async () => fireEvent.click(within(max).getByRole("button", { name: "Remove" })));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "Uwe" } });
    expect(screen.queryByLabelText("Team role")).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Add" })));
    expect(sent(fetch, "DELETE").map((x) => x.url)).toEqual(["/api/teams/ml/members/max"]);
    expect(sent(fetch, "PUT")).toEqual([{ url: "/api/teams/ml/members/uwe", body: { role: "member" } }]);
    const keys = screen.getByRole("region", { name: "Team keys" });
    expect(within(keys).getByText("ml-service")).toBeTruthy();
    expect(within(keys).queryByText("old")).toBeNull();
    expect(within(keys).getByText("And 1 revoked or expired key.")).toBeTruthy();
    expect(within(keys).getByRole("link", { name: "Make a team key" }).getAttribute("href")).toBe("/keys?team=ml");
    expect(screen.getByRole("link", { name: "chat" }).getAttribute("href")).toBe("/serving/3");
  });

  it("FY-WEB-1038 an admin chooses managers and sees why a team with working keys is not deleted", async () => {
    const fetch = server(OWNER, (url, init) => {
      if (url === "/api/teams/ml" && init?.method === "DELETE") {
        return { status: 409, body: { detail: { code: "team_has_keys", message: "1 of the team's API keys still work. Revoke them first.", keys: 1 } } };
      }
      if (url.startsWith("/api/teams/ml/members/") && init?.method === "PUT") return { status: 200, body: {} };
      if (url === "/api/teams/ml") return { status: 200, body: { team: page({ my_role: null }), month: "2026-11", resets_at: "2026-12-01T00:00:00Z" } };
      if (url === "/api/advisor") return { status: 200, body: { findings: [], snoozed: [], counts: { error: 0, warning: 0, info: 0 }, badge: 0, snooze_days: 30, checked_at: "" } };
      return undefined;
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount("/teams/ml");
    const role = await screen.findByLabelText("Team role of max");
    await act(async () => fireEvent.change(role, { target: { value: "manager" } }));
    expect(sent(fetch, "PUT")).toEqual([{ url: "/api/teams/ml/members/max", body: { role: "manager" } }]);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Delete team" })));
    expect(await screen.findByText(/still work. Revoke them first/)).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/teams/ml");
  });

  it("FY-WEB-1039 Teams is in the menu for admins always, for others only once they can see a team", async () => {
    server(MIA, (url) => (url === "/api/teams" ? { status: 200, body: teams([]) } : undefined));
    mount("/machines");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(nav).getByRole("link", { name: "Usage" })).toBeTruthy());
    expect(within(nav).queryByRole("link", { name: "Teams" })).toBeNull();
    cleanup();
    server(MIA, (url) => (url === "/api/teams" ? { status: 200, body: teams([team({ my_role: "member", can_manage: false })]) } : undefined));
    mount("/machines");
    const nav2 = await screen.findByRole("navigation", { name: "Main" });
    expect((await within(nav2).findByRole("link", { name: "Teams" })).getAttribute("href")).toBe("/teams");
    cleanup();
    const fetch = server(OWNER, () => undefined);
    mount("/machines");
    const nav3 = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav3).getByRole("link", { name: "Teams" })).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => u === "/api/teams")).toBe(false);     // admins need not ask
  });
});

const KEYS = {
  keys: [{ id: "k1", name: "ml-service", prefix: "fyk_abcd1234", models: null, rpm: null, tpm: null, cloud: "off", effective_rpm: null, effective_tpm: null,
    expires_at: null, created_at: "2026-11-26T08:10:00Z", last_used_at: null, revoked_at: null, revoked_by: null, status: "active",
    user: { id: null, username: null }, team: { id: 4, name: "ml", title: "Machine learning" }, created_by: "mia", this_minute: { requests: 0, tokens: 0 } }],
  limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null }, is_admin: false, gateway_url: "https://fy.lan:7860/v1",
  cloud: { default: "off", max: "off" }, models: [{ name: "qwen3-4b", api: "openai-chat" }], month: "2026-11", resets_at: "2026-12-01T00:00:00Z",
  person_budget: NO_BUDGET, teams: [{ id: 4, name: "ml", title: "Machine learning" }],
};

describe("team keys and usage", () => {
  it("FY-WEB-1040 a manager makes a key for the team (chosen from a team's page), and the team's keys say whose they are", async () => {
    const fetch = server(MIA, (url, init) => {
      if (url === "/api/keys" && init?.method === "POST") {
        return { status: 201, body: { key: "fyk_secret", api_key: { ...KEYS.keys[0], name: "batch" }, gateway_url: KEYS.gateway_url } };
      }
      if (url === "/api/keys") return { status: 200, body: KEYS };
      if (url === "/api/teams") return { status: 200, body: teams([team()]) };
      return undefined;
    });
    mount("/keys?team=ml");
    const owner = await screen.findByLabelText("Belongs to");
    expect((owner as HTMLSelectElement).value).toBe("ml");
    expect(within(owner).getByRole("option", { name: "You (a personal key)" })).toBeTruthy();
    const row = screen.getByText("ml-service").closest("tr")!;
    expect(within(row).getByText("Team Machine learning")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "batch" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create key" })));
    const [made] = sent(fetch, "POST");
    expect(made.url).toBe("/api/keys");
    expect(made.body.team).toBe("ml");
    cleanup();
    const fetch2 = server(MIA, (url, init) => {
      if (url === "/api/keys" && init?.method === "POST") return { status: 201, body: { key: "fyk_x", api_key: KEYS.keys[0], gateway_url: KEYS.gateway_url } };
      if (url === "/api/keys") return { status: 200, body: { ...KEYS, keys: [], teams: [] } };
      return undefined;
    });
    mount("/keys");
    fireEvent.click(await screen.findByRole("button", { name: "New key" }));
    expect(screen.queryByLabelText("Belongs to")).toBeNull();              // no team to make keys for: as before
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "laptop" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create key" })));
    expect("team" in sent(fetch2, "POST")[0].body).toBe(false);
  });

  it("FY-WEB-1041 a manager picks a team on the Usage page: its usage is asked with ?team= and the lead names it", async () => {
    const usage = (teamOn: boolean) => ({ from: "2026-11-20", to: "2026-11-26", days: 7, group_by: "model", everyone: false, is_admin: false,
      total: { id: "total", name: "Total", requests: teamOn ? 2 : 5, errors: 0, limited: 0, prompt_tokens: 0, completion_tokens: 0, tokens: 0,
        latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null },
      rows: [], team: teamOn ? { id: 4, name: "ml", title: "Machine learning" } : null, teams: [{ id: 4, name: "ml", title: "Machine learning" }] });
    const fetch = server(MIA, (url) => {
      if (url.startsWith("/api/usage/series")) return { status: 200, body: {} };
      if (url.startsWith("/api/usage")) return { status: 200, body: usage(url.includes("team=ml")) };
      if (url === "/api/teams") return { status: 200, body: teams([team()]) };
      return undefined;
    });
    mount("/usage");
    const pick = await screen.findByLabelText("Team");
    expect(screen.getByText("Your requests through the gateway, with your API keys and in the playground.")).toBeTruthy();
    await act(async () => fireEvent.change(pick, { target: { value: "ml" } }));
    expect(await screen.findByText("Requests made with the API keys of the team Machine learning.")).toBeTruthy();
    const asked = fetch.mock.calls.map(([u]) => u as string);
    expect(asked.some((u) => u.startsWith("/api/usage?") && u.includes("&team=ml"))).toBe(true);
    expect(asked.some((u) => u.startsWith("/api/usage/series?") && u.includes("&team=ml"))).toBe(true);
  });
});

// ------------------------------------------------------------------ a deployment's team
const GIB = 1024 ** 3;
const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 15 * GIB, ram: 0, amount: 15 * GIB, kv_tokens: 32768, notes: [], parts: [] };
const dep = (over: Partial<Deployment> = {}): Deployment => ({
  id: 3, name: "chat", recipe: "vllm", served_name: "qwen3-4b", params: { context: 8192, gpus: 1, share: "exclusive" },
  model: { id: 1, name: "qwen3-4b", source: "hf://Qwen/Qwen3-4B", task: "chat", format: "safetensors", revision: "1cfa", file: null, size_bytes: 8 * GIB },
  estimate, has_token: false, created_by: "owner", created_at: "2026-11-10T10:00:00Z",
  route: { served_name: "qwen3-4b", api: "openai-chat", upstream_model: "qwen3-4b" }, endpoints: [], cards: [], pinned: null, rollout: null,
  engines: [{ name: "vllm", title: "vLLM", version: "0.32.0", builtin: true, based_on: null, last_trial: null }, { name: "vllm-0-34", title: "vLLM 0.34.0", version: "0.34.0", builtin: false, based_on: "vllm", last_trial: null }],
  workload: { id: 11, name: "chat", kind: "service", runtime: "model", desired_state: "running", generation: 1, created_by: "owner",
    created_at: "2026-11-10T10:00:00Z", updated_at: "2026-11-10T10:00:00Z", status: "running", ready: 1, replicas: 1,
    spec: { name: "chat", kind: "service", runtime: { type: "model", recipe: "vllm", model: "qwen3-4b" }, env: {},
      resources: { cpu: null, memory: null, gpus: { count: 0, indices: null, vendor: null } }, ports: [], health: null, restart: "always",
      max_duration: null, retries: 0, retry_backoff: 5, replicas: 1, machine_id: null }, replica_list: [] },
  team: { id: 4, name: "ml", title: "Machine learning" }, can_manage: true, ...over,
} as Deployment);
const catalog = {
  can_manage: false, can_deploy: false,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa", file: null, format: "safetensors",
    task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm"], deployments: 1, created_by: "owner", created_at: "2026-11-10T09:00:00Z",
    summary: { params: 4e9, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" }, gguf_files: [] }],
  recipes: [{ name: "vllm", title: "vLLM", description: "", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"], api: { chat: "openai-chat" },
    share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"],
    params: { context: { type: "int", default: 8192, min: 256, max: 1048576, label: "Context length (tokens)" } },
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: null, driver: null, engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false },
} as unknown as CatalogResponse;
const plan = (b: { params: Record<string, unknown>; team?: string | null }): EditResponse => ({
  estimate, params: b.params, placement: { fits: true, machines: [], share_hint: null, placed: [] },
  changes: "team" in b ? { team: { before: "ml", after: b.team ?? null } } : {}, restart: "none", restart_text: "Nothing restarts.",
});

function deploymentServer(user: Record<string, unknown>, operator: boolean) {
  return server(user, (url, init) => {
    if (url === "/api/deployments/3" && init?.method === "PATCH") {
      const b = JSON.parse(String(init.body));
      return { status: 200, body: b.dry_run ? plan(b) : { ...plan(b), deployment: dep() } };
    }
    if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep(), events: [], server_time: "", can_manage: operator } };
    if (url === "/api/models") return { status: 200, body: catalog };
    if (url === "/api/machines") return { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: operator } };
    if (url === "/api/teams") return { status: 200, body: teams([team({ my_role: null, can_manage: false }), team({ id: 5, name: "ops", title: "Operations", my_role: null })]) };
    return undefined;
  });
}

describe("a deployment's team", () => {
  it("FY-WEB-1042 a team's manager changes the deployment but not its engine, extra arguments or team; Delete stays with operators", async () => {
    deploymentServer(MIA, false);
    mount("/serving/3");
    expect(await screen.findByRole("link", { name: "Team Machine learning" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Edit" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    cleanup();
    deploymentServer(MIA, false);
    mount("/serving/3/edit");
    expect(await screen.findByText(/You manage this deployment as a manager of the team Machine learning/)).toBeTruthy();
    expect(screen.getByLabelText("Context length (tokens)")).toBeTruthy();
    expect(screen.queryByLabelText("Engine version")).toBeNull();
    expect(screen.queryByLabelText("Extra engine arguments")).toBeNull();
    expect(screen.queryByLabelText("Team")).toBeNull();
  });

  it("FY-WEB-1043 an operator moves a deployment to another team or to none: the change is listed and saved with the PATCH", async () => {
    const fetch = deploymentServer(OLA, true);
    mount("/serving/3/edit");
    const pick = await screen.findByLabelText("Team");
    expect((pick as HTMLSelectElement).value).toBe("ml");
    fireEvent.change(pick, { target: { value: "" } });
    const changes = await screen.findByRole("list", { name: "Changes" }, { timeout: 3000 });
    expect(within(changes).getByText("No team")).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
    const saved = sent(fetch, "PATCH").map((x) => x.body).find((b) => !b.dry_run);
    expect(saved.team).toBeNull();
  });
});

describe("a deployment's team and GPU hours", () => {
  it("FY-WEB-1046 the Advanced summary of the Change page names engine arguments only to those who may change them", async () => {
    deploymentServer(MIA, false);
    mount("/serving/3/edit");
    const manager = await screen.findByText(/^Advanced: /);
    expect(manager.textContent).toBe("Advanced: memory override, first-answer wait, wake wait");
    cleanup();
    deploymentServer(OLA, true);
    mount("/serving/3/edit");
    expect((await screen.findByText(/^Advanced: /)).textContent).toBe("Advanced: memory override, engine arguments, first-answer wait, wake wait");
  });

  it("FY-WEB-1047 a deployment's page shows its GPU hours this month (one decimal) and its busy share; a CPU one its RAM", async () => {
    const gpu = { month: "2026-11", gpu_ms: 600_000, ram_mib_ms: 0, busy_ms: 150_000, cpu_busy_ms: 0, busy_share: 0.25, counting_since: "2026-11-26T06:00:00Z" };
    const total = { id: "total", name: "Total", requests: 3, errors: 0, limited: 0, prompt_tokens: 0, completion_tokens: 0, tokens: 0,
      latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null };
    const serve = (usage: Deployment["usage"]) => server(OLA, (url) => {
      if (url === "/api/deployments/3") return { status: 200, body: { deployment: dep({ usage }), events: [], server_time: "", can_manage: true } };
      return undefined;
    });
    serve({ days: 7, total, by_day: [], gpu });
    mount("/serving/3");
    const hours = await screen.findByText("GPU hours, 2026-11");
    expect(hours.nextElementSibling?.textContent).toBe("0.2");
    expect(screen.getByText("Busy").nextElementSibling?.textContent).toBe("25%");
    cleanup();
    serve({ days: 7, total, by_day: [], gpu: { ...gpu, gpu_ms: 0, busy_ms: 0, busy_share: null, ram_mib_ms: 2048 * 3_600_000 } });
    mount("/serving/3");
    expect((await screen.findByText("RAM, 2026-11")).nextElementSibling?.textContent).toBe("2.0 GiB·h");
    expect(screen.queryByText("GPU hours, 2026-11")).toBeNull();
  });
});

describe("teams in zh-TW", () => {
  it("FY-WEB-1044 the Teams page and a team's roles read in Traditional Chinese (團隊, 管理者, 成員)", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(MIA, (url) => {
      if (url === "/api/teams/ml") return { status: 200, body: { team: page(), month: "2026-11", resets_at: "2026-12-01T00:00:00Z" } };
      if (url === "/api/teams") return { status: 200, body: teams([team()]) };
      return undefined;
    });
    mount("/teams");
    const row = (await screen.findByRole("link", { name: "Machine learning" })).closest("tr")!;
    expect(within(row).getByText("管理者")).toBeTruthy();
    expect(within(row).getByText("2 人，1 位管理者")).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "可用的金鑰" })).toBeTruthy();
    cleanup();
    server(MIA, (url) => {
      if (url === "/api/teams/ml") return { status: 200, body: { team: page(), month: "2026-11", resets_at: "2026-12-01T00:00:00Z" } };
      if (url === "/api/teams") return { status: 200, body: teams([team()]) };
      return undefined;
    });
    mount("/teams/ml");
    expect(await screen.findByRole("heading", { name: "成員" })).toBeTruthy();
    expect(screen.getAllByText("成員").length).toBeGreaterThan(1);
    expect(screen.getByRole("heading", { name: "團隊金鑰" })).toBeTruthy();
    expect(screen.getByText("另外還有 1 把已撤銷或已過期的金鑰。")).toBeTruthy();
    // The names the server gives the Usage page's team rows.
    expect(rowName("none", "No team", "zh-TW")).toBe("不屬於任何團隊");
    expect(rowName("team:4", "team key (ML)", "zh-TW")).toBe("團隊金鑰（ML）");
    expect(rowName("7", "deleted team #7", "zh-TW")).toBe("已刪除的團隊 #7");
    expect(rowName("none", "No team", "en")).toBe("No team");
  });
});
