// 1.5.2 (#95): newer versions of the built-in engines. An admin's "Check for newer versions" asks the registries and
// GitHub now (POST /api/engine-updates/check, up to a minute and a half); the last result is kept on the server and
// read by everyone (GET /api/engine-updates). The engine cards show a badge, and an engine's page lists each newer
// version: which variants have an image of it (and why the others do not), the release notes, and the flags this
// engine passes that those notes mention. Trying a version (a copy of the engine pinned to it) comes next (PR 2): its
// button goes in the candidate's header, where `.eu-actions` is kept.
import { IconExternalLink, IconRefresh } from "@tabler/icons-react";
import { useState } from "react";
import { Link } from "react-router";

import { useT, type Translator } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { exact } from "../lib/format";
import { engineHref } from "../lib/pages";
import type { EngineUpdate, EngineUpdateCandidate, EngineUpdatesResponse, EngineUpdatesResult, EngineUpdateWhy } from "../lib/types";
import { Notice } from "./ui";

// A reason the server gave, in the reader's language (by its code), else as the server wrote it.
export function whyText(t: Translator, why: EngineUpdateWhy | undefined | null): string {
  if (!why) return "";
  const vars: Record<string, string> = {};
  for (const [k, v] of Object.entries(why)) {
    if (k === "code" || k === "text" || v === null || v === undefined) continue;
    vars[k] = Array.isArray(v) ? v.join(", ") : String(v);
  }
  let code = why.code;
  if (code === "no_image" && !vars.last) code = "no_image_any";
  if (code === "no_archive" && vars.similar) code = "no_archive_similar";
  return t.maybe(`engineupdates.why.${code}`, why.text, vars);
}

// "Check for newer versions" with the last check's time and who ran it.
export function CheckBar({ data, onChecked, compact = false }: {
  data: EngineUpdatesResponse | null; onChecked: (r: EngineUpdatesResult) => void; compact?: boolean;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const result = data?.result ?? null;
  const check = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<EngineUpdatesResponse>("/engine-updates/check", { method: "POST" });
      if (r.result) onChecked(r.result);
    } catch (e) { setError(errorText(e)); }
    setBusy(false);
  };
  return (
    <div className="eu-bar">
      <p className="small muted eu-last">
        {result ? <span title={exact(result.checked_at)}>{t("engineupdates.lastChecked", { when: t.relative(result.checked_at), name: result.checked_by })}</span>
          : data ? (data.can_check ? t("engineupdates.never") : t("engineupdates.neverAsk")) : null}
      </p>
      {data?.can_check && (
        <button type="button" className={compact ? "btn btn-quiet btn-small" : "btn"} onClick={() => void check()} disabled={busy}
          title={t("engineupdates.checkTitle")} aria-busy={busy}>
          <IconRefresh size={compact ? 14 : 16} aria-hidden="true" className={busy ? "spin" : undefined} />{" "}
          {busy ? t("engineupdates.checking") : result && compact ? t("engineupdates.checkAgain") : t("engineupdates.check")}
        </button>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </div>
  );
}

const BADGE_TONE: Record<string, string> = {
  newer: "chip-tone-run", newer_build: "chip-tone-run", up_to_date: "chip-tone-ok",
  partly_checked: "chip-tone-wait", could_not_check: "chip-tone-bad",
};

// The badge on an engine card: the newest version found, "Newer build", "Up to date" or "Could not check". Nothing
// for an engine that was not checked (custom engines, Forgeyard's own image) or before the first check.
export function UpdateBadge({ u }: { u: EngineUpdate | undefined }) {
  const t = useT();
  if (!u || !(u.status in BADGE_TONE)) return null;
  const label = u.status === "newer" ? t("engineupdates.badge.newer", { version: u.candidates[0]?.version ?? "" })
    : t.maybe(`engineupdates.badge.${u.status}`, u.status);
  const title = u.candidates.length ? t("engineupdates.badgeTitle", { list: u.candidates.map((c) => c.version).join(", ") })
    : u.errors.map((e) => `${e.where}: ${e.text}`).join("\n") || undefined;
  return (
    <Link to={`${engineHref(u.recipe)}#updates`} className={`chip eu-badge ${BADGE_TONE[u.status]}`} title={title}>{label}</Link>
  );
}

// The Newer versions section's body on a built-in engine's page.
export function EngineUpdates({ u, version }: { u: EngineUpdate | undefined; version: string }) {
  const t = useT();
  if (!u) return null;
  if (u.status === "not_checked") return <p className="muted small">{whyText(t, u.why)}</p>;
  return (
    <>
      {u.errors.length > 0 && (
        <Notice tone={u.status === "could_not_check" ? "error" : "warn"}>
          {u.status === "could_not_check" ? t("engineupdates.couldNot") : t("engineupdates.partly")}
          <ul className="eu-errors">{u.errors.map((e) => <li key={e.where}><span className="mono">{e.where}</span>: {e.text}</li>)}</ul>
        </Notice>
      )}
      {u.candidates.length === 0 && u.status !== "could_not_check" && (
        <p className="empty-line">{u.kind === "rolling" ? t("engineupdates.upToDateBuild")
          : u.newest ? t("engineupdates.upToDate", { version: u.newest }) : t("engineupdates.upToDatePlain")}</p>
      )}
      {u.upstream && <p className="muted small">{whyText(t, u.upstream)}</p>}
      {u.candidates.length > 0 && <p className="muted small lp-note">{t("engineupdates.note", { version })}</p>}
      {u.candidates.map((c) => <Candidate key={c.version} c={c} title={u.title} />)}
    </>
  );
}

function Candidate({ c, title }: { c: EngineUpdateCandidate; title: string }) {
  const t = useT();
  const have = c.variants.filter((v) => v.available).length;
  const date = c.release?.published_at ?? c.created;
  return (
    <article className="eu-cand" aria-label={`${title} ${c.version}`}>
      <header className="eu-cand-head">
        <h3><span className="mono">{c.version}</span></h3>
        {date && <span className="small muted">{t(c.kind === "build" ? "engineupdates.built" : "engineupdates.released", { date: t.date(date) })}</span>}
        <span className="small muted">{t("engineupdates.covered", { count: String(have), total: String(c.variants.length) })}</span>
        {c.release?.url && (
          <a className="small eu-notes" href={c.release.url} target="_blank" rel="noreferrer noopener">
            {t("engineupdates.notes")} <IconExternalLink size={13} aria-hidden="true" />
          </a>
        )}
        {/* PR 2 (#95): "Try this version" goes here. */}
        <span className="eu-actions" />
      </header>
      <div className="table-wrap">
        <table className="table eu-variants">
          <thead><tr><th>{t("engineupdates.col.variant")}</th><th>{t("engineupdates.col.version")}</th><th>{t("engineupdates.col.image")}</th></tr></thead>
          <tbody>{c.variants.map((v) => (
            <tr key={v.name}>
              <td data-label={t("engineupdates.col.variant")}><span className="mono small">{v.name}</span></td>
              <td data-label={t("engineupdates.col.version")} className="small">
                {v.available ? <span className="chip chip-tone-ok">{t("engineupdates.included")}</span> : <span className="chip chip-tone-off">{t("engineupdates.leftOut")}</span>}
              </td>
              <td data-label={t("engineupdates.col.image")} className="small lp-break">
                {v.available
                  ? (v.runtime === "process"
                    ? <div className="cell-stack"><span className="mono">{v.archive}</span><span className="muted">{t("engineupdates.archive")}</span></div>
                    : <div className="cell-stack"><span className="mono">{v.tag}</span><span className="mono muted" title={v.image}>{v.digest?.slice(0, 19)}…</span></div>)
                  : <span className="muted">{whyText(t, v.why)}</span>}
              </td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {c.notes ? <p className="muted small">{whyText(t, c.notes)}</p>
        : c.mentions && (c.mentions.length === 0 ? <p className="muted small">{t("engineupdates.mentionsNone")}</p> : (
          <div className="eu-mentions">
            <p className="small">{t("engineupdates.mentions")}</p>
            <ul>{c.mentions.map((m) => (
              <li key={m.flag}>
                <details>
                  <summary className="small"><code>{m.flag}</code> <span className="muted">· {t.plural("engineupdates.lines", m.lines)}</span></summary>
                  <ul className="eu-excerpts">{m.excerpts.map((x, i) => <li key={i} className="small">{x}</li>)}</ul>
                </details>
              </li>
            ))}</ul>
          </div>
        ))}
    </article>
  );
}
