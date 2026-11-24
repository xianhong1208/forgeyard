// @vitest-environment jsdom
// 1.4.2 (#121, #122): a machine's agent behind the server: the hint on its tile and page, and the update
// command admins see on its page.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import machinesFixture from "../test/machines.json";
import { LANG_KEY } from "../i18n/lang";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

const BEHIND = { agent: "1.4.0", server: "1.4.2", older: true, old_protocol: false, missing: [] };
const RUN = "sudo docker pull docker.io/example/forgeyard-agent:1.4.2 && \\\nsudo docker rm -f forgeyard-agent && \\\nsudo docker run -d \\\n  --gpus all \\\n  docker.io/example/forgeyard-agent:1.4.2";
const UPDATE = {
  machine_id: 1, name: "a100-box", agent_version: "1.4.0", server_version: "1.4.2", behind: BEHIND, method: "container",
  known: true, reason: null, image: "docker.io/example/forgeyard-agent:1.4.2", offline: false,
  steps: [{ title: "On a100-box", command: RUN }],
  alternative: { title: "a100-box cannot reach the registry?", steps: [{ title: "On a machine that can reach it", command: "sudo docker save x | gzip | ssh USER@a100-box 'gunzip -c | sudo docker load'" }] },
  notes: ["The agent does not report secrets. Before you run it, set HF_TOKEN in your shell (export NAME=…), or the new container starts without it."],
  guide: "/guide/en/how-to#update-an-agent",
};

function machines(behind: unknown = BEHIND) {
  const data = structuredClone(machinesFixture) as typeof machinesFixture & { machines: Record<string, unknown>[] };
  data.machines[0].agent_behind = behind;
  data.machines[0].agent_version = "1.4.0";
  data.machines[0].server_version = "1.4.2";
  data.machines[1].agent_behind = null;
  return data;
}

function server(route: (url: string) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.hash}</div>;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
        <Where />
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  localStorage.removeItem(LANG_KEY);
  vi.unstubAllGlobals();
});

describe("updating an agent", () => {
  it("FY-WEB-432 a tile says agent and server versions and opens the machine's update command", async () => {
    server((url) => (url === "/api/machines" ? { status: 200, body: machines() }
      : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } }
        : url === "/api/machines/1/agent-update" ? { status: 200, body: UPDATE } : undefined));
    mount("/machines");
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    const hint = await within(box).findByRole("link", { name: /agent 1\.4\.0, server 1\.4\.2: update/ });
    expect(hint.getAttribute("href")).toBe("/machines/1#update");
    const strix = screen.getByRole("article", { name: "Machine strix" });
    expect(within(strix).queryByText(/: update/)).toBeNull();
    fireEvent.click(hint);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/machines/1#update"));
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(await within(section).findByLabelText("Update command: On a100-box")).toBeTruthy();
  });

  it("FY-WEB-433 an admin sees the complete command with Copy, the alternative, the notes and the guide", async () => {
    const fetch = server((url) => (url === "/api/machines" ? { status: 200, body: machines() }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: UPDATE } : undefined));
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(within(section).getByText(/Its agent is 1\.4\.0; the server is 1\.4\.2/)).toBeTruthy();
    const block = await within(section).findByLabelText("Update command: On a100-box");
    expect(block.textContent).toBe(RUN);
    expect(within(section).getByText(/It runs in a container: pull docker\.io\/example\/forgeyard-agent:1\.4\.2/)).toBeTruthy();
    fireEvent.click(within(block.parentElement as HTMLElement).getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(RUN));
    expect(within(section).getByText("a100-box cannot reach the registry?")).toBeTruthy();
    expect(within(section).getByText(/set HF_TOKEN in your shell/)).toBeTruthy();
    expect(within(section).getByRole("link", { name: "Update an agent" }).getAttribute("href")).toMatch(/how-to#update-an-agent$/);
    expect(fetch.mock.calls.filter(([u]) => u === "/api/machines/1/agent-update").length).toBe(1);
  });

  it("FY-WEB-434 an agent that does not say how it runs: the reason and the guide, no command", async () => {
    server((url) => (url === "/api/machines" ? { status: 200, body: machines() }
      : url === "/api/machines/1/agent-update"
        ? { status: 200, body: { ...UPDATE, method: null, known: false, steps: [], alternative: null, notes: [], reason: "This agent does not say how it runs (agents before 1.4.2 do not)." } }
        : undefined));
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(await within(section).findByText(/agents before 1\.4\.2 do not/)).toBeTruthy();
    expect(within(section).queryByRole("button", { name: "Copy" })).toBeNull();
    expect(within(section).getByRole("link", { name: "Update an agent" })).toBeTruthy();
  });

  it("FY-WEB-436 a current agent: says it matches, and offers the command only when there is one", async () => {
    server((url) => (url === "/api/machines" ? { status: 200, body: machines(null) }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: { ...UPDATE, behind: null } } : undefined));
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(await within(section).findByText(/matches the server \(1\.4\.2\)\. The command below starts it again/)).toBeTruthy();
    expect(within(section).queryByText(/: update$/)).toBeNull();
    cleanup();
    server((url) => (url === "/api/machines" ? { status: 200, body: machines(null) }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: { ...UPDATE, behind: null, known: false, steps: [], alternative: null, notes: [], reason: "The agent could not describe how it runs." } } : undefined));
    mount("/machines/1");
    const again = await screen.findByRole("region", { name: "Update the agent" });
    await within(again).findByText("The agent could not describe how it runs.");
    expect(within(again).queryByText(/The command below/)).toBeNull();
  });

  it("FY-WEB-435 others see that it is behind but never ask for the command; nothing when current", async () => {
    const fetch = server((url) => (url === "/api/machines" ? { status: 200, body: { ...machines(), can_manage: false } } : undefined),
      { ...OWNER, role: "user", is_owner: false });
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(within(section).getByText("An admin sees the command that updates it here.")).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => String(u).includes("agent-update"))).toBe(false);
    cleanup();
    server((url) => (url === "/api/machines" ? { status: 200, body: { ...machines(null), can_manage: false } } : undefined),
      { ...OWNER, role: "user", is_owner: false });
    mount("/machines/1");
    await screen.findByRole("region", { name: "Memory map" });
    expect(screen.queryByRole("region", { name: "Update the agent" })).toBeNull();
    expect(screen.queryByText(/: update$/)).toBeNull();
  });

  it("FY-WEB-542 an agent too old for TLS: a chip on its page opens the update command, the Agent section and the update section say it is not encrypted", async () => {
    const data = machines();
    data.machines[0].replica_tls = { state: "plain", not_after: null, reason: null };
    data.machines[1].replica_tls = { state: "ok", not_after: "2026-12-21T12:00:00+00:00", reason: null };
    server((url) => (url === "/api/machines" ? { status: 200, body: data }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: UPDATE } : undefined));
    mount("/machines/1");
    const chip = await screen.findByRole("link", { name: /not encrypted: update the agent/ });
    expect(chip.getAttribute("href")).toBe("/machines/1#update");
    expect(screen.getByText(/^Not encrypted \(plain HTTP\): its agent is older than 1\.4\.5/)).toBeTruthy();
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect(within(section).getByText(/Traffic to this machine is not encrypted: update the agent\./)).toBeTruthy();
    fireEvent.click(chip);
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/machines/1#update"));
  });

  it("FY-WEB-543 a TLS machine: its certificate's expiry, no chip; one without a certificate shows the error chip", async () => {
    const data = machines(null);
    data.machines[0].replica_tls = { state: "ok", not_after: "2026-12-21T12:00:00+00:00", reason: null };
    data.machines[1].replica_tls = { state: "missing", not_after: null, reason: "the CSR cannot be read" };
    server((url) => (url === "/api/machines" ? { status: 200, body: data }
      : url === "/api/machines/1/agent-update" || url === "/api/machines/2/agent-update" ? { status: 200, body: { ...UPDATE, behind: null } } : undefined));
    mount("/machines/1");
    expect(await screen.findByText("Encrypted (TLS), certificate valid until 2026-12-21")).toBeTruthy();
    expect(screen.queryByText(/not encrypted/)).toBeNull();
    cleanup();
    mount(`/machines/${data.machines[1].id}`);
    expect(await screen.findByText("no replica certificate")).toBeTruthy();
    expect(screen.getByText(/no certificate from this server \(the CSR cannot be read\)/)).toBeTruthy();
  });

  it("FY-WEB-930 (1.5.4) an NVIDIA command that lists the host's devices says so under it, in English and Chinese", async () => {
    const LOOP = "$(for d in /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools /dev/nvidia-modeset /dev/nvidia[0-9]*; do [ -e \"$d\" ] && printf -- '--device %s ' \"$d\"; done)";
    const run = RUN.replace("--gpus all \\\n", `--gpus all \\\n  ${LOOP} \\\n`);
    const host = { ...UPDATE, steps: [{ title: "On a100-box", command: run }], nvidia_devices: "host" };
    server((url) => (url === "/api/machines" ? { status: 200, body: machines() }
      : url === "/api/machines/1/agent-update" ? { status: 200, body: host } : undefined));
    mount("/machines/1");
    const section = await screen.findByRole("region", { name: "Update the agent" });
    expect((await within(section).findByLabelText("Update command: On a100-box")).textContent).toBe(run);
    const note = within(section).getByText(/passes every NVIDIA device of the host \(\/dev\/nvidia\*\) with --device/);
    expect(note.textContent).toMatch(/a GPU installed since the agent started is included/);
    expect(note.textContent).toMatch(/systemctl daemon-reload/);
    expect(note.textContent).toMatch(/still this machine's GPU access setting/);
    cleanup();
    localStorage.setItem(LANG_KEY, "zh-TW");
    mount("/machines/1");
    const zh = await screen.findByRole("region", { name: "更新 agent" });
    await within(zh).findByLabelText("更新指令：On a100-box");
    const zhNote = within(zh).getByText(/找出主機上的每個 NVIDIA 裝置（\/dev\/nvidia\*），並用 --device 傳入/);
    expect(zhNote.textContent).toMatch(/agent 啟動後才裝上的 GPU 也包含在內/);
    expect(zhNote.textContent).toMatch(/仍然由這台機器的 GPU 存取設定決定/);
  });

  it("FY-WEB-931 (1.5.4) no such note for nodes listed by name, an AMD or CPU agent, or an older server", async () => {
    for (const nvidia_devices of ["listed", null, undefined]) {
      server((url) => (url === "/api/machines" ? { status: 200, body: machines() }
        : url === "/api/machines/1/agent-update" ? { status: 200, body: { ...UPDATE, nvidia_devices } } : undefined));
      mount("/machines/1");
      const section = await screen.findByRole("region", { name: "Update the agent" });
      await within(section).findByLabelText("Update command: On a100-box");
      expect(within(section).queryByText(/every NVIDIA device of the host/)).toBeNull();
      cleanup();
    }
  });
});
