// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { boxStyle, checkImage, depthTables, labelColours, parseLabels, promptFrom, shown, toImage } from "../lib/vision";
import { visionError } from "./PlaygroundVision";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...OWNER, id: 2, username: "ursula", role: "user", is_owner: false };
const TASKS = [
  { task: "detect", title: "Object detection", template: "object-detection", model: "PekingU/rtdetr_v2_r18vd" },
  { task: "segment", title: "Segmentation", template: "segmentation", model: "facebook/sam2.1-hiera-tiny" },
  { task: "depth", title: "Depth estimation", template: "depth", model: "depth-anything/Depth-Anything-V2-Small-hf" },
];
const base = { licence: "apache-2.0", ready: 1, reason: null, max_body_mb: 32 };
// 1.4 (#86): the vision models the tab offers; the 1.3 apps (WORKLOADS) are still in the options but not offered.
const mbase = { licence: "apache-2.0", ready: 1, running: true, reason: null };
const MODELS = [
  { ...mbase, name: "det", task: "detect", title: "Object detection", endpoint: "/v1/vision/detect", deployment_id: 3, model: TASKS[0].model },
  { ...mbase, name: "seg", task: "segment", title: "Segmentation", endpoint: "/v1/vision/segment", deployment_id: 4, model: TASKS[1].model },
  { ...mbase, name: "dav2", task: "depth", title: "Depth estimation", endpoint: "/v1/vision/depth", deployment_id: 5, model: TASKS[2].model },
];
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

// The Playground's picker lists the ready routes (1.4, #100): one per vision model here.
const UP = [{ replica: 0, machine: "cpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }];
const visionRoutes = (models = MODELS) => models.map((m, i) => ({ id: i + 1, served_name: m.name, api: m.task === "image-classify" ? "vision-classify" : `vision-${m.task}`, upstream_model: null,
  deployment_id: m.deployment_id, local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false,
  targets: [], usable: true, endpoints: UP }));

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER, routes = visionRoutes()) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : url === "/api/routes" ? { status: 200, body: { cloud: "off", is_admin: true, routes } }
        : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const options = (models = MODELS) => ({ status: 200, body: { models, workloads: WORKLOADS, tasks: TASKS } });

function mount(path = "/playground?tab=vision") {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

// Choose a model in the Playground's one picker.
async function choose(name: string) {
  fireEvent.click(await screen.findByLabelText("Model"));
  fireEvent.click(within(screen.getByRole("listbox")).getByRole("option", { name: new RegExp(`^${name}`) }));
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

describe("Playground: vision models (1.4; one picker since #100)", () => {
  it("FY-WEB-189: with no model at all (exposed apps are not offered), the old Vision address says how to deploy one (operators get the link)", async () => {
    server((url) => (url === "/api/playground/vision/options" ? options([]) : undefined), OWNER, []);
    mount();
    expect(await screen.findByRole("heading", { name: "Nothing to try yet" })).toBeTruthy();
    expect(screen.getByText(/chat, embeddings, speech, images, video or vision/)).toBeTruthy();
    expect(screen.getByRole("link", { name: /Deploy from the catalog/ }).getAttribute("href")).toBe("/models");
    expect(screen.queryByRole("tab")).toBeNull();
    cleanup();
    server((url) => (url === "/api/playground/vision/options" ? options([]) : undefined), USER, []);
    mount();
    expect(await screen.findByText(/ask one to deploy the model you need/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Deploy from the catalog/ })).toBeNull();
  });

  it("FY-WEB-190: detection sends the image through the console route and draws boxes with label and score, filtered by the slider", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options();
      if (url === "/api/playground/vision/detect") {
        expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
        expect(init?.method).toBe("POST");
        sent.push(init?.body as FormData);
        return { status: 200, body: DETECTED, headers: { "Content-Type": "application/json", "X-Forgeyard-Target": "local:det#0" } };
      }
    });
    mount();
    const panel = await panelFor("Object detection");
    fireEvent.click(screen.getByLabelText("Model"));
    expect(within(screen.getByRole("listbox")).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "det object detection", "seg segmentation", "dav2 depth"]);
    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
    expect(within(panel).getByText("POST /v1/vision/detect with model=det")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Deployment" }).getAttribute("href")).toBe("/serving/3");
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Detect objects" }));
    const boxes = await within(panel).findByRole("list", { name: "Detected objects" });
    expect((sent[0].get("image") as File).name).toBe("cats.jpg");
    expect(sent[0].get("model")).toBe("det");
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
      if (url === "/api/playground/vision/segment") {
        sent.push(init?.body as FormData);
        return { status: 200, body: { model: TASKS[1].model, width: 640, height: 480, ms: 300, masks: [
          { score: 0.95, area: 40000, box: [200, 100, 400, 300], png: PNG },
          { score: 0.71, area: 52000, box: [180, 90, 420, 330], png: PNG },
        ] }, headers: { "X-Forgeyard-Target": "local:seg#1" } };
      }
    });
    mount();
    await screen.findByRole("heading", { name: "Object detection" });
    await choose("seg");
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
    expect(sent[0].get("model")).toBe("seg");
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
      if (url === "/api/playground/vision/depth") {
        return { status: 200, body: { model: TASKS[2].model, width: 640, height: 480, ms: 900, depth_png: PNG, min: 0.81, max: 5.16 } };
      }
    });
    mount();
    await screen.findByRole("heading", { name: "Object detection" });
    await choose("dav2");
    const panel = await panelFor("Depth estimation");
    expect(within(panel).getByText("PNG, JPEG or WebP, at most 20 MiB.")).toBeTruthy();
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
      if (url === "/api/playground/vision/detect") {
        calls += 1;
        return { status: 400, body: { error: { message: "The image could not be read (send a PNG, JPEG or WebP).", code: "invalid_image" } } };
      }
    });
    mount();
    const panel = await panelFor("Object detection");
    fireEvent.change(within(panel).getByLabelText("Image"), { target: { files: [photo("notes.pdf", "application/pdf")] } });
    expect(await within(panel).findByText("notes.pdf is not a PNG, JPEG or WebP image.")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Image"), { target: { files: [photo("huge.jpg", "image/jpeg", 25 * 1024 * 1024)] } });
    expect(await within(panel).findByText("huge.jpg is 25.0 MiB: the most it takes is 20 MiB.")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: "Detect objects" })).toBeNull();
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Detect objects" }));
    expect(await within(panel).findByText("The model could not read this image. Send a PNG, JPEG or WebP picture.")).toBeTruthy();
    expect(calls).toBe(1);
    expect(visionError(413, { error: { code: "request_too_large", message: "x" } }, 4)).toBe("The image is too large for this model: it takes at most 4 MiB.");
    expect(visionError(400, { error: { code: "invalid_image", message: "x" } }, 4)).toMatch(/could not read this image/);
    expect(visionError(503, { error: { code: "no_ready_replica", message: "The workload 'd' is stopped." } }, 4)).toBe("The model is not ready: The workload 'd' is stopped.");
    expect(visionError(500, null, 4)).toBe("500: the server had a problem.");
    expect(visionError(404, { error: { code: "model_not_found", message: "x" } }, 4)).toMatch(/no longer deployed/);
    expect(visionError(504, { error: { code: "upstream_timeout", message: "x" } }, 4)).toMatch(/^The model did not answer in time/);
  });

  it("FY-WEB-194: a vision model opens from the Playground's address with no tab, and the overlay geometry", async () => {
    server((url) => (url === "/api/playground/vision/options" ? options() : undefined));
    mount("/playground?model=seg");
    expect(await screen.findByRole("heading", { name: "Segmentation" })).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();

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

  // 1.4.4 (#142): a zero-shot detector is told what to find.
  const ZS = { ...mbase, name: "gd", task: "detect", title: "Object detection", endpoint: "/v1/vision/detect", deployment_id: 6,
    model: "IDEA-Research/grounding-dino-tiny", zero_shot: true };
  const FOUND = { model: ZS.model, width: 640, height: 480, ms: 3900, detections: [
    { label: "cat", score: 0.8, box: [10, 54, 317, 475] }, { label: "remote control", score: 0.49, box: [40, 72, 176, 118] },
    { label: "cat", score: 0.21, box: [0, 0, 640, 480] }] };

  it("FY-WEB-500: a zero-shot detector asks what to find, sends the names as labels, and its slider starts at 0.3", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options([...MODELS, ZS]);
      if (url === "/api/playground/vision/detect") {
        sent.push(init?.body as FormData);
        return { status: 200, body: FOUND, headers: { "Content-Type": "application/json", "X-Forgeyard-Target": "local:gd#0" } };
      }
    }, OWNER, visionRoutes([...MODELS, ZS]));
    mount("/playground?model=gd");
    const panel = await panelFor("Object detection");
    expect(within(panel).getByText(/Finds what you name and draws a box/)).toBeTruthy();
    await pick(panel, photo());
    const find = within(panel).getByRole("button", { name: "Find" });
    expect((find as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Find these"), { target: { value: " cat, remote   control,, Cat " } });
    fireEvent.click(find);
    const boxes = await within(panel).findByRole("list", { name: "Detected objects" });
    expect(JSON.parse(sent[0].get("labels") as string)).toEqual(["cat", "remote control"]);
    expect(sent[0].get("model")).toBe("gd");
    expect(sent[0].get("threshold")).toBe("0.1");
    expect(within(boxes).getAllByRole("listitem").map((b) => b.getAttribute("aria-label"))).toEqual(["cat 0.80", "remote control 0.49"]);
    expect(within(panel).getByText("2 of 3 objects shown")).toBeTruthy();
    // Enter in the box asks again.
    fireEvent.change(within(panel).getByLabelText("Find these"), { target: { value: "sofa" } });
    fireEvent.keyDown(within(panel).getByLabelText("Find these"), { key: "Enter" });
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(JSON.parse(sent[1].get("labels") as string)).toEqual(["sofa"]);
  });

  it("FY-WEB-501: a detector with fixed classes has no Find these box and sends no labels; the zero-shot refusals read plainly", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options([...MODELS, ZS]);
      if (url === "/api/playground/vision/detect") {
        sent.push(init?.body as FormData);
        return { status: 200, body: DETECTED, headers: { "Content-Type": "application/json" } };
      }
    }, OWNER, visionRoutes([...MODELS, ZS]));
    mount("/playground?model=det");
    const panel = await panelFor("Object detection");
    expect(within(panel).queryByLabelText("Find these")).toBeNull();
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Detect objects" }));
    await within(panel).findByRole("list", { name: "Detected objects" });
    expect(sent[0].get("labels")).toBeNull();
    expect(visionError(400, { error: { code: "labels_required", message: "x" } }, 20)).toMatch(/^This model finds what you name/);
    expect(visionError(400, { error: { code: "labels_not_supported", message: "x" } }, 20)).toMatch(/fixed set of classes/);
  });

  it("FY-WEB-502: the names typed are checked before anything is sent", () => {
    expect(parseLabels(" cat , remote  control,, CAT,dog ")).toEqual({ labels: ["cat", "remote control", "dog"], error: null });
    expect(parseLabels(" , ").error).toMatch(/Name at least one thing/);
    expect(parseLabels(Array.from({ length: 33 }, (_, i) => `l${i}`).join(",")).error).toBe("At most 32 things at once.");
    expect(parseLabels("x".repeat(65)).error).toMatch(/longer than 64 characters/);
    expect(parseLabels("x".repeat(64)).error).toBeNull();
  });

  // 1.4.4 (#141): image classification, fixed classes or zero-shot.
  const VIT = { ...mbase, name: "vit", task: "image-classify", title: "Image classification", endpoint: "/v1/vision/classify", deployment_id: 7,
    model: "google/vit-base-patch16-224", zero_shot: false };
  const SIG = { ...VIT, name: "siglip2", deployment_id: 8, model: "google/siglip2-base-patch16-224", zero_shot: true };
  const classified = (labels: { label: string; score: number }[], multi = false) =>
    ({ status: 200, body: { model: "m", width: 640, height: 480, ms: 150, labels, multi_label: multi }, headers: { "Content-Type": "application/json" } });

  it("FY-WEB-504: a classifier with fixed classes sends the picture to /classify and lists the best classes with their scores", async () => {
    const sent: FormData[] = [];
    const urls: string[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options([...MODELS, VIT, SIG]);
      if (url.startsWith("/api/playground/vision/")) {
        urls.push(url);
        sent.push(init?.body as FormData);
        return classified([{ label: "tabby, tabby cat", score: 0.8562 }, { label: "Egyptian cat", score: 0.069 }]);
      }
    }, OWNER, visionRoutes([...MODELS, VIT, SIG]));
    mount("/playground?model=vit");
    const panel = await panelFor("Image classification");
    expect(within(panel).getByText("POST /v1/vision/classify with model=vit")).toBeTruthy();
    expect(within(panel).getByText(/Says what the picture shows/)).toBeTruthy();
    expect(within(panel).queryByLabelText("Labels")).toBeNull();
    await pick(panel, photo());
    fireEvent.click(within(panel).getByRole("button", { name: "Classify" }));
    const list = await within(panel).findByRole("list", { name: "Labels" });
    expect(urls).toEqual(["/api/playground/vision/classify"]);
    expect(sent[0].get("model")).toBe("vit");
    expect(sent[0].get("top_k")).toBe("5");
    expect(sent[0].get("labels")).toBeNull();
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["tabby, tabby cat0.856", "Egyptian cat0.069"]);
    expect(within(panel).getByText(/The scores share 1 between the labels/)).toBeTruthy();
    expect(within(panel).getByText(/2 labels · model 150 ms/)).toBeTruthy();
  });

  it("FY-WEB-505: a zero-shot classifier asks for labels, sends them, and says its scores stand on their own", async () => {
    const sent: FormData[] = [];
    server((url, init) => {
      if (url === "/api/playground/vision/options") return options([...MODELS, VIT, SIG]);
      if (url === "/api/playground/vision/classify") {
        sent.push(init?.body as FormData);
        return classified([{ label: "cats on a sofa", score: 0.6305 }, { label: "a remote control", score: 0.007 }, { label: "a dog", score: 0 }], true);
      }
    }, OWNER, visionRoutes([...MODELS, VIT, SIG]));
    mount("/playground?model=siglip2");
    const panel = await panelFor("Image classification");
    expect(within(panel).getByText(/Scores the picture against the labels you give/)).toBeTruthy();
    await pick(panel, photo());
    const go = within(panel).getByRole("button", { name: "Classify" });
    expect((go as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Labels"), { target: { value: "cats on a sofa, a remote control, a dog" } });
    fireEvent.click(go);
    const list = await within(panel).findByRole("list", { name: "Labels" });
    expect(JSON.parse(sent[0].get("labels") as string)).toEqual(["cats on a sofa", "a remote control", "a dog"]);
    expect(sent[0].get("top_k")).toBeNull();
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(panel).getByText(/Each label is scored on its own/)).toBeTruthy();
    expect(visionError(400, { error: { code: "labels_required", message: "This model classifies into the labels you send" } }, 20))
      .toMatch(/chooses among the labels you give/);
    expect(visionError(400, { error: { code: "labels_not_supported", message: "This classifier knows a fixed set" } }, 20))
      .toMatch(/cannot be given labels/);
  });
});

