// @vitest-environment jsdom
// 1.3.1 settings-simplify: the cloud policy (D3), values from .env (D1), the server's Hugging Face token
// (D2), where a provider is (D4), the context hint (D9) and the user forms without email (D11).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { CatalogResponse, EstimateResponse, KeysResponse, ProvidersResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, state: Record<string, unknown> = {}) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER, ...state } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const keys = (over: Partial<KeysResponse> = {}): KeysResponse => ({
  keys: [], limits: { default_rpm: null, default_tpm: null, max_rpm: null, max_tpm: null }, is_admin: true,
  gateway_url: "https://forge.example.org:7860/v1", models: [], cloud: { default: "off", max: "off" },
  env_seeds: { limits: [], cloud: [] }, ...over,
});

const bodies = (fetch: ReturnType<typeof server>, url: string, method: string) =>
  fetch.mock.calls.filter(([u, i]) => u === url && i?.method === method).map(([, i]) => JSON.parse(String(i?.body)));

describe("cloud policy (D3)", () => {
  it("FY-WEB-209 one select with four presets saves the two stored values; Custom shows both selects", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/keys/cloud" && init?.method === "PUT") return { status: 200, body: { cloud: JSON.parse(String(init.body)) } };
      if (url.startsWith("/api/keys")) return { status: 200, body: keys() };
    });
    mount("/keys");
    const select = await screen.findByLabelText("Policy") as HTMLSelectElement;
    expect(select.value).toBe("none");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(
      ["No cloud", "People may opt in to spill", "Spill by default", "Cloud allowed", "Custom"]);
    expect(screen.queryByLabelText("New keys get")).toBeNull();  // presets hide the two selects
    fireEvent.change(select, { target: { value: "allowed" } });
    expect(screen.getByText(/people may set their own keys to Allowed/)).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save cloud policy" })));
    fireEvent.change(select, { target: { value: "opt-in" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save cloud policy" })));
    fireEvent.change(select, { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("The most people may give their own keys"), { target: { value: "on" } });
    expect((screen.getByLabelText("New keys get") as HTMLSelectElement).value).toBe("off");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save cloud policy" })));
    await waitFor(() => expect(bodies(fetch, "/api/keys/cloud", "PUT")).toEqual([
      { default: "fallback", max: "on" }, { default: "off", max: "fallback" }, { default: "off", max: "on" }]));
  });

  it("FY-WEB-210 a stored pair that is no preset opens as Custom, and values from .env are marked", async () => {
    server((url) => url.startsWith("/api/keys") ? { status: 200, body: keys({ cloud: { default: "off", max: "on" },
      env_seeds: { limits: ["GATEWAY_MAX_RPM"], cloud: ["GATEWAY_CLOUD_MAX"] } }) } : undefined);
    mount("/keys");
    expect(((await screen.findByLabelText("Policy")) as HTMLSelectElement).value).toBe("custom");
    expect((screen.getByLabelText("The most people may give their own keys") as HTMLSelectElement).value).toBe("on");
    const notes = screen.getAllByRole("note");
    expect(notes.map((n) => n.textContent)).toEqual([expect.stringContaining("GATEWAY_MAX_RPM"), expect.stringContaining("GATEWAY_CLOUD_MAX")]);
    expect(notes.every((n) => /from \.env/.test(n.textContent ?? ""))).toBe(true);
  });
});

const KINDS = [
  { kind: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1", base_url_hint: "", auth: "bearer", embeddings: true, models_endpoint: true, default_external: true, key_required: true, note: "" },
  { kind: "openai_compatible", label: "OpenAI-compatible server", base_url: "", base_url_hint: "https://llm.example.org/v1", auth: "bearer", embeddings: true, models_endpoint: true, default_external: false, key_required: false, note: "" },
];
const EGRESS = { proxy: false, allowed_hosts: [], ca_file: false, private_hosts: false, cooldown_s: 30, read_timeout_s: 300 };

describe("provider: outside or inside your network (D4)", () => {
  it("FY-WEB-211 the tick follows the server's suggestion and says why, until the admin changes it", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/providers/where") {
        const b = JSON.parse(String(init?.body));
        if (b.kind === "openai") return { status: 200, body: { external: true, basis: "kind", reason: "OpenAI is a cloud service." } };
        if ((b.base_url ?? "").includes("203.0.113.7")) return { status: 200, body: { external: true, basis: "address", reason: "203.0.113.7 is a public address." } };
        if ((b.base_url ?? "").includes("192.168.1.30")) return { status: 200, body: { external: false, basis: "address", reason: "192.168.1.30 is a private address." } };
        return { status: 200, body: { external: false, basis: "unknown", reason: "Enter the base URL." } };
      }
      if (url === "/api/providers" && init?.method === "POST") return { status: 201, body: { provider: { id: 9, name: "x", models: [] } } };
      if (url === "/api/providers") return { status: 200, body: { providers: [], kinds: KINDS, egress: EGRESS, is_admin: true } satisfies ProvidersResponse };
    });
    mount("/providers");
    fireEvent.click(await screen.findByRole("button", { name: /Add a provider/ }));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Kind of provider" })).getByLabelText("OpenAI-compatible server"));
    const box = () => screen.getByRole("checkbox", { name: /Outside your network/ }) as HTMLInputElement;
    const base = screen.getByLabelText("Base URL");
    fireEvent.change(base, { target: { value: "https://203.0.113.7/v1" } });
    expect(await screen.findByText(/203.0.113.7 is a public address/, {}, { timeout: 3000 })).toBeTruthy();
    expect(box().checked).toBe(true);
    fireEvent.change(base, { target: { value: "http://192.168.1.30:8000/v1" } });
    expect(await screen.findByText(/192.168.1.30 is a private address/, {}, { timeout: 3000 })).toBeTruthy();
    expect(box().checked).toBe(false);
    expect(screen.getByRole("status", { name: "Why this is ticked" }).textContent).toContain("inside");
    fireEvent.click(box());  // the admin knows better
    expect(box().checked).toBe(true);
    expect(screen.getByText(/You changed this/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
    await waitFor(() => expect(bodies(fetch, "/api/providers", "POST")[0]).toMatchObject({ kind: "openai_compatible", external: true }));
  });
});

const GIB = 1024 ** 3;
const catalog = (over: Partial<CatalogResponse> = {}): CatalogResponse => ({
  can_manage: true, can_deploy: true, hf_token_set: false,
  models: [{ id: 1, name: "qwen3-4b", title: "Qwen3 4B", source: "hf://Qwen/Qwen3-4B", kind: "hf", ref: "main", revision: "1cfa9a72", file: null,
    format: "safetensors", task: "chat", task_label: "Chat", size_bytes: 8 * GIB, engine_args: {}, recipes: ["vllm"], deployments: 0,
    created_by: "owner", created_at: "2026-11-10T09:00:00Z", summary: { params: 4022468096, layers: 36, kv_heads: 8, max_context: 40960, architecture: "Qwen3ForCausalLM" },
    gguf_files: [] }],
  recipes: [{ name: "vllm", title: "vLLM", description: "Chat models", engine: "vllm", version: "0.32.0", tasks: ["chat"], formats: ["safetensors"],
    api: { chat: "openai-chat" }, share: "exclusive", builtin: true, estimate: "safetensors-kv", devices: ["gpu"],
    params: { context: { type: "int", default: 8192, min: 256, max: 1048576, label: "Context length (tokens)" },
      concurrency: { type: "int", default: 4, min: 1, label: "Concurrent requests" } },
    variants: [{ name: "cuda", vendor: "nvidia", runtime: "container", image: "vllm/vllm-openai:v0.32.0", arch: null, compute_capability: ">=7.0", driver: ">=580", engine: null }] }],
  sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false,
    env_seeds: [], hf_token: { set: false, last4: null, updated_by: null, updated_at: null } },
  ...over,
});
const estimate = { method: "safetensors-kv", device: "gpu" as const, gpus: 1, per_gpu: 14 * GIB, ram: 0, amount: 14 * GIB, kv_tokens: 32768, notes: [],
  parts: [{ label: "Weights", bytes: 8 * GIB, detail: "" }] };

describe("deploy form: context hint (D9) and the token (D2)", () => {
  it("FY-WEB-212 the context keeps its default; model max and the largest that fits are one click each", async () => {
    const fetch = server((url) => {
      if (url === "/api/models") return { status: 200, body: catalog({ hf_token_set: true }) };
      if (url === "/api/machines") return { status: 200, body: { machines: [], server_time: "", heartbeat_interval: 10, offline_after: 30, can_manage: true } };
      if (url === "/api/deployments/estimate") {
        const r: EstimateResponse = { estimate, params: {}, recipe: "vllm", model: "qwen3-4b",
          placement: { fits: true, machines: [], placed: [] }, context_hint: { param: "context", model_max: 40960, fits_max: 32768 } };
        return { status: 200, body: r };
      }
    });
    mount("/models/deploy?model=qwen3-4b");
    const input = await screen.findByLabelText("Context length (tokens)") as HTMLInputElement;
    expect(input.value).toBe("8192");
    const hint = await screen.findByLabelText("Context hint", {}, { timeout: 3000 });
    expect(hint.textContent).toBe("model max 40,960·largest that fits here 32,768");
    fireEvent.click(within(hint).getByRole("button", { name: /the largest that fits: 32,768/ }));
    expect(input.value).toBe("32768");
    fireEvent.click(within(hint).getByRole("button", { name: /the model's maximum: 40,960/ }));
    expect(input.value).toBe("40960");
    await waitFor(() => expect(bodies(fetch, "/api/deployments/estimate", "POST").at(-1).params.context).toBe(40960), { timeout: 3000 });
    expect((screen.getByLabelText("Hugging Face token") as HTMLInputElement).placeholder).toBe("the server's token");
    expect(screen.getByText(/one here replaces it for this deployment only/)).toBeTruthy();
  });
});

describe("the server's Hugging Face token (D2)", () => {
  it("FY-WEB-213 an admin sets and removes it; only its last four characters ever show", async () => {
    let info = { set: false, last4: null as string | null, updated_by: null as string | null, updated_at: null as string | null };
    const fetch = server((url, init) => {
      if (url === "/api/hf-token" && init?.method === "PUT") {
        info = { set: true, last4: "WXYZ", updated_by: "owner", updated_at: "2026-11-15T08:00:00Z" };
        return { status: 200, body: { hf_token: info } };
      }
      if (url === "/api/hf-token" && init?.method === "DELETE") {
        info = { set: false, last4: null, updated_by: null, updated_at: null };
        return { status: 200, body: { hf_token: info } };
      }
      if (url === "/api/models") return { status: 200, body: catalog({ sources: { ...catalog().sources!, hf_token: info } }) };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
      if (url === "/api/routes") return { status: 200, body: { routes: [], is_admin: true } };
    });
    mount("/models?tab=library");
    expect(await screen.findByText(/Gated models need a token/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save token" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: "  hf_example_token_WXYZ \n" } });  // pasted with spaces
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save token" })));
    expect(bodies(fetch, "/api/hf-token", "PUT")).toEqual([{ token: "hf_example_token_WXYZ" }]);
    expect(await screen.findByText("…WXYZ")).toBeTruthy();
    expect((screen.getByLabelText("Replace the token") as HTMLInputElement).value).toBe("");
    expect(document.body.textContent).not.toContain("hf_example_token");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Remove token" })));
    expect(await screen.findByText("Token removed.")).toBeTruthy();
  });

  it("FY-WEB-214 Model sources marks lists that still come from .env", async () => {
    server((url) => {
      if (url === "/api/models") return { status: 200, body: catalog({ sources: { ...catalog().sources!, url_hosts: ["mirror.example.org"], env_seeds: ["MODEL_URL_HOSTS"] } }) };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
    });
    mount("/models?tab=library");
    const note = await screen.findByRole("note");
    expect(note.textContent).toMatch(/from \.env.*MODEL_URL_HOSTS/);
  });
});

describe("no email field (D11)", () => {
  it("FY-WEB-215 New user and Register ask for no email, and nothing sends one", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/users" && init?.method === "POST") return { status: 201, body: { user: { ...OWNER, id: 5, username: "nina", role: "user", is_owner: false }, set_password_url: "https://forge.example.org/set-password#x", expires_at: "2026-11-18T00:00:00Z" } };
      if (url === "/api/users") return { status: 200, body: { users: [OWNER] } };
    });
    mount("/users");
    fireEvent.click(await screen.findByRole("button", { name: /Add user/ }));
    expect(screen.queryByLabelText(/Email/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "nina" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create and get link" })));
    expect(bodies(fetch, "/api/users", "POST")).toEqual([{ username: "nina", role: "user" }]);
    cleanup();
    vi.unstubAllGlobals();
    const reg = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/auth/state") return new Response(JSON.stringify({ needs_setup: false, registration: "approval", user: null }), { status: 200 });
      if (url === "/api/auth/register") return new Response(JSON.stringify({ ok: true, status: "pending" }), { status: 201 });
      return new Response(JSON.stringify({}), { status: 404, statusText: String(init?.method) });
    });
    vi.stubGlobal("fetch", reg);
    mount("/register");
    expect(await screen.findByLabelText("Username")).toBeTruthy();
    expect(screen.queryByLabelText(/Email/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "otto" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "a-long-pass-phrase-77" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Request account" })));
    const sent = reg.mock.calls.find(([u]) => u === "/api/auth/register");
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({ username: "otto", password: "a-long-pass-phrase-77" });
  });
});
