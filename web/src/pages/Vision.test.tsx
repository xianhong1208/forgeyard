// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { boxStyle, checkImage, depthTables, labelColours, promptFrom, shown, toImage } from "../lib/vision";
import { visionError } from "./PlaygroundVision";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
const TASKS = [
  { task: "detect", title: "Object detection", template: "object-detection", model: "PekingU/rtdetr_v2_r18vd" },
  { task: "segment", title: "Segmentation", template: "segmentation", model: "facebook/sam2.1-hiera-tiny" },
  { task: "depth", title: "Depth estimation", template: "depth", model: "depth-anything/Depth-Anything-V2-Small-hf" },
];
const base = { licence: "apache-2.0", ready: 1, reason: null, max_body_mb: 32 };
const WORKLOADS = [
  { ...base, name: "detector", workload_id: 7, task: "detect", title: "Object detection", model: TASKS[0].model, endpoint: "/v1/detect", path: "/x/detector" },
  { ...base, name: "segmenter", workload_id: 8, task: "segment", title: "Segmentation", model: TASKS[1].model, endpoint: "/v1/segment", path: "/x/segmenter" },
  { ...base, name: "depth", workload_id: 9, task: "depth", title: "Depth estimation", model: TASKS[2].model, endpoint: "/v1/depth", path: "/x/depth", max_body_mb: 4 },
];
// A 1x1 PNG, standing in for a mask and a depth map.
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==";
const DETECTED = { model: TASKS[0].model, width: 640, height: 480, ms: 41.2, detections: [
  { label: "cat", score: 0.97, box: [64, 48, 320, 240] },
  { label: "remote", score: 0.61, box: [400, 10, 480, 60] },
  { label: "couch", score: 0.31, box: [0, 200, 640, 480] },
] };

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url === "/api/routes" ? { status: 200, body: { cloud: "off", is_admin: true, routes: [] } }
        : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const options = (workloads = WORKLOADS) => ({ status: 200, body: { workloads, tasks: TASKS } });

function mount(path = "/playground?tab=vision") {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

function photo(name = "cats.jpg", type = "image/jpeg", size?: number): File {
  const f = new File(["fake image bytes"], name, { type });
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

async function panelFor(title: string): Promise<HTMLElement> {
  return (await screen.findByRole("heading", { name: title })).closest("section") as HTMLElement;
}

async function pick(panel: HTMLElement, file: File) {
  fireEvent.change(within(panel).getByLabelText("Image"), { target: { files: [file] } });
  return within(panel).findByAltText(/Your image/);
}

// jsdom loads no images: give the picture its natural size and its on-screen box, then fire load.
function loaded(img: HTMLElement, natural: [number, number], shownAs: [number, number]) {
  Object.defineProperty(img, "naturalWidth", { value: natural[0], configurable: true });
  Object.defineProperty(img, "naturalHeight", { value: natural[1], configurable: true });
  img.getBoundingClientRect = () => ({ left: 10, top: 20, width: shownAs[0], height: shownAs[1], right: 10 + shownAs[0], bottom: 20 + shownAs[1], x: 10, y: 20, toJSON: () => ({}) });
  fireEvent.load(img);
}

beforeEach(() => {
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => `blob:photo-${++n}`), revokeObjectURL: vi.fn() }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Playground: Vision (1.3)", () => {
  it("FY-WEB-189: with no exposed vision workload, says how to start one from a template (operators get the link)", async () => {
    server((url) => (url === "/api/playground/vision/options" ? options([]) : undefined));
    mount();
    expect(await screen.findByRole("heading", { name: "No vision workload to try yet" })).toBeTruthy();
    expect(screen.getByText(/Start one from a template on the Workloads page/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Start from a template/ }).getAttribute("href")).toBe("/workloads");
    expect(screen.getByText("facebook/sam2.1-hiera-tiny")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Vision" }).getAttribute("aria-selected")).toBe("true");
    cleanup();
    server((url) => (url === "/api/playground/vision/options" ? options([]) : undefined), USER);
    mount();
    expect(await screen.findByText(/ask one to start the one you need/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Start from a template/ })).toBeNull();
  });

  it("FY-WEB-190: detection sends the image through the console route and draws boxes with label and score, filtered by the slider", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options();
      if (url === "/api/playground/vision/detector/detect") {
        expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
        expect(init?.method).toBe("POST");
        sent.push(init?.body as FormData);
        return { status: 200, body: DETECTED, headers: { "Content-Type": "application/json", "X-Forgeyard-Target": "workload:detector#0" } };
      }
    });
    mount();
    const panel = await panelFor("Object detection");
    const select = screen.getByLabelText("Workload") as HTMLSelectElement;
    expect([...select.options].map((o) => o.text)).toEqual([
      "detector · Object detection · rtdetr_v2_r18vd", "segmenter · Segmentation · sam2.1-hiera-tiny",
      "depth · Depth estimation · Depth-Anything-V2-Small-hf"]);
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Detect objects" }));
    const boxes = await within(panel).findByRole("list", { name: "Detected objects" });
    expect((sent[0].get("image") as File).name).toBe("cats.jpg");
    expect(sent[0].get("threshold")).toBe("0.1");
    // At the default 0.5: the cat and the remote, placed in percent of the 640x480 picture.
    const shownBoxes = within(boxes).getAllByRole("listitem");
    expect(shownBoxes.map((b) => b.getAttribute("aria-label"))).toEqual(["cat 0.97", "remote 0.61"]);
    expect(shownBoxes[0].style.left).toBe("10%");
    expect(shownBoxes[0].style.top).toBe("10%");
    expect(shownBoxes[0].style.width).toBe("40%");
    expect(shownBoxes[0].style.height).toBe("40%");
    expect(within(shownBoxes[0]).getByText("cat 0.97")).toBeTruthy();
    expect(within(panel).getByText("2 of 3 objects shown")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText(/Score at least/), { target: { value: "0.3" } });
    expect(within(boxes).getAllByRole("listitem")).toHaveLength(3);
    expect(within(within(panel).getByRole("list", { name: "Objects" })).getAllByRole("listitem").map((li) => li.textContent))
      .toEqual(["cat 0.97", "remote 0.61", "couch 0.31"]);
    const [cat, remote] = within(boxes).getAllByRole("listitem");
    expect(cat.style.borderColor).not.toBe(remote.style.borderColor);
    fireEvent.change(within(panel).getByLabelText(/Score at least/), { target: { value: "0.95" } });
    expect(within(boxes).getAllByRole("listitem").map((b) => b.getAttribute("aria-label"))).toEqual(["cat 0.97"]);
    expect(within(panel).getByText(/replica 0 · 640×480 · 3 objects above 0.10 · model 41 ms/)).toBeTruthy();
  });

  it("FY-WEB-191: segmentation sends a dragged box or a clicked point in the picture's own pixels and shows the mask over it", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options();
      if (url === "/api/playground/vision/segmenter/segment") {
        sent.push(init?.body as FormData);
        return { status: 200, body: { model: TASKS[1].model, width: 640, height: 480, ms: 300, masks: [
          { score: 0.95, area: 40000, box: [200, 100, 400, 300], png: PNG },
          { score: 0.71, area: 52000, box: [180, 90, 420, 330], png: PNG },
        ] }, headers: { "X-Forgeyard-Target": "workload:segmenter#1" } };
      }
    });
    mount();
    await screen.findByRole("heading", { name: "Object detection" });
    fireEvent.change(screen.getByLabelText("Workload"), { target: { value: "segmenter" } });
    const panel = await panelFor("Segmentation");
    const img = await pick(panel, photo());
    loaded(img, [640, 480], [320, 240]);
    const stage = within(panel).getByTestId("vis-stage");
    // A drag from (100, 50) to (200, 150) on the half-size picture is the box [200, 100, 400, 300].
    fireEvent.pointerDown(stage, { clientX: 110, clientY: 70, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 210, clientY: 170, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 210, clientY: 170, pointerId: 1 });
    const mask = await within(panel).findByTestId("vis-mask");
    expect(sent[0].get("box")).toBe("[200,100,400,300]");
    expect(sent[0].get("points")).toBeNull();
    expect(mask.style.maskImage).toContain(`data:image/png;base64,${PNG}`);
    expect(mask.getAttribute("aria-label")).toBe("Mask 1: 40,000 pixels, score 0.95");
    expect(within(panel).getByTestId("vis-prompt").style.left).toBe("31.25%");
    fireEvent.click(within(panel).getByRole("button", { name: "Mask 2 · 0.71" }));
    expect(within(panel).getByTestId("vis-mask").getAttribute("aria-label")).toBe("Mask 2: 52,000 pixels, score 0.71");
    // A click (no move) is a point.
    fireEvent.pointerDown(stage, { clientX: 60, clientY: 45, pointerId: 2 });
    fireEvent.pointerUp(stage, { clientX: 61, clientY: 46, pointerId: 2 });
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1].get("points")).toBe("[[100,50]]");
    expect(sent[1].get("box")).toBeNull();
    await waitFor(() => expect(within(panel).getByTestId("vis-prompt").style.left).toBe("15.625%"));
  });

  it("FY-WEB-192: depth shows the depth map beside the photo, in colour or grey, with its legend", async () => {
    server((url) => {
      if (url === "/api/playground/vision/options") return options();
      if (url === "/api/playground/vision/depth/depth") {
        return { status: 200, body: { model: TASKS[2].model, width: 640, height: 480, ms: 900, depth_png: PNG, min: 0.81, max: 5.16 } };
      }
    });
    mount();
    await screen.findByRole("heading", { name: "Object detection" });
    fireEvent.change(screen.getByLabelText("Workload"), { target: { value: "depth" } });
    const panel = await panelFor("Depth estimation");
    expect(within(panel).getByText("PNG, JPEG or WebP, at most 4 MiB.")).toBeTruthy();
    await pick(panel, photo("street.png", "image/png"));
    fireEvent.click(within(panel).getByRole("button", { name: "Estimate depth" }));
    const depth = await within(panel).findByTestId("vis-depth");
    expect(depth.getAttribute("src")).toBe(`data:image/png;base64,${PNG}`);
    expect(depth.style.filter).toBe(`url("#fy-depth-colours")`);
    expect(within(panel).getAllByRole("figure")).toHaveLength(2);
    expect(within(panel).getByText("relative depth 0.81–5.16")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "Grey" }));
    expect(within(panel).getByTestId("vis-depth").style.filter).toBe("");
    expect(panel.querySelector("#fy-depth-colours feFuncR")?.getAttribute("tableValues")).toBe(depthTables().r);
  });

  it("FY-WEB-193: refuses what is not an image or too large before sending, and says what went wrong in plain words", async () => {
    let calls = 0;
    server((url) => {
      if (url === "/api/playground/vision/options") return options();
      if (url === "/api/playground/vision/detector/detect") {
        calls += 1;
        return { status: 429, body: { error: { message: "Every replica of 'detector' already has 1 request in flight.", type: "rate_limit_error", code: "workload_busy" } },
          headers: { "Retry-After": "1" } };
      }
    });
    mount();
    const panel = await panelFor("Object detection");
    fireEvent.change(within(panel).getByLabelText("Image"), { target: { files: [photo("notes.pdf", "application/pdf")] } });
    expect(await within(panel).findByText("notes.pdf is not a PNG, JPEG or WebP image.")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Image"), { target: { files: [photo("huge.jpg", "image/jpeg", 25 * 1024 * 1024)] } });
    expect(await within(panel).findByText("huge.jpg is 25.0 MiB: the most this workload takes is 20 MiB.")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: "Detect objects" })).toBeNull();
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Detect objects" }));
    expect(await within(panel).findByText("The workload is busy with other images right now. Try again in a moment.")).toBeTruthy();
    expect(calls).toBe(1);
    expect(visionError(413, { error: { code: "request_too_large", message: "x" } }, 4)).toBe("The image is too large for this workload: it takes at most 4 MiB.");
    expect(visionError(400, { error: { code: "invalid_image", message: "x" } }, 4)).toMatch(/could not read this image/);
    expect(visionError(503, { error: { code: "no_ready_replica", message: "The workload 'd' is stopped." } }, 4)).toBe("The workload is not ready: The workload 'd' is stopped.");
    expect(visionError(500, null, 4)).toBe("500: the server had a problem.");
  });

  it("FY-WEB-194: the Models and Vision tabs, and the overlay geometry", async () => {
    server((url) => (url === "/api/playground/vision/options" ? options() : undefined));
    mount("/playground");
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Vision" }));
    expect(await screen.findByRole("heading", { name: "Object detection" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Models" }));
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();

    expect(boxStyle([-10, 0, 700, 240], 640, 480)).toEqual({ left: "0.000%", top: "0.000%", width: "100.000%", height: "50.000%" });
    expect(toImage(160, 120, { width: 320, height: 240 }, 640, 480)).toEqual([320, 240]);
    expect(toImage(400, -5, { width: 320, height: 240 }, 640, 480)).toEqual([639, 0]);
    expect(promptFrom([10, 10], [12, 11], 3)).toEqual({ kind: "point", point: [10, 10] });
    expect(promptFrom([50, 80], [10, 20], 40)).toEqual({ kind: "box", box: [10, 20, 50, 80] });
    expect(shown(DETECTED.detections as never, 0.6).map((d) => d.label)).toEqual(["cat", "remote"]);
    const colours = labelColours([...DETECTED.detections, { label: "cat", score: 0.2, box: [0, 0, 1, 1] }] as never);
    expect([...colours.keys()]).toEqual(["cat", "remote", "couch"]);
    expect(new Set(colours.values()).size).toBe(3);
    expect(checkImage(photo("a.webp", "image/webp"), 20)).toBeNull();
  });
});
