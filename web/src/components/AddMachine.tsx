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
import { useT } from "../i18n/react";
import type { NewJoinToken, TlsInfo } from "../lib/types";
import { HelpLink } from "./HelpLink";
import { CopyBlock, CopyBox, Field, Notice } from "./ui";

// "Add a machine", once a join token exists: the three ways to put the agent on the machine, all
// pinning the same CA fingerprint.

type Way = "install" | "docker" | "manual";

const KINDS: { id: DockerKind; label: string }[] = [
  { id: "nvidia", label: "NVIDIA" },
  { id: "amd", label: "AMD" },
  { id: "cpu", label: "" },   // "CPU only", in the reader's language
];

export function AddMachineWays({ issued, tls }: { issued: NewJoinToken; tls?: TlsInfo | null }) {
  const t = useT();
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
    { id: "install", label: t("machines.way.install"), badge: canInstall ? t("machines.way.recommended") : undefined },
    { id: "docker", label: t("machines.way.docker") },
    { id: "manual", label: t("machines.way.manual") },
  ];
  return (
    <>
      <div className="tabs" role="tablist" aria-label={t("machines.way.tabs")}>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`way-${tab.id}`}
            aria-selected={way === tab.id}
            className={way === tab.id ? "on" : ""}
            aria-controls={`way-panel-${tab.id}`}
            onClick={() => setWay(tab.id)}
          >
            {tab.label}
            {tab.badge && <span className="tab-badge">{tab.badge}</span>}
          </button>
        ))}
      </div>

      {way === "install" && (
        <div role="tabpanel" id="way-panel-install" aria-labelledby="way-install">
          {canInstall ? (
            <ol className="steps">
              <li>
                {t.rich("machines.install.paste", { sudo: <code>sudo</code> })}
                <CopyBlock value={issued.install_command!} label={t("machines.install.command")} />
              </li>
              <li>
                {t("machines.install.checks", { inCommand: issued.ca_fingerprint ? t("machines.install.inCommand") : "" })}
                <ul className="what-it-does">
                  <li>{t.rich("machines.install.does1", { version: issued.version ?? "", dir: <code>/opt/forgeyard</code> })}</li>
                  <li>{t.rich("machines.install.does2", { account: <code>forgeyard-run</code> })}</li>
                  <li>{t.rich("machines.install.does3", { file: <code>/etc/forgeyard/agent.toml</code> })}</li>
                  <li>{t.rich("machines.install.does4", { service: <code>forgeyard-agent</code> })}</li>
                </ul>
              </li>
              <li>{t.rich("machines.install.again", { flag: <code>--uninstall</code> })}</li>
            </ol>
          ) : (
            <Notice>{t.rich("machines.install.httpsOnly", { https: <code>https://</code> })}</Notice>
          )}
          <HelpLink page="quick-start" anchor="3-add-a-machine">{t("machines.install.help")}</HelpLink>
        </div>
      )}

      {way === "docker" && (
        <div role="tabpanel" id="way-panel-docker" aria-labelledby="way-docker">
          <div className="choice-row" role="group" aria-label={t("machines.docker.gpus")}>
            {KINDS.map((k) => (
              <button key={k.id} type="button" aria-pressed={kind === k.id} onClick={() => setKind(k.id)}>
                {k.label || t("machines.docker.cpuOnly")}
              </button>
            ))}
          </div>
          <DockerFields form={docker} errors={dockerErrors} serverUrl={issued.server_url} tls={tls} onChange={changeDocker} />
          <p className="muted small">{t.rich("machines.docker.sudo", { sudo: <code>sudo</code>, group: <code>docker</code> })}</p>
          <ol className="steps">
            {!image.published && (
              <li>
                {t("machines.docker.build", { version })}
                <CopyBox value={buildCommand(image.local_tag)} label={t("machines.docker.buildCommand")} />
              </li>
            )}
            <li>
              {image.published ? (
                t.rich("machines.docker.startPublished", { image: <code>{image.reference}</code> })
              ) : (
                t("machines.docker.start")
              )}
              {run !== null ? (
                <CopyBlock value={run} label={t("machines.docker.command")} />
              ) : (
                <Notice tone="error">{t("machines.docker.fix")}</Notice>
              )}
            </li>
            <li>{t.rich("machines.docker.volume", { volume: <code>forgeyard-agent</code> })}</li>
          </ol>
          {image.published && <OfflineImage image={image} version={version} />}
          <p className="muted small">
            {t.rich("machines.docker.why", { strong: (text) => <strong>{text}</strong> })}
            {kind === "nvidia" ? t("machines.docker.whyNvidia") : kind === "amd" ? t("machines.docker.whyAmd") : ""}
          </p>
          <HelpLink page="how-to" anchor="run-the-agent-in-docker">{t("machines.docker.help")}</HelpLink>
        </div>
      )}

      {way === "manual" && (
        <div role="tabpanel" id="way-panel-manual" aria-labelledby="way-manual">
          <ol className="steps">
            <li>
              {t.rich("machines.manual.get", { version: issued.version ?? "", sync: <code>uv sync</code>, extra: <code>--extra nvidia</code> })}
            </li>
            <li>
              {t.rich("machines.manual.save", { file: <code>/etc/forgeyard/agent.toml</code>, init: <code>uv run --no-sync python -m forgeyard agent init</code> })}
              <CopyBlock value={issued.config_file ?? ""} label={t("machines.manual.config")} />
            </li>
            <li>
              {t("machines.manual.join")}
              {(issued.manual_commands ?? []).map((c, i) => (
                <CopyBox key={c} value={c} label={i === 0 ? t("machines.manual.joinCommand") : t("machines.manual.startCommand")} />
              ))}
            </li>
          </ol>
          <details className="trust-upgrade">
            <summary className="small">{t("machines.manual.noConfig")}</summary>
            <p className="small">{t("machines.manual.flags")}</p>
            <CopyBox value={issued.command} label={t("machines.manual.flagsCommand")} />
          </details>
          <HelpLink page="reference" anchor="agent-settings">{t("machines.manual.help")}</HelpLink>
        </div>
      )}
    </>
  );
}

// The published image on a machine that cannot pull it: carried over as a file, or built there.
function OfflineImage({ image, version }: { image: AgentImage; version: string }) {
  const t = useT();
  const carry = carryCommands(image);
  return (
    <details className="docker-advanced docker-offline">
      <summary className="small">{t("machines.offline.summary")}</summary>
      <p className="small">{t("machines.offline.save")}</p>
      <CopyBlock value={carry.save} label={t("machines.offline.saveCommand")} />
      <p className="small">{t.rich("machines.offline.copy", { file: <code>{IMAGE_FILE}</code>, scp: <code>scp</code> })}</p>
      <CopyBox value={carry.load} label={t("machines.offline.loadCommand")} />
      <p className="small muted">
        {t.rich("machines.offline.platform", { arm: <code>--platform linux/arm64</code>, amd: <code>--platform linux/amd64</code>, pull: <code>docker pull</code> })}
      </p>
      <p className="small">{t("machines.offline.build", { version })}</p>
      <CopyBox value={buildCommand(image.local_tag)} label={t("machines.docker.buildCommand")} />
      <p className="small muted">
        {pinnedByDigest(image)
          ? t.rich("machines.offline.useTag", { tag: <code>{image.local_tag}</code>, reference: <code>{image.reference}</code> })
          : t("machines.offline.runAbove")}
      </p>
      <p className="small">
        {t.rich("machines.offline.engines", { em: (text) => <em>{text}</em> })}{" "}
        <HelpLink page="how-to" anchor="offline-machines">{t("machines.offline.help")}</HelpLink>
      </p>
    </details>
  );
}

// The Docker tab's form: every value goes straight into the command below it.
function DockerFields({ form, errors, serverUrl, tls, onChange }: {
  form: DockerForm; errors: DockerErrors; serverUrl: string; tls?: TlsInfo | null; onChange: (form: DockerForm) => void;
}) {
  const t = useT();
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
      <legend className="sr-only">{t("machines.form.legend")}</legend>
      <div className="grid-2">
        <Field label={t("machines.form.modelDir")} name="docker-model-dir" value={form.modelDir} onChange={set("modelDir")}
          spellCheck={false} autoComplete="off" error={errors.modelDir}
          hint={t.rich("machines.form.modelDirHint", { env: <code>FORGEYARD_MODEL_CACHE</code> })} />
        <Field label={t("machines.form.name")} name="docker-name" value={form.name} onChange={set("name")} placeholder={t("machines.form.namePlaceholder")}
          spellCheck={false} autoComplete="off" error={errors.name}
          hint={t("machines.form.nameHint")} />
      </div>
      <Field label={t("machines.form.server")} name="docker-server" value={form.server} onChange={set("server")}
        spellCheck={false} autoComplete="off" inputMode="url" error={errors.server}
        hint={t("machines.form.serverHint")} />
      {form.server.trim().replace(/\/+$/, "") !== serverUrl && (
        <button type="button" className="btn btn-quiet btn-small docker-reset" onClick={() => onChange({ ...form, server: serverUrl })}>
          {t("machines.form.useThis", { url: serverUrl })}
        </button>
      )}
      {uncovered && (
        <Notice>
          {t.rich("machines.form.uncovered", { host, names: certNames, strong: (text) => <strong>{text}</strong> })}{" "}
          {tls?.mode === "forgeyard-ca" ? t.rich("machines.form.addName", { env: <code>TLS_EXTRA_NAMES</code> }) : t("machines.form.useName")}
        </Notice>
      )}
      <details className="docker-advanced" open={advancedOpen || undefined}>
        <summary className="small">{t("common.advanced")}</summary>
        <Field label={t("machines.form.portRange")} name="docker-port-range" value={form.portRange} onChange={set("portRange")}
          placeholder={AGENT_DEFAULT_PORT_RANGE} spellCheck={false} autoComplete="off" inputMode="numeric" error={errors.portRange}
          hint={t.rich("machines.form.portRangeHint", { env: <code>FORGEYARD_PORT_RANGE</code>, range: AGENT_DEFAULT_PORT_RANGE })} />
      </details>
    </fieldset>
  );
}
