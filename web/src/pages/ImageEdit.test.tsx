// @vitest-environment jsdom
// 1.5.5 (#143): images given to a model in the console. The Playground's Edit mode (/v1/images/edits) and a video's
// start image (/v1/videos input_reference), the server's new error codes in both languages, what the model page, the
// catalog and the recommended list say about such a model, and the deploy form's parameters for it (FY-WEB-976..985).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import en from "../i18n/en/server";
import { setLang } from "../i18n/lang";
import { serverMessage } from "../i18n/server";
import { abilities, editForm, imageModes, inputParamApplies, pickProblem, videoForm, type Picked } from "../lib/imageEdit";
import { playError } from "../lib/playground";
import type { CatalogModel, ModelPage } from "../lib/types";
import { paramApplies } from "./ModelDeploy";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const LOCAL = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [], usable: true };
const route = (id: number, name: string, api: string) => ({ ...LOCAL, id, served_name: name, api, upstream_model: name, deployment_id: id,
  endpoints: [{ replica: 0, machine: "gpu-01", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }] });
const ROUTES = { cloud: "off", is_admin: true, routes: [route(4, "klein", "openai-images"), route(5, "qedit", "openai-images"), route(6, "sd", "openai-images"),
  route(7, "ti2v", "openai-videos"), route(8, "i2v", "openai-videos"), route(9, "t2v", "openai-videos")] };
const IMAGE = { max_n: 2, max_side: 1024, default_size: "1024x1024", multiple: 16, formats: ["png", "jpeg", "webp"], max_steps: 50, max_prompt_chars: 4000 };
const IMG_OPTIONS: Record<string, unknown> = {
  klein: { model: "klein", ...IMAGE, generate: true, edit: 2, max_input_side: 2048, edit_formats: ["png", "jpeg", "webp"] },
  qedit: { model: "qedit", ...IMAGE, generate: false, edit: 1, max_input_side: 2048, edit_formats: ["png", "jpeg"] },
  sd: { model: "sd", ...IMAGE, generate: true, edit: 0, max_input_side: 0, edit_formats: [] },
};
const VIDEO = { default_size: "832x480", max_side: 832, max_pixels: 832 * 480, multiple: 16, seconds: 5, max_seconds: 5, fps: 16, max_steps: 50,
  max_prompt_chars: 2000, keep_minutes: 1440 };
const VID_OPTIONS: Record<string, unknown> = {
  ti2v: { model: "ti2v", ...VIDEO, image_input: "optional", max_input_side: 2048 },
  i2v: { model: "i2v", ...VIDEO, image_input: "required", max_input_side: 1024 },
  t2v: { model: "t2v", ...VIDEO, image_input: "none", max_input_side: 0 },
};
// A 1x1 PNG.
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg==";
const png = (name: string) => new File([Uint8Array.from(atob(PNG), (c) => c.charCodeAt(0))], name, { type: "image/png" });
const NOW = Math.floor(Date.now() / 1000);
const job = { id: "video_0123456789abcdef0123456789abcdef", object: "video", model: "ti2v", status: "queued", progress: 0, created_at: NOW,
  completed_at: null, expires_at: null, size: "832x480", seconds: "5", error: null };

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

function server(routeFn: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : routeFn(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function playground(extra?: (url: string, init?: RequestInit) => Reply | undefined) {
  return server((url, init) => {
    if (url === "/api/routes") return { status: 200, body: ROUTES };
    const img = /^\/api\/playground\/images\/options\?model=(\w+)$/.exec(url);
    if (img) return { status: 200, body: IMG_OPTIONS[img[1]] };
    const vid = /^\/api\/playground\/videos\/options\?model=(\w+)$/.exec(url);
    if (vid) return { status: 200, body: VID_OPTIONS[vid[1]] };
    if (url === "/api/playground/videos?limit=20") return { status: 200, body: { object: "list", data: [] } };
    return extra?.(url, init);
  });
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

// The entries of a multipart body the panel sent: plain fields as text, files as "file:<name>".
function entries(body: unknown): [string, string][] {
  expect(body).toBeInstanceOf(FormData);
  return [...(body as FormData).entries()].map(([k, v]) => [k, typeof v === "string" ? v : `file:${(v as File).name}`]);
}

async function imagePanel(name = "Image"): Promise<HTMLElement> {
  const p = (await screen.findByRole("heading", { name })).closest("section") as HTMLElement;
  await waitFor(() => expect((within(p).getByLabelText("Images") as HTMLSelectElement).disabled).toBe(false));
  return p;
}

beforeEach(() => {
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => `blob:image-${++n}`), revokeObjectURL: vi.fn() }));
});

afterEach(() => {
  act(() => setLang("en"));
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const catalog = (over: Partial<CatalogModel>): CatalogModel => ({
  id: 1, name: "m", title: null, source: "hf://acme/m", kind: "hf", ref: "main", revision: "a".repeat(40), file: null, format: "diffusers",
  task: "image", task_label: "Images", size_bytes: 1, engine_args: {}, recipes: [], deployments: 0, created_by: null, created_at: "2026-11-24T00:00:00",
  summary: {}, gguf_files: [], ...over,
}) as CatalogModel;

describe("Images given to a model (1.5.5, #143)", () => {
  it("FY-WEB-976 what a model does with images, what can be picked, and what is sent", () => {
    // Modes: generate first, edit when the model edits; a model that only edits is Edit alone; none known: Generate.
    expect(imageModes({ generate: true, edit: 4 })).toEqual(["generate", "edit"]);
    expect(imageModes({ generate: false, edit: 1 })).toEqual(["edit"]);
    expect(imageModes({ generate: true, edit: 0 })).toEqual(["generate"]);
    expect(imageModes({})).toEqual(["generate"]);
    expect(imageModes(null)).toEqual(["generate"]);
    // Picking: the count first, then the type (HEIC is not read by either engine), then the size.
    expect(pickProblem([png("a.png")], 0, 1)).toBeNull();
    expect(pickProblem([png("a.png"), png("b.png")], 1, 2)).toBe("At most 2 images.");
    expect(pickProblem([png("a.png")], 1, 1)).toBe("It takes one image: remove this one first.");
    expect(pickProblem([new File(["x"], "IMG_1.heic", { type: "image/heic" })], 0, 4)).toContain("IMG_1.heic is not a PNG, JPEG or WebP image");
    expect(pickProblem([new File(["x"], "a.gif", { type: "image/gif" })], 0, 4)).toContain("not a PNG, JPEG or WebP");
    const big = new File(["x"], "big.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: 21 * 1024 * 1024 });
    expect(pickProblem([big], 0, 4)).toBe("big.png is larger than 20 MiB.");
    // The edit as the SDK sends it: images in order as image[]; size and format only when chosen.
    const pics: Picked[] = [{ name: "a.png", uri: "data:", file: png("a.png") }, { name: "b.png", uri: "data:", file: png("b.png") }];
    expect(entries(editForm("klein", "Make it blue", 2, "", "", pics))).toEqual([["model", "klein"], ["prompt", "Make it blue"], ["n", "2"],
      ["image[]", "file:a.png"], ["image[]", "file:b.png"]]);
    expect(entries(editForm("klein", "x", 1, "768x768", "jpeg", pics.slice(0, 1)))).toEqual([["model", "klein"], ["prompt", "x"], ["n", "1"],
      ["size", "768x768"], ["output_format", "jpeg"], ["image[]", "file:a.png"]]);
    expect(entries(videoForm("ti2v", "Waves", "832x480", "5", pics[0]))).toEqual([["model", "ti2v"], ["prompt", "Waves"], ["size", "832x480"],
      ["seconds", "5"], ["input_reference", "file:a.png"]]);
    // What the catalog says: edits (only), image to video (optional / required), nothing for the rest.
    const klein = catalog({ image: { size: 1024, multiple: 16, single: false, components: {}, pipeline: "Flux2KleinPipeline", parts: {}, edit: 4, generate: true } });
    expect(abilities(klein).map((a) => [a.text, a.title])).toEqual([["Edits images", "/v1/images/edits: an instruction and up to 4 input images"]]);
    const qedit = catalog({ image: { size: 1024, multiple: 16, single: false, components: {}, pipeline: "QwenImageEditPipeline", parts: {}, edit: 1, generate: false } });
    expect(abilities(qedit).map((a) => [a.text, a.title])).toEqual([["Edits images only", "/v1/images/edits: an instruction and 1 input image"]]);
    const sd = catalog({ image: { size: 512, multiple: 64, single: true, components: {}, pipeline: null, parts: {}, edit: 0, generate: true } });
    expect(abilities(sd)).toEqual([]);
    // An entry that does not say whether it makes images from text is not called "only".
    const unsaid = catalog({ image: { size: 1024, multiple: 16, single: false, components: {}, pipeline: null, parts: {}, edit: 2 } });
    expect(abilities(unsaid).map((a) => a.text)).toEqual(["Edits images"]);
    const video = (image_input?: "none" | "optional" | "required") => catalog({ task: "video", video: { size: [1280, 704], multiple: 32, fps: 24, pipeline: "WanPipeline", parts: {}, image_input } });
    expect(abilities(video("optional")).map((a) => a.text)).toEqual(["Image to video"]);
    expect(abilities(video("required")).map((a) => a.text)).toEqual(["Image to video only"]);
    expect(abilities(video("none"))).toEqual([]);
    expect(abilities(video())).toEqual([]);
    // The deploy form's input parameters: shown for a model that takes images or does not say, hidden otherwise.
    for (const name of ["max_input_images", "max_input_side"]) {
      expect(inputParamApplies(name, klein)).toBe(true);
      expect(inputParamApplies(name, sd)).toBe(false);
      expect(paramApplies(name, sd)).toBe(false);
      expect(inputParamApplies(name, catalog({ image: null }))).toBe(true);
      expect(inputParamApplies(name, undefined)).toBe(true);
    }
    expect(inputParamApplies("max_input_side", video("optional"))).toBe(true);
    expect(inputParamApplies("max_input_side", video("none"))).toBe(false);
    expect(inputParamApplies("max_input_side", video())).toBe(true);
    expect(inputParamApplies("max_images", sd)).toBe(true);
  });

  it("FY-WEB-977 Edit mode sends the images as image[] with the instruction and shows the images given beside the results", async () => {
    const sent: unknown[] = [];
    playground((url, init) => {
      if (url === "/api/playground/images/edits") {
        sent.push(init?.body);
        expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
        expect((init?.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
        return { status: 200, body: { created: 1, data: [{ b64_json: PNG }, { b64_json: PNG }], output_format: "jpeg", size: "1024x768" },
          headers: { "X-Forgeyard-Replica": "klein#0" } };
      }
      if (url === "/api/playground/images/generations") {
        sent.push(JSON.parse(String(init?.body)));
        return { status: 200, body: { created: 1, data: [{ b64_json: PNG }], output_format: "png", size: "1024x1024" } };
      }
    });
    mount("/playground?model=klein");
    const p = await imagePanel();
    // Generate first, as before; Edit beside it.
    expect(within(p).getByRole("button", { name: "Make from text" }).getAttribute("aria-pressed")).toBe("true");
    expect(within(p).getByRole("button", { name: /Generate/ })).toBeTruthy();
    fireEvent.click(within(p).getByRole("button", { name: "Edit images" }));
    expect(within(p).getByLabelText("Instruction")).toBeTruthy();
    expect((within(p).getByLabelText("Instruction") as HTMLTextAreaElement).value).toBe("Turn it into a watercolour painting; keep everything where it is.");
    const edit = within(p).getByRole("button", { name: "Edit" }) as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    expect(within(p).getByText("Add an image to edit first.")).toBeTruthy();
    expect(within(p).getByText(/at most 2048 pixels on a side\. The first image sets the shape/)).toBeTruthy();
    const input = () => within(p).getByLabelText(/^Choose: Images to edit/, { selector: "input" });
    await act(async () => fireEvent.change(input(), { target: { files: [png("room.png"), png("sofa.png")] } }));
    expect(await within(p).findByText("Images to edit (2 of at most 2)")).toBeTruthy();
    expect(within(p).getByRole("list", { name: /Chosen: Images to edit/ }).textContent).toContain("1. room.png");
    // A third is refused here, before anything is read or sent.
    await act(async () => fireEvent.change(input(), { target: { files: [png("lamp.png")] } }));
    expect(within(p).getByRole("alert").textContent).toBe("At most 2 images.");
    // Sizes: the first image's shape by default (no size sent), or one the deployment takes; formats of its edits.
    expect([...(within(p).getByLabelText("Size") as HTMLSelectElement).options].map((o) => o.textContent)[0]).toBe("Like the first image (default)");
    expect([...(within(p).getByLabelText("Format") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["png", "jpeg", "webp"]);
    fireEvent.change(within(p).getByLabelText("Format"), { target: { value: "jpeg" } });
    fireEvent.change(within(p).getByLabelText("Images"), { target: { value: "2" } });
    fireEvent.change(within(p).getByLabelText("Instruction"), { target: { value: "Put the sofa from image 2 in the room" } });
    fireEvent.click(within(p).getByRole("button", { name: "Edit" }));
    const out = await within(p).findByLabelText("Edited images");
    expect(entries(sent[0])).toEqual([["model", "klein"], ["prompt", "Put the sofa from image 2 in the room"], ["n", "2"], ["output_format", "jpeg"],
      ["image[]", "file:room.png"], ["image[]", "file:sofa.png"]]);
    expect(within(out).getByText("replica 0 · from 2 images · 2 images · 1024x768 · JPEG", { exact: false })).toBeTruthy();
    expect(within(out).getAllByRole("img", { name: /^Image \d given:/ }).map((i) => i.getAttribute("alt"))).toEqual(["Image 1 given: room.png", "Image 2 given: sofa.png"]);
    expect(within(out).getAllByRole("link", { name: /Download/ }).map((a) => a.getAttribute("download"))).toEqual(["klein-edit-1.jpg", "klein-edit-2.jpg"]);
    // Back to Generate: the sample prompt follows the mode only when it was not changed; a generation sends JSON and
    // shows no images given, though the two chosen for editing are kept for the next edit.
    fireEvent.click(within(p).getByRole("button", { name: "Make from text" }));
    expect((within(p).getByLabelText("Prompt") as HTMLTextAreaElement).value).toBe("Put the sofa from image 2 in the room");
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    const made = await within(p).findByLabelText("Generated images");
    expect(sent[1]).toEqual({ model: "klein", prompt: "Put the sofa from image 2 in the room", n: 2, size: "1024x1024" });
    expect(within(made).queryByText("The images given")).toBeNull();
    expect(within(made).queryByText(/from 2 images/)).toBeNull();
    fireEvent.click(within(p).getByRole("button", { name: "Edit images" }));
    expect(within(p).getByText("Images to edit (2 of at most 2)")).toBeTruthy();
  });

  it("FY-WEB-978 a model that only edits opens in Edit; one that edits nothing has no Edit; the gateway's codes read in Chinese", async () => {
    playground((url) => {
      if (url === "/api/playground/images/edits") {
        return { status: 400, body: { error: { message: "'qedit' takes at most 1 input image per edit; this one has 2.", type: "invalid_request_error",
          param: "image", code: "too_many_input_images" } } };
      }
    });
    mount("/playground?model=qedit");
    let p = await imagePanel();
    expect(within(p).queryByRole("button", { name: "Make from text" })).toBeNull();
    expect(within(p).getByText(/This model edits images: give it one or more and say what to change/)).toBeTruthy();
    expect((within(p).getByLabelText("Instruction") as HTMLTextAreaElement).value).toBe("Turn it into a watercolour painting; keep everything where it is.");
    expect([...(within(p).getByLabelText("Format") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["png", "jpeg"]);
    await act(async () => fireEvent.change(within(p).getByLabelText(/^Choose: Images to edit/, { selector: "input" }), { target: { files: [png("a.png")] } }));
    await within(p).findByText("Images to edit (1 of at most 1)");
    expect((within(p).getByRole("button", { name: "Choose a file" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(p).getByRole("button", { name: "Edit" }));
    expect((await within(p).findByText(/^400: 'qedit' takes at most 1 input image/)).textContent).toBe("400: 'qedit' takes at most 1 input image per edit; this one has 2.");
    act(() => setLang("zh-TW"));
    p = (await screen.findByRole("heading", { name: "圖片" })).closest("section") as HTMLElement;
    expect(within(p).getByText("這個模型會編輯圖片：給它一張或多張圖片，再說要改什麼。它不會只靠文字產生圖片。")).toBeTruthy();
    expect(within(p).getByRole("button", { name: "編輯" })).toBeTruthy();
    fireEvent.click(within(p).getByRole("button", { name: "編輯" }));
    expect(await within(p).findByText("400：輸入的圖片太多：'qedit' takes at most 1 input image per edit; this one has 2.")).toBeTruthy();
    // A model that edits nothing: the 1.3 panel, no mode switch.
    cleanup();
    act(() => setLang("en"));
    mount("/playground?model=sd");
    p = await imagePanel();
    expect(within(p).queryByRole("group", { name: "What to do" })).toBeNull();
    expect(within(p).queryByRole("link", { name: "How to edit images" })).toBeNull();
    expect(within(p).getByLabelText("Prompt")).toBeTruthy();
  });

  it("FY-WEB-979 every code the gateway answers images and start images with has a translation that keeps the server's details", () => {
    const codes = ["edits_not_supported", "generation_not_supported", "missing_image", "mask_not_supported", "file_id_not_supported",
      "too_many_input_images", "input_image_too_large", "unsupported_image_format", "invalid_image", "media_too_large", "input_reference_required",
      "input_reference_not_supported", "image_too_large", "invalid_size", "unsupported_output_format"];
    for (const code of codes) {
      expect(Object.keys(en)).toContain(`server.code.${code}`);
      const zh = serverMessage(code, "DETAIL", "zh-TW");
      expect(zh, code).toContain("DETAIL");
      expect(zh.replace(/DETAIL|file_id|PNG|JPEG|WebP|Forgeyard/g, ""), code).not.toMatch(/[A-Za-z]{3,}/);
      expect(serverMessage(code, "As sent.", "en")).toBe("As sent.");
    }
    expect(playError(400, { error: { message: "Masks are not taken.", code: "mask_not_supported" } }, "zh-TW")).toBe("400：不接受遮罩：請在指示裡說明要改什麼。Masks are not taken.");
    expect(playError(400, { error: { message: "x", code: "input_reference_required" } }, "zh-TW")).toBe("400：這個模型要從圖片產生影片：請加入起始圖片。x");
  });

  it("FY-WEB-980 a video model's start image: required blocks Generate until there is one, sent as input_reference; optional and none", async () => {
    const sent: { model: string; body: unknown }[] = [];
    playground((url, init) => {
      if (url === "/api/playground/videos" && init?.method === "POST") {
        const body = init.body;
        sent.push({ model: body instanceof FormData ? String(body.get("model")) : JSON.parse(String(body)).model, body });
        return { status: 200, body: job };
      }
      if (url === `/api/playground/videos/${job.id}`) return { status: 200, body: job };
    });
    mount("/playground?model=i2v");
    let p = (await screen.findByRole("heading", { name: "Video" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(p).getByLabelText("Size") as HTMLSelectElement).disabled).toBe(false));
    expect(within(p).getByText("Start image (required)")).toBeTruthy();
    expect(within(p).getByText(/at most 1024 pixels on a side; it is scaled to the video's size/)).toBeTruthy();
    const go = () => within(p).getByRole("button", { name: /Generate/ }) as HTMLButtonElement;
    expect(go().disabled).toBe(true);
    expect(within(p).getByText("Choose a start image first.")).toBeTruthy();
    await act(async () => fireEvent.change(within(p).getByLabelText(/^Choose: Start image/, { selector: "input" }), { target: { files: [png("harbour.png")] } }));
    await waitFor(() => expect(go().disabled).toBe(false));
    fireEvent.change(within(p).getByLabelText("Prompt"), { target: { value: "The boats rock gently" } });
    fireEvent.click(go());
    await within(p).findByText("Started from harbour.png");
    expect(entries(sent[0].body)).toEqual([["model", "i2v"], ["prompt", "The boats rock gently"], ["size", "832x480"], ["seconds", "5"],
      ["input_reference", "file:harbour.png"]]);
    // Optional: without an image the request is JSON, as in 1.3.
    cleanup();
    mount("/playground?model=ti2v");
    p = (await screen.findByRole("heading", { name: "Video" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(p).getByLabelText("Size") as HTMLSelectElement).disabled).toBe(false));
    expect(within(p).getByText("Start image (optional)")).toBeTruthy();
    expect(within(p).getByRole("link", { name: "How to make a video from a picture" })).toBeTruthy();
    fireEvent.click(within(p).getByRole("button", { name: /Generate/ }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1].body).not.toBeInstanceOf(FormData);
    expect(JSON.parse(String(sent[1].body))).toEqual({ model: "ti2v", prompt: "A red kite flying over a green hill on a sunny day, slow camera pan.", size: "832x480", seconds: "5" });
    // None: no picker at all.
    cleanup();
    mount("/playground?model=t2v");
    p = (await screen.findByRole("heading", { name: "Video" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(p).getByLabelText("Size") as HTMLSelectElement).disabled).toBe(false));
    expect(within(p).queryByText(/Start image/)).toBeNull();
    expect(within(p).queryByRole("link", { name: "How to make a video from a picture" })).toBeNull();
  });

  it("FY-WEB-981 the Edit panel and the start image read in Chinese with no English left but names and formats", async () => {
    playground();
    act(() => setLang("zh-TW"));
    mount("/playground?model=klein");
    const p = (await screen.findByRole("heading", { name: "圖片" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(p).getByLabelText("張數") as HTMLSelectElement).disabled).toBe(false));
    fireEvent.click(within(p).getByRole("button", { name: "編輯圖片" }));
    await act(async () => fireEvent.change(within(p).getByLabelText(/^選擇：要編輯的圖片/, { selector: "input" }), { target: { files: [png("room.png")] } }));
    await within(p).findByText("要編輯的圖片（1 張，最多 2 張）");
    // The sample instruction is a prompt for the model (English, as the 1.3 sample prompt), not the console's words.
    const sample = (within(p).getByLabelText("指示") as HTMLTextAreaElement).value;
    const text = (p.textContent ?? "").replace(sample, "").replace(/room\.png|PNG|JPEG|WEBP|WebP|[0-9x]*\d[0-9x]*/g, "");
    expect(text).not.toMatch(/[A-Za-z]{2,}/);
    cleanup();
    mount("/playground?model=i2v");
    const v = (await screen.findByRole("heading", { name: "影片" })).closest("section") as HTMLElement;
    await waitFor(() => expect(within(v).getByText("起始圖片（必填）")).toBeTruthy());
    expect(within(v).getByText("請先選擇起始圖片。")).toBeTruthy();
  });
  it("FY-WEB-982 the model page says what a model does with images, links its model card's responsible-use notes, and the catalog marks it", async () => {
    const GIB = 1024 ** 3;
    const base = { kind: "hf", ref: "main", revision: "e7b7dc27f91deacad38e78976d1f2b499d76a294", file: null, format: "diffusers", size_bytes: 15 * GIB,
      engine_args: {}, recipes: ["vllm-omni-image"], deployments: 0, created_by: "owner", created_at: "2026-11-24T00:00:00", summary: {}, gguf_files: [],
      license: "apache-2.0", modalities: [], title: null };
    const img = (edit: number, generate: boolean) => ({ size: 1024, multiple: 16, single: false, components: {}, pipeline: "Flux2KleinPipeline", parts: {}, edit, generate });
    const vid = (image_input: string) => ({ size: [1280, 704], multiple: 32, fps: 24, pipeline: "WanPipeline", parts: {}, image_input });
    const models: Record<string, Partial<CatalogModel>> = {
      klein: { id: 1, name: "klein", source: "hf://black-forest-labs/FLUX.2-klein-4B", task: "image", task_label: "Images", image: img(4, true) as never },
      qedit: { id: 2, name: "qedit", source: "hf://Qwen/Qwen-Image-Edit", task: "image", task_label: "Images", image: img(1, false) as never },
      sd: { id: 3, name: "sd", source: "hf://second-state/stable-diffusion-v1-5-GGUF", task: "image", task_label: "Images", image: img(0, true) as never },
      ti2v: { id: 4, name: "ti2v", source: "hf://Wan-AI/Wan2.2-TI2V-5B-Diffusers", task: "video", task_label: "Video", recipes: ["vllm-omni-video"], video: vid("optional") as never },
      i2v: { id: 5, name: "i2v", source: "hf://Wan-AI/Wan2.2-I2V-A14B-Diffusers", task: "video", task_label: "Video", recipes: ["vllm-omni-video"], video: vid("required") as never },
    };
    const page = (m: Partial<CatalogModel>): ModelPage => ({ model: { ...base, ...m } as CatalogModel, recipes: [], can_manage: true, can_deploy: true,
      files: { recorded: false, downloaded_count: 0, downloaded_bytes: 0, files: [] }, machines: [], fits: {}, deployments: [] }) as unknown as ModelPage;
    server((url) => {
      const m = /^\/api\/models\/(\w+)$/.exec(url);
      if (m && models[m[1]]) return { status: 200, body: page(models[m[1]]) };
      if (url === "/api/machines") return { status: 200, body: { machines: [] } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [] } };
      if (url === "/api/models") return { status: 200, body: { models: Object.values(models).map((x) => ({ ...base, ...x })), recipes: [], can_manage: true } };
    });
    mount("/models/klein");
    await screen.findByRole("heading", { level: 1, name: "klein" });
    const chip = screen.getByText("Edits images").closest(".chip") as HTMLElement;
    expect(chip.getAttribute("title")).toBe("/v1/images/edits: an instruction and up to 4 input images");
    expect(screen.getByText("Up to 4 input images per request, with an instruction")).toBeTruthy();
    expect(screen.getByText(/a deployment takes 1 unless its Input images says more/)).toBeTruthy();
    const note = screen.getByRole("note");
    expect(note.textContent).toContain("No filter checks the images people send to this model or get back");
    expect(within(note).getByRole("link", { name: /Open the model card/ }).getAttribute("href")).toBe("https://huggingface.co/black-forest-labs/FLUX.2-klein-4B");
    cleanup();
    mount("/models/qedit");
    await screen.findByRole("heading", { level: 1, name: "qedit" });
    expect(screen.getByText("Edits images only")).toBeTruthy();
    expect(screen.getByText(/it makes no image from text alone/)).toBeTruthy();
    cleanup();
    mount("/models/sd");
    await screen.findByRole("heading", { level: 1, name: "sd" });
    expect(screen.queryByText(/Edits images/)).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
    cleanup();
    mount("/models/ti2v");
    await screen.findByRole("heading", { level: 1, name: "ti2v" });
    expect(screen.getByText("Image to video").closest(".chip")?.getAttribute("title")).toBe("/v1/videos: a start image (input_reference) is optional");
    expect(screen.getByText("Optional: from text, or from a picture")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("No filter checks the images");
    cleanup();
    act(() => setLang("zh-TW"));
    mount("/models/i2v");
    await screen.findByRole("heading", { level: 1, name: "i2v" });
    expect(screen.getByText("只能從圖片生成影片")).toBeTruthy();
    expect(screen.getByText("必填：每部影片都從一張圖片開始")).toBeTruthy();
    expect(within(screen.getByRole("note")).getByRole("link", { name: /開啟模型卡/ })).toBeTruthy();
    // The catalog's task column carries the same chips.
    cleanup();
    act(() => setLang("en"));
    mount("/models");
    const row = (name: string) => screen.getAllByRole("row").find((r) => within(r).queryByRole("link", { name }) !== null) as HTMLElement;
    await waitFor(() => expect(row("klein")).toBeTruthy());
    expect(within(row("klein")).getByText("Edits images")).toBeTruthy();
    expect(within(row("i2v")).getByText("Image to video only")).toBeTruthy();
    expect(within(row("sd")).queryByText(/Edits images|Image to video/)).toBeNull();
  });
  it("FY-WEB-983 the recommended list marks a model that edits images and one that makes a video from a picture", async () => {
    const entry = (name: string, task: string, group: string, can: string[]) => ({ name, title: name, source: `hf://acme/${name}`, revision: "a".repeat(40),
      file: null, group, task, format: "diffusers", recipe: task === "image" ? "vllm-omni-image" : "vllm-omni-video", device: "gpu", download_gib: 15,
      memory_gib: 34.7, gated: false, engine_args: {}, note: "A note.", in_catalog: null, license: "apache-2.0", inputs: [], mmproj: null, can });
    const recommended = { groups: [{ id: "images", title: "Images", description: "" }, { id: "video", title: "Video", description: "" }],
      models: [entry("klein", "image", "images", ["edit"]), entry("zimage", "image", "images", []), entry("ti2v", "video", "video", ["image-to-video"])] };
    server((url) => {
      if (url === "/api/models") return { status: 200, body: { models: [], recipes: [], can_manage: true, can_deploy: true,
        sources: { url_hosts: [], local_dirs: [], hf_endpoint: "https://huggingface.co", allow_private_hosts: false, env_seeds: [],
          hf_token: { set: false, last4: null, updated_by: null, updated_at: null } } } };
      if (url === "/api/models/recommended") return { status: 200, body: recommended };
      if (url === "/api/machines") return { status: 200, body: { machines: [] } };
      if (url === "/api/deployments") return { status: 200, body: { deployments: [], server_time: "", can_manage: true } };
      if (url === "/api/downloads") return { status: 200, body: { downloads: [], can_download: true } };
    });
    mount("/models");
    const li = async (name: string) => (await screen.findByText(name, { selector: ".strong" })).closest("li") as HTMLElement;
    expect(within(await li("klein")).getByText("Edits images").getAttribute("title")).toBe("What it does with images given to it");
    expect(within(await li("zimage")).queryByText("Edits images")).toBeNull();
    expect(within(await li("ti2v")).getByText("Image to video")).toBeTruthy();
    act(() => setLang("zh-TW"));
    await waitFor(() => expect(within(screen.getByText("klein", { selector: ".strong" }).closest("li") as HTMLElement).getByText("可編輯圖片")).toBeTruthy());
    expect(within(screen.getByText("ti2v", { selector: ".strong" }).closest("li") as HTMLElement).getByText("圖片生成影片")).toBeTruthy();
  });
});

