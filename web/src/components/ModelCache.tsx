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
import { modelHref } from "../lib/pages";
import type { DownloadsResponse, FetchVia, Machine, MachineCache } from "../lib/types";
import { useApi } from "../lib/useApi";
import { useT } from "../i18n/react";

type Removed = { ok: boolean; freed_bytes: number };

// How often the downloads under way are read again (faster than the machine's page while one runs).
const DOWNLOADS_POLL_MS = 2000;

export function ModelCacheSection({ m, poll, canManage = false, onChanged, onError }: {
  m: Machine; poll: number; canManage?: boolean; onChanged: (text: string) => void; onError: (text: string) => void;
}) {
  const t = useT();
  const { data, error, reload } = useApi<MachineCache>(`/machines/${m.id}/cache`);
  const dl = useApi<DownloadsResponse>(`/downloads?machine_id=${m.id}`);
  const [busy, setBusy] = useState<string | null>(null);
  const [dialog, setDialog] = useState(false);
  const running = (dl.data?.downloads ?? []).some(isActive);
  const reloadDl = dl.reload;
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "hidden") {
        reload();
        reloadDl();
      }
    }, running ? DOWNLOADS_POLL_MS : poll);
    return () => window.clearInterval(timer);
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
      onChanged(t("machine.cache.removed", { title: row.title, machine: m.name, freed: bytes(r.freed_bytes) }));
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(null);
      reload();
    }
  };
  const prune = async () => {
    if (!c || !window.confirm(t("machine.cache.pruneQuestion", { size: bytes(c.partial_bytes), machine: m.name }))) return;
    setBusy("partial");
    try {
      const r = await api<Removed>(`/machines/${m.id}/cache/remove-partial`, { method: "POST" });
      onChanged(t("machine.cache.pruned", { machine: m.name, freed: bytes(r.freed_bytes) }));
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
        <h2 id="mk-cache">{t("machine.cache.title")} {c && c.reported && <span className="muted small">· {count}</span>}</h2>
        <span className="cache-head-side">
          {c && c.reported && c.ok !== false && <span className="cache-disk small">{diskLine(c)}</span>}
          {canDownload && c && c.reported && (
            <button type="button" className="btn btn-small" disabled={!prefetch}
              title={prefetch ? t("machine.cache.downloadTitle") : t("machine.cache.downloadOld")}
              onClick={() => setDialog(true)}>
              <IconDownload size={14} aria-hidden="true" /> {t("machine.cache.downloadHere")}
            </button>
          )}
        </span>
      </div>
      {dialog && <DownloadDialog machineId={m.id} onClose={() => setDialog(false)} onStarted={said(onChanged)} />}
      {!c && !error && <p className="muted small">{t("common.loading")}</p>}
      {!c && error && <p className="muted small">{error}</p>}
      {c && !c.reported && <p className="empty-line">{t("machine.cache.none")}</p>}
      {c && c.reported && c.ok === false && <p className="empty-line">{t("machine.cache.unusable", { reason: c.reason ?? "" })}</p>}
      {c && c.reported && c.ok !== false && (
        <>
          {shares && (
            <div className="cache-disk-map">
              <span className="membar membar-md" role="img" aria-label={t("machine.cache.diskLabel", { disk: diskLine(c) })}>
                <span className="membar-fy cache-bar-cache" style={{ width: `${(shares.cache * 100).toFixed(2)}%` }} />
                <span className="membar-other" style={{ width: `${(shares.other * 100).toFixed(2)}%` }} />
              </span>
              <span className="memlegend small muted">
                <span><i className="sw sw-fy" /> {t("machine.cache.legendCache")}</span>
                <span><i className="sw sw-other" /> {t("machine.cache.legendOther")}</span>
                <span><i className="sw sw-free" /> {t("machine.cache.legendFree")}</span>
              </span>
            </div>
          )}
          {downloads.length > 0 && (
            <div className="cache-downloads">
              <h3 className="small muted">{t("machine.cache.downloads")}</h3>
              <DownloadList list={downloads} show="model" canCancel={canDownload} onChanged={said(onChanged)} onError={onError} />
            </div>
          )}
          {!c.sizes && <p className="field-hint cache-note">{c.remove_note}</p>}
          {c.sizes && c.remove_note && <p className="field-hint cache-note">{c.remove_note}</p>}
          {rows.length === 0 ? (
            <p className="empty-line">{t("machine.cache.empty")}</p>
          ) : (
            <ul className="cachelist">
              {rows.map((row) => <CacheItem key={row.key} row={row} c={c} busy={busy} onRemove={(r) => void remove(r)} />)}
            </ul>
          )}
          {c.truncated && <p className="field-hint cache-note">{t("machine.cache.truncated")}</p>}
          {(c.partial_bytes ?? 0) > 0 && (
            <div className="cache-partial">
              <span className="cache-partial-text">
                <span><IconFileDownload size={15} aria-hidden="true" /> {t.rich("machine.cache.partial", { size: <strong>{bytes(c.partial_bytes)}</strong>,
                  files: t.plural("machine.cache.files", c.partial_files ?? 0) })}</span>
                <small className="muted">{t("machine.cache.partialHint")}</small>
              </span>
              <button type="button" className="btn btn-quiet btn-small" disabled={busy !== null || c.remove_note !== null}
                title={c.remove_note ?? undefined} onClick={() => void prune()}>
                {busy === "partial" ? t("machine.removing") : t("machine.cache.prune")}
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
  const t = useT();
  const [saving, setSaving] = useState(false);
  const value: FetchVia = m.fetch_via ?? "auto";
  const relays = (m.features ?? []).includes("relay");
  const images = (m.features ?? []).includes("relay_images");
  const save = async (next: FetchVia) => {
    setSaving(true);
    try {
      await api(`/machines/${m.id}/fetch-via`, { method: "PUT", body: { fetch_via: next } });
      onChanged(t("machine.fetchVia.saved", { name: m.name, value: FETCH_VIA_LABEL[next] }));
    } catch (e) {
      onError(errorText(e));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="field cache-fetchvia">
      <label htmlFor={`fetch-via-${m.id}`}>{t("machine.fetchVia.label")}</label>
      <select id={`fetch-via-${m.id}`} value={value} disabled={saving} onChange={(e) => void save(e.target.value as FetchVia)}>
        {(Object.keys(FETCH_VIA_LABEL) as FetchVia[]).map((k) => <option key={k} value={k}>{FETCH_VIA_LABEL[k]}</option>)}
      </select>
      <p className="field-hint">
        {FETCH_VIA_HINT[value]}
        {value !== "direct" && !relays && t("machine.fetchVia.noRelay")}
        {value !== "direct" && relays && !images && t("machine.fetchVia.noImages")}
        {value !== "direct" && t("machine.fetchVia.unpinned")}
      </p>
    </div>
  );
}

function CacheItem({ row, c, busy, onRemove }: { row: CacheRow; c: MachineCache; busy: string | null; onRemove: (r: CacheRow) => void }) {
  const t = useT();
  const blocked = removeBlocked(c, row);
  const Icon = row.kind === "engine" ? IconPuzzle : IconDatabase;
  return (
    <li className={`cacherow${row.part ? " cacherow-part" : ""}`}>
      <span className="runlist-k" aria-hidden="true">{row.part ? <span className="cacherow-branch" /> : <Icon size={16} stroke={1.7} />}</span>
      <span className="cacherow-main">
        <strong className="mono">{row.title}</strong>
        <small>
          {row.sub}
          {row.catalog.length > 0 && <>{t("machine.cache.catalog")}{row.catalog.map((x, i) => <span key={x.id}>{i ? ", " : ""}<Link to={modelHref(x.name)}>{x.name}</Link></span>)}</>}
        </small>
      </span>
      <span className="cacherow-use">
        {row.usedBy.map((u) => (
          userHref(u)
            ? <Link key={`${u.kind}-${u.id}`} to={userHref(u)!} className="chip chip-tone-run" title={t("machine.usedByDeployment", { name: u.name })}>{t("machine.inUse", { name: u.name })}</Link>
            : <span key={`${u.kind}-${u.id}`} className="chip chip-tone-run" title={t("machine.usedByApp", { name: u.name })}>{t("machine.inUse", { name: u.name })}</span>
        ))}
      </span>
      <span className="cacherow-size mono">{row.bytes === null ? t("machine.sizeUnknown") : bytes(row.bytes)}</span>
      {c.sizes && (
        <span className="cacherow-act">
          <button type="button" className="btn btn-danger btn-small" disabled={blocked !== null || busy !== null}
            title={blocked ?? t("machine.cache.removeTitle", { title: row.title })} aria-label={t("machine.removeNamed", { name: row.title })}
            onClick={() => onRemove(row)}>
            <IconTrash size={14} aria-hidden="true" /> {busy === row.key ? t("machine.removing") : t("machine.remove")}
          </button>
        </span>
      )}
    </li>
  );
}
