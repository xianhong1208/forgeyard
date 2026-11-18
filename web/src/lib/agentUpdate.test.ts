// 1.4.2 (#121, #122): the "update the agent" hint and how the update command is introduced.
import { describe, expect, it } from "vitest";

import { behindDetail, behindText, methodLine } from "./agentUpdate";
import type { AgentUpdate } from "./types";

const behind = (over = {}) => ({ agent: "1.4.0", server: "1.4.2", older: true, old_protocol: false, missing: [], ...over });

describe("agent behind the server", () => {
  it("FY-WEB-430 an older agent says both versions; a build from older code names what it lacks; none is null", () => {
    expect(behindText({ agent_behind: behind() })).toBe("agent 1.4.0, server 1.4.2: update");
    expect(behindText({ agent_behind: behind({ agent: "1.4.2", older: false, missing: ["image_save"] }) }))
      .toBe("agent 1.4.2 lacks image_save: update");
    expect(behindText({ agent_behind: behind({ agent: "1.4.2", older: false, missing: ["a", "b", "c", "d"] }) }))
      .toBe("agent 1.4.2 lacks a, b +2: update");
    expect(behindText({ agent_behind: null })).toBeNull();
    expect(behindText({})).toBeNull();
    expect(behindDetail(behind({ old_protocol: true }))).toMatch(/older protocol/);
    expect(behindDetail(behind())).toMatch(/^Its agent is 1\.4\.0; the server is 1\.4\.2/);
    expect(behindDetail(behind({ agent: "1.4.2", older: false, missing: ["relay"] }))).toMatch(/does not offer relay/);
  });

  it("FY-WEB-431 the line above the command says how the agent runs", () => {
    const u = { method: "container", known: true, offline: false, image: "docker.io/x/forgeyard-agent:1.4.2" } as AgentUpdate;
    expect(methodLine(u)).toBe("It runs in a container: pull docker.io/x/forgeyard-agent:1.4.2, then start the same container on it.");
    expect(methodLine({ ...u, offline: true })).toMatch(/gets its files through the server: carry the image/);
    expect(methodLine({ ...u, method: "install.sh" })).toBe("It was installed with install.sh: run it again, without a join token.");
    expect(methodLine({ ...u, known: false })).toBe("How this agent runs is not known.");
  });
});
