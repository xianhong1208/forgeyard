// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import fixture from "../test/machines.json";
import { POLL_MS } from "./Machines";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
type Reply = { status: number; body: unknown };

function server(route: (url: string, init?: RequestInit) => Reply | undefined, user = OWNER) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = url === "/api/auth/state"
      ? { status: 200, body: { needs_setup: false, registration: "off", user } }
      : route(url, init) ?? { status: 404, body: {} };
    return new Response(JSON.stringify(r.body), { status: r.status });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount() {
  return render(
    <MemoryRouter initialEntries={["/machines"]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

const machines = (over: Partial<typeof fixture> = {}) => ({ status: 200, body: { ...fixture, ...over } });

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("machines page", () => {
  it("FY-WEB-033 shows each machine, its cards, arch and free memory", async () => {
    server((url) => (url === "/api/machines" ? machines() : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } } : undefined));
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    expect(within(box).getByText("online")).toBeTruthy();
    expect(within(box).getByText("simulated inventory")).toBeTruthy();
    expect(within(box).getByText("rack=r2")).toBeTruthy();
    expect(within(box).getByText("8 × NVIDIA A100 80GB PCIe")).toBeTruthy();
    expect(within(box).getAllByText("NVIDIA A100 80GB PCIe")).toHaveLength(8);
    expect(within(box).getAllByText("8.0")).toHaveLength(8);
    expect(within(box).getAllByText(/sm_80/)).toHaveLength(8);
    // card 0: 4023779328 bytes free of 80 GiB, shown plainly and as a nearly full bar
    const first = within(box).getAllByRole("listitem")[0];
    expect(first.textContent).toContain("3.7 GiB free");
    expect(first.textContent).toContain("of 80 GiB");
    expect(within(first).getByRole("meter").getAttribute("aria-valuenow")).toBe("95");
    expect(within(box).getByText("Heartbeat 4 s ago")).toBeTruthy();
  });

  it("FY-WEB-034 an APU shows gfx1151 and unified memory; an offline machine says since when", async () => {
    server((url) => (url === "/api/machines" ? machines() : url === "/api/machines/join-tokens" ? { status: 200, body: { tokens: [] } } : undefined));
    mount();
    const strix = await screen.findByRole("article", { name: "Machine strix" });
    expect(within(strix).getByText("gfx1151")).toBeTruthy();
    expect(within(strix).getByText("unified memory")).toBeTruthy();
    // An AMD card's driver is the ROCm version, and says so; NVIDIA keeps "driver".
    expect(within(strix).getByText("ROCm 7.0.2")).toBeTruthy();
    expect(within(await screen.findByRole("article", { name: "Machine a100-box" })).getAllByText("driver 580.178.04")).toHaveLength(8);
    expect(within(strix).getByText("offline")).toBeTruthy();
    expect(within(strix).getByText("Offline · last heartbeat 41 s ago")).toBeTruthy();
  });

  it("FY-WEB-035 Add machine shows the join command once", async () => {
    const created = { id: "t1", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner",
      token: "fyj_secret", server_url: "http://127.0.0.1:7860", valid_minutes: 15,
      command: "uv run --no-sync python -m forgeyard agent --server http://127.0.0.1:7860 --join-token fyj_secret" };
    const fetch = server((url, init) => {
      if (url === "/api/machines") return machines({ machines: [] });
      if (url === "/api/machines/join-tokens" && init?.method === "POST") return { status: 201, body: created };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return undefined;
    });
    mount();
    expect(await screen.findByText("No machines yet")).toBeTruthy();
    expect(screen.getByText(/one line installs the agent as a service/)).toBeTruthy();
    const [, inEmpty] = screen.getAllByRole("button", { name: "Add a machine" });
    fireEvent.click(inEmpty); // the empty page's own button opens the same form
    expect(screen.getAllByRole("button", { name: "Add a machine" })).toHaveLength(1);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    // Plain HTTP: no install script (HTTPS only), so the manual way is open, with the flags command too.
    expect((await screen.findByRole("tab", { name: "Manual (config file)" })).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("Join command with flags").textContent).toBe(created.command);
    fireEvent.click(screen.getByRole("tab", { name: /One-line install/ }));
    expect(screen.getByText(/served only over HTTPS/)).toBeTruthy();
    expect(screen.getByText(/only works on this computer/)).toBeTruthy();
    expect(screen.getByText(/Waiting for the machine to join/)).toBeTruthy();
    const post = fetch.mock.calls.find(([u, i]) => u === "/api/machines/join-tokens" && i?.method === "POST");
    expect((post?.[1]?.headers as Record<string, string>)["X-Forgeyard"]).toBe("1");
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText(/fyj_secret/)).toBeNull();
  });

  it("FY-WEB-036 operators and users see machines but cannot manage them", async () => {
    const fetch = server((url) => (url === "/api/machines" ? machines({ can_manage: false }) : undefined), { ...OWNER, role: "operator", is_owner: false });
    mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    expect(screen.queryByRole("button", { name: "Add a machine" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/join-tokens")).toBe(false);
  });

  it("FY-WEB-037 the page follows status changes without a reload", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let online = true;
    server((url) => {
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      if (url !== "/api/machines") return undefined;
      const [a, b] = fixture.machines;
      return machines({ machines: [{ ...a, status: online ? "online" : "offline" }, b] });
    });
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    expect(within(box).getByText("online")).toBeTruthy();
    online = false;
    await act(async () => {
      vi.advanceTimersByTime(POLL_MS + 10);
    });
    await waitFor(() => expect(within(screen.getByRole("article", { name: "Machine a100-box" })).getByText("offline")).toBeTruthy());
  });

  it("FY-WEB-038 revoke asks first, then calls the API", async () => {
    const fetch = server((url, init) => {
      if (url === "/api/machines/1/revoke" && init?.method === "POST") return { status: 200, body: {} };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return url === "/api/machines" ? machines() : undefined;
    });
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mount();
    const box = await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(within(box).getByRole("button", { name: "Revoke" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(fetch.mock.calls.some(([u]) => u === "/api/machines/1/revoke")).toBe(false);
    confirm.mockReturnValue(true);
    await act(async () => fireEvent.click(within(box).getByRole("button", { name: "Revoke" })));
    expect(fetch.mock.calls.some(([u, i]) => u === "/api/machines/1/revoke" && i?.method === "POST")).toBe(true);
    expect(await screen.findByText("Revoked a100-box; its agent was disconnected.")).toBeTruthy();
  });

  it("FY-WEB-119 over https the join command pins the CA, and offline agents get the trust command", async () => {
    const fp = "sha256:" + "cd".repeat(32);
    const trustCmd = `uv run --no-sync python -m forgeyard agent trust --server https://forge.lan:7860 --ca-fingerprint ${fp}`;
    const created = { id: "t2", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner",
      token: "fyj_secret", server_url: "https://forge.lan:7860", valid_minutes: 15, ca_fingerprint: fp, trust_command: trustCmd,
      command: `uv run --no-sync python -m forgeyard agent --server https://forge.lan:7860 --join-token fyj_secret --ca-fingerprint ${fp}`,
      install_command: `curl -fsSk https://forge.lan:7860/api/tls/ca.pem -o forgeyard-ca.pem && \\\n  sudo sh -s -- --join fyj_secret --ca-fingerprint ${fp}`,
      docker_build_command: "docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .",
      docker_commands: { nvidia: "docker run … --gpus all … forgeyard-agent:1.1.0", amd: "docker run … --device /dev/kfd … forgeyard-agent:1.1.0", cpu: "docker run … forgeyard-agent:1.1.0" },
      config_file: `server = "https://forge.lan:7860"\nca_fingerprint = "${fp}"\n`,
      manual_commands: ["uv run --no-sync python -m forgeyard agent join --join-token fyj_secret", "uv run --no-sync python -m forgeyard agent"],
      version: "1.1.0" };
    server((url, init) => {
      if (url === "/api/machines") return machines();
      if (url === "/api/tls") return { status: 200, body: { mode: "forgeyard-ca", https: true, admin: { trust_command: trustCmd, server_certificate: null } } };
      if (url === "/api/machines/join-tokens" && init?.method === "POST") return { status: 201, body: created };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return undefined;
    });
    mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    // The fixture has an offline machine: the one-time trust command is offered.
    expect((await screen.findByText("Machines offline since this server switched to HTTPS?")).tagName).toBe("SUMMARY");
    expect(screen.getByLabelText("Trust command").textContent).toBe(trustCmd);
    fireEvent.click(screen.getByRole("button", { name: "Add a machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    // Three ways; the one-line install first, and every one pins the fingerprint.
    expect((await screen.findByRole("tab", { name: /One-line install/ })).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("Install command").textContent).toBe(created.install_command);
    expect(screen.getByText(/trusts only the certificate authority with the fingerprint/)).toBeTruthy();
    expect(screen.queryByText(/only works on this computer/)).toBeNull();
    // Docker: built in the page from the token, the server's address and version (FY-WEB-123..135).
    fireEvent.click(screen.getByRole("tab", { name: "Docker" }));
    expect(screen.getByLabelText("Build command").textContent).toBe("sudo docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .");
    const docker = () => screen.getByLabelText("Docker command").textContent ?? "";
    expect(docker()).toMatch(/^sudo docker run -d --name forgeyard-agent .*\n {2}--gpus all \\\n/);
    for (const part of ["-e FORGEYARD_SERVER=https://forge.lan:7860", `-e FORGEYARD_CA_FINGERPRINT=${fp}`, "-e FORGEYARD_JOIN_TOKEN=fyj_secret",
      "-v /srv/forgeyard-models:/srv/forgeyard-models", "-e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models"]) expect(docker()).toContain(part);
    expect(docker().endsWith("\n  forgeyard-agent:1.1.0")).toBe(true);
    expect(screen.queryByText(/^Advanced \/ offline/)).toBeNull(); // nothing published: the build is a step
    fireEvent.click(screen.getByRole("button", { name: "AMD" }));
    expect(docker()).toContain("--device /dev/kfd --device /dev/dri");
    expect(docker()).not.toContain("--gpus");
    expect(screen.getByText(/given by their number on the host: the image has no render group/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "CPU only" }));
    expect(docker()).not.toMatch(/--gpus|--device/);
    fireEvent.click(screen.getByRole("tab", { name: "Manual (config file)" }));
    expect(screen.getByLabelText("Config file").textContent).toBe(created.config_file);
    expect(screen.getByLabelText("Join command").textContent).toBe(created.manual_commands[0]);
    expect(screen.getByLabelText("Start command").textContent).toBe(created.manual_commands[1]);
    expect(screen.getByLabelText("Join command with flags").textContent).toBe(created.command);
    // "How do I…?" links go to the guide.
    expect(screen.getByRole("link", { name: "How do I add a machine?" }).getAttribute("href")).toMatch(/^\/guide\/(en|zh-TW)\/quick-start#3-add-a-machine$/);
  });
  it("FY-WEB-135 the Docker tab is a form: the command follows it live, bad values give errors, values are remembered", async () => {
    const fp = "sha256:" + "ab".repeat(32);
    const created = { id: "t3", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner",
      token: "fyj_secret", server_url: "https://forge.lan:7860", valid_minutes: 15, ca_fingerprint: fp, trust_command: null,
      command: "uv run --no-sync python -m forgeyard agent --join-token fyj_secret", install_command: "curl … | sudo sh",
      docker_build_command: "unused", docker_commands: { nvidia: "unused", amd: "unused", cpu: "unused" }, config_file: "",
      manual_commands: [], version: "1.1.1" };
    const cert = { names: ["localhost", "forge.lan"], ips: ["127.0.0.1", "::1"], expires_at: "2027-11-09T00:00:00Z", issuer: "Forgeyard CA" };
    server((url, init) => {
      if (url === "/api/machines") return machines();
      if (url === "/api/tls") return { status: 200, body: { mode: "forgeyard-ca", https: true, admin: { trust_command: null, server_certificate: cert } } };
      if (url === "/api/machines/join-tokens" && init?.method === "POST") return { status: 201, body: created };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return undefined;
    });
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const view = mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(screen.getByRole("button", { name: "Add a machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    fireEvent.click(await screen.findByRole("tab", { name: "Docker" }));
    fireEvent.click(screen.getByRole("button", { name: "AMD" }));
    const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
    const docker = () => screen.getByLabelText("Docker command").textContent ?? "";
    expect((screen.getByLabelText("Server address, as that machine reaches it") as HTMLInputElement).value).toBe("https://forge.lan:7860");

    type("Model directory on that machine", "/data/models");
    type("Machine name", "amd-01");
    type("Server address, as that machine reaches it", "https://127.0.0.1:7860");
    type("Port range", "33000-33002");
    expect(docker().split(" \\\n  ").slice(-8)).toEqual([
      "-v /data/models:/data/models", "-e FORGEYARD_SERVER=https://127.0.0.1:7860", `-e FORGEYARD_CA_FINGERPRINT=${fp}`,
      "-e FORGEYARD_JOIN_TOKEN=fyj_secret", "-e FORGEYARD_MODEL_CACHE=/data/models", "-e FORGEYARD_PORT_RANGE=33000-33002",
      "-e FORGEYARD_NAME=amd-01", "forgeyard-agent:1.1.1"]);
    expect(screen.queryByText(/is not in this server's certificate/)).toBeNull(); // 127.0.0.1 is in it
    // The same values on the other kinds.
    fireEvent.click(screen.getByRole("button", { name: "CPU only" }));
    expect(docker()).toContain("-e FORGEYARD_NAME=amd-01");

    // Copy copies exactly what is shown.
    const block = screen.getByLabelText("Docker command").parentElement!;
    await act(async () => fireEvent.click(within(block).getByRole("button", { name: /Copy/ })));
    expect(writeText).toHaveBeenCalledWith(docker());

    // An address the certificate does not cover: a warning naming TLS_EXTRA_NAMES; the command stays.
    type("Server address, as that machine reaches it", "https://192.168.1.21:7860");
    expect(screen.getByText(/is not in this server's certificate/).textContent).toContain("192.168.1.21");
    expect(screen.getByText("TLS_EXTRA_NAMES")).toBeTruthy();
    expect(docker()).toContain("-e FORGEYARD_SERVER=https://192.168.1.21:7860");
    fireEvent.click(screen.getByRole("button", { name: "Use this server's address, https://forge.lan:7860" }));
    expect(docker()).toContain("-e FORGEYARD_SERVER=https://forge.lan:7860");
    expect(screen.queryByText(/is not in this server's certificate/)).toBeNull();

    // A bad value: its error, and no command to copy.
    type("Model directory on that machine", "/data/my models");
    expect(screen.getByText("No spaces: the command is pasted into a shell.")).toBeTruthy();
    expect(screen.queryByLabelText("Docker command")).toBeNull();
    expect(screen.getByText("Fix the fields marked above to get the command.")).toBeTruthy();
    type("Model directory on that machine", "/data/models");

    // Remembered in this browser: a new join command starts from the same values.
    view.unmount();
    mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(screen.getByRole("button", { name: "Add a machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    fireEvent.click(await screen.findByRole("tab", { name: "Docker" }));
    expect((screen.getByLabelText("Model directory on that machine") as HTMLInputElement).value).toBe("/data/models");
    expect((screen.getByLabelText("Machine name") as HTMLInputElement).value).toBe("amd-01");
    expect((screen.getByLabelText("Port range") as HTMLInputElement).value).toBe("33000-33002");
    expect(docker()).toContain("-e FORGEYARD_PORT_RANGE=33000-33002");
  });

  it("FY-WEB-145 with a published agent image: pulled by the command, building and carrying it are advanced", async () => {
    const image = "docker.io/forgeyard/forgeyard-agent:1.2.0";
    const created = { id: "t4", created_at: "2026-11-09T10:00:00Z", expires_at: "2026-11-09T10:15:00Z", created_by: "owner",
      token: "fyj_secret", server_url: "https://forge.lan:7860", valid_minutes: 15, ca_fingerprint: null, trust_command: null,
      command: "unused", install_command: "unused", agent_image: { reference: image, published: true, local_tag: image },
      docker_build_command: "unused", docker_commands: { nvidia: "unused", amd: "unused", cpu: "unused" }, config_file: "",
      manual_commands: [], version: "1.2.0" };
    server((url, init) => {
      if (url === "/api/machines") return machines();
      if (url === "/api/tls") return { status: 200, body: { mode: "forgeyard-ca", https: true, admin: { trust_command: null, server_certificate: null } } };
      if (url === "/api/machines/join-tokens" && init?.method === "POST") return { status: 201, body: created };
      if (url === "/api/machines/join-tokens") return { status: 200, body: { tokens: [] } };
      return undefined;
    });
    mount();
    await screen.findByRole("article", { name: "Machine a100-box" });
    fireEvent.click(screen.getByRole("button", { name: "Add a machine" }));
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Create join command" })));
    fireEvent.click(await screen.findByRole("tab", { name: "Docker" }));
    const panel = screen.getByRole("tabpanel");
    const docker = screen.getByLabelText("Docker command").textContent ?? "";
    expect(docker.endsWith(`\n  ${image}`)).toBe(true);
    expect(panel.textContent).toContain(`Docker pulls ${image} (linux/amd64 and linux/arm64) the first time`);
    expect(panel.textContent).not.toContain("the image is not published yet");
    // The steps have no build; it is under Advanced / offline, with the save | load path.
    const steps = panel.querySelector("ol.steps")!;
    expect(within(steps as HTMLElement).queryByLabelText("Build command")).toBeNull();
    const offline = screen.getByText(/^Advanced \/ offline/).closest("details")!;
    expect(offline.open).toBe(false);
    expect(within(offline).getByLabelText("Save command").textContent).toBe(
      `sudo docker pull ${image} && \\\n  sudo docker save ${image} | gzip > forgeyard-agent.tar.gz`);
    expect(within(offline).getByLabelText("Load command").textContent).toBe("gunzip -c forgeyard-agent.tar.gz | sudo docker load");
    expect(within(offline).getByLabelText("Build command").textContent).toBe(`sudo docker build -f Dockerfile.agent -t ${image} .`);
    expect(offline.textContent).toContain("Docker finds the image locally and does not pull it.");
    // The engine images go the same way; the guide says how (FY-WEB-170 has the recipe's commands).
    expect(within(offline).getByRole("link", { name: "Offline machines" }).getAttribute("href")).toBe("/guide/en/how-to#offline-machines");
  });
});
