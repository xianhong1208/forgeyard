import { afterEach, describe, expect, it, vi } from "vitest";

import {
  agentImage,
  buildCommand,
  carryCommands,
  pinnedByDigest,
  certificateCovers,
  emptyForm,
  loadForm,
  modelDirError,
  nameError,
  portRangeError,
  runCommand,
  saveForm,
  serverError,
  serverHost,
  validate,
  type DockerForm,
  type DockerJoin,
} from "./dockerAgent";

const FP = "sha256:" + "3f".repeat(32);
const JOIN: DockerJoin = { server: "https://192.168.1.20:7860", fingerprint: FP, token: "fyj_example", image: "forgeyard-agent:1.1.1" };
const form = (over: Partial<DockerForm> = {}): DockerForm => ({ ...emptyForm(JOIN.server), ...over });

// What was run by hand on a real AMD machine (through an SSH tunnel to the server), with the
// example token, fingerprint and version.
const AMD_BY_HAND = `sudo docker run -d --name forgeyard-agent --restart always --network host \\
  --device /dev/kfd --device /dev/dri \\
  --group-add "$(getent group video | cut -d: -f3)" \\
  --group-add "$(getent group render | cut -d: -f3)" \\
  -v /opt/rocm:/opt/rocm:ro \\
  -v /var/run/docker.sock:/var/run/docker.sock \\
  -v forgeyard-agent:/var/lib/forgeyard-agent \\
  -v /srv/forgeyard-models:/srv/forgeyard-models \\
  -e FORGEYARD_SERVER=https://127.0.0.1:7860 \\
  -e FORGEYARD_CA_FINGERPRINT=${FP} \\
  -e FORGEYARD_JOIN_TOKEN=fyj_example \\
  -e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models \\
  -e FORGEYARD_PORT_RANGE=33000-33002 \\
  -e FORGEYARD_NAME=amd-01 \\
  forgeyard-agent:1.1.1`;

describe("docker run command for the agent", () => {
  it("FY-WEB-123 the AMD command equals the one run by hand", () => {
    const cmd = runCommand("amd", form({ server: "https://127.0.0.1:7860", portRange: "33000-33002", name: "amd-01" }), JOIN);
    expect(cmd).toBe(AMD_BY_HAND);
  });

  it("FY-WEB-124 defaults: this server, /srv/forgeyard-models, no name or port range, the server's version", () => {
    const nvidia = runCommand("nvidia", form(), JOIN)!;
    expect(nvidia.split(" \\\n  ")).toEqual([
      "sudo docker run -d --name forgeyard-agent --restart always --network host",
      "--gpus all",
      "-v /var/run/docker.sock:/var/run/docker.sock",
      "-v forgeyard-agent:/var/lib/forgeyard-agent",
      "-v /srv/forgeyard-models:/srv/forgeyard-models",
      "-e FORGEYARD_SERVER=https://192.168.1.20:7860",
      `-e FORGEYARD_CA_FINGERPRINT=${FP}`,
      "-e FORGEYARD_JOIN_TOKEN=fyj_example",
      "-e FORGEYARD_MODEL_CACHE=/srv/forgeyard-models",
      "forgeyard-agent:1.1.1",
    ]);
    expect(nvidia).not.toMatch(/FORGEYARD_NAME|FORGEYARD_PORT_RANGE|kfd|rocm/);
    expect(buildCommand("forgeyard-agent:1.1.1")).toBe("sudo docker build -f Dockerfile.agent -t forgeyard-agent:1.1.1 .");
  });

  it("FY-WEB-125 CPU only has no GPU options; no fingerprint means no FORGEYARD_CA_FINGERPRINT", () => {
    const cpu = runCommand("cpu", form(), { ...JOIN, fingerprint: null })!;
    expect(cpu).not.toMatch(/--gpus|--device|--group-add|\/opt\/rocm|FORGEYARD_CA_FINGERPRINT/);
    expect(cpu.split(" \\\n  ")[1]).toBe("-v /var/run/docker.sock:/var/run/docker.sock");
    expect(runCommand("nvidia", form(), { ...JOIN, fingerprint: null })).not.toMatch(/--device|--group-add/);
  });

  it("FY-WEB-126 the model directory fills both sides of -v and the model cache; spaces and a trailing / are cleaned", () => {
    const cmd = runCommand("cpu", form({ modelDir: "  /data/models/ ", name: " gpu-01 ", portRange: " 33000-33002 ",
      server: " https://forge.example.com:7860/ " }), JOIN)!;
    expect(cmd).toContain("-v /data/models:/data/models \\");
    expect(cmd).toContain("-e FORGEYARD_MODEL_CACHE=/data/models \\");
    expect(cmd).toContain("-e FORGEYARD_NAME=gpu-01 \\");
    expect(cmd).toContain("-e FORGEYARD_PORT_RANGE=33000-33002 \\");
    expect(cmd).toContain("-e FORGEYARD_SERVER=https://forge.example.com:7860 \\");
    expect(cmd).not.toContain("srv/forgeyard-models");
  });

  it("FY-WEB-127 an invalid field gives no command at all", () => {
    for (const bad of [{ modelDir: "srv/models" }, { name: "-bad" }, { server: "192.168.1.20:7860" }, { portRange: "33000" }]) {
      expect(runCommand("amd", form(bad), JOIN)).toBeNull();
      expect(Object.keys(validate(form(bad)))).toEqual(Object.keys(bad));
    }
    expect(validate(form())).toEqual({});
  });
});

describe("the agent image: published or built locally", () => {
  const DIGEST = "sha256:" + "ab".repeat(32);
  const PUBLISHED = { reference: "docker.io/forgeyard/forgeyard-agent:1.2.0", published: true,
    local_tag: "docker.io/forgeyard/forgeyard-agent:1.2.0" };

  it("FY-WEB-141 the published image is the last line of the command, the rest as before; nothing to build", () => {
    const cmd = runCommand("amd", form({ server: "https://127.0.0.1:7860", portRange: "33000-33002", name: "amd-01" }),
      { ...JOIN, image: PUBLISHED.reference })!;
    // The command run by hand on the AMD machine, with only the image changed.
    expect(cmd).toBe(AMD_BY_HAND.replace(/forgeyard-agent:1\.1\.1$/, "docker.io/forgeyard/forgeyard-agent:1.2.0"));
    expect(cmd.split(" \\\n  ").at(-1)).toBe("docker.io/forgeyard/forgeyard-agent:1.2.0");
    expect(cmd.startsWith("sudo docker run -d --name forgeyard-agent --restart always --network host \\\n")).toBe(true);
  });

  it("FY-WEB-142 a server that does not name an image means a local build of its version", () => {
    expect(agentImage(undefined, "1.2.0")).toEqual({ reference: "forgeyard-agent:1.2.0", published: false,
      local_tag: "forgeyard-agent:1.2.0" });
    expect(agentImage(null, "1.2.0").published).toBe(false);
    expect(agentImage(PUBLISHED, "9.9.9")).toBe(PUBLISHED);
    expect(pinnedByDigest(PUBLISHED)).toBe(false);
    expect(pinnedByDigest({ ...PUBLISHED, reference: `docker.io/forgeyard/forgeyard-agent@${DIGEST}` })).toBe(true);
  });

  it("FY-WEB-143 offline: pull and save where the registry is reachable, load on the machine", () => {
    expect(carryCommands(PUBLISHED)).toEqual({
      save: "sudo docker pull docker.io/forgeyard/forgeyard-agent:1.2.0 && \\\n"
        + "  sudo docker save docker.io/forgeyard/forgeyard-agent:1.2.0 | gzip > forgeyard-agent.tar.gz",
      load: "gunzip -c forgeyard-agent.tar.gz | sudo docker load",
    });
    expect(buildCommand(PUBLISHED.local_tag)).toBe(
      "sudo docker build -f Dockerfile.agent -t docker.io/forgeyard/forgeyard-agent:1.2.0 .");
  });

  it("FY-WEB-144 a pinned digest is pulled by digest and saved under its tag (a loaded image has no digest)", () => {
    const pinned = { reference: `docker.io/forgeyard/forgeyard-agent@${DIGEST}`, published: true,
      local_tag: "docker.io/forgeyard/forgeyard-agent:1.2.0" };
    expect(carryCommands(pinned).save.split(" && \\\n  ")).toEqual([
      `sudo docker pull docker.io/forgeyard/forgeyard-agent@${DIGEST}`,
      `sudo docker tag docker.io/forgeyard/forgeyard-agent@${DIGEST} docker.io/forgeyard/forgeyard-agent:1.2.0`,
      "sudo docker save docker.io/forgeyard/forgeyard-agent:1.2.0 | gzip > forgeyard-agent.tar.gz",
    ]);
    expect(runCommand("cpu", form(), { ...JOIN, image: pinned.reference })!.endsWith(`forgeyard-agent@${DIGEST}`)).toBe(true);
  });
});

describe("docker form validation", () => {
  it.each([
    ["/srv/forgeyard-models", null],
    ["/mnt/disk_2/models+cache", null],
    ["", /Enter the directory/],
    ["   ", /Enter the directory/],
    ["srv/models", /absolute path/],
    ["~/models", /absolute path/],
    ["/srv/my models", /No spaces/],
    ["/srv/models\t", null], // trailing whitespace is trimmed
    ["/srv/a:/b", /No colon/],
    ["/srv/'models'", /No quotes/],
    ['/srv/"models"', /No quotes/],
    ["/srv/`id`", /No quotes/],
    ["/srv/$HOME", /Only letters/],
    ["/srv/a;rm", /Only letters/],
    ["/srv/../etc", /No \.\./],
    ["/", /not \/ itself/],
    ["///", /not \/ itself/],
  ])("FY-WEB-128 model directory %j", (v, want) => {
    const got = modelDirError(v);
    if (want === null) expect(got).toBeNull();
    else expect(got).toMatch(want);
  });

  it.each([
    ["", null],
    ["amd-01", null],
    ["gpu_01.lab", null],
    ["a", null],
    ["_x_", null],
    ["x".repeat(64), null],
    ["x".repeat(65), /At most 64/],
    ["-amd", /not starting or ending/],
    ["amd.", /not starting or ending/],
    ["amd 01", /Letters, digits/],
    ["amd$1", /Letters, digits/],
    ["機器", /Letters, digits/],
  ])("FY-WEB-129 machine name %j", (v, want) => {
    const got = nameError(v);
    if (want === null) expect(got).toBeNull();
    else expect(got).toMatch(want);
  });

  it.each([
    ["https://192.168.1.20:7860", null],
    ["https://127.0.0.1:7860/", null],
    ["http://forge.example.com", null],
    ["https://[fd00::20]:7860", null],
    ["https://forge.example.com/forgeyard", null],
    ["", /Enter the address/],
    ["192.168.1.20:7860", /An address like/],
    ["ftp://forge.example.com", /An address like/],
    ["https://forge example.com", /An address like/],
    ["https://forge.example.com:7860?x=1", /An address like/],
    ["https://$(id).example.com", /An address like/],
    ["https://forge.example.com:99999", /1-65535/],
    ["https://forge.example.com:0", /1-65535/],
  ])("FY-WEB-130 server address %j", (v, want) => {
    const got = serverError(v);
    if (want === null) expect(got).toBeNull();
    else expect(got).toMatch(want);
  });

  it.each([
    ["", null],
    ["33000-33002", null],
    ["1024-65535", null],
    ["33000", /LOW-HIGH/],
    ["33000 - 33002", /LOW-HIGH/],
    ["a-b", /LOW-HIGH/],
    ["80-90", /1024-65535/],
    ["33002-33000", /low before high/],
    ["60000-70000", /1024-65535/],
  ])("FY-WEB-131 port range %j", (v, want) => {
    const got = portRangeError(v);
    if (want === null) expect(got).toBeNull();
    else expect(got).toMatch(want);
  });

  it("FY-WEB-132 the certificate check: names, IPs, one-label wildcards, IPv6 without brackets", () => {
    const names = ["localhost", "forge.example.com", "*.lab.example.com"];
    const ips = ["127.0.0.1", "::1", "192.168.1.20"];
    expect(serverHost("https://[::1]:7860")).toBe("::1");
    expect(serverHost("https://Forge.Example.com:7860/")).toBe("forge.example.com");
    expect(serverHost("not a url")).toBe("");
    for (const host of ["127.0.0.1", "::1", "192.168.1.20", "localhost", "forge.example.com", "gpu.lab.example.com"]) {
      expect(certificateCovers(host, names, ips)).toBe(true);
    }
    for (const host of ["192.168.1.21", "other.example.com", "a.b.lab.example.com", "lab.example.com"]) {
      expect(certificateCovers(host, names, ips)).toBe(false);
    }
  });
});

describe("docker form remembered per browser", () => {
  afterEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("FY-WEB-133 values come back; the server address only when it was changed", () => {
    saveForm(form({ modelDir: "/data/models", name: "amd-01", portRange: "33000-33002" }), JOIN.server);
    expect(loadForm(JOIN.server)).toEqual(form({ modelDir: "/data/models", name: "amd-01", portRange: "33000-33002" }));
    // Saved with this server's own address: a later server address wins.
    expect(loadForm("https://192.168.1.21:7860").server).toBe("https://192.168.1.21:7860");
    saveForm(form({ server: "https://127.0.0.1:7860" }), JOIN.server);
    expect(loadForm("https://192.168.1.21:7860").server).toBe("https://127.0.0.1:7860");
  });

  it("FY-WEB-134 blocked or broken storage gives the defaults and never throws", () => {
    localStorage.setItem("forgeyard.add-machine.docker", "{not json");
    expect(loadForm(JOIN.server)).toEqual(form());
    localStorage.setItem("forgeyard.add-machine.docker", JSON.stringify({ modelDir: 5, name: null }));
    expect(loadForm(JOIN.server)).toEqual(form());
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(loadForm(JOIN.server)).toEqual(form());
    expect(() => saveForm(form(), JOIN.server)).not.toThrow();
  });
});
