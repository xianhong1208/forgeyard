// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import type { SeriesLine, SeriesResponse } from "../lib/charts";
import type { UsageResponse } from "../lib/types";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
type Reply = { status: number; body?: unknown };

function server(route: (url: string) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string) => {
    const r = url === "/api/auth/state" ? { status: 200, body: { needs_setup: false, registration: "off", user } } : route(url) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount() {
  return render(<MemoryRouter initialEntries={["/usage"]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const usage = (over: Partial<UsageResponse> = {}): UsageResponse => {
  const row = { id: "chat", name: "chat", requests: 12, errors: 0, limited: 0, prompt_tokens: 120, completion_tokens: 240, tokens: 360,
    latency_avg_ms: 420, latency_p50_ms: 380, latency_p95_ms: 1900, cost_micros: 0, unpriced: 0 };
  return { from: "2026-11-09", to: "2026-11-15", days: 7, group_by: "model", total: { ...row, id: "total", name: "Total" }, rows: [row],
    everyone: false, is_admin: true, ...over };
};

const DAYS = ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"];

// An answer of /api/usage/series over four days; `lines` as [id, name, values].
function series(url: string, lines: [string, string, number[]][], over: Partial<SeriesResponse> = {}): SeriesResponse {
  const q = new URLSearchParams(url.split("?")[1]);
  const out: SeriesLine[] = lines.map(([id, name, values]) => ({ id, name, values, total: values.reduce((a, b) => a + b, 0), ...(id === "other" ? { other: true } : {}) }));
  return {
    step: (q.get("step") ?? "day") as SeriesResponse["step"], split: (q.get("split") ?? "none") as SeriesResponse["split"], metric: q.get("metric") ?? "requests",
    from: DAYS[0], to: DAYS[3], periods: DAYS.map((d, i) => ({ start: d, end: d, partial: i === 3 })), series: out,
    total: DAYS.map((_, i) => out.reduce((a, l) => a + l.values[i], 0)), totals: { requests: 10 }, omitted: 0,
    everyone: q.get("everyone") === "true", is_admin: true, ...over,
  };
}

const called = (fetch: ReturnType<typeof server>, part: string) => fetch.mock.calls.some(([u]) => String(u).startsWith("/api/usage/series") && String(u).includes(part));

describe("Usage over time (1.4)", () => {
  it("FY-WEB-236 charts requests per day, with the numbers as a table; the view and what is shown change the request", async () => {
    const fetch = server((url) => {
      if (url.startsWith("/api/usage/series")) return { status: 200, body: series(url, [["total", "Total", [3, 0, 5, 2]]], { totals: { requests: 10, images: 4 } }) };
      if (url.startsWith("/api/usage")) return { status: 200, body: usage() };
    });
    mount();
    const chart = await screen.findByRole("group", { name: /Requests per day/ });
    expect(called(fetch, "step=day&periods=30&split=none&metric=requests")).toBe(true);
    expect(chart.querySelectorAll("path").length).toBe(2);                     // the days so far, and today dashed
    expect(screen.queryByRole("list", { name: /series and their totals/ })).toBeNull();   // one line: no legend
    fireEvent.click(screen.getByText("Show the numbers"));
    const table = screen.getByRole("table", { name: "Requests per day" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1].textContent).toContain("Sun, Nov 15, 2026 (so far)");      // the latest first
    expect(rows[1].textContent).toContain("2");
    expect(rows[3].textContent).toContain("Fri, Nov 13, 2026");
    const show = screen.getByLabelText("Show") as HTMLSelectElement;
    expect([...show.options].map((o) => o.textContent)).toContain("Images made");     // counted in the range
    expect([...show.options].map((o) => o.textContent)).not.toContain("Characters spoken");
    fireEvent.change(show, { target: { value: "completion_tokens" } });
    await waitFor(() => expect(called(fetch, "metric=completion_tokens")).toBe(true));
    expect(await screen.findByRole("group", { name: /Tokens out per day/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Weekly" }));
    await waitFor(() => expect(called(fetch, "step=week&periods=13&split=none&metric=completion_tokens")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Monthly" }));
    await waitFor(() => expect(called(fetch, "step=month&periods=12")).toBe(true));
  });

  it("FY-WEB-237 the arrow keys read each day's values aloud, the tooltip shows them", async () => {
    server((url) => {
      if (url.startsWith("/api/usage/series")) return { status: 200, body: series(url, [["chat", "chat", [3, 0, 5, 2]], ["embed", "embed", [1, 1, 0, 4]]]) };
      if (url.startsWith("/api/usage")) return { status: 200, body: usage() };
    });
    mount();
    const chart = await screen.findByRole("group", { name: /Requests per day/ });
    fireEvent.focus(chart);
    const live = document.getElementById(chart.getAttribute("aria-describedby")!) as HTMLElement;
    expect(live.textContent).toBe("Sun, Nov 15, 2026 (so far): chat 2, embed 4; total 6");
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    expect(live.textContent).toBe("Sat, Nov 14, 2026: chat 5, embed 0; total 5");
    fireEvent.keyDown(chart, { key: "Home" });
    expect(live.textContent).toContain("Thu, Nov 12, 2026: chat 3, embed 1");
    fireEvent.keyDown(chart, { key: "ArrowLeft" });                           // stays on the first
    expect(live.textContent).toContain("Thu, Nov 12, 2026");
    expect(chart.querySelector(".lc-tip")?.textContent).toContain("Thu, Nov 12, 2026");
    fireEvent.keyDown(chart, { key: "End" });
    expect(live.textContent).toContain("Sun, Nov 15");
    fireEvent.keyDown(chart, { key: "Escape" });
    expect(live.textContent).toBe("");
    expect(chart.querySelector(".lc-tip")).toBeNull();
  });

  it("FY-WEB-238 split by model: a legend with each line's total and Other; Person only for admins looking at everyone's", async () => {
    const fetch = server((url) => {
      if (url.startsWith("/api/usage/series")) {
        const split = new URLSearchParams(url.split("?")[1]).get("split");
        return { status: 200, body: split === "none" ? series(url, [["total", "Total", [1, 1, 1, 1]]])
          : split === "key" ? series(url, [["3", "laptop (fyk_AbCd…)", [1, 0, 0, 2]]])
          : series(url, [["chat", "chat", [3, 0, 5, 2]], ["embed", "embed", [1, 1, 0, 4]], ["other", "Other (3)", [0, 2, 0, 1]]], { omitted: 2 }) };
      }
      if (url.startsWith("/api/usage")) return { status: 200, body: usage({ everyone: url.includes("everyone=true") }) };
    });
    mount();
    const split = (await screen.findByLabelText("Split by")) as HTMLSelectElement;
    expect([...split.options].map((o) => o.textContent)).toEqual(["Total", "Key", "Model", "Provider"]);
    fireEvent.change(split, { target: { value: "key" } });                   // one line, but split: it is named
    const one = await screen.findByRole("list", { name: /Requests per day, by key: series and their totals/ });
    expect(within(one).getByRole("listitem").textContent).toBe("laptop (fyk_AbCd…)3");
    fireEvent.change(split, { target: { value: "model" } });
    const legend = await screen.findByRole("list", { name: /Requests per day, by model: series and their totals/ });
    const items = within(legend).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual(["chat10", "embed6", "Other (3)3"]);
    expect(screen.getByText(/the rest are added up as Other/)).toBeTruthy();
    expect(screen.getByText(/2 with none are left out/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Everyone" }));
    await waitFor(() => expect(called(fetch, "split=model&metric=requests&everyone=true")).toBe(true));
    await waitFor(() => expect([...(screen.getByLabelText("Split by") as HTMLSelectElement).options].map((o) => o.textContent)).toContain("Person"));
    fireEvent.change(screen.getByLabelText("Split by"), { target: { value: "user" } });
    await waitFor(() => expect(called(fetch, "split=user&metric=requests&everyone=true")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Mine" }));            // a person split needs everyone's
    await waitFor(() => expect(fetch.mock.calls.map(([u]) => String(u)).filter((u) => u.startsWith("/api/usage/series")).at(-1))
      .toMatch(/split=none&metric=requests$/));
  });

  it("FY-WEB-239 the Cost tab charts cost in dollars; with nothing to chart it says why; people see only theirs", async () => {
    const fetch = server((url) => {
      if (url.startsWith("/api/usage/series")) {
        const cost = url.includes("metric=cost_micros");
        return { status: 200, body: cost && url.includes("step=day") ? series(url, [["total", "Total", [0, 1_850, 250_000, 0]]], { is_admin: false })
          : series(url, cost ? [] : [["total", "Total", [1, 2, 3, 4]]], { is_admin: false }) };
      }
      if (url.startsWith("/api/budgets")) return { status: 200, body: { month: "2026-11", resets_at: "2026-12-01T00:00:00Z", is_admin: false, everyone: false, people: [], keys: [], providers: [] } };
      if (url.startsWith("/api/usage")) return { status: 200, body: usage({ is_admin: false }) };
    }, USER);
    mount();
    await screen.findByRole("group", { name: /Requests per day/ });
    fireEvent.click(screen.getByRole("tab", { name: "Cost" }));
    const chart = await screen.findByRole("group", { name: /Cost per day/ });
    expect(called(fetch, "metric=cost_micros")).toBe(true);
    expect(screen.queryByLabelText("Show")).toBeNull();                      // the Cost tab always charts cost
    expect(chart.textContent).toContain("$0.3");                               // the top axis tick, in dollars
    fireEvent.focus(chart);
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    expect(document.getElementById(chart.getAttribute("aria-describedby")!)?.textContent).toBe("Fri, Nov 13, 2026: Total $0.00185");
    fireEvent.click(screen.getByRole("button", { name: "Weekly" }));
    expect(await screen.findByText(/No cost in the last 13 weeks: local replicas cost nothing/)).toBeTruthy();
    expect(fetch.mock.calls.every(([u]) => !String(u).includes("everyone"))).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Requests" }));            // the view stays
    expect(await screen.findByRole("group", { name: /Requests per week/ })).toBeTruthy();
  });

  it("FY-WEB-240 a line keeps its colour when another metric re-ranks the lines", async () => {
    server((url) => {
      if (url.startsWith("/api/usage/series")) {
        const tokens = url.includes("metric=tokens");
        return { status: 200, body: series(url, tokens ? [["embed", "embed", [9, 9, 9, 9]], ["chat", "chat", [1, 1, 1, 1]]]
          : [["chat", "chat", [3, 3, 3, 3]], ["embed", "embed", [1, 1, 1, 1]]]) };
      }
      if (url.startsWith("/api/usage")) return { status: 200, body: usage() };
    });
    mount();
    fireEvent.change(await screen.findByLabelText("Split by"), { target: { value: "model" } });
    const colourOf = (name: string) => {
      const legend = screen.getByRole("list", { name: /series and their totals/ });
      const li = within(legend).getByText(name).closest("li") as HTMLElement;
      return (li.querySelector(".lc-key") as HTMLElement).style.borderColor;
    };
    await screen.findByRole("list", { name: /Requests per day, by model/ });
    expect([colourOf("chat"), colourOf("embed")]).toEqual(["var(--series-1)", "var(--series-2)"]);
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "tokens" } });
    await screen.findByRole("list", { name: /Tokens \(in \+ out\) per day, by model/ });
    const legend = screen.getByRole("list", { name: /series and their totals/ });
    expect(within(legend).getAllByRole("listitem")[0].textContent).toContain("embed");    // re-ranked …
    expect([colourOf("chat"), colourOf("embed")]).toEqual(["var(--series-1)", "var(--series-2)"]);   // … same colours
  });
});
