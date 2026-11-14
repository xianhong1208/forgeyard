// The gateway (M4): snippets for a new key, numbers for usage, and reading a streamed answer.

import type { ApiKey } from "./types";

export const API_ENDPOINT: Record<string, string> = {
  "openai-chat": "/chat/completions",
  "openai-embeddings": "/embeddings",
  rerank: "/rerank",
  "openai-audio": "/audio/transcriptions",
  "openai-speech": "/audio/speech",
  "openai-images": "/images/generations",
  "openai-videos": "/videos",
};

export const API_KIND: Record<string, string> = {
  "openai-chat": "chat",
  "openai-embeddings": "embeddings",
  rerank: "rerank",
  "openai-audio": "speech to text",
  "openai-speech": "text to speech",
  "openai-images": "images",
  "openai-videos": "video",
};

// A curl call and a Python call with the official OpenAI SDK, for the model's protocol. With a
// trustUrl (the server's own certificate authority), both trust that CA's file, forgeyard-ca.pem.
export function snippets(gatewayUrl: string, key: string, model: string, api: string, trustUrl?: string | null): { curl: string; python: string } {
  const base = gatewayUrl.replace(/\/$/, "");
  const curl = trustUrl ? `# forgeyard-ca.pem: this server's certificate authority, from ${trustUrl}\ncurl --cacert forgeyard-ca.pem` : "curl";
  const caLines = trustUrl
    ? `# forgeyard-ca.pem: this server's certificate authority, from ${trustUrl}\nca = ssl.create_default_context(cafile="forgeyard-ca.pem")\n`
    : "";
  const sdk = trustUrl ? "import ssl\n\nimport httpx\nfrom openai import OpenAI\n\n" : "from openai import OpenAI\n\n";
  const client = `${caLines}client = OpenAI(base_url="${base}", api_key="${key}"${trustUrl ? ", http_client=httpx.Client(verify=ca)" : ""})`;
  if (api === "openai-embeddings") {
    return {
      curl: `${curl} ${base}/embeddings \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "input": "Forgeyard runs models on your own machines."}'`,
      python: `${sdk}${client}\nout = client.embeddings.create(model="${model}", input="Forgeyard runs models on your own machines.")\nprint(len(out.data[0].embedding))`,
    };
  }
  if (api === "rerank") {
    return {
      curl: `${curl} ${base}/rerank \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "query": "capital of France", "documents": ["Paris is in France.", "Berlin is in Germany."]}'`,
      python: `${trustUrl ? "import ssl\n\n" : ""}import httpx  # rerank is not part of the OpenAI SDK\n\n${caLines}r = httpx.post("${base}/rerank", headers={"Authorization": "Bearer ${key}"},\n               json={"model": "${model}", "query": "capital of France",\n                     "documents": ["Paris is in France.", "Berlin is in Germany."]}${trustUrl ? ",\n               verify=ca" : ""})\nprint(r.json()["results"])`,
    };
  }
  if (api === "openai-speech") {
    return {
      curl: `${curl} ${base}/audio/speech \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "input": "Hello from Forgeyard.", "voice": "alloy", "response_format": "wav"}' \\\n  -o hello.wav`,
      python: `${sdk}${client}\nwith client.audio.speech.with_streaming_response.create(\n    model="${model}", voice="alloy", input="Hello from Forgeyard.", response_format="wav",\n) as response:\n    response.stream_to_file("hello.wav")`,
    };
  }
  if (api === "openai-images") {
    return {
      curl: `${curl} ${base}/images/generations \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "prompt": "A lighthouse on a rocky coast at sunset", "size": "1024x1024"}' \\\n  | python3 -c 'import base64, json, sys; open("image.png", "wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'`,
      python: `import base64\n\n${sdk}${client}\nout = client.images.generate(model="${model}", prompt="A lighthouse on a rocky coast at sunset", size="1024x1024")\nwith open("image.png", "wb") as f:\n    f.write(base64.b64decode(out.data[0].b64_json))`,
    };
  }
  if (api === "openai-videos") {
    // A video is a job: ask for it, poll until it is made, then fetch the MP4.
    return {
      curl: `${curl} ${base}/videos \\\n  -H "Authorization: Bearer ${key}" \\\n  -F model=${model} -F prompt="A red kite over a green hill" -F seconds=5\n# then poll ${base}/videos/<id> until "status" is "completed", and:\n${curl} ${base}/videos/<id>/content -H "Authorization: Bearer ${key}" -o video.mp4`,
      python: `import time\n\n${sdk}${client}\nvideo = client.videos.create(model="${model}", prompt="A red kite over a green hill", seconds="5")\nwhile video.status in ("queued", "in_progress"):\n    time.sleep(5)\n    video = client.videos.retrieve(video.id)\nif video.status == "completed":\n    client.videos.download_content(video.id).write_to_file("video.mp4")\nelse:\n    print(video.error)`,
    };
  }
  if (api === "openai-audio") {
    return {
      curl: `${curl} ${base}/audio/transcriptions \\\n  -H "Authorization: Bearer ${key}" \\\n  -F model=${model} -F file=@speech.mp3`,
      python: `${sdk}${client}\nwith open("speech.mp3", "rb") as f:\n    print(client.audio.transcriptions.create(model="${model}", file=f).text)`,
    };
  }
  return {
    curl: `${curl} ${base}/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello!"}]}'`,
    python: `${sdk}${client}\nstream = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Hello!"}],\n    stream=True,\n)\nfor chunk in stream:\n    if chunk.choices:\n        print(chunk.choices[0].delta.content or "", end="", flush=True)`,
  };
}

export function num(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n.toLocaleString("en-US");
}

export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)} s` : `${Math.round(n)} ms`;
}

export function keyModels(k: ApiKey): string {
  return k.models === null ? "All deployments" : k.models.join(", ");
}

// Server-sent events from a fetch() body: calls onData with each `data:` payload as it arrives.
export async function readEvents(body: ReadableStream<Uint8Array>, onData: (data: string) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split(/\r?\n\r?\n/);
    buf = parts.pop() ?? "";
    for (const part of parts) {
      const data = part
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (data) onData(data);
    }
  }
  const rest = buf.trim();
  if (rest.startsWith("data:")) onData(rest.slice(5).trimStart());
}

// The playground's error text from either answer shape: the gateway's {"error": …} or the console's {"detail": …}.
export function answerError(status: number, body: unknown): string {
  const b = body as { error?: { message?: string }; detail?: { message?: string } } | null;
  const message = b?.error?.message ?? b?.detail?.message;
  if (message) return `${status}: ${message}`;
  return status >= 500 ? `${status}: the server had a problem.` : `Request failed (${status}).`;
}

// ------------------------------------------------------------------ images in chat (1.3)
export type Attached = { name: string; uri: string };

// The playground reads an image in the browser and sends it as a data: URI; the gateway checks it
// and refuses anything over the model's largest request (413).
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

export function readImage(file: File): Promise<Attached> {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith("image/")) {
      reject(new Error(`${file.name} is not an image.`));
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      reject(new Error(`${file.name} is larger than ${MAX_IMAGE_BYTES / 1024 / 1024} MiB.`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, uri: String(reader.result) });
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.readAsDataURL(file);
  });
}

// A message's content: plain text, or text and image parts when images are attached.
export function chatContent(text: string, images?: Attached[]): string | { type: string; text?: string; image_url?: { url: string } }[] {
  if (!images?.length) return text;
  return [...(text ? [{ type: "text", text }] : []), ...images.map((i) => ({ type: "image_url", image_url: { url: i.uri } }))];
}

// ------------------------------------------------------------------ text to speech (1.3)
export const SPEECH_FORMATS = ["mp3", "opus", "aac", "flac", "wav", "pcm"] as const;

// What an <audio> element plays for each format (pcm has no header: the Playground wraps it in a WAV).
export const SPEECH_MIME: Record<string, string> = {
  mp3: "audio/mpeg", opus: "audio/ogg", aac: "audio/aac", flac: "audio/flac", wav: "audio/wav", pcm: "audio/wav",
};

export type Voice = { id: string; name: string; language?: string; gender?: string };

// 16-bit mono PCM with a WAV header, so a browser can play it.
export function pcmToWav(pcm: Uint8Array, rate: number, channels = 1): Uint8Array<ArrayBuffer> {
  const n = pcm.length - (pcm.length % (2 * channels));
  const out = new Uint8Array(44 + n);
  const v = new DataView(out.buffer);
  const text = (at: number, s: string) => { for (let i = 0; i < s.length; i++) out[at + i] = s.charCodeAt(i); };
  text(0, "RIFF"); v.setUint32(4, 36 + n, true); text(8, "WAVE"); text(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, channels, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2 * channels, true); v.setUint16(32, 2 * channels, true); v.setUint16(34, 16, true);
  text(36, "data"); v.setUint32(40, n, true);
  out.set(pcm.subarray(0, n), 44);
  return out;
}

// The sample rate of a pcm answer: x-audio-sample-rate (vLLM-Omni), the Content-Type's rate= (llama.cpp), else 24 kHz.
export function pcmRate(headers: Headers): number {
  const h = Number(headers.get("x-audio-sample-rate"));
  if (h > 0) return h;
  const m = /rate=(\d+)/.exec(headers.get("content-type") ?? "");
  return m ? Number(m[1]) : 24000;
}

// ------------------------------------------------------------------ image generation (1.3)
// What an image model's deployments take, from /api/playground/images/options.
export type ImageOptions = {
  model: string; max_n: number; max_side: number; default_size: string; multiple: number; formats: string[];
  max_steps: number; max_prompt_chars: number;
};

const IMAGE_PRESETS = ["256x256", "512x512", "768x768", "1024x1024", "1024x768", "768x1024", "1280x720", "720x1280",
  "1536x1024", "1024x1536", "2048x2048"];

// The sizes to offer: the deployment's default first, then the usual ones it takes (within its largest
// side, sides a multiple of what its model needs).
export function imageSizes(o: ImageOptions): string[] {
  const fits = (s: string) => {
    const [w, h] = s.split("x").map(Number);
    return w <= o.max_side && h <= o.max_side && w % o.multiple === 0 && h % o.multiple === 0;
  };
  return [o.default_size, ...IMAGE_PRESETS.filter((s) => s !== o.default_size && fits(s))];
}

export const IMAGE_MIME: Record<string, string> = { png: "image/png", jpeg: "image/jpeg", webp: "image/webp" };

// ------------------------------------------------------------------ video generation (1.3)
// What a video model's deployments take, from /api/playground/videos/options.
export type VideoOptions = {
  model: string; default_size: string; max_side: number; max_pixels: number; multiple: number; seconds: number;
  max_seconds: number; fps: number; max_steps: number; max_prompt_chars: number; keep_minutes: number;
};

// A video job as OpenAI's Videos API answers it.
export type Video = {
  id: string; object: "video"; model: string; status: "queued" | "in_progress" | "completed" | "failed"; progress: number;
  created_at: number; completed_at: number | null; expires_at: number | null; size: string; seconds: string;
  error: { code: string; message: string } | null;
};

const VIDEO_PRESETS = ["832x480", "480x832", "640x480", "480x640", "624x624", "1280x720", "720x1280", "1280x704", "704x1280",
  "1024x576", "576x1024"];

// The sizes to offer: the deployment's default first, then the usual ones it takes (within its largest side and
// its pixels per frame, sides a multiple of what its model needs).
export function videoSizes(o: VideoOptions): string[] {
  const fits = (s: string) => {
    const [w, h] = s.split("x").map(Number);
    return Math.max(w, h) <= o.max_side && w * h <= o.max_pixels && w % o.multiple === 0 && h % o.multiple === 0;
  };
  return [o.default_size, ...VIDEO_PRESETS.filter((s) => s !== o.default_size && fits(s))];
}

// "4 s (65 frames at 16 fps)": what a length makes on a model that makes k x 4 + 1 frames.
export function videoFrames(seconds: number, fps: number, step = 4): number {
  const n = Math.max(1, seconds * fps);
  return step > 1 ? Math.floor(n / step) * step + 1 : n;
}

// "1 min 05 s" for an elapsed time in seconds.
export function elapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, "0")} s`;
}
