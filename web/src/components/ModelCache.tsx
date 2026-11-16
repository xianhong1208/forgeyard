// "Models in cache" on a machine's page (1.4): what its agent has downloaded, with sizes and the
// disk around it, and Remove (refused while a deployment there uses it; partial downloads too). Also
// "Download a model here…" and the downloads under way (1.4, issue #35), and, for admins, how the machine
// reaches the internet.
import { IconDatabase, IconDownload, IconFileDownload, IconPuzzle, IconTrash } from "@tabler/icons-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { DownloadDialog, DownloadList } from "./DownloadTo";
import { api, errorText } from "../lib/api";
import { FETCH_VIA_HINT, FETCH_VIA_LABEL, isActive, recentDownloads } from "../lib/downloads";
import { cacheRows, diskLine, diskShares, removeBlocked, removeQuestion, userHref, type CacheRow } from "../lib/cache";
import { bytes } from "../lib/machines";
import type { DownloadsResponse, FetchVia, Machine, MachineCache } from "../lib/types";
import { useApi } from "../lib/useApi";

type Removed = { ok: boolean; freed_bytes: number };

// How often the downloads under way are read again (faster than the machine's page while one runs).
const DOWNLOADS_POLL_MS = 2000;

export function ModelCacheSection({ m, poll, canManage = false, onChanged, onError }: {
  m: Machine; poll: number; canManage?: boolean; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const { data, error, reload } = useApi<MachineCache>(`/machines/${m.id}/cache`);
  const dl = useApi<DownloadsResponse>(`/downloads?machine_id=${m.id}`);
  const [busy, setBusy] = useState<string | null>(null);
  const [dialog, setDialog] = useState(false);
  const running = (dl.data?.downloads ?? []).some(isActive);
  const reloadDl = dl.reload;
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState !== "hidden") {
        reload();
        reloadDl();
      }
    }, running ? DOWNLOADS_POLL_MS : poll);
    return () => window.clearInterval(t);
  }, [reload, reloadDl, poll, running]);
  const downloads = recentDownloads(dl.data?.downloads ?? [], "model");
  const canDownload = dl.data?.can_download ?? false;
  const prefetch = (m.features ?? []).includes("prefetch");
  const said = (fn: (text: string) => void) => (text: string) => { fn(text); reloadDl(); reload(); };

  const c = data;
  const rows = c ? cacheRows(c) : [];
  const count = c ? c.models.length + c.engines.length : 0;

  const remove = async (row: CacheRow) => {
    if (!c || !window.confirm(removeQuestion(m.name, row))) return;
    setBusy(row.key);
    try {
      const r = await api<Removed>(`/machines/${m.id}/cache/remove`, { method: "POST", body: { key: row.key } });
      onChanged(`Removed ${row.title} from ${m.name}'s cache: ${bytes(r.freed_bytes)} freed.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
      reload();
    }
  };
  const prune = async () => {
    if (!c || !window.confirm(`Remove the partial downloads (${bytes(c.partial_bytes)}) from ${m.name}'s cache? A download that starts again begins from zero.`)) return;
    setBusy("partial");
    try {
      const r = await api<Removed>(`/machines/${m.id}/cache/remove-partial`, { method: "POST" });
      onChanged(`Removed the partial downloads from ${m.name}'s cache: ${bytes(r.freed_bytes)} freed.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
      reload();
    }
  };

  const shares = c && c.sizes ? diskShares(c) : null;
  return (
    <section className="panel mk-section" aria-labelledby="mk-cache">
      <div className="panel-head">
        <h2 id="mk-cache">Models in cache {c && c.reported && <span className="muted small">· {count}</span>}</h2>
        <span className="cache-head-side">
          {c && c.reported && c.ok !== false && <span className="cache-disk small">{diskLine(c)}</span>}
          {canDownload && c && c.reported && (
            <button type="button" className="btn btn-small" disabled={!prefetch}
              title={prefetch ? "Download a model into this machine's cache now, before deploying it" : "This machine's agent cannot download ahead: update the agent."}
              onClick={() => setDialog(true)}>
              <IconDownload size={14} aria-hidden="true" /> Download a model here…
            </button>
          )}
        </span>
      </div>
      {dialog && <DownloadDialog machineId={m.id} onClose={() => setDialog(false)} onStarted={said(onChanged)} />}
      {!c && !error && <p className="muted small">Loading…</p>}
      {!c && error && <p className="muted small">{error}</p>}
      {c && !c.reported && <p className="empty-line">This machine's agent serves no models (it has no model cache).</p>}
      {c && c.reported && c.ok === false && <p className="empty-line">The model cache cannot be used: {c.reason}</p>}
      {c && c.reported && c.ok !== false && (
        <>
          {shares && (
            <div className="cache-disk-map">
              <span className="membar membar-md" role="img" aria-label={`The cache's disk: ${diskLine(c)}`}>
                <span className="membar-fy cache-bar-cache" style={{ width: `${(shares.cache * 100).toFixed(2)}%` }} />
                <span className="membar-other" style={{ width: `${(shares.other * 100).toFixed(2)}%` }} />
              </span>
              <span className="memlegend small muted">
                <span><i className="sw sw-fy" /> models in cache</span>
                <span><i className="sw sw-other" /> other files</span>
                <span><i className="sw sw-free" /> free</span>
              </span>
            </div>
          )}
          {downloads.length > 0 && (
            <div className="cache-downloads">
              <h3 className="small muted">Downloads</h3>
              <DownloadList list={downloads} show="model" canCancel={canDownload} onChanged={said(onChanged)} onError={onError} />
            </div>
          )}
          {!c.sizes && <p className="field-hint cache-note">{c.remove_note}</p>}
          {c.sizes && c.remove_note && <p className="field-hint cache-note">{c.remove_note}</p>}
          {rows.length === 0 ? (
            <p className="empty-line">Nothing is in this machine's cache yet. A model is downloaded here the first time a deployment runs here, or ahead of it with Download a model here.</p>
          ) : (
            <ul className="cachelist">
              {rows.map((row) => <CacheItem key={row.key} row={row} c={c} busy={busy} onRemove={(r) => void remove(r)} />)}
            </ul>
          )}
          {c.truncated && <p className="field-hint cache-note">The agent listed only the largest models: it has more than fit in one report.</p>}
          {(c.partial_bytes ?? 0) > 0 && (
            <div className="cache-partial">
              <span className="cache-partial-text">
                <span><IconFileDownload size={15} aria-hidden="true" /> Partial downloads: <strong>{bytes(c.partial_bytes)}</strong> in {c.partial_files} {c.partial_files === 1 ? "file" : "files"}</span>
                <small className="muted">An unfinished download resumes from them.</small>
              </span>
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null || c.remove_note !== null}
                title={c.remove_note ?? undefined} onClick={() => void prune()}>
                {busy === "partial" ? "Removing…" : "Remove partial downloads"}
              </button>
            </div>
          )}
        </>
      )}
      {canManage && c && c.reported && <FetchViaField m={m} onChanged={onChanged} onError={onError} />}
    </section>
  );
}

// Admins only (D9): how this machine reaches the internet for models, engines and images.
function FetchViaField({ m, onChanged, onError }: { m: Machine; onChanged: (text: string) => void; onError: (text: string) => void }) {
  const [saving, setSaving] = useState(false);
  const value: FetchVia = m.fetch_via ?? "auto";
  const relays = (m.features ?? []).includes("relay");
  const save = async (next: FetchVia) => {
    setSaving(true);
    try {
      await api(`/machines/${m.id}/fetch-via`, { method: "PUT", body: { fetch_via: next } });
      onChanged(`${m.name}'s internet access for downloads: ${FETCH_VIA_LABEL[next]}.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="field cache-fetchvia">
      <label htmlFor={`fetch-via-${m.id}`}>Internet access for downloads</label>
      <select id={`fetch-via-${m.id}`} value={value} disabled={saving} onChange={(e) => void save(e.target.value as FetchVia)}>
        {(Object.keys(FETCH_VIA_LABEL) as FetchVia[]).map((k) => <option key={k} value={k}>{FETCH_VIA_LABEL[k]}</option>)}
      </select>
      <p className="field-hint">
        {FETCH_VIA_HINT[value]}
        {value !== "direct" && !relays && " This machine's agent cannot download through the server yet: update the agent. Until then it downloads directly."}
        {" "}Engine images are still pulled directly.
      </p>
    </div>
  );
}

function CacheItem({ row, c, busy, onRemove }: { row: CacheRow; c: MachineCache; busy: string | null; onRemove: (r: CacheRow) => void }) {
  const blocked = removeBlocked(c, row);
  const Icon = row.kind === "engine" ? IconPuzzle : IconDatabase;
  return (
    <li className={`cacherow${row.part ? " cacherow-part" : ""}`}>
      <span className="runlist-k" aria-hidden="true">{row.part ? <span className="cacherow-branch" /> : <Icon size={16} stroke={1.7} />}</span>
      <span className="cacherow-main">
        <strong className="mono">{row.title}</strong>
        <small>
          {row.sub}
          {row.catalog.length > 0 && <> · catalog: {row.catalog.map((x) => x.name).join(", ")}</>}
        </small>
      </span>
      <span className="cacherow-use">
        {row.usedBy.map((u) => (
          userHref(u)
            ? <Link key={`${u.kind}-${u.id}`} to={userHref(u)!} className="chip chip-tone-run" title={`Used by the ${u.kind} ${u.name} on this machine`}>in use: {u.name}</Link>
            : <span key={`${u.kind}-${u.id}`} className="chip chip-tone-run" title={`Used by the app ${u.name} on this machine`}>in use: {u.name}</span>
        ))}
      </span>
      <span className="cacherow-size mono">{row.bytes === null ? "size unknown" : bytes(row.bytes)}</span>
      {c.sizes && (
        <span className="cacherow-act">
          <button type="button" className="btn btn-danger btn-small" disabled={blocked !== null || busy !== null}
            title={blocked ?? `Remove ${row.title} from this machine's cache`} aria-label={`Remove ${row.title}`}
            onClick={() => onRemove(row)}>
            <IconTrash size={14} aria-hidden="true" /> {busy === row.key ? "Removing…" : "Remove"}
          </button>
        </span>
      )}
    </li>
  );
}
