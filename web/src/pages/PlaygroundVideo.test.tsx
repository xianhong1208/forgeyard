// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { elapsed, snippets, videoFrames, videoSizes } from "../lib/gateway";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const LOCAL = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [], usable: true };
const ROUTES = { cloud: "off", is_admin: true, routes: [
  { ...LOCAL, id: 7, served_name: "wan", api: "openai-videos", upstream_model: "wan", deployment_id: 7,
    endpoints: [{ replica: 0, machine: "gpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }] },
] };
const OPTIONS = { model: "wan", default_size: "832x480", max_side: 832, max_pixels: 832 * 480, multiple: 16, seconds: 5,
  max_seconds: 5, fps: 16, max_steps: 50, max_prompt_chars: 40, keep_minutes: 1440 };
const NOW = Math.floor(Date.now() / 1000);
const job = (status: string, extra: Record<string, unknown> = {}) => ({
  id: "video_0123456789abcdef0123456789abcdef", object: "video", model: "wan", status, progress: status === "completed" ? 100 : 0,
  created_at: NOW - 30, completed_at: status === "completed" ? NOW : null, expires_at: status === "completed" ? NOW + 86400 : null,
  size: "480x832", seconds: "3", quality: "standard", remixed_from_video_id: null, error: null, ...extra,
});

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function panel(): Promise<HTMLElement> {
  const p = (await screen.findByRole("heading", { name: "Video" })).closest("section") as HTMLElement;
  await waitFor(() => expect((within(p).getByLabelText("Size") as HTMLSelectElement).value).toBe("832x480"));
  return p;
}

describe("Playground: Video (1.3)", () => {
  it("FY-WEB-195: asks for a video through the console endpoint, follows the job and plays it with a download link", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const sent: { url: string; method: string; body: unknown; csrf: string | undefined }[] = [];
    let status = "queued";
    server((url, init) => {
      const method = init?.method ?? "GET";
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/videos/options?model=wan") return { status: 200, body: OPTIONS };
      if (url === "/api/playground/videos?limit=20") return { status: 200, body: { object: "list", data: status === "queued" ? [] : [job(status)] } };
      if (url.startsWith("/api/playground/videos")) {
        sent.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null,
          csrf: (init?.headers as Record<string, string> | undefined)?.["X-Forgeyard"] });
      }
      if (url === "/api/playground/videos" && method === "POST") return { status: 200, body: job("queued") };
      if (url === `/api/playground/videos/${job("x").id}`) return { status: 200, body: job(status) };
    });
    mount("/playground");
    const p = await panel();
    expect([...(within(p).getByLabelText("Size") as HTMLSelectElement).options].map((o) => o.value))
      .toEqual(["832x480", "480x832", "640x480", "480x640", "624x624"]);
    expect([...(within(p).getByLabelText("Length") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["1", "2", "3", "4", "5"]);
    expect(within(p).getByText("81 frames at 16 fps")).toBeTruthy();
    fireEvent.change(within(p).getByLabelText("Prompt"), { target: { value: "A paper boat" } });
    fireEvent.change(within(p).getByLabelText("Size"), { target: { value: "480x832" } });
    fireEvent.change(within(p).getByLabelText("Length"), { target: { value: "3" } });
    expect(within(p).getByText("49 frames at 16 fps")).toBeTruthy();
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    const progress = await within(p).findByRole("status", { name: "Video job" });
    expect(sent[0]).toEqual({ url: "/api/playground/videos", method: "POST", csrf: "1",
      body: { model: "wan", prompt: "A paper boat", size: "480x832", seconds: "3" } });
    expect(within(progress).getByText("Waiting for the replica")).toBeTruthy();
    expect((within(p).getByRole("button", { name: /Generate/ }) as HTMLButtonElement).disabled).toBe(true);
    status = "in_progress";
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(await within(p).findByText("Being made")).toBeTruthy();
    status = "completed";
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    const out = await within(p).findByRole("figure", { name: "Generated video" });
    const video = out.querySelector("video") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(`/api/playground/videos/${job("x").id}/content`);
    const link = within(out).getByRole("link", { name: /Download/ });
    expect(link.getAttribute("href")).toBe(`/api/playground/videos/${job("x").id}/content`);
    expect(link.getAttribute("download")).toBe("wan-89abcdef.mp4");
    expect(within(out).getByText(/480x832 · 3 s · made in 30 s/)).toBeTruthy();
    expect(sent.filter((s) => s.method === "GET").every((s) => s.csrf === "1")).toBe(true);
    expect(await within(p).findByText("Your videos with this model")).toBeTruthy();
  });

  it("FY-WEB-196: shows the gateway's refusal and a failed job's reason; Cancel deletes the job at work", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let answer: Reply = { status: 429, body: { error: { message: "You have 2 videos waiting or being made on 'wan' already (at most 2 at once).",
      type: "rate_limit_error", param: null, code: "too_many_jobs" } }, headers: { "Retry-After": "60" } };
    const deleted: string[] = [];
    let polled = job("in_progress");
    server((url, init) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/videos/options?model=wan") return { status: 200, body: OPTIONS };
      if (url === "/api/playground/videos?limit=20") return { status: 200, body: { object: "list", data: [] } };
      if (url === "/api/playground/videos" && init?.method === "POST") return answer;
      if (init?.method === "DELETE") { deleted.push(url); return { status: 200, body: { id: job("x").id, object: "video.deleted", deleted: true } }; }
      if (url === `/api/playground/videos/${job("x").id}`) return { status: 200, body: polled };
    });
    mount("/playground");
    const p = await panel();
    fireEvent.change(within(p).getByLabelText("Prompt"), { target: { value: "x".repeat(41) } });
    expect((within(p).getByRole("button", { name: /Generate/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(p).getByLabelText("Prompt"), { target: { value: "A red fox" } });
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    expect(await within(p).findByText(/429: You have 2 videos waiting/)).toBeTruthy();
    answer = { status: 200, body: job("queued") };
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    await within(p).findByRole("status", { name: "Video job" });
    fireEvent.click(within(p).getByRole("button", { name: /Cancel/ }));
    await waitFor(() => expect(deleted).toEqual([`/api/playground/videos/${job("x").id}`]));
    await waitFor(() => expect(within(p).queryByRole("status", { name: "Video job" })).toBeNull());
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    await within(p).findByRole("status", { name: "Video job" });
    polled = job("failed", { error: { code: "replica_restarted", message: "The replica making this video restarted, and its jobs went with it." } });
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(await within(p).findByText(/restarted, and its jobs went with it/)).toBeTruthy();
  });

  it("FY-WEB-197: sizes within the deployment's caps, frame counts, elapsed times and the snippets", () => {
    expect(videoSizes(OPTIONS)).toEqual(["832x480", "480x832", "640x480", "480x640", "624x624"]);
    const big = { ...OPTIONS, default_size: "1280x704", max_side: 1280, max_pixels: 1280 * 704, multiple: 32 };
    expect(videoSizes(big)).toEqual(["1280x704", "832x480", "480x832", "640x480", "480x640", "704x1280", "1024x576", "576x1024"]);
    expect(videoSizes(big)).not.toContain("1280x720");
    expect(videoFrames(5, 16)).toBe(81);
    expect(videoFrames(5, 24)).toBe(121);
    expect(videoFrames(1, 16, 1)).toBe(16);
    expect(elapsed(42)).toBe("42 s");
    expect(elapsed(125)).toBe("2 min 05 s");
    const s = snippets("https://fy.example/v1", "fyk_x", "wan", "openai-videos");
    expect(s.python).toContain('client.videos.create(model="wan"');
    expect(s.python).toContain("client.videos.retrieve(video.id)");
    expect(s.python).toContain('download_content(video.id).write_to_file("video.mp4")');
    expect(s.curl).toContain("https://fy.example/v1/videos");
    expect(s.curl).toContain("-F model=wan");
    expect(s.curl).toContain("/videos/<id>/content");
  });
});
