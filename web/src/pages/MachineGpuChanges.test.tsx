// @vitest-environment jsdom
// 1.5.4: a machine's GPU changes (GPUs added or removed since its agent last reported them) and GPUs its host has but
// its agent does not see, on the machine's page, in English and Chinese.
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { LANG_KEY, setLang } from "../i18n/lang";
import { cardLabel, changeText, hiddenText, recentChanges } from "../lib/gpuChanges";
import machinesFixture from "../test/machines.json";
import type { GpuChange, Machine } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const GIB = 1024 ** 3;
const A100 = { vendor: "nvidia", model: "NVIDIA A100 80GB PCIe", vram_total: 80 * GIB };
const ADDED: GpuChange = { at: "2026-11-24T09:12:03+00:00", gpus: 9, removed: [], added: [{ key: "nvidia:GPU-new", index: 8, ...A100 }] };
const REMOVED: GpuChange = { at: "2026-11-24T10:00:00+00:00", gpus: 8, added: [], removed: [{ key: "nvidia:GPU-3", index: 3, ...A100 }] };
const CENSUS = { host_gpus: 9, visible_gpus: 8, missing: ["0000:c1:00.0"], vendors: { nvidia: { host: 9, visible: 8, missing: ["0000:c1:00.0"] } } };

function machines(runtime: Record<string, unknown>) {
  const data = structuredClone(machinesFixture) as typeof machinesFixture & { machines: Record<string, unknown>[] };
  data.machines[0].runtime_info = { ...(data.machines[0].runtime_info as object), ...runtime };
  return data;
}

function server(body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : url === "/api/machines" ? { status: 200, body } : { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  }));
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.removeItem(LANG_KEY);
});

describe("GPU changes", () => {
  it("FY-WEB-940 the words: a card, a change, the newest first, and the host's GPUs the agent does not see", () => {
    expect(cardLabel(ADDED.added[0])).toBe("GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB)");
    expect(cardLabel({ ...ADDED.added[0], vram_total: null, model: "" })).toBe("GPU #8 (Unknown)");
    expect(changeText({ ...ADDED, removed: REMOVED.removed })).toBe(
      "added GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB); removed GPU #3 (NVIDIA A100 80GB PCIe, 80 GiB)");
    const m = { runtime_info: { gpu_changes: [ADDED, REMOVED] } } as Pick<Machine, "runtime_info">;
    expect(recentChanges(m).map((c) => c.at)).toEqual([REMOVED.at, ADDED.at]);
    expect(recentChanges(m, 1)).toHaveLength(1);
    expect(recentChanges({ runtime_info: {} })).toEqual([]);
    expect(hiddenText(CENSUS)).toBe("The host has 9 NVIDIA GPUs; its agent sees 8");
    expect(hiddenText({ ...CENSUS, vendors: { nvidia: { host: 8, visible: 8, missing: [] } } })).toBeNull();
    expect(hiddenText(undefined)).toBeNull();
    setLang("zh-TW");
    expect(changeText(ADDED)).toBe("加裝 GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB)");
    expect(hiddenText(CENSUS)).toBe("主機有 9 張 NVIDIA GPU，它的 agent 只看到 8 張");
    setLang("en");
  });

  it("FY-WEB-941 the machine's GPU access section lists its GPU changes and says what the agent does not see", async () => {
    server(machines({ gpu_changes: [ADDED, REMOVED], gpu_census: CENSUS }));
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "GPU access and runtimes" });
    expect(section.id).toBe("gpu-access");
    const list = within(section).getByRole("heading", { name: "GPU changes" }).parentElement as HTMLElement;
    const items = within(list).getAllByRole("listitem").map((li) => li.textContent);
    expect(items[0]).toMatch(/removed GPU #3 \(NVIDIA A100 80GB PCIe, 80 GiB\)$/);
    expect(items[1]).toMatch(/added GPU #8 \(NVIDIA A100 80GB PCIe, 80 GiB\)$/);
    expect(within(section).getByText(/The host has 9 NVIDIA GPUs; its agent sees 8\. Re-create the agent/)).toBeTruthy();
    cleanup();
    localStorage.setItem(LANG_KEY, "zh-TW");
    mount("/machines/1");
    const zh = await screen.findByRole("region", { name: "GPU 存取和執行環境" });
    expect(within(zh).getByRole("heading", { name: "GPU 變更" })).toBeTruthy();
    expect(within(zh).getByText(/加裝 GPU #8/)).toBeTruthy();
    expect(within(zh).getByText(/主機有 9 張 NVIDIA GPU，它的 agent 只看到 8 張。請用下方的更新指令重新建立 agent/)).toBeTruthy();
  });

  it("FY-WEB-942 nothing extra for a machine without changes or whose agent sees every GPU", async () => {
    server(machines({ gpu_census: { ...CENSUS, visible_gpus: 9, vendors: { nvidia: { host: 9, visible: 9, missing: [] } } } }));
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "GPU access and runtimes" });
    expect(within(section).queryByRole("heading", { name: "GPU changes" })).toBeNull();
    expect(within(section).queryByText(/its agent sees/)).toBeNull();
  });
});
