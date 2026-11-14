// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { imageSizes, snippets } from "../lib/gateway";
import { busyTime, megapixels } from "./Usage";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const LOCAL = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [], usable: true };
const ROUTES = { cloud: "off", is_admin: true, routes: [
  { ...LOCAL, id: 4, served_name: "pics", api: "openai-images", upstream_model: "pics", deployment_id: 4,
    endpoints: [{ replica: 0, machine: "gpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }] },
] };
const OPTIONS = { model: "pics", max_n: 4, max_side: 1024, default_size: "1024x1024", multiple: 16, formats: ["png", "jpeg", "webp"],
  max_steps: 50, max_prompt_chars: 30 };
// A 1x1 PNG.
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==";

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

beforeEach(() => {
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => `blob:image-${++n}`), revokeObjectURL: vi.fn() }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Playground: Image (1.3)", () => {
  it("FY-WEB-171: offers the sizes the deployment takes, generates through the console endpoint and shows the images", async () => {
    const sent: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/images/options?model=pics") return { status: 200, body: OPTIONS };
      if (url === "/api/playground/images/generations") {
        sent.push(JSON.parse(String(init?.body)));
        expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
        return { status: 200, body: { created: 1, data: [{ b64_json: PNG }, { b64_json: PNG }], output_format: "png", size: "768x768" },
          headers: { "Content-Type": "application/json", "X-Forgeyard-Replica": "pics#0" } };
      }
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Image" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(panel).getByLabelText("Size") as HTMLSelectElement).value).toBe("1024x1024"));
    const sizes = [...(within(panel).getByLabelText("Size") as HTMLSelectElement).options].map((o) => o.value);
    expect(sizes).toEqual(["1024x1024", "256x256", "512x512", "768x768", "1024x768", "768x1024"]);
    expect([...(within(panel).getByLabelText("Images") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["1", "2", "3", "4"]);
    fireEvent.change(within(panel).getByLabelText("Prompt"), { target: { value: "A red fox" } });
    expect(within(panel).getByText("9 of at most 30 characters")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Size"), { target: { value: "768x768" } });
    fireEvent.change(within(panel).getByLabelText("Images"), { target: { value: "2" } });
    fireEvent.click(within(panel).getByRole("button", { name: /Generate/ }));
    const out = await within(panel).findByLabelText("Generated images");
    expect(sent[0]).toEqual({ model: "pics", prompt: "A red fox", n: 2, size: "768x768" });
    expect(within(out).getAllByRole("img")).toHaveLength(2);
    expect(within(out).getByText(/replica 0 · 2 images · 768x768 · PNG/)).toBeTruthy();
    const links = within(out).getAllByRole("link", { name: /Download/ });
    expect(links.map((a) => a.getAttribute("download"))).toEqual(["pics-1.png", "pics-2.png"]);
  });

  it("FY-WEB-172: refuses a prompt over the cap before sending, and shows the gateway's error", async () => {
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/images/options?model=pics") return { status: 200, body: OPTIONS };
      if (url === "/api/playground/images/generations") {
        return { status: 429, body: { error: { message: "Every replica of 'pics' is busy and 8 requests are waiting already. Try again later.",
          type: "rate_limit_error", param: null, code: "queue_full" } }, headers: { "Retry-After": "40" } };
      }
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Image" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(panel).getByLabelText("Size") as HTMLSelectElement).value).toBe("1024x1024"));
    fireEvent.change(within(panel).getByLabelText("Prompt"), { target: { value: "x".repeat(31) } });
    expect((within(panel).getByRole("button", { name: /Generate/ }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Prompt"), { target: { value: "A red fox" } });
    fireEvent.click(within(panel).getByRole("button", { name: /Generate/ }));
    expect(await within(panel).findByText(/requests are waiting already/)).toBeTruthy();
  });

  it("FY-WEB-173: sizes within the deployment's caps, the snippets, and usage's units", () => {
    expect(imageSizes({ ...OPTIONS, max_side: 512, multiple: 64, default_size: "512x512" })).toEqual(["512x512", "256x256"]);
    expect(imageSizes({ ...OPTIONS, max_side: 1280, multiple: 16 })).toContain("1280x720");
    expect(imageSizes({ ...OPTIONS, max_side: 1280, multiple: 64 })).not.toContain("1280x720");
    const s = snippets("https://fy.example/v1", "fyk_x", "pics", "openai-images");
    expect(s.python).toContain('client.images.generate(model="pics"');
    expect(s.python).toContain("b64decode(out.data[0].b64_json)");
    expect(s.curl).toContain("/images/generations");
    expect(s.curl).toContain('"size": "1024x1024"');
    expect(megapixels(12_600)).toBe("12.6 MP");
    expect(megapixels(262)).toBe("0.26 MP");
    expect(megapixels(0)).toBe("0 MP");
    expect(busyTime(12_300)).toBe("12.3 s");
    expect(busyTime(7_500_000)).toBe("2 h 05 min");
    expect(busyTime(0)).toBe("—");
  });
});
