import { IconRefresh, IconX } from "@tabler/icons-react";
import { useCallback, useEffect, useState } from "react";

import { api, errorText } from "../lib/api";
import { PRICE_FIELDS, PRICE_LABEL, perMillion, SOURCE_LABEL } from "../lib/prices";
import type { PriceApplied, PricePreview, PriceRow } from "../lib/types";
import { Notice } from "./ui";

const MATCH: Record<string, { label: string; tone: string; title: string }> = {
  exact: { label: "same id", tone: "ok", title: "An OpenRouter model, by its own id." },
  confirmed: { label: "confirmed", tone: "ok", title: "The OpenRouter model chosen for it before (or just now)." },
  suggested: { label: "suggested", tone: "wait", title: "A guess from its name: check that it is the same model before you tick it." },
  not_listed: { label: "not listed", tone: "bad", title: "OpenRouter does not list this id (any more)." },
  none: { label: "no match", tone: "off", title: "Nothing on OpenRouter matched its name. Enter the OpenRouter id to compare." },
};

const SKIPPED: Record<string, string> = {
  manual: "kept the price entered by hand",
  changed: "OpenRouter's price changed since you looked; refresh again",
  not_listed: "OpenRouter does not list it",
  not_found: "the model was removed",
};

// Ticked at first: a confirmed or exact match whose prices change, unless someone entered them by hand.
function ticked(r: PriceRow): boolean {
  return r.after !== null && r.changed && (r.match === "exact" || r.match === "confirmed") && !r.manual;
}

function Diff({ row }: { row: PriceRow }) {
  const after = row.after;
  const fields = PRICE_FIELDS.filter((f) => row.before[f] !== null || (after && after[f] !== null));
  if (!after) return <span className="muted small">{row.before.price_input === null && row.before.price_output === null ? "no price yet" : "kept as is"}</span>;
  return (
    <ul className="price-diff">
      {fields.map((f) => {
        const same = row.before[f] === after[f];
        return (
          <li key={f} className={same ? "muted" : ""}>
            <span className="price-diff-name">{PRICE_LABEL[f]}</span>{" "}
            {same ? <span className="mono">{perMillion(after[f])}</span> : (
              <><s className="mono muted">{perMillion(row.before[f])}</s> <span aria-label="becomes">→</span> <strong className="mono">{perMillion(after[f])}</strong></>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// "Refresh prices from OpenRouter": a preview of old -> new per model; nothing changes until Apply.
export function PriceRefresh({ providerId, onClose, onApplied }: { providerId?: number; onClose: () => void; onApplied: (text: string) => void }) {
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
  const show = (out: PricePreview) => {
    setData(out);
    setPicked((was) => Object.fromEntries(out.rows.map((r) => [r.model_id, r.after !== null && (was[r.model_id] ?? ticked(r))])));
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
      const skipped = out.skipped.map((s) => `${name(s.model_id)}: ${SKIPPED[s.reason] ?? s.reason}`);
      onApplied(`Prices from OpenRouter applied to ${out.applied.length} model${out.applied.length === 1 ? "" : "s"}.${skipped.length ? ` Skipped ${skipped.join("; ")}.` : ""}`);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setApplying(false);
    }
  };

  return (
    <section className="panel price-refresh" role="dialog" aria-modal="false" aria-labelledby="price-refresh-title">
      <div className="panel-head">
        <h2 id="price-refresh-title">Refresh prices from OpenRouter</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={onClose}><IconX size={14} aria-hidden="true" /> Close</button>
      </div>
      <p className="muted small">
        Read from OpenRouter's public model list (no key is sent). Nothing changes until you apply the rows you tick. OpenRouter's prices are usually
        the provider's list price, but not always: spot-check them against the provider's pricing page.
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      {loading && !data && <p className="muted">Reading OpenRouter's model list…</p>}
      {data && rows.length === 0 && <p className="muted">No models of providers outside your network. A provider inside your network costs nothing.</p>}
      {data && rows.length > 0 && (
        <>
          <p className="small">{data.listed} models listed on OpenRouter · {data.matched} of yours matched · {data.changed} with a different price.</p>
          <div className="table-wrap">
            <table className="table price-table">
              <thead><tr><th><span className="sr-only">Apply</span></th><th>Model</th><th>Match on OpenRouter</th><th>Per million tokens</th></tr></thead>
              <tbody>
                {rows.map((r) => {
                  const m = MATCH[r.match] ?? MATCH.none;
                  return (
                    <tr key={r.model_id} className={r.after ? "" : "row-off"}>
                      <td data-label="Apply" className="price-pick">
                        <label className="check">
                          <input type="checkbox" disabled={!r.after} checked={!!picked[r.model_id]}
                            aria-label={`Apply OpenRouter's prices to ${r.provider}/${r.upstream_model}${r.manual ? ", replacing the prices entered by hand" : ""}`}
                            onChange={() => setPicked((x) => ({ ...x, [r.model_id]: !x[r.model_id] }))} />
                          {r.manual && <span className="small">overwrite</span>}
                        </label>
                      </td>
                      <td data-label="Model">
                        <div>
                          <div className="mono">{r.upstream_model}</div>
                          <div className="muted small">{r.provider}{r.source ? ` · now ${SOURCE_LABEL[r.source] ?? r.source}` : ""}</div>
                        </div>
                      </td>
                      <td data-label="Match"><div>
                        <span className={`chip chip-tone-${m.tone}`} title={m.title}>{m.label}</span>{r.ref && <> <code className="egress-host">{r.ref}</code></>}
                        {(r.match === "none" || r.match === "suggested" || r.match === "not_listed") && (
                          <form className="price-ref" onSubmit={(e) => { e.preventDefault(); void load(refs); }}>
                            <input aria-label={`OpenRouter id for ${r.upstream_model}`} placeholder="vendor/model on OpenRouter" value={refs[r.model_id] ?? ""}
                              onChange={(e) => setRefs((x) => ({ ...x, [r.model_id]: e.target.value }))} />
                            <button type="submit" className="btn btn-quiet btn-small" disabled={loading || !(refs[r.model_id] ?? "").trim()}>Compare</button>
                          </form>
                        )}
                      </div></td>
                      <td data-label="Prices"><Diff row={r} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="row-gap price-actions">
            <button type="button" className="btn btn-primary" disabled={applying || chosen.length === 0} onClick={() => void apply()}>
              {applying ? "Applying…" : `Apply ${chosen.length || ""} ticked`}
            </button>
            <button type="button" className="btn btn-quiet" disabled={loading} onClick={() => void load(refs)}><IconRefresh size={15} aria-hidden="true" /> Read again</button>
            <span className="muted small">Suggested matches and prices entered by hand stay as they are unless you tick them.</span>
          </div>
        </>
      )}
    </section>
  );
}
