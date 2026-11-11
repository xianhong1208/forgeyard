// "Add a machine" → Docker: the `docker run` command for the agent image, from a small form (model
// directory, machine name, server address, port range), and the ways to get the image onto a machine
// without registry access (`docker save | docker load`, or `docker build` from a checkout). Pure, so
// every field and every guard is tested on its own; the page only shows what this returns.

export type DockerKind = "nvidia" | "amd" | "cpu";

// What the person types. Empty name / port range = the agent's own default (host name, 30000-32767).
export type DockerForm = { modelDir: string; name: string; server: string; portRange: string };
export type DockerErrors = Partial<Record<keyof DockerForm, string>>;

// The agent image the server names (forgeyard/install/images.py): the published one, pulled by
// `docker run` (reference: repository:version, or repository@sha256:… when an admin pinned a digest),
// or, while nothing is published, a local build. local_tag: the name without a digest, for a local
// build or `docker load`.
export type AgentImage = { reference: string; published: boolean; local_tag: string };

// What the join token gives: the server's address, the pinned CA, the token and the agent image.
export type DockerJoin = { server: string; fingerprint: string | null; token: string; image: string };

export const DEFAULT_MODEL_DIR = "/srv/forgeyard-models";
export const IMAGE = "forgeyard-agent";
export const AGENT_DEFAULT_PORT_RANGE = "30000-32767";

export function emptyForm(server: string): DockerForm {
  return { modelDir: DEFAULT_MODEL_DIR, name: "", server, portRange: "" };
}

// Every value goes into a shell command unquoted, and the model directory also into `-v A:A`: only
// characters that mean nothing to either are accepted, so a typo can never produce a broken (or
// different) command.
const PATH_CHARS = /^[A-Za-z0-9._+/-]+$/;
// The machine name as the server keeps it (forgeyard/machines/store.py clean_name).
const NAME_RE = /^[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_])?$/;
// http(s)://host[:port][/path]: a DNS name, an IPv4 address or a bracketed IPv6 address.
const SERVER_RE = /^(https?):\/\/([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::(\d{1,5}))?(\/[A-Za-z0-9._~/-]*)?$/;
const RANGE_RE = /^(\d{1,5})-(\d{1,5})$/;

export function modelDirError(v: string): string | null {
  const dir = v.trim();
  if (!dir) return "Enter the directory on that machine where models are kept, e.g. /srv/forgeyard-models.";
  if (/[:]/.test(dir)) return "No colon: Docker's -v splits the mount at it.";
  if (/\s/.test(dir)) return "No spaces: the command is pasted into a shell.";
  if (/["'`]/.test(dir)) return "No quotes: the command is pasted into a shell.";
  if (!dir.startsWith("/")) return "An absolute path, starting with /.";
  if (!PATH_CHARS.test(dir)) return "Only letters, digits and . _ - + / in the path.";
  if (dir.split("/").includes("..")) return "No .. in the path.";
  if (cleanDir(dir) === "/") return "A directory for models, not / itself.";
  return null;
}

export function nameError(v: string): string | null {
  const name = v.trim();
  if (!name) return null; // optional: the machine's host name
  if (name.length > 64) return "At most 64 characters.";
  if (!NAME_RE.test(name)) return "Letters, digits, dots, dashes and underscores; not starting or ending with a dot or dash.";
  return null;
}

export function serverError(v: string): string | null {
  const url = cleanServer(v);
  if (!url) return "Enter the address the machine uses to reach this server.";
  const m = SERVER_RE.exec(url);
  if (!m) return "An address like https://192.168.1.20:7860: http:// or https://, a host name or IP, an optional port.";
  if (m[3] !== undefined && (Number(m[3]) < 1 || Number(m[3]) > 65535)) return "The port must be 1-65535.";
  return null;
}

export function portRangeError(v: string): string | null {
  const range = v.trim();
  if (!range) return null; // optional: the agent's default
  const m = RANGE_RE.exec(range);
  if (!m) return "LOW-HIGH, e.g. 33000-33002.";
  const [lo, hi] = [Number(m[1]), Number(m[2])];
  if (lo < 1024 || hi > 65535 || lo > hi) return "Within 1024-65535, low before high.";
  return null;
}

export function validate(form: DockerForm): DockerErrors {
  const errors: DockerErrors = {};
  const checks: [keyof DockerForm, string | null][] = [
    ["modelDir", modelDirError(form.modelDir)],
    ["name", nameError(form.name)],
    ["server", serverError(form.server)],
    ["portRange", portRangeError(form.portRange)],
  ];
  for (const [key, error] of checks) if (error) errors[key] = error;
  return errors;
}

function cleanDir(v: string): string {
  return v.trim().replace(/\/+$/, "") || "/";
}

function cleanServer(v: string): string {
  return v.trim().replace(/\/+$/, "");
}

// The host of a valid server address, without IPv6 brackets ("" for an invalid one).
export function serverHost(v: string): string {
  const m = SERVER_RE.exec(cleanServer(v));
  return m ? m[2].replace(/^\[|\]$/g, "").toLowerCase() : "";
}

export function serverScheme(v: string): string {
  return SERVER_RE.exec(cleanServer(v))?.[1] ?? "";
}

// Whether a certificate with these DNS names and IP addresses is valid for `host` (a wildcard covers
// exactly one label, as in TLS).
export function certificateCovers(host: string, names: string[], ips: string[]): boolean {
  const h = host.toLowerCase();
  if (ips.some((ip) => ip.toLowerCase() === h)) return true;
  return names.some((raw) => {
    const n = raw.toLowerCase();
    if (n === h) return true;
    if (!n.startsWith("*.")) return false;
    const dot = h.indexOf(".");
    return dot > 0 && h.slice(dot + 1) === n.slice(2);
  });
}

export function imageTag(version: string): string {
  return `${IMAGE}:${version}`;
}

// The server's answer, or (from a server that does not say) a local build of its version.
export function agentImage(image: AgentImage | null | undefined, version: string): AgentImage {
  return image ?? { reference: imageTag(version), published: false, local_tag: imageTag(version) };
}

export function pinnedByDigest(image: AgentImage): boolean {
  return image.reference.includes("@sha256:");
}

// `sudo` on every command: the page says once to leave it out as root or for a member of the docker group.
export function buildCommand(localTag: string): string {
  return `sudo docker build -f Dockerfile.agent -t ${localTag} .`;
}

export const IMAGE_FILE = "forgeyard-agent.tar.gz";

// For a machine that cannot reach the registry: pull and save on one that can, load on this one. A
// pinned digest is pulled by digest and saved under local_tag: a loaded image keeps its tag, not
// the digest it was pulled by.
export function carryCommands(image: AgentImage): { save: string; load: string } {
  const lines = [`sudo docker pull ${image.reference}`];
  if (image.reference !== image.local_tag) lines.push(`sudo docker tag ${image.reference} ${image.local_tag}`);
  lines.push(`sudo docker save ${image.local_tag} | gzip > ${IMAGE_FILE}`);
  return { save: lines.join(" && \\\n  "), load: `gunzip -c ${IMAGE_FILE} | sudo docker load` };
}

const AMD_GROUPS = [
  '--group-add "$(getent group video | cut -d: -f3)"',
  '--group-add "$(getent group render | cut -d: -f3)"',
];

// The `docker run` line, or null while a field is invalid (the page shows the errors instead).
export function runCommand(kind: DockerKind, form: DockerForm, join: DockerJoin): string | null {
  if (Object.keys(validate(form)).length > 0) return null;
  const dir = cleanDir(form.modelDir);
  const name = form.name.trim();
  const range = form.portRange.trim();
  const lines = ["sudo docker run -d --name forgeyard-agent --restart always --network host"];
  if (kind === "nvidia") {
    lines.push("--gpus all");
  } else if (kind === "amd") {
    // Numeric ids: Docker looks group names up in the image's /etc/group, which has no "render".
    lines.push("--device /dev/kfd --device /dev/dri", ...AMD_GROUPS, "-v /opt/rocm:/opt/rocm:ro");
  }
  lines.push(
    "-v /var/run/docker.sock:/var/run/docker.sock",
    "-v forgeyard-agent:/var/lib/forgeyard-agent",
    `-v ${dir}:${dir}`,
    `-e FORGEYARD_SERVER=${cleanServer(form.server)}`,
  );
  if (join.fingerprint) lines.push(`-e FORGEYARD_CA_FINGERPRINT=${join.fingerprint}`);
  lines.push(`-e FORGEYARD_JOIN_TOKEN=${join.token}`, `-e FORGEYARD_MODEL_CACHE=${dir}`);
  if (range) lines.push(`-e FORGEYARD_PORT_RANGE=${range}`);
  if (name) lines.push(`-e FORGEYARD_NAME=${name}`);
  lines.push(join.image);
  return lines.join(" \\\n  ");
}

// ------------------------------------------------------------------ remembered per browser
const KEY = "forgeyard.add-machine.docker";

// The last values typed here; the server address only when it was changed from this server's.
export function loadForm(server: string): DockerForm {
  const form = emptyForm(server);
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<DockerForm> | null;
    if (saved && typeof saved === "object") {
      for (const key of ["modelDir", "name", "server", "portRange"] as const) {
        if (typeof saved[key] === "string" && (key !== "server" || saved[key])) form[key] = saved[key];
      }
    }
  } catch {
    // storage blocked or unreadable: the defaults
  }
  return form;
}

export function saveForm(form: DockerForm, server: string): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...form, server: cleanServer(form.server) === server ? "" : form.server }));
  } catch {
    // not remembered; the form still works
  }
}
