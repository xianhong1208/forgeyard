// "Download to…" (1.4, issue #35): what the dialog says of each machine, and how a download reads in a list.
import { bytes } from "./machines";
import { downloadFraction, downloadText } from "./models";
import type { DownloadTarget, FetchVia, ModelDownload } from "./types";
import { when } from "./format";

export const TARGET_LABEL: Record<DownloadTarget["state"], string> = {
  ok: "fits", cached: "already here", downloading: "downloading", too_old: "update the agent", no_cache: "no model cache",
  offline: "offline", no_space: "not enough disk", revoked: "revoked",
};

export const TARGET_TONE: Record<DownloadTarget["state"], string> = {
  ok: "ok", cached: "run", downloading: "wait", too_old: "bad", no_cache: "off", offline: "off", no_space: "bad",
  revoked: "off",
};

export function pickable(t: DownloadTarget): boolean {
  return t.state === "ok";
}

// "1.19 GiB to download · 48 GiB free · engine: llama.cpp-1.0.0 archive (cpu-process)".
export function targetDetail(t: DownloadTarget): string {
  const parts: string[] = [];
  if (t.model_cached) parts.push("model already here");
  else if (t.need_bytes) parts.push(`${bytes(t.need_bytes)} to download`);
  if (t.free_bytes !== null && t.free_bytes !== undefined) parts.push(`${bytes(t.free_bytes)} free`);
  if (t.engine) {
    const what = t.engine.kind === "image" ? `image ${shortImage(t.engine.name)}` : `${t.engine.name} archive`;
    parts.push(`engine: ${what}${t.engine.cached ? " (already here)" : ""}${t.variant ? ` · ${t.variant}` : ""}`);
  } else if (t.engine_note) {
    parts.push(`no engine: ${t.engine_note}`);
  }
  if (t.via === "server" && !t.model_cached) parts.push(t.from_machine ? `via the server, from ${t.from_machine}` : "via the server");
  return parts.join(" · ");
}

// ghcr.io/ggml-org/llama.cpp:server-v1.0.0-b12359@sha256:… -> llama.cpp:server-v1.0.0-b12359
export function shortImage(image: string): string {
  const ref = image.split("@")[0];
  return ref.split("/").pop() ?? ref;
}

export function isActive(d: ModelDownload): boolean {
  return d.state === "queued" || d.state === "downloading";
}

// What a download's line says: its progress, or how it ended.
export function downloadLine(d: ModelDownload): string {
  if (d.state === "downloading" || d.state === "queued") {
    const progress = d.download ? downloadText({ download: d.download }) : null;
    const frac = d.download ? downloadFraction({ download: d.download }) : null;
    const text = progress ? `${progress}${frac !== null ? ` (${Math.round(frac * 100)}%)` : ""}` : d.reason || (d.state === "queued" ? "Waiting to start" : "Starting");
    return d.waiting ? `${text} · ${d.waiting}` : text;
  }
  if (d.state === "done") {
    const via = d.via === "machine" ? ` via the server, from ${d.source?.name ?? "another machine"}` : d.via === "server" ? " via the server" : "";
    return `Downloaded ${when(d.finished_at)}${via}${d.recipe ? `, with the ${d.recipe} engine` : ""}`;
  }
  if (d.state === "cancelled") return `${capital(d.reason || "cancelled")} ${when(d.finished_at)}`.trim();
  return `Failed: ${d.reason || "unknown reason"}`;
}

function capital(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

// The rows a list shows: every one under way, then the latest finished one per machine (or model), at most `limit`.
export function recentDownloads(list: ModelDownload[], key: "machine" | "model", limit = 6): ModelDownload[] {
  const active = list.filter(isActive);
  const seen = new Set(active.map((d) => (key === "machine" ? d.machine?.id : d.model?.id)));
  const out = [...active];
  for (const d of list) {
    if (isActive(d)) continue;
    const k = key === "machine" ? d.machine?.id : d.model?.id;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(d);
  }
  return out.slice(0, Math.max(limit, active.length));
}

export const FETCH_VIA_LABEL: Record<FetchVia, string> = {
  auto: "Auto", server: "Always through the server", direct: "Direct only",
};

// What each choice does (1.4, "relay"): shown under the machine's Internet access setting.
export const FETCH_VIA_HINT: Record<FetchVia, string> = {
  auto: "Downloads models, engine archives and pinned engine images directly, and through the server once the source cannot be reached from here.",
  server: "Every model file, engine archive and pinned engine image comes through the server, which stores nothing: for a machine with no internet access of its own.",
  direct: "Always downloads directly, never through the server.",
};
