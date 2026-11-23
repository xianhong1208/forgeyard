// 1.4.2 (#121, #122): the "update the agent" hint on a machine's tile and page, and how its update command is
// introduced. Pure, so each case is tested on its own.
import { t } from "../i18n/core";
import type { AgentBehind, AgentUpdate, Machine } from "./types";

// "agent 1.4.0, server 1.4.2: update", or for a build of the server's version from older code
// "agent 1.4.2 lacks image_save: update". null when the agent need not be updated.
export function behindText(m: Pick<Machine, "agent_behind">): string | null {
  const b = m.agent_behind;
  if (!b) return null;
  if (b.older || b.old_protocol || b.missing.length === 0) return t("fleet.behind.version", { agent: b.agent ?? "?", server: b.server });
  const shown = b.missing.slice(0, 2).join(", ") + (b.missing.length > 2 ? ` +${b.missing.length - 2}` : "");
  return t("fleet.behind.lacks", { agent: b.agent ?? "?", missing: shown });
}

// A sentence for the machine's page.
export function behindDetail(b: AgentBehind): string {
  if (b.old_protocol) return `Its agent (${b.agent ?? "unknown version"}) speaks an older protocol than the server (${b.server}) and cannot work correctly. Update it.`;
  if (b.older) return `Its agent is ${b.agent ?? "of an unknown version"}; the server is ${b.server}. Update it to get this release's fixes and features.`;
  return `Its agent is the server's version (${b.server}) but does not offer ${b.missing.join(", ")}: it was built from older code. Update it.`;
}

export const METHOD_LABEL: Record<string, string> = {
  container: "runs in a container",
  "install.sh": "installed with install.sh",
  manual: "installed by hand",
  unknown: "could not tell",
};

// One line on how the agent runs and what the command does.
export function methodLine(u: AgentUpdate): string {
  if (!u.method || !u.known) return "How this agent runs is not known.";
  const how = METHOD_LABEL[u.method] ?? u.method;
  if (u.method === "container") {
    return u.offline
      ? `It ${how} and gets its files through the server: carry the image ${u.image} to it, then start the same container on the new image.`
      : `It ${how}: pull ${u.image}, then start the same container on it.`;
  }
  if (u.method === "install.sh") return `It was ${how}: run it again, without a join token.`;
  return `It was ${how}.`;
}
