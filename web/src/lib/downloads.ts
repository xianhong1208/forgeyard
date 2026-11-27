// "Download to…" (1.4, issue #35): what the dialog says of each machine, and how a download reads in a list.
import { labels, t as tr } from "../i18n/core";
import { bytes } from "./machines";
import { downloadFraction, downloadText } from "./models";
import type { DownloadTarget, FetchVia, ModelDownload } from "./types";
import { when } from "./format";

export const TARGET_LABEL: Record<DownloadTarget["state"], string> = labels({
  ok: "machine.target.ok", cached: "machine.target.cached", downloading: "machine.target.downloading", too_old: "machine.target.too_old",
  no_cache: "machine.target.no_cache", offline: "machine.target.offline", no_space: "machine.target.no_space", revoked: "machine.target.revoked",
  no_engine: "machine.target.no_engine",
});

export const TARGET_TONE: Record<DownloadTarget["state"], string> = {
  ok: "ok", cached: "run", downloading: "wait", too_old: "bad", no_cache: "off", offline: "off", no_space: "bad",
  revoked: "off", no_engine: "bad",
};

export function pickable(t: DownloadTarget): boolean {
  return t.state === "ok";
}

// "1.19 GiB to download · 48 GiB free · engine: llama.cpp-1.0.0 archive (cpu-process)".
export function targetDetail(t: DownloadTarget): string {
  const parts: string[] = [];
  if (t.model_cached) parts.push(tr("machine.target.modelHere"));
  else if (t.need_bytes) parts.push(tr("machine.target.toDownload", { size: bytes(t.need_bytes) }));
  if (t.free_bytes !== null && t.free_bytes !== undefined) parts.push(tr("machine.free", { free: bytes(t.free_bytes) }));
  if (t.engine) {
    const what = t.engine.kind === "image" ? tr("machine.target.engineImage", { image: shortImage(t.engine.name) })
      : tr("machine.target.engineArchive", { name: t.engine.name });
    parts.push(tr("machine.target.engine", { what, here: t.engine.cached ? tr("machine.target.engineHere") : "",
      variant: t.variant ? ` · ${t.variant}` : "" }));
  } else if (t.engine_note) {
    parts.push(tr("machine.target.noEngine", { note: t.engine_note }));
  }
  if (t.via === "server" && !t.model_cached) parts.push(t.from_machine ? tr("machine.target.viaFrom", { machine: t.from_machine }) : tr("machine.target.via"));
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
    const text = progress ? `${progress}${frac !== null ? ` (${Math.round(frac * 100)}%)` : ""}`
      : d.reason || (d.state === "queued" ? tr("machine.dl.waiting") : tr("machine.dl.starting"));
    return d.waiting ? `${text} · ${d.waiting}` : text;
  }
  if (d.state === "done") {
    const via = d.via === "machine" ? tr("machine.dl.viaMachine", { name: d.source?.name ?? tr("machine.dl.anotherMachine") })
      : d.via === "server" ? tr("machine.dl.viaServer") : "";
    // Only when and how the model came (1.5.6, #282): whether an engine image is on the machine is current state, shown
    // on the engine's page and the deploy form, not history. An engine alone (1.5.2): its name is the row's title.
    return tr("machine.dl.done", { when: when(d.finished_at), via });
  }
  // The server's reason as it sent it, else "Cancelled" in the reader's language.
  if (d.state === "cancelled") return (d.reason ? `${capital(d.reason)} ${when(d.finished_at)}` : tr("machine.dl.cancelled", { when: when(d.finished_at) })).trim();
  return tr("machine.dl.failed", { reason: d.reason || tr("machine.dl.unknownReason") });
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

// The catalog list's rows (1.6.2): only what needs the reader there and then. A download under way (its progress and
// Cancel), and one that failed in the last day, so a download started from the catalog that fails is seen there.
// Finished downloads are history: the model's page lists them. A failure followed by a newer download to the same
// machine is gone with it (the latest per machine counts).
export const CATALOG_FAILED_MS = 24 * 3600 * 1000;

export function catalogDownloads(list: ModelDownload[], now = Date.now()): ModelDownload[] {
  return recentDownloads(list, "machine").filter((d) => isActive(d)
    || (d.state === "failed" && d.finished_at !== null && now - Date.parse(d.finished_at) < CATALOG_FAILED_MS));
}

export const FETCH_VIA_LABEL: Record<FetchVia, string> = labels({
  auto: "machine.fetchVia.auto", server: "machine.fetchVia.server", direct: "machine.fetchVia.direct",
});

// What each choice does (1.4, "relay"): shown under the machine's Internet access setting.
export const FETCH_VIA_HINT: Record<FetchVia, string> = labels({
  auto: "machine.fetchVia.hint.auto", server: "machine.fetchVia.hint.server", direct: "machine.fetchVia.hint.direct",
});
