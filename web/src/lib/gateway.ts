// The gateway (M4): snippets for a new key, numbers for usage, and reading a streamed answer.

import type { ApiKey } from "./types";

export const API_ENDPOINT: Record<string, string> = {
  "openai-chat": "/chat/completions",
  "openai-embeddings": "/embeddings",
  rerank: "/rerank",
  "openai-audio": "/audio/transcriptions",
};

export const API_KIND: Record<string, string> = {
  "openai-chat": "chat",
  "openai-embeddings": "embeddings",
  rerank: "rerank",
  "openai-audio": "speech to text",
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
