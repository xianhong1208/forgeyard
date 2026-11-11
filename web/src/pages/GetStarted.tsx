import { IconArrowRight, IconCheck } from "@tabler/icons-react";
import { useState } from "react";
import { Link, Navigate } from "react-router";

import { SetupHealth } from "../components/Advisor";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { CopyBox, Notice, PageHeader, TrustHint } from "../components/ui";
import { api, errorText } from "../lib/api";
import { API_KIND } from "../lib/gateway";
import { MACHINES } from "../lib/nav";
import { KEY_ENV, currentStep, exampleCall, type Checklist, type Step } from "../lib/onboarding";
import { useApi } from "../lib/useApi";
import { usePoll } from "./Workloads";

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
  if (!data || show === null) return <p className="muted">Loading…</p>;
  if (!show) return <Navigate to={MACHINES} replace />;
  return <GetStarted c={data} reload={reload} />;
}

// "/start": always the checklist (Get started in the account menu).
export function GetStartedPage() {
  const { data, error, reload } = useChecklist();
  if (error && !data) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  return <GetStarted c={data} reload={reload} />;
}

function GetStarted({ c: polled, reload }: { c: Checklist; reload: () => void }) {
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
      setMessage({ tone: "ok", text: hidden
        ? "Hidden. Forgeyard now opens on Machines; Get started stays in your account menu."
        : "Forgeyard will open on this list again until every step is done." });
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    }
  };

  return (
    <>
      <PageHeader index="Get started" title={setup ? "Set up Forgeyard" : "Use the models"}
        lead={setup
          ? "Six steps from a fresh install to your first API call. Each step ticks itself off as soon as it is really done."
          : "Three steps to call the models on this server from your own programs. Each step ticks itself off when it is done."} />
      <HelpLinks>
        <HelpLink page="quick-start">The quick start, every step with its commands</HelpLink>
        <HelpLink page="concepts">How Forgeyard fits together</HelpLink>
        <HelpLink page="troubleshooting">Troubleshooting</HelpLink>
      </HelpLinks>
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      <section className="onb-progress" aria-label="Progress">
        <ol className="onb-track" aria-hidden="true">
          {c.steps.map((s) => <li key={s.id} className={s.done ? "on" : s === next ? "next" : ""} />)}
        </ol>
        <p className="onb-count">
          {c.complete ? <><strong>All {c.total} steps done.</strong> Forgeyard is ready to use.</>
            : <><strong>{c.done} of {c.total} done.</strong> Next step: {next?.title}.</>}
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
            <p className="muted small">This list is hidden from your home page. You can always open it from your account menu.</p>
            <button type="button" className="btn btn-quiet" onClick={() => void setHidden(false)}>Show it when I open Forgeyard</button>
          </>
        ) : (
          <>
            <p className="muted small">{c.complete ? "Done with setup? Forgeyard opens on Machines once every step is done." : "Know your way around already? Hide this list; it stays in your account menu."}</p>
            <button type="button" className="btn btn-quiet" onClick={() => void setHidden(true)}>Hide this list</button>
          </>
        )}
      </div>
    </>
  );
}

function StepRow({ s, n, current }: { s: Step; n: number; current: boolean }) {
  const state = s.done ? "done" : current ? "current" : "open";
  return (
    <li className={`onb-step onb-${state}`} aria-label={`Step ${n}: ${s.title}${s.done ? " (done)" : ""}`}>
      <span className="onb-mark" aria-hidden="true">{s.done ? <IconCheck size={16} stroke={2.4} /> : n}</span>
      <div className="onb-body">
        <h2>{s.title}</h2>
        <p className="onb-why">{s.why}</p>
        <p className={`onb-detail${s.done ? " onb-detail-done" : ""}`}>
          {s.done ? "Done. " : ""}{s.detail}
        </p>
        {s.who && !s.done && <p className="onb-who small">{s.who}</p>}
      </div>
      {s.action && (
        <Link to={s.action.to} className={`btn ${current && !s.who ? "btn-primary" : s.done ? "btn-quiet" : ""} onb-action`}>
          {s.done ? "Open" : s.action.label}{current && !s.who ? <IconArrowRight size={16} aria-hidden="true" /> : null}
        </Link>
      )}
    </li>
  );
}

function ExampleCall({ c }: { c: Checklist }) {
  const [tab, setTab] = useState<"python" | "curl">("python");
  const model = c.example?.model ?? "MODEL_NAME";
  const call = exampleCall(c.gateway_url, model, c.example?.api ?? "openai-chat", c.trust_url);
  return (
    <section className="panel onb-example" aria-labelledby="example-title">
      <h2 id="example-title">Call /v1</h2>
      <p>
        Forgeyard speaks the OpenAI API at this address. Use it as the <code>base_url</code> of any OpenAI client, with your API key
        in <code>{KEY_ENV}</code> and a deployment's name as the <code>model</code>.
      </p>
      <CopyBox value={c.gateway_url} label="Gateway address" />
      {c.trust_url && <TrustHint url={c.trust_url} />}
      <p className="muted small">
        {c.example
          ? <>The example uses <code>{c.example.model}</code> ({API_KIND[c.example.api] ?? c.example.api}){c.example.ready ? "." : ", which is not ready yet."}</>
          : <>Nothing is deployed yet: replace <code>MODEL_NAME</code> once a model is deployed. Deployed names are listed under API keys.</>}
      </p>
      <div className="tabs snippet-tabs" role="tablist" aria-label="Example">
        <button type="button" role="tab" aria-selected={tab === "python"} className={tab === "python" ? "on" : ""} onClick={() => setTab("python")}>Python (openai)</button>
        <button type="button" role="tab" aria-selected={tab === "curl"} className={tab === "curl" ? "on" : ""} onClick={() => setTab("curl")}>curl</button>
      </div>
      <pre className="snippet" aria-label={`${tab} example`}>{tab === "python" ? call.python : call.curl}</pre>
      <p className="muted small">Python needs <code>pip install {c.example?.api === "rerank" ? "httpx" : "openai"}</code>. Requests show up under Usage within a few seconds.</p>
    </section>
  );
}
