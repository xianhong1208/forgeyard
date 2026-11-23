// A deployment's replicas: their live output, and the notice for replicas their agent never took.
// (On the app pages until 1.4; apps left the console in #88.)
import { IconPlayerPause, IconPlayerPlay } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";

import { HelpLink } from "./HelpLink";
import { Notice } from "./ui";
import { api, errorText } from "../lib/api";
import { useT } from "../i18n/react";
import type { LogsResponse, Workload } from "../lib/types";

export const LOGS_POLL_MS = 2000;

// 1.3.1: replicas placed on a connected machine that its agent has not taken (see the troubleshooting guide).
export function NotPickedUpNotice({ w }: { w: Workload }) {
  const t = useT();
  const stuck = w.replica_list.filter((r) => r.not_picked_up);
  if (!stuck.length) return null;
  return (
    <Notice tone="error">
      <strong>{stuck.length === 1 ? t("deployment.stuckOne", { ordinal: stuck[0].ordinal }) : t("deployment.stuckMany", { count: stuck.length })}</strong>
      <ul className="stuck-list">
        {stuck.map((r) => <li key={r.ordinal}>#{r.ordinal}: {r.not_picked_up}.</li>)}
      </ul>
      <HelpLink page="troubleshooting" anchor="a-replica-stays-pending">{t("deployment.stuckHelp")}</HelpLink>
    </Notice>
  );
}

export function Output({ w }: { w: Workload }) {
  const t = useT();
  const [ordinal, setOrdinal] = useState(0);
  const [follow, setFollow] = useState(true);
  const [logs, setLogs] = useState<LogsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pre = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api<LogsResponse>(`/workloads/${w.id}/replicas/${ordinal}/logs?lines=300`).then(
        (r) => {
          if (!alive) return;
          setLogs(r);
          setError(null);
        },
        (e) => alive && setError(errorText(e)),
      );
    void load();
    const timer = follow
      ? window.setInterval(() => {
          if (document.visibilityState !== "hidden") void load();
        }, LOGS_POLL_MS)
      : undefined;
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [w.id, ordinal, follow]);

  useEffect(() => {
    if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [logs, follow]);

  return (
    <section className="panel" aria-labelledby="output-title">
      <div className="panel-head">
        <h2 id="output-title">{t("deployment.out.title")}</h2>
        <div className="row-gap">
          {w.replica_list.length > 1 && (
            <select aria-label={t("deployment.col.replica")} value={ordinal} onChange={(e) => setOrdinal(Number(e.target.value))}>
              {w.replica_list.map((r) => <option key={r.ordinal} value={r.ordinal}>{t("deployment.out.replica", { ordinal: r.ordinal })}</option>)}
            </select>
          )}
          <button type="button" className="btn btn-quiet btn-small" onClick={() => setFollow((v) => !v)} aria-pressed={follow}>
            {follow ? <IconPlayerPause size={14} aria-hidden="true" /> : <IconPlayerPlay size={14} aria-hidden="true" />}
            {follow ? t("deployment.out.pause") : t("deployment.out.follow")}
          </button>
        </div>
      </div>
      <p className="muted small output-source" role="status">
        {logs?.source === "live" ? (
          <><span className={`pulse${follow ? "" : " pulse-off"}`} aria-hidden="true" /> {t("deployment.out.live", {
            how: follow ? t("deployment.out.every", { seconds: LOGS_POLL_MS / 1000 }) : t("deployment.out.paused") })}</>
        ) : logs ? (logs.note ? t.maybe(`deployment.out.sent.${logs.note}`, logs.note) : t("deployment.out.kept")) : t("common.loading")}
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      <pre className="logs" ref={pre} aria-label={t("deployment.out.label", { ordinal })}>{logs?.text || (logs ? t("deployment.out.empty") : "")}</pre>
    </section>
  );
}
