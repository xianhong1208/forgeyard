// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import fixture from "../test/machines.json";
import { POLL_MS } from "./Machines";

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

function mount() {
  return render(
    <MemoryRouter initialEntries={["/machines"]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

const machines = (over: Partial<typeof fixture> = {}) => ({ status: 200, body: { ...fixture, ...over } });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("machines page", () => {
  it("FY-WEB-033 shows each machine, its cards, arch and free memory", async () => {
    server((url) => (url === "/api/machines" ? machines() : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } } : undefined));
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    expect(within(box).getByText("online")).toBeTruthy();
    expect(within(box).getByText("simulated inventory")).toBeTruthy();
    expect(within(box).getByText("rack=r2")).toBeTruthy();
    expect(within(box).getByText("8 × NVIDIA A100 80GB PCIe")).toBeTruthy();
    expect(within(box).getAllByText("NVIDIA A100 80GB PCIe")).toHaveLength(8);
    expect(within(box).getAllByText("8.0")).toHaveLength(8);
    expect(within(box).getAllByText(/sm_80/)).toHaveLength(8);
    // card 0: 4023779328 bytes free of 80 GiB, shown plainly and as a nearly full bar
    const first = within(box).getAllByRole("listitem")[0];
    expect(first.textContent).toContain("3.7 GiB free");
    expect(first.textContent).toContain("of 80 GiB");
    expect(within(first).getByRole("meter").getAttribute("aria-valuenow")).toBe("95");
    expect(within(box).getByText("Heartbeat 4 s ago")).toBeTruthy();
  });

  it("FY-WEB-034 an APU shows gfx1151 and unified memory; an offline machine says since when", async () => {
    server((url) => (url === "/api/machines" ? machines() : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } } : undefined));
    mount();
    const strix = await screen.findByRole("article", { name: "Machine strix" });
    expect(within(strix).getByText("gfx1151")).toBeTruthy();
    expect(within(strix).getByText("unified memory")).toBeTruthy();
    expect(within(strix).getByText("offline")).toBeTruthy();
    expect(within(strix).getByText("Offline · last heartbeat 41 s ago")).toBeTruthy();
  });

  it("FY-WEB-035 Add machine shows the join command once", async () => {
    const created = { id: "t1", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner",
      token: "fyj_secret", server_url: "http://127.0.0.1:7860", valid_minutes: 15,
      command: "uv run --no-sync python -m forgeyard agent --server http://127.0.0.1:7860 --join-token fyj_secret" };
    const fetch = server((url, init) => {
      if (url === "/api/machines") return machines({ machines: [] });
      if (url === "/api/machines/join-tokens" && init?.method === "POST") return { status: 201, body: created };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return undefined;
    });
    mount();
    expect(await screen.findByText("No machines yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    expect((await screen.findByLabelText("Join command")).textContent).toBe(created.command);
    expect(screen.getByText(/only works on this computer/)).toBeTruthy();
    expect(screen.getByText(/Waiting for the machine to join/)).toBeTruthy();
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/machines/join-tokens" && i?.method === "POST");
    expect((post?.[1]?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText(/fyj_secret/)).toBeNull();
  });

  it("FY-WEB-036 operators and users see machines but cannot manage them", async () => {
    const fetch = server((url) => (url === "/api/machines" ? machines({ can_manage: false }) : undefined), { ...OWNER, role: "operator", is_owner: false });
    mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    expect(screen.queryByRole("button", { name: "Add machine" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/join-tokens")).toBe(false);
  });

  it("FY-WEB-037 the page follows status changes without a reload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let online = true;
    server((url) => {
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      if (url !== "/api/machines") return undefined;
      const [a, b] = fixture.machines;
      return machines({ machines: [{ ...a, status: online ? "online" : "offline" }, b] });
    });
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    expect(within(box).getByText("online")).toBeTruthy();
    online = false;
    await act(async () => {
      vi.advanceTimersByTime(POLL_MS + 10);
    });
    await waitFor(() => expect(within(screen.getByRole("article", { name: "Machine a100-box" })).getByText("offline")).toBeTruthy());
  });

  it("FY-WEB-038 revoke asks first, then calls the API", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/machines/1/revoke" && init?.method === "POST") return { status: 200, body: {} };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return url === "/api/machines" ? machines() : undefined;
    });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(within(box).getByRole("button", { name: "Revoke" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/1/revoke")).toBe(false);
    confirm.mockReturnValue(true);
    await act(async () => fireEvent.click(within(box).getByRole("button", { name: "Revoke" })));
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/machines/1/revoke" && i?.method === "POST")).toBe(true);
    expect(await screen.findByText("Revoked a100-box; its agent was disconnected.")).toBeTruthy();
  });
});
