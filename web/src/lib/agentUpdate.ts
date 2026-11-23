// 1.4.2 (#121, #122): the "update the agent" hint on a machine's tile and page, and how its update command is
// introduced. Pure, so each case is tested on its own.
import { labels, t } from "../i18n/core";
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
  if (b.old_protocol) return t("machine.behind.oldProtocol", { agent: b.agent ?? t("machine.behind.unknownVersion"), server: b.server });
  if (b.older) return t("machine.behind.older", { agent: b.agent ?? t("machine.behind.ofUnknownVersion"), server: b.server });
  return t("machine.behind.lacks", { server: b.server, missing: b.missing.join(", ") });
}

export const METHOD_LABEL: Record<string, string> = labels({
  container: "machine.method.container",
  "install.sh": "machine.method.install",
  manual: "machine.method.manual",
  unknown: "machine.method.unknown",
});

// One line on how the agent runs and what the command does.
export function methodLine(u: AgentUpdate): string {
  if (!u.method || !u.known) return t("machine.methodLine.unknown");
  const how = METHOD_LABEL[u.method] ?? u.method;
  if (u.method === "container") {
    return u.offline
      ? t("machine.methodLine.containerOffline", { how, image: String(u.image) })
      : t("machine.methodLine.container", { how, image: String(u.image) });
  }
  if (u.method === "install.sh") return t("machine.methodLine.install", { how });
  return t("machine.methodLine.other", { how });
}
