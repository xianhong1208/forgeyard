import { useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { useT } from "../i18n/react";
import { STEP_HEAD, STEP_WORD, formatTick, formatValue, keepApart, labelIndexes, niceTicks, periodLabel, periodTitle, textUnits, type Period, type SeriesLine,
  type Step } from "../lib/charts";

type Kind = Parameters<typeof formatValue>[1];

export type ChartLine = SeriesLine & { slot: number };

const HEIGHT = 240;
const NARROW = 520;   // below this width: a shorter chart and fewer axis labels
const PAD = { top: 14, right: 14, bottom: 30 };

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

function colour(l: ChartLine): string {
  return l.other ? "var(--series-other)" : `var(--series-${l.slot + 1})`;
}

// A line chart of one metric over days, weeks or months: one line per series, a crosshair with every
// series' value on hover or with the arrow keys, a legend with each series' total, and the numbers as a
// table under "Show the numbers". The period still going on is drawn dashed.
export function LineChart({ title, periods, step, lines, total, kind, legend }: {
  title: string; periods: Period[]; step: Step; lines: ChartLine[]; total: number[]; kind: Kind;
  legend?: boolean;   // name the lines under the chart; by default when there are several
}) {
  const t = useT();
  const [box, width] = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const id = useId();
  const n = periods.length;
  const narrow = width < NARROW;
  const height = narrow ? 200 : HEIGHT;
  const max = Math.max(0, ...lines.flatMap((l) => l.values));
  const ticks = niceTicks(max, narrow ? 3 : 4);
  const top = ticks[ticks.length - 1];
  const tickText = ticks.map((t) => formatTick(t, kind));
  const left = Math.max(28, 10 + 7 * Math.max(...tickText.map(textUnits)));
  const plotW = Math.max(40, width - left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;
  const x = (i: number) => left + (n <= 1 ? plotW / 2 : (i * plotW) / (n - 1));
  const y = (v: number) => PAD.top + plotH - (top ? (v / top) * plotH : 0);
  // x labels: evenly spaced, the first and last aligned to the plot's edges, none overlapping (10.5 px Martian Mono: about 7.4 px a character).
  const anchor = (i: number) => (n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle");
  const spaced = labelIndexes(n, Math.floor(plotW / (step === "month" ? 82 : 64)));
  const spans = spaced.map((i): [number, number] => {
    const w = textUnits(periodLabel(periods[i], step)) * 7.4;
    const a = anchor(i);
    return a === "start" ? [x(i), x(i) + w] : a === "end" ? [x(i) - w, x(i)] : [x(i) - w / 2, x(i) + w / 2];
  });
  const labels = keepApart(spans, 14).map((k) => spaced[k]);
  const multi = lines.length > 1;
  const partial = n > 1 && periods[n - 1]?.partial;

  const path = (values: number[], from: number, to: number) =>
    values.slice(from, to + 1).map((v, k) => `${k ? "L" : "M"}${x(from + k).toFixed(1)},${y(v).toFixed(1)}`).join("");

  const pick = (e: PointerEvent<SVGRectElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    setActive(n <= 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round((px / Math.max(1, r.width)) * (n - 1)))));
  };
  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    const at = active ?? n - 1;
    const next = e.key === "ArrowLeft" ? at - 1 : e.key === "ArrowRight" ? at + 1 : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : null;
    if (next === null) {
      if (e.key === "Escape") setActive(null);
      return;
    }
    e.preventDefault();
    setActive(Math.max(0, Math.min(n - 1, next)));
  };
  const a = active !== null && active < n ? active : null;
  const values = a === null ? "" : lines.map((l) => `${l.name} ${formatValue(l.values[a], kind)}`).join(t("usage.chart.join"));
  const readout = a === null ? "" : multi ? t("usage.chart.readoutTotal", { period: periodTitle(periods[a], step), values, total: formatValue(total[a], kind) })
    : t("usage.chart.readout", { period: periodTitle(periods[a], step), values });
  const tipLeft = a === null ? 0 : Math.min(Math.max(x(a), 112), width - 112);

  return (
    <div className="lc">
      <div ref={box} className="lc-plot" tabIndex={0} role="group" aria-roledescription={t("usage.chart.roledescription")}
        aria-label={t("usage.chart.keys", { title, step: STEP_WORD[step] })} aria-describedby={`${id}-read`}
        onKeyDown={keys} onFocus={() => setActive((v) => v ?? n - 1)} onBlur={() => setActive(null)}>
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" focusable="false">
          {ticks.map((t, i) => (
            <g key={t}>
              <line className={i === 0 ? "lc-base" : "lc-grid"} x1={left} x2={left + plotW} y1={y(t)} y2={y(t)} />
              <text className="lc-tick" x={left - 8} y={y(t)} dy="0.32em" textAnchor="end">{tickText[i]}</text>
            </g>
          ))}
          {labels.map((i) => (
            <text key={i} className="lc-tick" x={x(i)} y={height - 8}
              textAnchor={anchor(i)}>{periodLabel(periods[i], step)}</text>
          ))}
          {a !== null && <line className="lc-cross" x1={x(a)} x2={x(a)} y1={PAD.top} y2={PAD.top + plotH} />}
          {lines.map((l) => (
            <g key={l.id} className={l.other ? "lc-line lc-other" : "lc-line"} style={{ stroke: colour(l) }}>
              {n === 1 ? <circle cx={x(0)} cy={y(l.values[0])} r={4} style={{ fill: colour(l) }} /> : <>
                <path d={path(l.values, 0, partial ? n - 2 : n - 1)} />
                {partial && <path className="lc-partial" d={path(l.values, n - 2, n - 1)} />}
              </>}
              {a !== null && n > 1 && <circle className="lc-dot" cx={x(a)} cy={y(l.values[a])} r={4.5} style={{ fill: colour(l) }} />}
            </g>
          ))}
          <rect className="lc-hit" x={left} y={PAD.top} width={plotW} height={plotH}
            onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setActive(null)} />
        </svg>
        {a !== null && (
          <div className="lc-tip" style={{ left: tipLeft }} aria-hidden="true">
            <div className="lc-tip-title">{periodTitle(periods[a], step)}</div>
            {lines.map((l) => (
              <div key={l.id} className="lc-tip-row"><span className="lc-swatch" style={{ background: colour(l) }} /><span className="lc-tip-name">{l.name}</span><span className="lc-tip-value">{formatValue(l.values[a], kind)}</span></div>
            ))}
            {multi && <div className="lc-tip-row lc-tip-total"><span className="lc-tip-name">{t("usage.chart.total")}</span><span className="lc-tip-value">{formatValue(total[a], kind)}</span></div>}
          </div>
        )}
        <p id={`${id}-read`} className="sr-only" aria-live="polite">{readout}</p>
      </div>
      {(legend ?? multi) && (
        <ul className="lc-legend" aria-label={t("usage.chart.legend", { title })}>
          {lines.map((l) => (
            <li key={l.id}><span className={`lc-key${l.other ? " lc-key-other" : ""}`} style={{ borderColor: colour(l) }} aria-hidden="true" />
              <span className="lc-legend-name">{l.name}</span>{l.cloud !== undefined && <span className={`chip ${l.cloud ? "chip-tone-wait" : "chip-tone-ok"}`}>{l.cloud ? t("usage.chip.cloud") : t("usage.chip.local")}</span>}
              <span className="lc-legend-value">{formatValue(l.total, kind)}</span></li>
          ))}
        </ul>
      )}
      <details className="lc-numbers">
        <summary>{t("usage.chart.numbers")}</summary>
        <div className="table-wrap">
          <table className="table lc-table">
            <caption className="sr-only">{title}</caption>
            <thead><tr><th scope="col">{STEP_HEAD[step]}</th>{lines.map((l) => <th key={l.id} scope="col">{l.name}</th>)}{multi && <th scope="col">{t("usage.chart.total")}</th>}</tr></thead>
            <tbody>
              {periods.map((p, i) => (
                <tr key={p.start}>
                  <th scope="row" data-label={STEP_WORD[step]}>{periodTitle(p, step)}</th>
                  {lines.map((l) => <td key={l.id} data-label={l.name} className="num">{formatValue(l.values[i], kind)}</td>)}
                  {multi && <td data-label={t("usage.chart.total")} className="num strong">{formatValue(total[i], kind)}</td>}
                </tr>
              )).reverse()}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
