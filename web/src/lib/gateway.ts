// The gateway (M4): snippets for a new key, numbers for usage, and reading a streamed answer.

import type { ApiKey } from "./types";

export const API_ENDPOINT: Record<string, string> = {
  "openai-chat": "/chat/completions",
  "openai-embeddings": "/embeddings",
  rerank: "/rerank",
  "openai-audio": "/audio/transcriptions",
  "openai-speech": "/audio/speech",
};

export const API_KIND: Record<string, string> = {
  "openai-chat": "chat",
  "openai-embeddings": "embeddings",
  rerank: "rerank",
  "openai-audio": "speech to text",
  "openai-speech": "text to speech",
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

