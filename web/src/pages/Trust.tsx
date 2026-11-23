import { IconDownload, IconLock, IconShieldCheck } from "@tabler/icons-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth";
import { Mark } from "../components/Mark";
import { HelpLink, HelpLinks } from "../components/HelpLink";
import { KeyBackupButton } from "../components/KeyBackup";
import { CopyBox, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import type { Key } from "../i18n/core";
import { useT } from "../i18n/react";
import { exact } from "../lib/format";
import { platformOf, type Platform } from "../lib/trust";
import type { TlsInfo } from "../lib/types";

// "Trust this server": public on purpose. A new device has to get the CA before it can trust the
// server, so before anyone signs in on it.

// Names of products stay as they are; the others are translated (admin.trust.platform.<id>).
const PLATFORMS: { id: Platform; label: string | Key }[] = [
  { id: "windows", label: "Windows" },
  { id: "macos", label: "macOS" },
  { id: "linux", label: "admin.trust.platform.linux" },
  { id: "firefox", label: "admin.trust.platform.firefox" },
  { id: "ios", label: "admin.trust.platform.ios" },
  { id: "android", label: "Android" },
];

// Each step is one sentence with its <strong>, <em> and <code> parts (admin.trust.<platform>.<n>).
const STEPS: Record<Exclude<Platform, "linux">, Key[]> = {
  windows: ["admin.trust.win.1", "admin.trust.win.2", "admin.trust.win.3", "admin.trust.win.4", "admin.trust.win.5"],
  macos: ["admin.trust.mac.1", "admin.trust.mac.2", "admin.trust.mac.3"],
  firefox: ["admin.trust.ff.1", "admin.trust.ff.2", "admin.trust.ff.3"],
  ios: ["admin.trust.ios.1", "admin.trust.ios.2", "admin.trust.ios.3"],
  android: ["admin.trust.android.1", "admin.trust.android.2", "admin.trust.android.3"],
};

function Step({ k, ca }: { k: Key; ca: string }) {
  const t = useT();
  const parts = { ca, strong: (x: string) => <strong>{x}</strong>, em: (x: string) => <em>{x}</em>, code: (x: string) => <code>{x}</code> };
  return <li>{t.rich(k as "admin.trust.mac.2", parts as never)}</li>;
}

function Steps({ platform, ca }: { platform: Platform; ca: string }): ReactNode {
  const t = useT();
  if (platform === "linux") {
    return (
      <>
        <p>{t("admin.trust.linux.chrome")}</p>
        <ol className="steps">
          {(["admin.trust.linux.1", "admin.trust.linux.2", "admin.trust.linux.3"] as Key[]).map((k) => <Step key={k} k={k} ca={ca} />)}
        </ol>
        <p>{t("admin.trust.linux.system")}</p>
        <CopyBox label={t("admin.trust.linux.debian")} value="sudo cp forgeyard-ca.pem /usr/local/share/ca-certificates/forgeyard-ca.crt && sudo update-ca-certificates" />
        <p>{t("admin.trust.linux.fedora")}</p>
        <CopyBox label="Fedora" value="sudo cp forgeyard-ca.pem /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust" />
      </>
    );
  }
  return <ol className="steps">{STEPS[platform].map((k) => <Step key={k} k={k} ca={ca} />)}</ol>;
}

export function TrustPage() {
  const t = useT();
  const { user } = useAuth();
  const [info, setInfo] = useState<TlsInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Platform>(() => platformOf(navigator.userAgent));

  useEffect(() => {
    api<TlsInfo>("/tls").then(setInfo, (e) => setError(errorText(e)));
  }, []);

  const ca = info?.ca;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <Mark size={26} />
            <span>Forgeyard</span>
          </Link>
          <span className="topbar-spacer" />
          <Link to={user ? "/" : "/login"} className="topbar-link">{user ? t("admin.trust.backTo") : t("admin.trust.signIn")}</Link>
        </div>
      </header>
      <main className="content trust">
        <PageHeader index="HTTPS" title={t("admin.trust.title")} lead={t("admin.trust.lead")} />
        <HelpLinks>
          <HelpLink page="troubleshooting" anchor="the-browser-warns-about-the-certificate">{t("admin.trust.help.warns")}</HelpLink>
          <HelpLink page="how-to" anchor="use-your-own-certificate-or-a-reverse-proxy">{t("admin.trust.help.own")}</HelpLink>
          <HelpLink page="how-to" anchor="back-up-and-restore">{t("admin.trust.help.backup")}</HelpLink>
        </HelpLinks>
        {error && <Notice tone="error">{error}</Notice>}
        {info && info.mode === "off" && <PlainHttp admin={user?.role === "admin"} https={info.https} />}
        {info && info.mode === "own" && !ca && (
          <Notice tone="ok">{t("admin.trust.ownTrusted")}</Notice>
        )}
        {ca && (
          <>
            <section className="panel trust-why" aria-labelledby="why-title">
              <h2 id="why-title"><IconLock size={18} aria-hidden="true" /> {t("admin.trust.why")}</h2>
              <p>{ca.own ? t("admin.trust.whyOwn") : t("admin.trust.whyOrg")}</p>
              <p>{t("admin.trust.whyAfter")}</p>
            </section>

            <section className="panel" aria-labelledby="ca-title">
              <h2 id="ca-title"><IconShieldCheck size={18} aria-hidden="true" /> {t("admin.trust.ca")}</h2>
              <dl className="facts">
                <dt>{t("admin.trust.name")}</dt>
                <dd>{ca.name.replace(/^CN=|,O=.*$/g, "")}</dd>
                <dt>{t("admin.trust.validUntil")}</dt>
                <dd>{exact(ca.expires_at)}</dd>
                <dt>{t("admin.trust.fingerprint")}</dt>
                <dd><code className="fingerprint">{ca.fingerprint_colons}</code></dd>
              </dl>
              <p className="muted small trust-check">{t("admin.trust.check")}</p>
              <div className="row-gap">
                <a className="btn btn-primary" href="/api/tls/ca.crt" download="forgeyard-ca.crt"><IconDownload size={16} aria-hidden="true" /> forgeyard-ca.crt</a>
                <a className="btn" href="/api/tls/ca.pem" download="forgeyard-ca.pem"><IconDownload size={16} aria-hidden="true" /> forgeyard-ca.pem</a>
              </div>
              <p className="muted small">{t.rich("admin.trust.twice", { strong: (x) => <strong>{x}</strong> })}</p>
            </section>

            <section className="panel" aria-labelledby="install-title">
              <h2 id="install-title">{t("admin.trust.install")}</h2>
              <div className="trust-platforms">
                {PLATFORMS.map((p) => (
                  <details key={p.id} open={open === p.id} onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && setOpen(p.id)}>
                    <summary>{t.maybe(p.label, p.label)}</summary>
                    <Steps platform={p.id} ca={ca.name.replace(/^CN=|,O=.*$/g, "")} />
                  </details>
                ))}
              </div>
            </section>

            <section className="panel" aria-labelledby="programs-title">
              <h2 id="programs-title">{t("admin.trust.programs")}</h2>
              <p>{t("admin.trust.agents")}</p>
              <p>{t.rich("admin.trust.programsUse", { v1: <code>/v1</code>, strong: (x) => <strong>{x}</strong> })}</p>
              <CopyBox label="curl" value={`curl --cacert forgeyard-ca.pem ${info.server_url}/api/health`} />
              <CopyBox label="Python" value="SSL_CERT_FILE=forgeyard-ca.pem python app.py" />
              <p className="muted small">
                {t.rich("admin.trust.inCode", {
                  code: <code>OpenAI(base_url=…, http_client=DefaultHttpxClient(verify=ssl.create_default_context(cafile="forgeyard-ca.pem")))</code>,
                })}
              </p>
            </section>
          </>
        )}
        {info?.admin && <AdminPart info={info} />}
      </main>
    </div>
  );
}

function PlainHttp({ admin, https }: { admin: boolean; https: boolean }) {
  const t = useT();
  if (https) {
    return <Notice tone="ok">{t("admin.trust.proxy")}</Notice>;
  }
  return (
    <Notice tone="error">
      {t.rich("admin.trust.plain", { strong: (x) => <strong>{x}</strong> })}
      {admin
        ? t.rich("admin.trust.plainAdmin", { off: <code>TLS=off</code>, proxies: <code>TRUSTED_PROXIES</code>, em: (x) => <em>{x}</em> })
        : t("admin.trust.plainUser")}
    </Notice>
  );
}

function AdminPart({ info }: { info: TlsInfo }) {
  const t = useT();
  const a = info.admin!;
  const ownCa = info.mode === "forgeyard-ca";
  const cert = a.server_certificate;
  return (
    <section className="panel" aria-labelledby="admin-title">
      <h2 id="admin-title">{t("admin.trust.admins")}</h2>
      {cert && (
        <dl className="facts">
          <dt>{t("admin.trust.serverCert")}</dt>
          <dd>{t(ownCa ? "admin.trust.certUntilRenew" : "admin.trust.certUntil", { date: exact(cert.expires_at) })}</dd>
          <dt>{t("admin.trust.names")}</dt>
          <dd className="mono small">{[...cert.names, ...cert.ips].join(", ")}</dd>
        </dl>
      )}
      <p className="trust-admin-lead">{ownCa ? t("admin.trust.backupCa") : t("admin.trust.backup")}</p>
      <div className="trust-admin-actions"><KeyBackupButton label={t("admin.trust.backupButton")} /></div>
      {a.trust_command && (
        <>
          <p className="trust-admin-lead">{t("admin.trust.oldAgents")}</p>
          <CopyBox label={t("admin.trust.command")} value={a.trust_command} />
        </>
      )}
    </section>
  );
}
