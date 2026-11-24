// @vitest-environment jsdom
// A machine's GPU access (M2), and the old Deployments address (the app pages that were tested here left the
// console in 1.4, #88).
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import machinesFixture from "../test/machines.json";

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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("deployments link", () => {
  it("FY-WEB-040 users can look but not start; the old Deployments link lands on Serving (M3; Models until 1.3)", async () => {
    server((url) => (url === "/api/workloads" ? { status: 200, body: { workloads: [], server_time: "", can_manage: false } }
      : url === "/api/deployments" ? { status: 200, body: { deployments: [], server_time: "", can_manage: false } } : undefined),
      { ...OWNER, role: "user", is_owner: false });
    mount("/deployments");
    expect(await screen.findByText("No deployments yet")).toBeTruthy();
    expect(screen.getByTestId("where").textContent).toBe("/serving");
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
    expect(confirm.mock.calls[0]).toEqual(["Let Forgeyard assign only GPU #4 on a100-box? No model will ever get another card of this machine."]);
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
