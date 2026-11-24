// The Playground's Text classification panel (1.4.4, #144): texts, one per line, to /v1/classify; for each, every
// label's probability. A model with fixed labels names them from its config.json (kept in the catalog), the most
// likely first. 1.5.4 (#144): a zero-shot (NLI) model on the transformers engine chooses among the labels each
// request names: chips for the labels, the hypothesis template (best in the texts' own language) and multi-label;
// its bars keep the order of the labels asked.
import { IconAlertTriangle, IconX } from "@tabler/icons-react";
import { useState, type FormEvent, type KeyboardEvent } from "react";

import { HelpLink } from "../components/HelpLink";
import { Notice } from "../components/ui";
import { useT } from "../i18n/react";
import {
  DEFAULT_LABELS, DEFAULT_TEMPLATE, MAX_LABELS, ZH_TEMPLATE, addLabels, englishOnly, hasCjk, languagesLine, templateProblem,
} from "../lib/encoders";
import { ms } from "../lib/gateway";
import { failure, now, post } from "../lib/playground";
import type { ClassifyAnswer, TextClassifier } from "../lib/types";

const CLASSIFY_SAMPLE = "I love how quiet this laptop is.\nThe delivery was late and the box was crushed.\n這家餐廳的服務很好，下次還會再來。";
const ZERO_SHOT_SAMPLE = "The new laptop has a faster chip and a longer battery life.\nThe soup was cold and the waiter ignored us.\n我的發票金額開錯了，請幫我更正。";
const CLASSIFY_SHOWN = 5;
const DEFAULT_MAX_PAIRS = 256;   // the transformers recipe's max_pairs

type Out = { texts: string[]; answer: ClassifyAnswer & { labels?: string[]; multi_label?: boolean }; took: number; asked: string[] | null };

export function Classify({ model, labels, languages, maxPairs }: {
  model: string; labels: TextClassifier | null; languages?: string[] | null; maxPairs?: number | null;
}) {
  const t = useT();
  const zeroShot = !!labels?.zero_shot;
  const [text, setText] = useState(zeroShot ? ZERO_SHOT_SAMPLE : CLASSIFY_SAMPLE);
  const [chosen, setChosen] = useState<string[]>(DEFAULT_LABELS);
  const [typed, setTyped] = useState("");
  const [labelErr, setLabelErr] = useState<string | null>(null);
  const [template, setTemplate] = useState("");
  const [multi, setMulti] = useState(false);
  const [out, setOut] = useState<Out | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const list = text.split("\n").map((x) => x.trim()).filter(Boolean);
  const pairs = list.length * chosen.length;
  const cap = maxPairs ?? DEFAULT_MAX_PAIRS;
  const tplProblem = templateProblem(template.trim());
  const cjk = hasCjk(text);
  const englishModel = englishOnly(languages);

  const commit = (value: string) => {
    const r = addLabels(chosen, value);
    setChosen(r.labels);
    setLabelErr(r.error);
    setTyped("");
  };
  const onLabelKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === "," || e.key === "，" || e.key === "、") {
      e.preventDefault();
      commit(typed);
    } else if (e.key === "Backspace" && !typed && chosen.length) {
      setChosen(chosen.slice(0, -1));
    }
  };

  const go = async (e: FormEvent) => {
    e.preventDefault();
    let asked: string[] | null = null;
    if (zeroShot) {
      const r = typed.trim() ? addLabels(chosen, typed) : { labels: chosen, error: null };
      if (typed.trim()) { setChosen(r.labels); setTyped(""); setLabelErr(r.error); }
      asked = r.labels;
      if (!asked.length || tplProblem) return;
    }
    setBusy(true); setErr(null); setOut(null);
    const started = now();
    try {
      const body: Record<string, unknown> = { model, input: list };
      if (asked) {
        body.labels = asked;
        if (multi) body.multi_label = true;
        if (template.trim()) body.hypothesis_template = template.trim();
      }
      const res = await post("classify", body);
      if (!res.ok) { setErr(await failure(res)); return; }
      setOut({ texts: list, answer: await res.json(), took: now() - started, asked });
    } catch { setErr(t("playground.unreachable")); } finally { setBusy(false); }
  };

  const fixedMulti = labels?.problem === "multi_label_classification";
  return (
    <section className="panel play" aria-labelledby="cls-title">
      <div className="panel-head">
        <h2 id="cls-title">{t("playground.cls.title")}</h2>
        {zeroShot && <HelpLink page="how-to" anchor="classify-text-without-training-zero-shot">{t("playground.cls.zs.howTo")}</HelpLink>}
      </div>
      {zeroShot ? (
        <p className="muted small">{t("playground.cls.zs.about")}{languages?.length ? t("playground.cls.zs.languages", { names: languagesLine(languages).text }) : ""}</p>
      ) : (
        <p className="muted small">
          {labels?.labels ? `${labelLine(t, labels.labels, labels.label_names ?? [])} ` : ""}
          {fixedMulti ? t("playground.cls.multi") : t("playground.cls.single")}
        </p>
      )}
      {englishModel && cjk && (
        <p className="cls-warn small" role="note"><IconAlertTriangle size={15} aria-hidden="true" /> {t("playground.cls.englishOnly")}</p>
      )}
      <form onSubmit={go}>
        <div className="field"><label htmlFor="cls-input">{t("playground.cls.texts")}</label><textarea id="cls-input" rows={4} value={text} onChange={(e) => setText(e.target.value)} /></div>
        {zeroShot && (
          <>
            <div className="field">
              <label htmlFor="cls-label-input">{t("playground.cls.zs.labels", { count: chosen.length, max: MAX_LABELS })}</label>
              <div className="chips-input">
                <ul className="chips-list" aria-label={t("playground.cls.zs.chosen")}>
                  {chosen.map((l) => (
                    <li key={l} className="chip chip-big">
                      <span className="chip-text">{l}</span>
                      <button type="button" className="emb-x" aria-label={t("playground.cls.zs.removeLabel", { label: l })}
                        onClick={() => setChosen(chosen.filter((x) => x !== l))}><IconX size={12} aria-hidden="true" /></button>
                    </li>
                  ))}
                </ul>
                <input id="cls-label-input" value={typed} autoComplete="off" placeholder={chosen.length ? t("playground.cls.zs.more") : t("playground.cls.zs.placeholder")}
                  onChange={(e) => { const v = e.target.value; if (/[,，、]/.test(v)) commit(v); else setTyped(v); }}
                  onKeyDown={onLabelKey} onBlur={() => { if (typed.trim()) commit(typed); }} aria-describedby="cls-label-help" />
              </div>
              <p id="cls-label-help" className="field-hint">{t("playground.cls.zs.labelsHint")}</p>
              {labelErr && <p className="field-error" role="alert">{labelErr}</p>}
            </div>
            <div className="grid-2 cls-zs-row">
              <div className={`field${tplProblem ? " has-error" : ""}`}>
                <label htmlFor="cls-template">{t("playground.cls.zs.template")}</label>
                <input id="cls-template" value={template} placeholder={DEFAULT_TEMPLATE} autoComplete="off" onChange={(e) => setTemplate(e.target.value)}
                  aria-describedby="cls-template-help" />
                {tplProblem && <p className="field-error" role="alert">{tplProblem}</p>}
                <p id="cls-template-help" className="field-hint">
                  {t.rich("playground.cls.zs.templateHint", { code: <code>{"{}"}</code> })}{" "}
                  {template.trim() !== ZH_TEMPLATE && (
                    <button type="button" className="linkish" onClick={() => setTemplate(ZH_TEMPLATE)}>{t("playground.cls.zs.useZh", { template: ZH_TEMPLATE })}</button>
                  )}
                </p>
              </div>
              <div className="field">
                <span className="label-like">{t("playground.cls.zs.multiTitle")}</span>
                <label className="check-line"><input type="checkbox" checked={multi} onChange={(e) => setMulti(e.target.checked)} /> {t("playground.cls.zs.multi")}</label>
                <p className="field-hint">{multi ? t("playground.cls.zs.multiOn") : t("playground.cls.zs.multiOff")}</p>
              </div>
            </div>
            <p className={`small ${pairs > cap ? "field-error" : "muted"}`} role="status">
              {t("playground.cls.zs.pairs", { texts: list.length, labels: chosen.length, pairs, max: cap })}
            </p>
          </>
        )}
        <button type="submit" className="btn btn-primary" style={{ marginTop: 10 }}
          disabled={busy || !list.length || (zeroShot && ((!chosen.length && !typed.trim()) || !!tplProblem || pairs > cap))}>
          {busy ? t("playground.waiting") : t("playground.cls.classify")}
        </button>
      </form>
      {err && <div style={{ marginTop: 12 }}><Notice tone="error">{err}</Notice></div>}
      {out && (
        <>
          <ol className="cls-results" aria-label={t("playground.cls.labels")}>
            {[...out.answer.data].sort((a, b) => a.index - b.index).map((d) => (
              <ClassifyResult key={d.index} text={out.texts[d.index] ?? ""} d={d}
                names={out.asked ? out.answer.labels ?? out.asked : labels?.label_names ?? []} keepOrder={!!out.asked} />
            ))}
          </ol>
          <p className="muted small">{out.answer.usage?.prompt_tokens != null ? t("playground.cls.tokens", { count: String(out.answer.usage.prompt_tokens) }) : ""}{ms(out.took)}
            {out.asked ? ` · ${out.answer.multi_label ? t("playground.cls.zs.ownScore") : t("playground.cls.single")}` : ""}</p>
        </>
      )}
    </section>
  );
}

// "3 labels: positive, negative, neutral." (at most six names, then "…").
function labelLine(t: ReturnType<typeof useT>, count: number, names: string[]): string {
  if (!names.length) return t("playground.cls.labelsOnly", { count: String(count) });
  const list = names.slice(0, 6).join(t("playground.cls.join"));
  return t(names.length > 6 ? "playground.cls.labelsMore" : "playground.cls.labelsNamed", { count: String(count), names: list });
}

// One text's answer. A fixed-label model's bars go most likely first (the top five, then "Show all"); a zero-shot
// model's keep the order of the labels asked, all of them, the most likely marked.
function ClassifyResult({ text, d, names, keepOrder }: { text: string; d: ClassifyAnswer["data"][number]; names: string[]; keepOrder: boolean }) {
  const t = useT();
  const [all, setAll] = useState(false);
  // Without the names (a local model), the engine still names the most likely label.
  const top = d.probs.indexOf(Math.max(...d.probs));
  const named = d.probs.map((p, i) => ({ i, p, name: names[i] ?? (i === top && d.label ? d.label : t("playground.cls.label", { i: String(i) })) }));
  const ranked = keepOrder ? named : [...named].sort((a, b) => b.p - a.p);
  const shown = all || keepOrder ? ranked : ranked.slice(0, CLASSIFY_SHOWN);
  const best = named[top];
  return (
    <li className="cls-item">
      <p className="cls-text">{text}</p>
      <p className="cls-top"><strong>{d.label ?? best?.name}</strong> <span className="mono small">{(best?.p ?? 0).toFixed(3)}</span></p>
      <ul className="cls-bars" aria-label={t("playground.cls.probs", { n: String(d.index + 1) })}>
        {shown.map((r) => (
          <li key={r.i} className={keepOrder && r.i === top ? "cls-best" : undefined}>
            <span className="cls-name" title={r.name}>{r.name}</span>
            <span className="cls-bar" aria-hidden="true"><span style={{ width: `${Math.max(0, Math.min(1, r.p)) * 100}%` }} /></span>
            <span className="mono small">{r.p.toFixed(3)}</span>
          </li>
        ))}
      </ul>
      {!keepOrder && ranked.length > CLASSIFY_SHOWN && (
        <button type="button" className="linkish small" onClick={() => setAll(!all)}>
          {all ? t("playground.cls.top") : t("playground.cls.all", { count: String(ranked.length) })}
        </button>
      )}
    </li>
  );
}
