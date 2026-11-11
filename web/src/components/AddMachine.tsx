import { useState } from "react";

import type { NewJoinToken } from "../lib/types";
import { HelpLink } from "./HelpLink";
import { CopyBlock, CopyBox, Notice } from "./ui";

// "Add a machine", once a join token exists: the three ways to put the agent on the machine, all
// pinning the same CA fingerprint.

type Way = "install" | "docker" | "manual";
type Kind = "nvidia" | "amd" | "cpu";

const KINDS: { id: Kind; label: string }[] = [
  { id: "nvidia", label: "NVIDIA" },
  { id: "amd", label: "AMD" },
  { id: "cpu", label: "CPU only" },
];

export function AddMachineWays({ issued }: { issued: NewJoinToken }) {
  const canInstall = Boolean(issued.install_command);
  const [way, setWay] = useState<Way>(canInstall ? "install" : "manual");
  const [kind, setKind] = useState<Kind>("nvidia");
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
          <ol className="steps">
            <li>
              Build the agent image once on that machine, from a Forgeyard checkout of version {issued.version} (the image is not
              published yet):
              <CopyBox value={issued.docker_build_command ?? ""} label="Build command" />
            </li>
            <li>
              Start it. Change <code>/srv/forgeyard-models</code> to a disk with room for models, the same path on both sides of the
              colon.
              <CopyBlock value={issued.docker_commands?.[kind] ?? ""} label="Docker command" />
            </li>
            <li>
              The machine's credential is kept in the <code>forgeyard-agent</code> volume, so the same command after a restart or an
              upgrade keeps the machine; the join token is used only once.
            </li>
          </ol>
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
