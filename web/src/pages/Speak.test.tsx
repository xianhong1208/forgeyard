// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { pcmRate, pcmToWav, snippets } from "../lib/gateway";
import type { CatalogModel } from "../lib/types";
import { languagesText } from "./AddModel";
import { paramApplies } from "./ModelDeploy";
import { audioLength } from "./Usage";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const LOCAL = { local_only: false, spill_on_long_prompt: false, wait_ms: 0, kind: "local", has_local: true, has_cloud: false, targets: [], usable: true };
const ROUTES = { cloud: "off", is_admin: true, routes: [
  { ...LOCAL, id: 3, served_name: "voice", api: "openai-speech", upstream_model: "voice", deployment_id: 3,
    endpoints: [{ replica: 0, machine: "gpu-a", state: "healthy", ready: true, url: "http://10.0.0.5:31000/" }] },
] };
const VOICES = { object: "list", model: "voice", default: "vivian", voices: [{ id: "ryan", name: "ryan" }, { id: "vivian", name: "vivian" }],
  formats: ["wav", "pcm", "mp3"], default_format: "wav", stream_formats: ["audio", "sse"], max_input_chars: 40 };

type Reply = { status: number; body?: unknown; bytes?: Uint8Array<ArrayBuffer>; headers?: Record<string, string> };

function server(route: (url: string, init?: RequestInit) => Reply | undefined) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } }
      : route(url, init) ?? { status: 404, body: {} };
    if (r.bytes) return new Response(r.bytes, { status: r.status, headers: r.headers });
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:speech"), revokeObjectURL: vi.fn() }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Playground: Speak (1.3)", () => {
  it("FY-WEB-148: lists the model's voices and formats, speaks through the console endpoint and plays the answer", async () => {
    const sent: Record<string, unknown>[] = [];
    server((url, init) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/audio/voices?model=voice") return { status: 200, body: VOICES };
      if (url === "/api/playground/audio/speech") {
        sent.push(JSON.parse(String(init?.body)));
        expect((init?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
        return { status: 200, bytes: pcmToWav(new Uint8Array(4800), 24000),
          headers: { "Content-Type": "audio/wav", "X-Forgeyard-Replica": "voice#0" } };
      }
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Speak" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(panel).getByLabelText("Voice") as HTMLSelectElement).value).toBe("vivian"));
    expect([...(within(panel).getByLabelText("Format") as HTMLSelectElement).options].map((o) => o.value)).toEqual(["wav", "pcm", "mp3"]);
    fireEvent.change(within(panel).getByLabelText("Text"), { target: { value: "你好，世界" } });
    expect(within(panel).getByText("5 of at most 40 characters")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Voice"), { target: { value: "ryan" } });
    fireEvent.click(within(panel).getByLabelText(/Stream/));
    fireEvent.click(within(panel).getByRole("button", { name: "Speak" }));
    expect(await within(panel).findByLabelText("The speech")).toBeTruthy();
    expect(sent[0]).toEqual({ model: "voice", input: "你好，世界", voice: "ryan", response_format: "wav", stream_format: "audio" });
    expect(within(panel).getByText(/replica 0 · voice ryan · first bytes .* · 5 characters/)).toBeTruthy();
    expect(within(panel).getByRole("link", { name: /Download/ }).getAttribute("download")).toBe("voice.wav");
  });

  it("FY-WEB-149: refuses text over the cap before sending, and shows the gateway's error", async () => {
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/audio/voices?model=voice") return { status: 200, body: { ...VOICES, voices: [], default: null } };
      if (url === "/api/playground/audio/speech") {
        return { status: 400, body: { error: { message: "Voice cloning is not available.", code: "voice_cloning_not_supported" } } };
      }
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Speak" })).closest("section") as HTMLElement;
    expect(await within(panel).findByText("The model's own voice")).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText("Text"), { target: { value: "x".repeat(41) } });
    expect((within(panel).getByRole("button", { name: "Speak" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText("Text"), { target: { value: "Short." } });
    fireEvent.change(within(panel).getByLabelText("Format"), { target: { value: "mp3" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Speak" }));
    expect(await within(panel).findByText("400: Voice cloning is not available.")).toBeTruthy();
  });
});

describe("Playground: Speak, raw pcm (1.3)", () => {
  it("FY-WEB-152: wraps a pcm answer in a WAV header at the rate the answer states, so the player can play it", async () => {
    const blobs: Blob[] = [];
    (URL.createObjectURL as unknown as ReturnType<typeof vi.fn>).mockImplementation((b: Blob) => { blobs.push(b); return "blob:x"; });
    server((url) => {
      if (url === "/api/routes") return { status: 200, body: ROUTES };
      if (url === "/api/playground/audio/voices?model=voice") return { status: 200, body: VOICES };
      if (url === "/api/playground/audio/speech") {
        return { status: 200, bytes: new Uint8Array(3200), headers: { "Content-Type": "audio/pcm", "x-audio-sample-rate": "16000" } };
      }
    });
    mount("/playground");
    const panel = (await screen.findByRole("heading", { name: "Speak" })).closest("section") as HTMLElement;
    await waitFor(() => expect((within(panel).getByLabelText("Format") as HTMLSelectElement).value).toBe("wav"));
    fireEvent.change(within(panel).getByLabelText("Format"), { target: { value: "pcm" } });
    fireEvent.change(within(panel).getByLabelText("Text"), { target: { value: "Raw samples." } });
    fireEvent.click(within(panel).getByRole("button", { name: "Speak" }));
    expect(await within(panel).findByLabelText("The speech")).toBeTruthy();
    const wav = new Uint8Array(await blobs[0].arrayBuffer());
    expect(blobs[0].type).toBe("audio/wav");
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
    expect(new DataView(wav.buffer).getUint32(24, true)).toBe(16000);
    expect(wav.length).toBe(44 + 3200);
  });
});

describe("text-to-speech helpers (1.3)", () => {
  it("FY-WEB-150: wraps pcm in a WAV header, reads its rate, and writes speech snippets with the SDK", () => {
    const wav = pcmToWav(new Uint8Array([1, 2, 3, 4, 5]), 22050);
    const v = new DataView(wav.buffer);
    expect(String.fromCharCode(...wav.slice(0, 4))).toBe("RIFF");
    expect(v.getUint32(24, true)).toBe(22050);
    expect(v.getUint32(40, true)).toBe(4);       // whole 16-bit samples only
    expect(wav.length).toBe(48);
    expect(pcmRate(new Headers({ "x-audio-sample-rate": "16000" }))).toBe(16000);
    expect(pcmRate(new Headers({ "content-type": "audio/pcm;rate=22050;encoding=signed-int;bits=16" }))).toBe(22050);
    expect(pcmRate(new Headers())).toBe(24000);
    const s = snippets("http://h/v1", "fyk_x", "voice", "openai-speech");
    expect(s.curl).toContain("http://h/v1/audio/speech");
    expect(s.curl).toContain("-o hello.wav");
    expect(s.python).toContain("client.audio.speech.with_streaming_response.create(");
  });

  it("FY-WEB-151: the deploy form shows the input cap for speech models only, and their languages and audio length read well", () => {
    const tts = { task: "tts", modalities: [] } as unknown as CatalogModel;
    const chat = { task: "chat", modalities: ["image"] } as unknown as CatalogModel;
    expect(paramApplies("max_input_chars", tts)).toBe(true);
    expect(paramApplies("max_input_chars", chat)).toBe(false);
    expect(paramApplies("max_body_mb", tts)).toBe(false);
    expect(paramApplies("context", tts)).toBe(true);
    expect(languagesText(["zh", "en", "ja", "ko"])).toBe("Chinese, English +2");
    expect(languagesText(["en"])).toBe("English");
    expect(audioLength(0)).toBe("—");
    expect(audioLength(12_345)).toBe("12.3 s");
    expect(audioLength(65_000)).toBe("1 min 05 s");
  });
});
