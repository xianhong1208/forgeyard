// 1.4.2 (#121, #122): "Update the agent" on a machine's page, and the hint on its tile. Admins get the complete
// command for that machine (GET /api/machines/{id}/agent-update), built from how its agent said it runs; everyone
// sees whether it is behind the server.
import { IconArrowUpCircle } from "@tabler/icons-react";
import { useEffect, useRef } from "react";
import { Link, useLocation } from "react-router";

import { behindDetail, behindText, methodLine } from "../lib/agentUpdate";
import type { AgentUpdate, Machine, UpdateStep } from "../lib/types";
import { useApi } from "../lib/useApi";
import { HelpLink } from "./HelpLink";
import { CopyBlock, Notice } from "./ui";

// The tile's and the page's hint: a link to the machine's update command.
export function BehindChip({ m }: { m: Machine }) {
  const text = behindText(m);
  if (!text) return null;
  return (
    <Link to={`/machines/${m.id}#update`} className="chip chip-update" title="Open the command that updates this agent">
      <IconArrowUpCircle size={13} aria-hidden="true" /> {text}
    </Link>
  );
}

function Steps({ steps }: { steps: UpdateStep[] }) {
  return (
    <ol className="update-steps">
      {steps.map((s, i) => (
        <li key={i}>
          <p className="update-step-title">{s.title}</p>
          <CopyBlock value={s.command} label={`Update command: ${s.title}`} />
        </li>
      ))}
    </ol>
  );
}

export function AgentUpdateSection({ m, canManage }: { m: Machine; canManage: boolean }) {
  const path = canManage && m.status !== "revoked" ? `/machines/${m.id}/agent-update` : null;
  const { data, error, reload } = useApi<AgentUpdate>(path);
  const location = useLocation();
  const ref = useRef<HTMLElement>(null);
  // A new agent version (it was just updated) or a new connection changes the command: ask again.
  const seen = useRef(`${m.agent_version}|${m.connected_at}`);
  useEffect(() => {
    const now = `${m.agent_version}|${m.connected_at}`;
    if (now === seen.current) return;
    seen.current = now;
    if (path) reload();
  }, [m.agent_version, m.connected_at, path, reload]);
  // Opened from a tile's or Setup health's link: bring the section into view once it has its content.
  const scrolled = useRef(false);
  useEffect(() => {
    if (scrolled.current || location.hash !== "#update" || !(data || !canManage) || !ref.current) return;
    scrolled.current = true;
    ref.current.scrollIntoView?.({ block: "start" });
  }, [location.hash, data, canManage]);

  if (m.status === "revoked" || (!canManage && !m.agent_behind)) return null;
  const behind = m.agent_behind;
  return (
    <section className="panel mk-section agent-update" id="update" ref={ref} aria-labelledby="mk-update">
      <div className="panel-head">
        <h2 id="mk-update">Update the agent</h2>
        {behind && <span className="chip chip-update">{behindText(m)}</span>}
      </div>
      {behind ? (
        <p className="update-why">{behindDetail(behind)}</p>
      ) : (
        <p className="muted small">
          Agent {m.agent_version ?? "of an unknown version"} matches the server{m.server_version ? ` (${m.server_version})` : ""}.
          {data?.known && data.steps.length > 0 ? " The command below starts it again on the server's version, should you need to." : ""}
        </p>
      )}
      {!canManage && <p className="muted small">An admin sees the command that updates it here.</p>}
      {canManage && error && <Notice tone="error">{error}</Notice>}
      {canManage && !data && !error && <p className="muted small">Loading…</p>}
      {data && !data.known && (
        <Notice>{data.reason ?? "How this agent runs is not known."}</Notice>
      )}
      {data && data.known && (
        <>
          <p className="small">{methodLine(data)}</p>
          {data.steps.length > 0 && <Steps steps={data.steps} />}
          {data.alternative && (
            <details className="update-alt">
              <summary>{data.alternative.title}</summary>
              <Steps steps={data.alternative.steps} />
            </details>
          )}
        </>
      )}
      {data && data.notes.length > 0 && (
        <ul className="update-notes">{data.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
      )}
      {canManage && <HelpLink page="how-to" anchor="update-an-agent">Update an agent</HelpLink>}
    </section>
  );
}
