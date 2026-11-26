// @vitest-environment jsdom
// GPU hours on the Usage page (1.6.1, #190): the tab for admins, operators and a team's managers (not for other people),
// the totals in GPU hours with one decimal and the busy share, the split by deployment, team, machine or GPU model, a
// manager's own team, CPU replicas' RAM, and the words in Traditional Chinese.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY } from "../i18n/lang";
import type { GpuRow, GpuUsageResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const MIA = { ...OWNER, id: 2, username: "mia", role: "user", is_owner: false };
const UWE = { ...OWNER, id: 4, username: "uwe", role: "user", is_owner: false };
const ML = { id: 4, name: "ml", title: "Machine learning" };
const H = 3_600_000;

const row = (id: string, name: string, gpu_ms: number, busy_ms: number, over: Partial<GpuRow> = {}): GpuRow =>
  ({ id, name, gpu_ms, busy_ms, ram_mib_ms: 0, cpu_busy_ms: 0, busy_share: gpu_ms ? busy_ms / gpu_ms : null, ...over });

function gpu(url: string, rows: GpuRow[], over: Partial<GpuUsageResponse> = {}): GpuUsageResponse {
  const sum = rows.reduce((a, r) => ({ gpu_ms: a.gpu_ms + r.gpu_ms, busy_ms: a.busy_ms + r.busy_ms, ram_mib_ms: a.ram_mib_ms + r.ram_mib_ms }),
    { gpu_ms: 0, busy_ms: 0, ram_mib_ms: 0 });
  return { from: "2026-11-20", to: "2026-11-26", days: 7, group_by: new URL(url, "http://x").searchParams.get("group") ?? "deployment",
    total: { id: "total", name: "Total", ...sum, cpu_busy_ms: 0, busy_share: sum.gpu_ms ? sum.busy_ms / sum.gpu_ms : null },
    rows, team: null, teams: [ML], everything: true, counting_since: "2026-11-26T06:00:00Z", ...over };
}

function usage(teams: typeof ML[]) {
  return { from: "2026-11-20", to: "2026-11-26", days: 7, group_by: "model", everyone: false, is_admin: false, rows: [], team: null, teams,
    total: { id: "total", name: "Total", requests: 0, errors: 0, limited: 0, prompt_tokens: 0, completion_tokens: 0, tokens: 0,
      latency_avg_ms: null, latency_p50_ms: null, latency_p95_ms: null } };
}

function server(user: Record<string, unknown>, teams: typeof ML[], gpuAnswer: (url: string) => GpuUsageResponse) {
  const fetch = vi.fn(async (url: string) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url.startsWith("/api/usage/gpu") ? { status: 200, body: gpuAnswer(url) }
        : url.startsWith("/api/usage/series") ? { status: 200, body: {} }
          : url.startsWith("/api/usage") ? { status: 200, body: usage(teams) }
            : { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const mount = () => render(<MemoryRouter initialEntries={["/usage"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
const asked = (fetch: ReturnType<typeof server>) => fetch.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith("/api/usage/gpu"));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("GPU hours", () => {
  it("FY-WEB-1048 an admin sees GPU hours in total and per deployment (one decimal, share, busy), then per machine", async () => {
    const fetch = server(OWNER, [ML], (url) => url.includes("group=machine")
      ? gpu(url, [row("1", "a100", 0.75 * H, 0.1 * H)])
      : gpu(url, [row("3", "chat", 0.6 * H, 0.15 * H), row("5", "embed", 0.15 * H, 0)]));
    mount();
    const tab = await screen.findByRole("tab", { name: "GPU hours" }, { timeout: 5000 });
    await act(async () => fireEvent.click(tab));
    const dl = await screen.findByLabelText("GPU hours in this period");
    expect(within(dl).getByText("GPU hours").nextElementSibling?.textContent).toBe("0.8");
    expect(within(dl).getByText("Busy").nextElementSibling?.textContent).toMatch(/^20% of the GPU time with a request in flight$/);
    const table = screen.getByRole("table", { name: "GPU hours by deployment" });
    const chat = within(table).getByText("chat").closest("tr")!;
    expect([...chat.querySelectorAll("td")].map((c) => c.textContent).slice(-3)).toEqual(["0.6", "80%", "25%"]);
    expect(screen.getByText(/Nothing is counted while the server is down/)).toBeTruthy();
    expect(asked(fetch)[0]).toBe("/api/usage/gpu?days=7&group=deployment");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Machine" })));
    expect(await screen.findByRole("table", { name: "GPU hours by machine" })).toBeTruthy();
    expect(asked(fetch).at(-1)).toBe("/api/usage/gpu?days=7&group=machine");
    expect(screen.getByRole("option", { name: "Every deployment" })).toBeTruthy();
  });

  it("FY-WEB-1049 a team's manager sees their team's GPU hours (asked with ?team=); someone in no team has no GPU hours tab", async () => {
    const fetch = server(MIA, [ML], (url) => gpu(url, [row("3", "chat", 0.6 * H, 0)], { team: ML, everything: false }));
    mount();
    const tab = await screen.findByRole("tab", { name: "GPU hours" }, { timeout: 5000 });
    await act(async () => fireEvent.click(tab));
    await screen.findByRole("table", { name: "GPU hours by deployment" });
    expect(asked(fetch)[0]).toBe("/api/usage/gpu?days=7&group=deployment&team=ml");
    expect(screen.queryByRole("option", { name: "Every deployment" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Team" })).toBeNull();   // one team: no split by team
    cleanup();
    vi.unstubAllGlobals();
    server(UWE, [], (url) => gpu(url, []));
    mount();
    await screen.findByRole("tab", { name: "Cost" }, { timeout: 5000 });
    expect(screen.queryByRole("tab", { name: "GPU hours" })).toBeNull();
  });

  it("FY-WEB-1050 CPU replicas show their RAM in GiB-hours with a CPU chip; the words read in Traditional Chinese", async () => {
    localStorage.setItem(LANG_KEY, "zh-TW");
    server(OWNER, [ML], (url) => gpu(url, [row("3", "chat", 0.6 * H, 0.15 * H),
      row("7", "small", 0, 0, { vendor: "cpu", ram_mib_ms: 2048 * H, busy_share: null })]));
    mount();
    const tab = await screen.findByRole("tab", { name: "GPU 時數" }, { timeout: 5000 });
    await act(async () => fireEvent.click(tab));
    const dl = await screen.findByLabelText("這段期間的 GPU 時數");
    expect(within(dl).getByText("忙碌比例").nextElementSibling?.textContent).toBe("25% 的 GPU 時間正在處理請求");
    expect(within(dl).getByText("CPU 副本").nextElementSibling?.textContent).toBe("2.0 GiB·小時 保留的記憶體 × 時數");
    const table = screen.getByRole("table", { name: "依部署分組的 GPU 時數" });
    const small = within(table).getByText("small").closest("tr")!;
    expect(within(small).getByText("CPU")).toBeTruthy();
    expect(screen.getByText(/伺服器停止運作的期間不計算/)).toBeTruthy();
  });
});
