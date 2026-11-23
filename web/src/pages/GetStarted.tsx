import { IconArrowRight, IconCheck } from "@tabler/icons-react";
import { useState } from "react";
import { Link, Navigate } from "react-router";

import { SetupHealth } from "../components/Advisor";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBox, Notice, PageHeader, TrustHint } from "../components/ui";
import { useT, type Translator } from "../i18n/react";
import { api, errorText } from "../lib/api";
import { API_KIND } from "../lib/gateway";
import { MACHINES } from "../lib/nav";
import { KEY_ENV, currentStep, exampleCall, type Checklist, type Step } from "../lib/onboarding";
import { useApi } from "../lib/useApi";
import { usePoll } from "../lib/poll";

// Steps tick themselves off from real state (a machine joining, a deployment becoming ready), so
// the page follows along while it is open.
export const CHECKLIST_POLL_MS = 5000;

function useChecklist() {
  const r = useApi<Checklist>("/onboarding");
  usePoll(r.reload, CHECKLIST_POLL_MS);
  return r;
}

// "/": the checklist until it is done or hidden, then the machines. Decided on the first answer
// only, so finishing the last step (or hiding the list) while looking at it does not move the page.
export function HomePage() {
  const { data, error, reload } = useChecklist();
  const [show, setShow] = useState<boolean | null>(null);
  if (show === null && data) setShow(!data.hidden && !data.complete);
  if (error && !data) return <Navigate to={MACHINES} replace />;
  if (!data || show === null) return <Loading />;
  if (!show) return <Navigate to={MACHINES} replace />;
  return <GetStarted c={data} reload={reload} />;
}

// "/start": always the checklist (Get started in the account menu).
export function GetStartedPage() {
  const { data, error, reload } = useChecklist();
  if (error && !data) return <Notice tone="error">{error}</Notice>;
  if (!data) return <Loading />;
  return <GetStarted c={data} reload={reload} />;
}

function Loading() {
  const t = useT();
  return <p className="muted">{t("common.loading")}</p>;
}

// The server writes the steps in English (GET /api/onboarding, unchanged): another language takes a step's
// title and why by its id, and its fixed sentences (who does it, the button, the details that name nothing) by
// their text. A detail that names machines or models stays as the server wrote it.
function stepText(t: Translator, s: Step) {
  const sent = (text: string | null) => (text === null ? null : t.lang === "en" ? text : t.maybe(`start.sent.${text}`, text));
  return {
    title: t.lang === "en" ? s.title : t.maybe(`start.step.${s.id}.title`, s.title),
    why: t.lang === "en" ? s.why : t.maybe(`start.step.${s.id}.why`, s.why),
    detail: sent(s.detail) ?? "",
    who: sent(s.who),
    label: s.action ? sent(s.action.label) : null,
  };
}

function GetStarted({ c: polled, reload }: { c: Checklist; reload: () => void }) {
  const t = useT();
  const [saved, setSaved] = useState<Checklist | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  // What a change answered, until the next poll says the same.
  const c = saved && saved.hidden !== polled.hidden ? saved : polled;
  const next = currentStep(c);
  const setup = c.kind === "setup";

  const setHidden = async (hidden: boolean) => {
    setMessage(null);
    try {
      setSaved(await api<Checklist>("/onboarding", { method: "PUT", body: { hidden } }));
      reload();
      setMessage({ tone: "ok", text: hidden ? t("start.hidden") : t("start.shown") });
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  return (
    <>
      <PageHeader index={t("start.index")} title={setup ? t("start.titleSetup") : t("start.titleUse")}
        lead={setup ? t("start.leadSetup") : t("start.leadUse")} />
      <HelpLinks>
        <HelpLink page="quick-start">{t("start.help.quick")}</HelpLink>
        <HelpLink page="concepts">{t("start.help.concepts")}</HelpLink>
        <HelpLink page="troubleshooting">{t("start.help.troubleshooting")}</HelpLink>
      </HelpLinks>
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      <section className="onb-progress" aria-label={t("start.progress")}>
        <ol className="onb-track" aria-hidden="true">
          {c.steps.map((s) => <li key={s.id} className={s.done ? "on" : s === next ? "next" : ""} />)}
        </ol>
        <p className="onb-count">
          {c.complete ? t.rich("start.allDone", { total: c.total, strong: (text) => <strong>{text}</strong> })
            : t.rich("start.someDone", { done: c.done, total: c.total, next: next ? stepText(t, next).title : "", strong: (text) => <strong>{text}</strong> })}
        </p>
      </section>

      <ol className="onb-steps">
        {c.steps.map((s, i) => <StepRow key={s.id} s={s} n={i + 1} current={s === next} />)}
      </ol>

      <SetupHealth />

      <ExampleCall c={c} />

      <div className="onb-foot">
        {c.hidden ? (
          <>
            <p className="muted small">{t("start.hiddenNote")}</p>
            <button type="button" className="btn btn-quiet" onClick={() => void setHidden(false)}>{t("start.showIt")}</button>
          </>
        ) : (
          <>
            <p className="muted small">{c.complete ? t("start.doneNote") : t("start.hideNote")}</p>
            <button type="button" className="btn btn-quiet" onClick={() => void setHidden(true)}>{t("start.hideIt")}</button>
          </>
        )}
      </div>
    </>
  );
}

function StepRow({ s, n, current }: { s: Step; n: number; current: boolean }) {
  const t = useT();
  const text = stepText(t, s);
  const state = s.done ? "done" : current ? "current" : "open";
  return (
    <li className={`onb-step onb-${state}`} aria-label={`${t("start.stepLabel", { n, title: text.title })}${s.done ? t("start.stepDoneSuffix") : ""}`}>
      <span className="onb-mark" aria-hidden="true">{s.done ? <IconCheck size={16} stroke={2.4} /> : n}</span>
      <div className="onb-body">
        <h2>{text.title}</h2>
        <p className="onb-why">{text.why}</p>
        <p className={`onb-detail${s.done ? " onb-detail-done" : ""}`}>
          {s.done ? `${t("start.doneMark")} ` : ""}{text.detail}
        </p>
        {text.who && !s.done && <p className="onb-who small">{text.who}</p>}
      </div>
      {s.action && (
        <Link to={s.action.to} className={`btn ${current && !s.who ? "btn-primary" : s.done ? "btn-quiet" : ""} onb-action`}>
          {s.done ? t("common.open") : text.label}{current && !s.who ? <IconArrowRight size={16} aria-hidden="true" /> : null}
        </Link>
      )}
    </li>
  );
}

function ExampleCall({ c }: { c: Checklist }) {
  const t = useT();
  const [tab, setTab] = useState<"python" | "curl">("python");
  const model = c.example?.model ?? "MODEL_NAME";
  const call = exampleCall(c.gateway_url, model, c.example?.api ?? "openai-chat", c.trust_url);
  return (
    <section className="panel onb-example" aria-labelledby="example-title">
      <h2 id="example-title">{t("start.example.title")}</h2>
      <p>{t.rich("start.example.lead", { baseUrl: <code>base_url</code>, env: <code>{KEY_ENV}</code>, model: <code>model</code> })}</p>
      <CopyBox value={c.gateway_url} label={t("start.example.address")} />
      {c.trust_url && <TrustHint url={c.trust_url} />}
      <p className="muted small">
        {c.example
          ? t.rich(c.example.ready ? "start.example.uses" : "start.example.usesNotReady",
            { model: <code>{c.example.model}</code>, kind: API_KIND[c.example.api] ?? c.example.api })
          : t.rich("start.example.nothing", { placeholder: <code>MODEL_NAME</code> })}
      </p>
      <div className="tabs snippet-tabs" role="tablist" aria-label={t("start.example.tabs")}>
        <button type="button" role="tab" aria-selected={tab === "python"} className={tab === "python" ? "on" : ""} onClick={() => setTab("python")}>Python (openai)</button>
        <button type="button" role="tab" aria-selected={tab === "curl"} className={tab === "curl" ? "on" : ""} onClick={() => setTab("curl")}>curl</button>
      </div>
      <pre className="snippet" aria-label={t("start.example.snippet", { tab })}>{tab === "python" ? call.python : call.curl}</pre>
      <p className="muted small">{t.rich("start.example.needs", { install: <code>pip install {c.example && (c.example.api === "rerank" || c.example.api === "classify" || c.example.api.startsWith("vision-")) ? "httpx" : "openai"}</code> })}</p>
    </section>
  );
}
