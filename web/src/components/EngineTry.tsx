// 1.5.2 (#95 PR 2): "Try this version". On a built-in engine's page each newer version gets the button (admins); its
// dialog names the copy, lists the variants included and left out (and why), says what the engine's project signs and
// that Forgeyard verifies it, warns that a new version can change flags, defaults and memory use, and repeats the
// release notes' mentions. POST /api/recipes/{name}/try makes the copy; the dialog then links to its page. A copy's
// page shows where it came from, each variant's pinned digest and what was verified, and what was left out.
import { IconCopy, IconExternalLink, IconFlask, IconShieldCheck, IconShieldOff, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { useT, type Translator } from "../i18n/react";
import { ApiError, api, errorText } from "../lib/api";
import { digestOf, engineHref } from "../lib/pages";
import type { EnginePin, EngineSignature, EngineSigned, EngineTryResponse, EngineUpdateCandidate, Recipe } from "../lib/types";
import { whyText } from "./EngineUpdates";
import { Notice } from "./ui";

// The copy's name as the server would choose it: `vllm` + `0.34.0` -> `vllm-0-34-0` (dots are not allowed in names).
export function copyName(recipe: string, version: string): string {
  const slug = version.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
  return `${recipe}-${slug}`.slice(0, 40).replace(/-+$/, "");
}

// A refusal: in English as the server wrote it (it names the variant, the digest, the reason), in Chinese the
// console's sentence for its code with the server's specifics kept. A 502 (a registry, GitHub or Sigstore that could
// not be reached) says what failed too, never only "the server had a problem".
export function tryError(t: Translator, e: unknown): string {
  if (!(e instanceof ApiError)) return errorText(e);
  const sent = e.detail.message ?? "";
  if (t.lang === "en" && sent) return sent;
  return t.maybe(`enginetry.err.${e.code}`, sent || errorText(e), { message: sent });
}

function unsignedText(t: Translator, engine: string, signed: EngineSigned | undefined): string {
  const text = signed && !signed.signed ? signed.text : "";
  return t.maybe(`enginetry.unsigned.${engine}`, text);
}

export function TryButton({ recipe, candidate, signed }: { recipe: Recipe; candidate: EngineUpdateCandidate; signed?: EngineSigned }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const can = candidate.variants.some((v) => v.available);
  return (
    <>
      <button type="button" className="btn btn-small" onClick={() => setOpen(true)} disabled={!can}
        title={can ? t("enginetry.tryTitle", { name: recipe.name, version: candidate.version }) : t("enginetry.nothing")}>
        <IconFlask size={14} aria-hidden="true" /> {t("enginetry.try")}
      </button>
      {open && <TryDialog recipe={recipe} candidate={candidate} signed={signed} onClose={() => setOpen(false)} />}
    </>
  );
}

export function TryDialog({ recipe, candidate, signed, onClose }: {
  recipe: Recipe; candidate: EngineUpdateCandidate; signed?: EngineSigned; onClose: () => void;
}) {
  const t = useT();
  const [name, setName] = useState(() => copyName(recipe.name, candidate.version));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [made, setMade] = useState<EngineTryResponse | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("input, button")?.focus();
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, busy]);
  const included = candidate.variants.filter((v) => v.available);
  const left = candidate.variants.filter((v) => !v.available);
  const mentions = candidate.mentions ?? [];
  // Between two sentences: a space in English, none after Chinese full-width punctuation.
  const gap = t.lang === "en" ? " " : "";

  const make = async () => {
    setBusy(true);
    setError(null);
    try {
      setMade(await api<EngineTryResponse>(`/recipes/${encodeURIComponent(recipe.name)}/try`, { method: "POST",
        body: { version: candidate.version, name: name.trim() } }));
    } catch (e) { setError(tryError(t, e)); }
    setBusy(false);
  };

  return (
    <div className="dlg-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="dlg panel et-dlg" role="dialog" aria-modal="true" aria-labelledby="et-title" ref={box}>
        <div className="dlg-head">
          <h2 id="et-title"><IconFlask size={18} aria-hidden="true" /> {t("enginetry.dlgTitle", { title: recipe.title, version: candidate.version })}</h2>
          <button type="button" className="btn btn-quiet btn-small dlg-x" aria-label={t("common.close")} onClick={onClose} disabled={busy}><IconX size={16} aria-hidden="true" /></button>
        </div>
        {made ? (
          <>
            <Notice tone="ok">{t("enginetry.done", { name: made.recipe.name, from: `${recipe.name} ${recipe.version}`, version: candidate.version })}</Notice>
            <div className="dlg-foot">
              <button type="button" className="btn btn-quiet" onClick={onClose}>{t("enginetry.close")}</button>
              <Link className="btn btn-primary" to={engineHref(made.recipe.name)} onClick={onClose}>{t("enginetry.open", { name: made.recipe.name })}</Link>
            </div>
          </>
        ) : (
          <>
            <p className="small dlg-lead">{t("enginetry.lead", { name: recipe.name, version: candidate.version })}</p>
            <div className="field">
              <label htmlFor="et-name">{t("enginetry.name")}</label>
              <input id="et-name" className="mono" value={name} maxLength={40} spellCheck={false} autoComplete="off"
                onChange={(e) => setName(e.target.value)} aria-describedby="et-name-hint" />
              <p className="field-hint" id="et-name-hint">{t("enginetry.nameHint")}</p>
            </div>
            <div className="et-block">
              <h3 className="small">{t("enginetry.variants")}</h3>
              <ul className="et-variants">
                {included.map((v) => (
                  <li key={v.name}>
                    <span className="chip chip-tone-ok">{t("enginetry.included")}</span> <span className="mono small">{v.name}</span>{" "}
                    <span className="mono small muted lp-break">{v.runtime === "process" ? t("enginetry.archiveLine", { archive: v.archive ?? "", sha: (v.sha256 ?? "").slice(0, 12) + "…" }) : `${v.tag} @ ${v.digest?.slice(0, 19)}…`}</span>
                  </li>
                ))}
                {left.map((v) => (
                  <li key={v.name}>
                    <span className="chip chip-tone-off">{t("enginetry.leftOut")}</span> <span className="mono small">{v.name}</span>{" "}
                    <span className="small muted">{whyText(t, v.why)}</span>
                  </li>
                ))}
              </ul>
              {left.length > 0 && <p className="small muted et-note">{t("enginetry.leftOutNote", { name: recipe.name })}</p>}
            </div>
            <div className="et-block">
              <h3 className="small">{t("enginetry.sigTitle")}</h3>
              {signed?.signed
                ? <p className="small et-sig"><IconShieldCheck size={16} aria-hidden="true" /> {t("enginetry.signed", { repository: signed.repository })}</p>
                : <p className="small et-sig muted"><IconShieldOff size={16} aria-hidden="true" /> {t("enginetry.unsigned", { text: unsignedText(t, recipe.engine, signed) })}</p>}
            </div>
            <Notice tone="warn">
              {t("enginetry.warn", { name: recipe.name, from: recipe.version })}
              {mentions.length > 0 && <>{gap}{t.rich("enginetry.mentions", { list: <>{mentions.map((m, i) => (
                <span key={m.flag}>{i ? t("library.listSep") : ""}<code className="et-flag">{m.flag}</code></span>))}</> })}</>}
              {candidate.release?.url && <>{gap}<a href={candidate.release.url} target="_blank" rel="noreferrer noopener">{t("enginetry.notes")} <IconExternalLink size={12} aria-hidden="true" /></a></>}
            </Notice>
            {error && <Notice tone="error">{error}</Notice>}
            <div className="dlg-foot">
              <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
              <button type="button" className="btn btn-primary" onClick={() => void make()} disabled={busy || !name.trim() || included.length === 0} aria-busy={busy}>
                <IconCopy size={16} aria-hidden="true" /> {busy ? t("enginetry.making") : t("enginetry.make")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ a copy's page
function SignatureCell({ sig }: { sig: EngineSignature | null | undefined }) {
  const t = useT();
  if (!sig || !sig.status) return <span className="chip" title={t("enginetry.notRecordedTitle")}>{t("enginetry.notRecorded")}</span>;
  if (sig.status === "verified") {
    return (
      <div className="cell-stack">
        <span><span className="chip chip-tone-ok"><IconShieldCheck size={12} aria-hidden="true" /> {t("enginetry.verified")}</span></span>
        <span className="mono muted lp-break">{t("enginetry.signer", { repository: sig.repository ?? "", workflow: sig.workflow ?? "", ref: sig.ref ?? "" })}</span>
        {sig.run?.startsWith("https://github.com/") && <a className="small" href={sig.run} target="_blank" rel="noreferrer noopener">{t("enginetry.run")} <IconExternalLink size={12} aria-hidden="true" /></a>}
      </div>
    );
  }
  return <span className="chip chip-tone-wait" title={sig.note ?? undefined}>{t("enginetry.unsignedChip")}</span>;
}

export function CopySection({ r, pins }: { r: Recipe; pins: Record<string, EnginePin> | undefined }) {
  const t = useT();
  if (!r.based_on) return null;
  const link = <Link to={engineHref(r.based_on)} className="mono">{r.based_on}</Link>;
  const c = r.copy;
  const archives = Object.fromEntries((c?.archives ?? []).map((a) => [a.variant, a]));
  return (
    <>
      <p className="lp-desc">{c
        ? t.rich("enginetry.copyLead", { link, from: c.from_version, version: r.version, date: t.date(c.made_at), who: c.made_by, name: r.based_on })
        : t.rich("enginetry.copyLeadManual", { link, name: r.based_on })}</p>
      <div className="table-wrap">
        <table className="table et-pins">
          <thead><tr><th>{t("enginetry.col.variant")}</th><th>{t("enginetry.col.pinned")}</th><th>{t("enginetry.col.signature")}</th></tr></thead>
          <tbody>{r.variants.map((v) => {
            const pin = pins?.[v.name];
            const arch = archives[v.name];
            return (
              <tr key={v.name}>
                <td data-label={t("enginetry.col.variant")}><span className="mono small">{v.name}</span></td>
                <td data-label={t("enginetry.col.pinned")} className="small lp-break">
                  {v.runtime === "container"
                    ? <div className="cell-stack"><span className="mono" title={v.image ?? undefined}>{digestOf(v.image) ?? "—"}…</span>{pin?.recorded_at ? <span className="muted">{t("enginetry.pinnedBy", { date: t.date(pin.recorded_at), who: pin.recorded_by })}</span> : null}</div>
                    : <span className="mono">{v.engine?.split("/").pop()}</span>}
                </td>
                <td data-label={t("enginetry.col.signature")} className="small">
                  <SignatureCell sig={v.runtime === "container" ? pin?.signature : (arch?.signature as EngineSignature | undefined)} />
                </td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      {c && c.left_out.length > 0 && (
        <div className="et-block">
          <h3 className="small">{t("enginetry.leftOutTitle")}</h3>
          <ul className="et-variants">{c.left_out.map((x) => (
            <li key={x.variant}><span className="mono small">{x.variant}</span> <span className="small muted">{whyText(t, { code: x.code, text: x.text, ...x.params })}</span></li>
          ))}</ul>
        </div>
      )}
    </>
  );
}
