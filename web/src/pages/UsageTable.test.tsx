// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { BreakdownTable, cutRows, sortRows, type BreakdownColumn } from "../components/BreakdownTable";
import type { UsageResponse, UsageRow } from "../lib/types";
import { addUp } from "./Usage";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body?: unknown };

function server(route: (url: string) => Reply | undefined) {
  const fetch = vi.fn(async (url: string) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } } : route(url) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount() {
  return render(<MemoryRouter initialEntries={["/usage"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

// window.matchMedia for the phone layout's query; `flip` changes the width afterwards.
function screenWidth(narrow: boolean) {
  let matches = narrow;
  const listeners = new Set<() => void>();
  vi.stubGlobal("matchMedia", (media: string) => ({
    get matches() { return matches; }, media,
    addEventListener: (_: string, l: () => void) => listeners.add(l), removeEventListener: (_: string, l: () => void) => listeners.delete(l),
  }));
  return { flip: (to: boolean) => { matches = to; act(() => listeners.forEach((l) => l())); } };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const base = { errors: 0, limited: 0, prompt_tokens: 10, completion_tokens: 20, tokens: 30, latency_avg_ms: 100, latency_p50_ms: 90, latency_p95_ms: 300, unpriced: 0 };
// 20 models: model-01 has the most requests (40) down to model-20 (21); the cost runs the other way round.
const MODELS: UsageRow[] = Array.from({ length: 20 }, (_, i) => {
  const n = String(i + 1).padStart(2, "0");
  return { ...base, id: `m${n}`, name: `model-${n}-with-a-rather-long-name`, requests: 40 - i, completion_tokens: (i + 1) * 100, cost_micros: (i + 1) * 1_000_000 };
});
const DAYS: UsageRow[] = Array.from({ length: 20 }, (_, i) => {
  const d = `2026-11-${String(i + 1).padStart(2, "0")}`;
  return { ...base, id: d, name: d, requests: 5 + (i % 3) };
});

function usage(url: string, over: Partial<UsageResponse> = {}): UsageResponse {
  const group = new URLSearchParams(url.split("?")[1]).get("group") ?? "model";
  const rows = group === "day" ? DAYS : MODELS;
  const total = { ...addUp(rows), id: "total", name: "Total", latency_avg_ms: 100, latency_p50_ms: 90, latency_p95_ms: 300 };
  return { from: "2026-11-01", to: "2026-11-20", days: 30, group_by: group, total, rows, everyone: false, is_admin: true, ...over };
}

function routes(over: Partial<UsageResponse> = {}) {
  return server((url) => {
    if (url.startsWith("/api/usage/series")) return { status: 200, body: { error: "not here" } };
    if (url.startsWith("/api/usage")) return { status: 200, body: usage(url, over) };
    if (url.startsWith("/api/budgets")) return { status: 200, body: { month: "2026-11", resets_at: "2026-12-01T00:00:00Z", everyone: false, is_admin: true, people: [], keys: [], providers: [] } };
  });
}

const bodyRows = (table: HTMLElement) => within(table).getAllByRole("row").slice(1) as HTMLTableRowElement[];
const names = (table: HTMLElement) => bodyRows(table).map((r) => r.cells[0].textContent);

describe("Usage breakdown tables (1.4, #82)", () => {
  it("FY-WEB-258 the top 15 rows by requests, the rest added up in one row; Show all and back", async () => {
    screenWidth(false);
    routes();
    mount();
    const table = await screen.findByRole("table", { name: "Requests by model" });
    let rows = bodyRows(table);
    expect(rows.length).toBe(16);                                         // 15 and "5 others"
    expect(rows[0].textContent).toContain("model-01");
    expect(rows[14].textContent).toContain("model-15");
    const rest = rows[15];
    expect(rest.cells[0].textContent).toBe("5 others");
    expect(rest.cells[1].textContent).toBe(String(25 + 24 + 23 + 22 + 21)); // requests of model-16 … model-20
    expect(rest.cells[4].textContent).toBe("9,000");                        // tokens out: (16 + … + 20) × 100   // tokens out
    expect(rest.cells[5].textContent).toBe("—");                          // latencies do not add up
    expect(rows[0].querySelector("[title='model-01-with-a-rather-long-name']")).toBeTruthy();   // the full name on hover
    const all = screen.getByRole("button", { name: "Show all 20" });
    expect(all.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(all);
    rows = bodyRows(table);
    expect(rows.length).toBe(20);
    expect(rows[19].textContent).toContain("model-20");
    expect(screen.queryByText("5 others")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show the top 15" }));
    expect(bodyRows(table).length).toBe(16);
  });

  it("FY-WEB-259 a column heading sorts by it, again the other way round; Day starts with the latest", async () => {
    screenWidth(false);
    const fetch = routes();
    mount();
    const table = await screen.findByRole("table", { name: "Requests by model" });
    const head = (name: string) => within(table).getByRole("columnheader", { name: new RegExp(`^${name}`) });
    expect(head("Requests").getAttribute("aria-sort")).toBe("descending");
    fireEvent.click(within(head("Tokens out")).getByRole("button"));
    expect(head("Tokens out").getAttribute("aria-sort")).toBe("descending");
    expect(head("Requests").getAttribute("aria-sort")).toBe("none");
    expect(names(table)[0]).toContain("model-20");                       // the most tokens out
    expect(names(table)[15]).toBe("5 others");                            // model-01 … model-05
    fireEvent.click(within(head("Tokens out")).getByRole("button"));
    expect(head("Tokens out").getAttribute("aria-sort")).toBe("ascending");
    expect(names(table)[0]).toContain("model-01");
    fireEvent.click(within(head("Model")).getByRole("button"));          // names: A to Z first
    expect(head("Model").getAttribute("aria-sort")).toBe("ascending");
    expect(names(table)[0]).toContain("model-01");
    fireEvent.click(screen.getByRole("button", { name: "Day" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/usage?days=7&group=day")).toBe(true));
    const days = await screen.findByRole("table", { name: "Requests by day" });
    expect(within(days).getByRole("columnheader", { name: /^Day/ }).getAttribute("aria-sort")).toBe("descending");
    expect(names(days)[0]).toBe("2026-11-20");
    expect(names(days)[14]).toBe("2026-11-06");
    expect(names(days)[15]).toBe("5 others");
    fireEvent.click(within(within(days).getByRole("columnheader", { name: /^Requests/ })).getByRole("button"));
    fireEvent.click(within(within(days).getByRole("columnheader", { name: /^Day/ })).getByRole("button"));
    expect(names(days)[0]).toBe("2026-11-20");                            // back to days: the latest first again
  });

  it("FY-WEB-260 a narrow screen: one line per row with the number, the other columns behind a button", async () => {
    const width = screenWidth(true);
    routes();
    mount();
    const list = await screen.findByRole("list", { name: "Requests by model" });
    expect(screen.queryByRole("table", { name: "Requests by model" })).toBeNull();
    const items = within(list).getAllByRole("listitem");
    expect(items.length).toBe(16);
    const first = within(items[0]).getByRole("button");
    expect(first.textContent).toContain("model-01-with-a-rather-long-name");
    expect(first.textContent).toContain("40");
    expect(first.querySelector("[title='model-01-with-a-rather-long-name']")).toBeTruthy();
    expect(within(items[0]).queryByText("Tokens out")).toBeNull();        // closed: only the name and the number
    expect(first.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("true");
    const more = document.getElementById(first.getAttribute("aria-controls")!)!;
    expect(more).toBeTruthy();
    expect(within(more).getByText("Tokens out").nextElementSibling?.textContent).toBe("100");
    expect(within(more).getByText("p95").nextElementSibling?.textContent).toBe("300 ms");
    expect(within(more).queryByText("Requests")).toBeNull();              // already on the line
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(within(items[0]).queryByText("Tokens out")).toBeNull();
    const rest = within(items[15]).getByRole("button");
    expect(rest.textContent).toContain("5 others");
    expect(rest.textContent).toContain("115");
    fireEvent.click(rest);
    expect(within(items[15]).getByText("Tokens out").nextElementSibling?.textContent).toBe("9,000");
    // Sort by another column: its number is on the line.
    fireEvent.change(screen.getByLabelText("Sort by"), { target: { value: "completion_tokens" } });
    const sorted = within(list).getAllByRole("listitem");
    expect(within(sorted[0]).getByRole("button").textContent).toContain("model-20");
    expect(within(sorted[0]).getByRole("button").textContent).toContain("2,000");
    fireEvent.click(screen.getByRole("button", { name: "Show all 20" }));
    expect(within(list).getAllByRole("listitem").length).toBe(20);
    // Wider again: the table, with the same order and every row.
    width.flip(false);
    const table = await screen.findByRole("table", { name: "Requests by model" });   // once the page has heard of the change
    expect(bodyRows(table).length).toBe(20);
    expect(names(table)[0]).toContain("model-20");
  });

  it("FY-WEB-261 the Cost tab ranks by cost, its last row adds up the rest's cost and share", async () => {
    const width = screenWidth(false);
    routes();
    mount();
    await screen.findByRole("table", { name: "Requests by model" });
    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    fireEvent.click(await screen.findByRole("button", { name: "Model" }));
    const table = await screen.findByRole("table", { name: "Cost by model" });
    expect(within(table).getByRole("columnheader", { name: /^Cost/ }).getAttribute("aria-sort")).toBe("descending");
    expect(within(table).queryByRole("button", { name: /^Share/ })).toBeNull();          // follows Cost: not a sort of its own
    const rows = bodyRows(table);
    expect(rows[0].textContent).toContain("model-20");
    expect(rows[0].textContent).toContain("$20.00");
    expect(rows[15].cells[0].textContent).toBe("5 others");
    expect(rows[15].textContent).toContain("$15.00");                     // model-01 … model-05: 1+2+3+4+5
    expect(rows[15].textContent).toContain("7%");                         // of $210
    width.flip(true);
    const list = await screen.findByRole("list", { name: "Cost by model" });
    expect(within(within(list).getAllByRole("listitem")[0]).getByRole("button").textContent).toContain("$20.00");
  });

  it("FY-WEB-262 sorting and cutting: ties by name, no value last both ways, nothing cut at the limit", () => {
    const rows = [
      { id: "a", name: "b", v: 2 }, { id: "b", name: "a", v: 2 }, { id: "c", name: "c", v: null }, { id: "d", name: "d", v: 5 },
    ];
    const cols: BreakdownColumn<(typeof rows)[number]>[] = [{ id: "v", label: "V", value: (r) => r.v, cell: (r) => r.v }];
    screenWidth(false);
    expect(sortRows(rows, cols, { by: "v", desc: true }).map((r) => r.name)).toEqual(["d", "a", "b", "c"]);
    expect(sortRows(rows, cols, { by: "v", desc: false }).map((r) => r.name)).toEqual(["a", "b", "d", "c"]);
    expect(sortRows(rows, cols, { by: "name", desc: true }).map((r) => r.name)).toEqual(["d", "c", "b", "a"]);
    expect(cutRows([1, 2, 3], 3, false)).toEqual({ shown: [1, 2, 3], hidden: [] });
    expect(cutRows([1, 2, 3, 4], 3, false)).toEqual({ shown: [1, 2, 3], hidden: [4] });
    expect(cutRows([1, 2, 3, 4], 3, true)).toEqual({ shown: [1, 2, 3, 4], hidden: [] });
    // "Show all" only when something is left out.
    const table = (n: number) => render(<BreakdownTable label="t" nameLabel="Name" rows={rows.slice(0, n)} columns={cols} metric="v" limit={3} others={(h) => h[0]} />);
    table(3);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
    cleanup();
    table(4);
    expect(screen.getByRole("button", { name: "Show all 4" })).toBeTruthy();
    const sum = addUp([{ ...base, id: "x", name: "x", requests: 2, images: 1 }, { ...base, id: "y", name: "y", requests: 3, cloud: true }]);
    expect(sum).toMatchObject({ requests: 5, tokens: 60, images: 1, latency_p50_ms: null });
    expect(sum.cloud).toBeUndefined();
  });
});
