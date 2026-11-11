import { useState, type ChangeEvent } from "react";

import {
  AGENT_DEFAULT_PORT_RANGE,
  agentImage,
  buildCommand,
  carryCommands,
  certificateCovers,
  IMAGE_FILE,
  pinnedByDigest,
  loadForm,
  runCommand,
  saveForm,
  serverHost,
  serverScheme,
  validate,
  type AgentImage,
  type DockerErrors,
  type DockerForm,
  type DockerKind,
} from "../lib/dockerAgent";
import type { NewJoinToken, TlsInfo } from "../lib/types";
import { HelpLink } from "./HelpLink";
import { CopyBlock, CopyBox, Field, Notice } from "./ui";

// "Add a machine", once a join token exists: the three ways to put the agent on the machine, all
// pinning the same CA fingerprint.

type Way = "install" | "docker" | "manual";

const KINDS: { id: DockerKind; label: string }[] = [
  { id: "nvidia", label: "NVIDIA" },
  { id: "amd", label: "AMD" },
  { id: "cpu", label: "CPU only" },
];

export function AddMachineWays({ issued, tls }: { issued: NewJoinToken; tls?: TlsInfo | null }) {
  const canInstall = Boolean(issued.install_command);
  const [way, setWay] = useState<Way>(canInstall ? "install" : "manual");
  const [kind, setKind] = useState<DockerKind>("nvidia");
  // One form for the three GPU kinds, kept here so it survives switching tabs.
  const [docker, setDocker] = useState<DockerForm>(() => loadForm(issued.server_url));
  const changeDocker = (next: DockerForm) => {
    setDocker(next);
    saveForm(next, issued.server_url);
  };
  const version = issued.version ?? "latest";
  const image = agentImage(issued.agent_image, version);
  const dockerErrors = validate(docker);
  const run = runCommand(kind, docker, {
    server: issued.server_url, fingerprint: issued.ca_fingerprint ?? null, token: issued.token, image: image.reference,
  });
  const tabs: { id: Way; label: string; badge?: string }[] = [
    { id: "install", label: "One-line install", badge: canInstall ? "recommended" : undefined },
    { id: "docker", label: "Docker" },
    { id: "manual", label: "Manual (config file)" },
  ];
  return (
    <>
      <div className="tabs" role="tablist" aria-label="How to install the agent">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`way-${t.id}`}
            aria-selected={way === t.id}
            className={way === t.id ? "on" : ""}
            aria-controls={`way-panel-${t.id}`}
            onClick={() => setWay(t.id)}
          >
            {t.label}
            {t.badge && <span className="tab-badge">{t.badge}</span>}
          </button>
        ))}
      </div>

      {way === "install" && (
        <div role="tabpanel" id="way-panel-install" aria-labelledby="way-install">
          {canInstall ? (
            <ol className="steps">
              <li>
                On the new machine (Linux, x86_64 or aarch64, with internet access for a few Python packages), paste this into a
                terminal. It asks for your <code>sudo</code> password; as root, leave out <code>sudo</code>.
                <CopyBlock value={issued.install_command!} label="Install command" />
              </li>
              <li>
                It first downloads the server's certificate authority and checks it against the fingerprint
                {issued.ca_fingerprint ? " in the command" : ""}; only then does it fetch the installer, over a connection checked
                against that authority. Then it:
                <ul className="what-it-does">
                  <li>installs the agent {issued.version} from this server in <code>/opt/forgeyard</code>;</li>
                  <li>creates the account <code>forgeyard-run</code> that process workloads run as;</li>
                  <li>writes <code>/etc/forgeyard/agent.toml</code> from what it finds (GPUs, Docker, systemd);</li>
                  <li>joins with the token, and starts the <code>forgeyard-agent</code> service (systemd).</li>
                </ul>
              </li>
              <li>Running the same line again later upgrades the agent and keeps the machine. Add <code>--uninstall</code> to remove it.</li>
            </ol>
          ) : (
            <Notice>
              The install script is served only over HTTPS, and this page was not reached over HTTPS. Use the manual way, or open
              Forgeyard at its <code>https://</code> address.
            </Notice>
          )}
          <HelpLink page="quick-start" anchor="3-add-a-machine">What the installer does, step by step</HelpLink>
        </div>
      )}

      {way === "docker" && (
        <div role="tabpanel" id="way-panel-docker" aria-labelledby="way-docker">
          <div className="choice-row" role="group" aria-label="The machine's GPUs">
            {KINDS.map((k) => (
              <button key={k.id} type="button" aria-pressed={kind === k.id} onClick={() => setKind(k.id)}>
                {k.label}
              </button>
            ))}
          </div>
          <DockerFields form={docker} errors={dockerErrors} serverUrl={issued.server_url} tls={tls} onChange={changeDocker} />
          <p className="muted small">
            The commands start with <code>sudo</code>; as root, or if your account is in the <code>docker</code> group, leave it out.
          </p>
          <ol className="steps">
            {!image.published && (
              <li>
                Build the agent image once on that machine, from a Forgeyard checkout of version {version} (the image is not
                published yet):
                <CopyBox value={buildCommand(image.local_tag)} label="Build command" />
              </li>
            )}
            <li>
              {image.published ? (
                <>
                  On the new machine, start the agent. Docker pulls <code>{image.reference}</code> (linux/amd64 and linux/arm64) the
                  first time; the command follows the fields above.
                </>
              ) : (
                <>Start it. The command follows the fields above.</>
              )}
              {run !== null ? (
                <CopyBlock value={run} label="Docker command" />
              ) : (
                <Notice tone="error">Fix the fields marked above to get the command.</Notice>
              )}
            </li>
            <li>
              The machine's credential is kept in the <code>forgeyard-agent</code> volume, so the same command after a restart or an
              upgrade keeps the machine; the join token is used only once.
            </li>
          </ol>
          {image.published && <OfflineImage image={image} version={version} />}
          <p className="muted small">
            <strong>Why host network:</strong> model replicas listen on the host's 127.0.0.1 behind the agent's key guard, which must
            reach them. <strong>Why the same path:</strong> the agent asks the host's Docker to mount models by their path on the
            host.{kind === "nvidia" ? " Needs the NVIDIA Container Toolkit." : kind === "amd"
              ? " Uses the host's ROCm tools, mounted read-only. The video and render groups are given by their number on the host: the image has no render group."
              : ""}
          </p>
          <HelpLink page="how-to" anchor="run-the-agent-in-docker">Run the agent in Docker</HelpLink>
        </div>
      )}

      {way === "manual" && (
        <div role="tabpanel" id="way-panel-manual" aria-labelledby="way-manual">
          <ol className="steps">
            <li>
              On the machine, get Forgeyard {issued.version} and run <code>uv sync</code>, with <code>--extra nvidia</code> on NVIDIA
              machines.
            </li>
            <li>
              Save this as <code>/etc/forgeyard/agent.toml</code>, or let <code>uv run --no-sync python -m forgeyard agent init</code>{" "}
              write a complete one with every setting explained:
              <CopyBlock value={issued.config_file ?? ""} label="Config file" />
            </li>
            <li>
              In the Forgeyard folder, join (once), then start the agent and keep it running:
              {(issued.manual_commands ?? []).map((c, i) => (
                <CopyBox key={c} value={c} label={i === 0 ? "Join command" : "Start command"} />
              ))}
            </li>
          </ol>
          <details className="trust-upgrade">
            <summary className="small">Without a config file</summary>
            <p className="small">The same in one command, with every setting as a flag:</p>
            <CopyBox value={issued.command} label="Join command with flags" />
          </details>
          <HelpLink page="reference" anchor="agent-settings">Every agent setting</HelpLink>
        </div>
      )}
    </>
  );
}

// The published image on a machine that cannot pull it: carried over as a file, or built there.
function OfflineImage({ image, version }: { image: AgentImage; version: string }) {
  const carry = carryCommands(image);
  return (
    <details className="docker-advanced docker-offline">
      <summary className="small">Advanced / offline: no access to the registry, or build the image yourself</summary>
      <p className="small">On a machine that can reach the registry, pull the image and save it to a file:</p>
      <CopyBlock value={carry.save} label="Save command" />
      <p className="small">
        Copy <code>{IMAGE_FILE}</code> to the new machine (<code>scp</code>, a USB disk), and load it there:
      </p>
      <CopyBox value={carry.load} label="Load command" />
      <p className="small muted">
        The two machines differ (x86_64 and arm64)? Add <code>--platform linux/arm64</code> or <code>--platform linux/amd64</code>{" "}
        to <code>docker pull</code>.
      </p>
      <p className="small">Or build it on the new machine, from a Forgeyard checkout of version {version}:</p>
      <CopyBox value={buildCommand(image.local_tag)} label="Build command" />
      <p className="small muted">
        {pinnedByDigest(image) ? (
          <>
            Then use <code>{image.local_tag}</code> instead of <code>{image.reference}</code> as the last line of the command above: a
            loaded or built image has the tag, not the digest.
          </>
        ) : (
          <>Then run the command above: Docker finds the image locally and does not pull it.</>
        )}
      </p>
    </details>
  );
}

// The Docker tab's form: every value goes straight into the command below it.
function DockerFields({ form, errors, serverUrl, tls, onChange }: {
  form: DockerForm; errors: DockerErrors; serverUrl: string; tls?: TlsInfo | null; onChange: (form: DockerForm) => void;
}) {
  const set = (key: keyof DockerForm) => (e: ChangeEvent<HTMLInputElement>) => onChange({ ...form, [key]: e.target.value });
  const cert = tls?.admin?.server_certificate;
  const host = serverHost(form.server);
  const uncovered = Boolean(cert && host && !errors.server && serverScheme(form.server) === "https"
    && !certificateCovers(host, cert.names, cert.ips));
  const certNames = cert ? [...cert.names, ...cert.ips].join(", ") : "";
  // Open at first when a port range is set, so it is not hidden in the command's inputs.
  const [advancedOpen] = useState(() => Boolean(form.portRange.trim()));
  return (
    <fieldset className="docker-form">
      <legend className="sr-only">Settings for the command</legend>
      <div className="grid-2">
        <Field label="Model directory on that machine" name="docker-model-dir" value={form.modelDir} onChange={set("modelDir")}
          spellCheck={false} autoComplete="off" error={errors.modelDir}
          hint={<>A disk with room for models. Mounted at the same path in the container and used as <code>FORGEYARD_MODEL_CACHE</code>.</>} />
        <Field label="Machine name" name="docker-name" value={form.name} onChange={set("name")} placeholder="its host name"
          spellCheck={false} autoComplete="off" error={errors.name}
          hint={<>Optional: how it is listed here. Empty: the machine's host name.</>} />
      </div>
      <Field label="Server address, as that machine reaches it" name="docker-server" value={form.server} onChange={set("server")}
        spellCheck={false} autoComplete="off" inputMode="url" error={errors.server}
        hint={<>This server's address by default. Change it when the machine comes through an SSH tunnel, a VPN or another LAN address.</>} />
      {form.server.trim().replace(/\/+$/, "") !== serverUrl && (
        <button type="button" className="btn btn-quiet btn-small docker-reset" onClick={() => onChange({ ...form, server: serverUrl })}>
          Use this server's address, {serverUrl}
        </button>
      )}
      {uncovered && (
        <Notice>
          <strong>{host}</strong> is not in this server's certificate ({certNames}), so the agent will refuse the connection.{" "}
          {tls?.mode === "forgeyard-ca" ? (
            <>Add it to <code>TLS_EXTRA_NAMES</code> and restart the server, or use one of those names.</>
          ) : (
            <>Use one of those names, or a certificate that includes it.</>
          )}
        </Notice>
      )}
      <details className="docker-advanced" open={advancedOpen || undefined}>
        <summary className="small">Advanced</summary>
        <Field label="Port range" name="docker-port-range" value={form.portRange} onChange={set("portRange")}
          placeholder={AGENT_DEFAULT_PORT_RANGE} spellCheck={false} autoComplete="off" inputMode="numeric" error={errors.portRange}
          hint={<>Host ports for services and model replicas (<code>FORGEYARD_PORT_RANGE</code>, default {AGENT_DEFAULT_PORT_RANGE}). Set it when only some ports reach that machine, e.g. through an SSH tunnel.</>} />
      </details>
    </fieldset>
  );
}
