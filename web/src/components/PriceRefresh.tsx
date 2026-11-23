import { IconRefresh, IconX } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { PRICE_FIELDS, PRICE_LABEL, perMillion, SOURCE_LABEL } from "../lib/prices";
import type { PriceApplied, PricePreview, PriceRow } from "../lib/types";
import { Notice } from "./ui";

// How a model matched on OpenRouter: the chip's tone; its label and tooltip are cloud.refresh.match.<match>[.title].
const MATCH_TONE: Record<string, string> = { exact: "ok", confirmed: "ok", suggested: "wait", not_listed: "bad", none: "off" };

// Ticked at first: a confirmed or exact match whose prices change, unless someone entered them by hand.
function ticked(r: PriceRow): boolean {
  return r.after !== null && r.changed && (r.match === "exact" || r.match === "confirmed") && !r.manual;
}

function Diff({ row }: { row: PriceRow }) {
  const t = useT();
  const after = row.after;
  const fields = PRICE_FIELDS.filter((f) => row.before[f] !== null || (after && after[f] !== null));
  if (!after) return <span className="muted small">{row.before.price_input === null && row.before.price_output === null ? t("cloud.refresh.noPriceYet") : t("cloud.refresh.kept")}</span>;
  return (
    <ul className="price-diff">
      {fields.map((f) => {
        const same = row.before[f] === after[f];
        return (
          <li key={f} className={same ? "muted" : ""}>
            <span className="price-diff-name">{PRICE_LABEL[f]}</span>{" "}
            {same ? <span className="mono">{perMillion(after[f])}</span> : (
              <>{row.before[f] === null ? <span className="muted">{t("cloud.refresh.notSet")}</span> : <s className="mono muted">{perMillion(row.before[f])}</s>}{" "}
                <span aria-label={t("cloud.refresh.becomes")}>→</span> <strong className="mono">{perMillion(after[f])}</strong></>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// "Refresh prices from OpenRouter": a preview of old -> new per model; nothing changes until Apply.
export function PriceRefresh({ providerId, onClose, onApplied }: { providerId?: number; onClose: () => void; onApplied: (text: string) => void }) {
  const t = useT();
  const [data, setData] = useState<PricePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [picked, setPicked] = useState<Record<number, boolean>>({});
  const [refs, setRefs] = useState<Record<number, string>>({});
  const [applying, setApplying] = useState(false);

  const ask = useCallback((chosen: Record<number, string>) => {
    const body = { provider_id: providerId ?? null, refs: Object.fromEntries(Object.entries(chosen).filter(([, v]) => v.trim())) };
    return api<PricePreview>("/prices/openrouter/preview", { method: "POST", body });
  }, [providerId]);
  // A row keeps its tick while its match stays the same; a new match (one the admin just typed) starts
  // ticked when it is confirmed.
  const show = (out: PricePreview) => {
    const before = new Map((data?.rows ?? []).map((r) => [r.model_id, r.ref]));
    setData(out);
    setPicked((was) => Object.fromEntries(out.rows.map((r) => [r.model_id, r.after !== null
      && (before.get(r.model_id) === r.ref && r.model_id in was ? was[r.model_id] : ticked(r))])));
  };
  const load = async (chosen: Record<number, string>) => {
    setLoading(true);
    setError(null);
    try { show(await ask(chosen)); } catch (e) { setError(errorText(e)); } finally { setLoading(false); }
  };

  useEffect(() => {
    let live = true;
    ask({}).then((out) => { if (live) show(out); }, (e) => { if (live) setError(errorText(e)); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [ask]);

  const rows = data?.rows ?? [];
  const chosen = rows.filter((r) => picked[r.model_id] && r.after && r.ref);
  const apply = async () => {
    setApplying(true);
    setError(null);
    try {
      const items = chosen.map((r) => ({ model_id: r.model_id, ref: r.ref, ...r.after, overwrite_manual: r.manual }));
      const out = await api<PriceApplied>("/prices/openrouter/apply", { method: "POST", body: { items } });
      const name = (id: number) => rows.find((r) => r.model_id === id)?.upstream_model ?? `#${id}`;
      const skipped = out.skipped.map((s) => `${name(s.model_id)}: ${t.maybe(`cloud.refresh.skip.${s.reason}`, s.reason)}`);
      onApplied(`${t.plural("cloud.refresh.applied", out.applied.length)}${skipped.length ? t("cloud.refresh.skipped", { list: skipped.join("; ") }) : ""}`);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setApplying(false);
    }
  };

  return (
    <section className="panel price-refresh" role="dialog" aria-modal="false" aria-labelledby="price-refresh-title">
      <div className="panel-head">
        <h2 id="price-refresh-title">{t("cloud.refresh.title")}</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose}><IconX size={14} aria-hidden="true" /> {t("common.close")}</button>
      </div>
      <p className="muted small">{t("cloud.refresh.lead")}</p>
      {error && <Notice tone="error">{error}</Notice>}
      {loading && !data && <p className="muted">{t("cloud.refresh.reading")}</p>}
      {data && rows.length === 0 && <p className="muted">{t("cloud.refresh.empty")}</p>}
      {data && rows.length > 0 && (
        <>
          <p className="small">{t("cloud.refresh.summary", { listed: String(data.listed), matched: String(data.matched), changed: String(data.changed) })}</p>
          <div className="table-wrap">
            <table className="table price-table">
              <thead>
                <tr>
                  <th><span className="sr-only">{t("cloud.refresh.col.apply")}</span></th><th>{t("cloud.refresh.col.model")}</th>
                  <th>{t("cloud.refresh.col.match")}</th><th>{t("cloud.refresh.col.prices")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const match = r.match in MATCH_TONE ? r.match : "none";
                  const model = `${r.provider}/${r.upstream_model}`;
                  return (
                    <tr key={r.model_id} className={r.after ? "" : "row-off"}>
                      <td data-label={t("cloud.refresh.col.apply")} className="price-pick">
                        <label className="check">
                          <input type="checkbox" disabled={!r.after} checked={!!picked[r.model_id]}
                            aria-label={r.manual ? t("cloud.refresh.applyAriaManual", { model }) : t("cloud.refresh.applyAria", { model })}
                            onChange={() => setPicked((x) => ({ ...x, [r.model_id]: !x[r.model_id] }))} />
                          {r.manual && <span className="small">{t("cloud.refresh.overwrite")}</span>}
                        </label>
                      </td>
                      <td data-label={t("cloud.refresh.col.model")}>
                        <div>
                          <div className="mono">{r.upstream_model}</div>
                          <div className="muted small">{r.provider}{r.source ? t("cloud.refresh.now", { source: SOURCE_LABEL[r.source] ?? r.source }) : ""}</div>
                        </div>
                      </td>
                      <td data-label={t("cloud.refresh.col.matchShort")}><div>
                        <span className={`chip chip-tone-${MATCH_TONE[match]}`} title={t.maybe(`cloud.refresh.match.${match}.title`, "")}>
                          {t.maybe(`cloud.refresh.match.${match}`, match)}
                        </span>{r.ref && <> <code className="egress-host">{r.ref}</code></>}
                        {(r.match === "none" || r.match === "suggested" || r.match === "not_listed") && (
                          <form className="price-ref" onSubmit={(e) => { e.preventDefault(); void load(refs); }}>
                            <input aria-label={t("cloud.refresh.idAria", { model: r.upstream_model })} placeholder={t("cloud.refresh.idPlaceholder")} value={refs[r.model_id] ?? ""}
                              onChange={(e) => setRefs((x) => ({ ...x, [r.model_id]: e.target.value }))} />
                            <button type="submit" className="btn btn-quiet btn-small" disabled={loading || !(refs[r.model_id] ?? "").trim()}>{t("cloud.refresh.compare")}</button>
                          </form>
                        )}
                      </div></td>
                      <td data-label={t("cloud.refresh.col.pricesShort")}><Diff row={r} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="row-gap price-actions">
            <button type="button" className="btn btn-primary" disabled={applying || chosen.length === 0} onClick={() => void apply()}>
              {applying ? t("cloud.refresh.applying") : chosen.length ? t("cloud.refresh.apply", { n: String(chosen.length) }) : t("cloud.refresh.applyNone")}
            </button>
            <button type="button" className="btn btn-quiet" disabled={loading} onClick={() => void load(refs)}><IconRefresh size={15} aria-hidden="true" /> {t("cloud.refresh.readAgain")}</button>
            <span className="muted small">{t("cloud.refresh.note")}</span>
          </div>
        </>
      )}
    </section>
  );
}
