import { IconDownload, IconLock, IconShieldCheck } from "@tabler/icons-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth";
import { Mark } from "../components/Mark";
import { CopyBox, Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { exact } from "../lib/format";
import { platformOf, type Platform } from "../lib/trust";
import type { TlsInfo } from "../lib/types";

// "Trust this server": public on purpose. A new device has to get the CA before it can trust the
// server, so before anyone signs in on it.

const PLATFORMS: { id: Platform; label: string }[] = [
  { id: "windows", label: "Windows" },
  { id: "macos", label: "macOS" },
  { id: "linux", label: "Linux (Chrome, system)" },
  { id: "firefox", label: "Firefox (any system)" },
  { id: "ios", label: "iPhone and iPad" },
  { id: "android", label: "Android" },
];

function Steps({ platform, ca }: { platform: Platform; ca: string }): ReactNode {
  switch (platform) {
    case "windows":
      return (
        <ol className="steps">
          <li>Download <strong>forgeyard-ca.crt</strong> and open it.</li>
          <li>Choose <em>Install Certificate…</em>, then <em>Current User</em> (or <em>Local Machine</em> for everyone on this PC).</li>
          <li>Choose <em>Place all certificates in the following store</em> → <em>Browse…</em> → <strong>Trusted Root Certification Authorities</strong>, then <em>Next</em> and <em>Finish</em>.</li>
          <li>Windows shows the fingerprint and asks once more: compare it with the one above, then <em>Yes</em>.</li>
          <li>Close every Chrome or Edge window and open Forgeyard again.</li>
        </ol>
      );
    case "macos":
      return (
        <ol className="steps">
          <li>Download <strong>forgeyard-ca.pem</strong> and open it: Keychain Access adds it to the <em>login</em> keychain.</li>
          <li>In Keychain Access, double-click <strong>{ca}</strong>, open <em>Trust</em>, and set <em>When using this certificate</em> to <strong>Always Trust</strong>.</li>
          <li>Close the window and confirm with your password. Safari and Chrome use it at once; Firefox needs its own step (below).</li>
        </ol>
      );
    case "linux":
      return (
        <>
          <p>Chrome and Chromium:</p>
          <ol className="steps">
            <li>Download <strong>forgeyard-ca.pem</strong>.</li>
            <li>Open <code>chrome://certificate-manager</code> → <em>Local certificates</em> → <em>Installed by you</em> → <em>Trusted certificates</em> → <em>Import</em>, and pick the file.</li>
            <li>Reload Forgeyard.</li>
          </ol>
          <p>The whole system (curl, Python, other programs), Debian and Ubuntu:</p>
          <CopyBox label="Debian and Ubuntu" value="sudo cp forgeyard-ca.pem /usr/local/share/ca-certificates/forgeyard-ca.crt && sudo update-ca-certificates" />
          <p>Fedora, RHEL, Rocky:</p>
          <CopyBox label="Fedora" value="sudo cp forgeyard-ca.pem /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust" />
        </>
      );
    case "firefox":
      return (
        <ol className="steps">
          <li>Download <strong>forgeyard-ca.pem</strong>.</li>
          <li>Open <em>Settings</em> → <em>Privacy &amp; Security</em> → <em>Certificates</em> → <em>View Certificates…</em> → <em>Authorities</em> → <em>Import…</em>.</li>
          <li>Pick the file, tick <strong>Trust this CA to identify websites</strong>, and choose <em>OK</em>.</li>
        </ol>
      );
    case "ios":
      return (
        <ol className="steps">
          <li>Open this page in <strong>Safari</strong> and download <strong>forgeyard-ca.crt</strong>; allow the configuration profile.</li>
          <li>Open <em>Settings</em> → <em>General</em> → <em>VPN &amp; Device Management</em>, choose <strong>{ca}</strong> and <em>Install</em>.</li>
          <li>Then <em>Settings</em> → <em>General</em> → <em>About</em> → <em>Certificate Trust Settings</em>, and switch on full trust for it. iOS skips this step otherwise, and the warning stays.</li>
        </ol>
      );
    case "android":
      return (
        <ol className="steps">
          <li>Download <strong>forgeyard-ca.crt</strong>.</li>
          <li>Open <em>Settings</em> → <em>Security &amp; privacy</em> → <em>More security settings</em> → <em>Encryption &amp; credentials</em> → <em>Install a certificate</em> → <strong>CA certificate</strong> (the names differ a little between phones), and pick the file.</li>
          <li>Chrome trusts it from then on. Other apps may not: Android lets each app decide whether to trust certificates you add.</li>
        </ol>
      );
  }
}

export function TrustPage() {
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
          <Link to={user ? "/" : "/login"} className="topbar-link">{user ? "Back to Forgeyard" : "Sign in"}</Link>
        </div>
      </header>
      <main className="content trust">
        <PageHeader index="HTTPS" title="Trust this server" lead="Do this once on each device you use Forgeyard from. Agents and programs have their own step, at the end." />
        {error && <Notice tone="error">{error}</Notice>}
        {info && info.mode === "off" && <PlainHttp admin={user?.role === "admin"} https={info.https} />}
        {info && info.mode === "own" && !ca && (
          <Notice tone="ok">This server uses a certificate your devices already trust. There is nothing to install.</Notice>
        )}
        {ca && (
          <>
            <section className="panel trust-why" aria-labelledby="why-title">
              <h2 id="why-title"><IconLock size={18} aria-hidden="true" /> Why your browser warns</h2>
              <p>
                Forgeyard always encrypts the connection. To prove it is really this server, it shows a certificate, and a browser only
                believes certificates vouched for by an authority it knows. {ca.own
                  ? "This server made its own small certificate authority when it first started, so no browser knows it yet."
                  : "This server's certificate comes from your organisation's certificate authority, which this device does not know yet."}
              </p>
              <p>
                Until you trust that authority, the browser cannot tell this server from an impostor, so it warns. Trusting it once on this
                device removes the warning, and from then on the browser checks the server on every visit. The encryption stays the same;
                only who vouches for it changes.
              </p>
            </section>

            <section className="panel" aria-labelledby="ca-title">
              <h2 id="ca-title"><IconShieldCheck size={18} aria-hidden="true" /> The certificate authority</h2>
              <dl className="facts">
                <dt>Name</dt>
                <dd>{ca.name.replace(/^CN=|,O=.*$/g, "")}</dd>
                <dt>Valid until</dt>
                <dd>{exact(ca.expires_at)}</dd>
                <dt>SHA-256 fingerprint</dt>
                <dd><code className="fingerprint">{ca.fingerprint_colons}</code></dd>
              </dl>
              <p className="muted small trust-check">
                Before you trust it, compare the fingerprint with one from a source you already trust: the server's startup log, the join
                command on the Machines page, or your administrator. This page itself came over a connection your browser could not check yet.
              </p>
              <div className="row-gap">
                <a className="btn btn-primary" href="/api/tls/ca.crt" download="forgeyard-ca.crt"><IconDownload size={16} aria-hidden="true" /> forgeyard-ca.crt</a>
                <a className="btn" href="/api/tls/ca.pem" download="forgeyard-ca.pem"><IconDownload size={16} aria-hidden="true" /> forgeyard-ca.pem</a>
              </div>
              <p className="muted small">The same certificate twice: <strong>.crt</strong> for Windows, iPhone and Android; <strong>.pem</strong> for macOS, Linux, Firefox and programs.</p>
            </section>

            <section className="panel" aria-labelledby="install-title">
              <h2 id="install-title">Install it</h2>
              <div className="trust-platforms">
                {PLATFORMS.map((p) => (
                  <details key={p.id} open={open === p.id} onToggle={(e) => (e.currentTarget as HTMLDetailsElement).open && setOpen(p.id)}>
                    <summary>{p.label}</summary>
                    <Steps platform={p.id} ca={ca.name.replace(/^CN=|,O=.*$/g, "")} />
                  </details>
                ))}
              </div>
            </section>

            <section className="panel" aria-labelledby="programs-title">
              <h2 id="programs-title">Programs and agents</h2>
              <p>Agents need nothing from here: the join command carries the fingerprint, and each agent trusts this authority only.</p>
              <p>Programs that call the API (<code>/v1</code>) use the <strong>forgeyard-ca.pem</strong> file:</p>
              <CopyBox label="curl" value={`curl --cacert forgeyard-ca.pem ${info.server_url}/api/health`} />
              <CopyBox label="Python" value="SSL_CERT_FILE=forgeyard-ca.pem python app.py" />
              <p className="muted small">
                Or in code, with the official OpenAI SDK: <code>OpenAI(base_url=…, http_client=httpx.Client(verify=ssl.create_default_context(cafile="forgeyard-ca.pem")))</code>.
                The API keys page shows complete examples.
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
  if (https) {
    return <Notice tone="ok">This server sits behind a proxy that handles HTTPS. Trust whatever certificate that proxy uses.</Notice>;
  }
  return (
    <Notice tone="error">
      <strong>This server does not use HTTPS.</strong> Passwords, session cookies and API keys cross the network unencrypted.
      {admin ? (
        <> It was started with <code>TLS=off</code>. Remove that setting to use Forgeyard's own certificate authority (the default), or put a
          proxy that terminates TLS in front of it and tell Forgeyard about it (<code>TRUSTED_PROXIES</code>); see <em>docs/https-and-trust.md</em>.</>
      ) : (
        <> Tell an administrator.</>
      )}
    </Notice>
  );
}

function AdminPart({ info }: { info: TlsInfo }) {
  const a = info.admin!;
  const cert = a.server_certificate;
  return (
    <section className="panel" aria-labelledby="admin-title">
      <h2 id="admin-title">For admins</h2>
      {cert && (
        <dl className="facts">
          <dt>Server certificate</dt>
          <dd>valid until {exact(cert.expires_at)}{info.mode === "forgeyard-ca" ? "; renewed by itself 60 days before" : ""}</dd>
          <dt>Names in it</dt>
          <dd className="mono small">{[...cert.names, ...cert.ips].join(", ")}</dd>
        </dl>
      )}
      {a.trust_command && (
        <>
          <p className="trust-admin-lead">An agent that joined before this server used HTTPS keeps reconnecting over plain HTTP and stays offline. Run this once on its machine, in the Forgeyard folder, then restart the agent. It keeps its credential: no new join token.</p>
          <CopyBox label="Trust command" value={a.trust_command} />
        </>
      )}
    </section>
  );
}
