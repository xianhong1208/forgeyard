# HTTPS and trust

Forgeyard speaks HTTPS from the first start, without you buying, requesting or renewing a
certificate. This page explains how, what each device has to do once, how agents and programs
trust the server, how to use your own certificate or a reverse proxy instead, and how stored
secrets are encrypted.

- [What happens on the first start](#what-happens-on-the-first-start)
- [Why the browser warns, and trusting the CA once per device](#why-the-browser-warns-and-trusting-the-ca-once-per-device)
- [Agents: the fingerprint in the join command](#agents-the-fingerprint-in-the-join-command)
- [Upgrading from 1.0: agents that joined over plain HTTP](#upgrading-from-10-agents-that-joined-over-plain-http)
- [Programs: curl, Python and the OpenAI SDK](#programs-curl-python-and-the-openai-sdk)
- [Your own certificate](#your-own-certificate)
- [Behind a reverse proxy (nginx, Caddy)](#behind-a-reverse-proxy-nginx-caddy)
- [Secrets encrypted at rest](#secrets-encrypted-at-rest)
- [What to back up](#what-to-back-up)
- [Settings](#settings)

## What happens on the first start

With no TLS settings at all, the server makes a small **certificate authority** (CA) of its own
and a server certificate signed by it, in `data/tls/`:

| File | What it is |
|---|---|
| `ca.key` | The CA's private key (EC P-256), mode 0600. It never leaves this machine. |
| `ca.crt` | The CA certificate, valid for 10 years. This is what devices and agents trust. |
| `server.key` | The server's private key, mode 0600. |
| `server.crt` | The server certificate (397 days), followed by the CA certificate. |

The server certificate names every address people and agents use:

- the host of `PUBLIC_URL`;
- every name in `ALLOWED_HOSTS` and `TLS_EXTRA_NAMES`;
- `HOST`, unless it is `0.0.0.0` or `::`;
- the machine's host name, short and fully qualified;
- the machine's own IP addresses;
- `localhost`, `127.0.0.1` and `::1`.

A new server certificate, signed by the same CA, is made by itself:

- when less than 60 days are left;
- when an address is missing (you set a new `PUBLIC_URL`, the machine got a new address);
- when it cannot be read, or was not signed by the current CA.

A running server checks twice a day and switches to the new certificate without a restart.
Devices and agents notice nothing, because they trust the CA, not the server certificate. The CA
itself lasts 10 years; the server logs a warning when less than a year is left.

The server accepts TLS 1.2 and newer. The CA's fingerprint is `sha256:` followed by the SHA-256 of
its DER form, in lower-case hex; agents also accept it with colons (`AB:CD:…`) or as plain hex.
Anyone can download the CA, without signing in: `/api/tls/ca.pem` (PEM, saved as
`forgeyard-ca.pem`) and `/api/tls/ca.crt` (DER, saved as `forgeyard-ca.crt`). Note that the file
`data/tls/ca.crt` on the server is PEM, despite its name.

The startup log shows the CA's fingerprint:

```
INFO forgeyard: HTTPS with Forgeyard's own CA. CA fingerprint sha256:3f9a…
```

Plain HTTP happens only when you ask for it with `TLS=off`, which is meant for running behind a
proxy that terminates TLS (see [below](#behind-a-reverse-proxy-nginx-caddy)). While pages still
arrive over plain HTTP, every admin sees a red warning at the top of each page.

There is no redirect from `http://` to `https://` on the same port: one port speaks one protocol.
A browser that opens `http://host:7860` gets an empty answer. Use `https://`.

## Why the browser warns, and trusting the CA once per device

A browser believes a certificate only when an authority it already knows vouches for it. The
public authorities (Let's Encrypt and others) only vouch for names on the public internet, not for
a machine on your LAN or VPN. So this server vouches for itself with its own small CA.

Until you trust that CA on a device, the browser cannot tell this server from an impostor and
shows a warning. The connection is encrypted either way. Trusting the CA once on a device removes
the warning, and from then on the browser checks the server on every visit.

On each device:

1. Open Forgeyard and click past the warning once. In Chrome: *Advanced* → *Proceed*. In
   Firefox: *Advanced* → *Accept the risk*.
2. On the sign-in page, follow **Trust this server**, or open `https://<server>/trust`. The page
   works without signing in.
3. Compare the fingerprint shown there with one from a source you already trust: the server's
   startup log, the join command on the Machines page, or your administrator.
4. Download the CA and follow the steps for the device:

   | Device | File | Where |
   |---|---|---|
   | Windows | `forgeyard-ca.crt` | Open it → *Install Certificate* → *Trusted Root Certification Authorities* |
   | macOS | `forgeyard-ca.pem` | Keychain Access → the certificate → *Trust* → *Always Trust* |
   | Linux, Chrome | `forgeyard-ca.pem` | `chrome://certificate-manager` → *Installed by you* → *Trusted certificates* → *Import* |
   | Linux, whole system | `forgeyard-ca.pem` | `sudo cp forgeyard-ca.pem /usr/local/share/ca-certificates/forgeyard-ca.crt && sudo update-ca-certificates` (Debian, Ubuntu) or `/etc/pki/ca-trust/source/anchors/` + `sudo update-ca-trust` (Fedora, RHEL) |
   | Firefox, any system | `forgeyard-ca.pem` | *Settings* → *Privacy & Security* → *View Certificates* → *Authorities* → *Import*, tick *Trust this CA to identify websites* |
   | iPhone, iPad | `forgeyard-ca.crt` | In Safari, then *Settings* → *General* → *VPN & Device Management* → install; then *About* → *Certificate Trust Settings* → full trust |
   | Android | `forgeyard-ca.crt` | *Settings* → *Security* → *Encryption & credentials* → *Install a certificate* → *CA certificate* |

The page opens the steps for the device it is viewed on.

What trusting a CA means: the device then believes **any** certificate this CA signs. Forgeyard
uses its CA only to sign its own server certificate, and the CA's key stays in `data/tls/ca.key`
on the server (mode 0600). Whoever obtains that file could impersonate other sites to the devices
that trust it, so protect it like a password, and back it up with the same care.

Why there is no HSTS with Forgeyard's own CA: `Strict-Transport-Security` makes a browser refuse to
let anyone click past a certificate warning for that host, for a year. A device that does not
trust the CA yet would then never reach the *Trust this server* page. HSTS is sent only with
[your own certificate](#your-own-certificate), which browsers trust from the start.

## Agents: the fingerprint in the join command

Every join command on the Machines page carries the CA's fingerprint, whichever tab you use:

- **One-line install:** the command downloads the CA, checks its SHA-256 against the fingerprint
  with `sha256sum -c`, and only then downloads `install.sh` over a connection checked against that
  CA. The script checks the CA again, downloads the agent's wheel with that CA and checks the wheel's
  SHA-256, then passes `--ca-fingerprint` on to the agent. The server serves `install.sh` and the
  wheel only over HTTPS (directly, or through a trusted proxy that says `X-Forwarded-Proto: https`).
- **Docker:** `-e FORGEYARD_CA_FINGERPRINT=sha256:…`.
- **Manual:** `ca_fingerprint` in `agent.toml`, or the flag:

```
uv run --no-sync python -m forgeyard agent --server https://forge.lan:7860 \
    --join-token fyj_… --ca-fingerprint sha256:3f9a…
```

On its first start the agent does the following, in this order:

1. It downloads the server's CA from `/api/tls/ca.pem`. It sends nothing secret yet.
2. It refuses the CA unless its SHA-256 matches the fingerprint.
3. It joins over HTTPS, verifying the server against **that CA only**, not the system's list of
   authorities.
4. It keeps the CA in its state directory (`server-ca.pem` next to `credential.json`). The pinned
   CA may also be an intermediate CA.

From then on the agent connects over `wss://` and accepts only server certificates signed by the
pinned CA. A machine in the middle with any other certificate is refused, whether it sends its own
CA (wrong fingerprint) or the real CA next to its own certificate (not signed by it). The agent
then logs an error and keeps retrying, but never connects.

`--ca-file <pem>` pins a CA file you already have instead, for example your company's CA or a TLS
proxy's CA. Without either option, an `https://` server is checked against the system's trust
store, which is right for a publicly trusted certificate.

## Upgrading from 1.0: agents that joined over plain HTTP

Forgeyard 1.0 spoke plain HTTP. After the upgrade the server speaks HTTPS on the same port, so an
agent that still has `http://…` saved cannot connect. It stays offline, and its log says what to
run:

```
WARNING forgeyard.agent: If the server now uses HTTPS (Forgeyard 1.1 and later do by default), run once: …
```

On the server:

1. Update Forgeyard (`git pull`, then `uv sync`: 1.1 adds the `cryptography` package).
2. Restart it. The first start makes the CA, the server certificate and `data/secret.key`, and
   encrypts the stored secrets.
3. Note the fingerprint from the log, or from the Machines page or *Trust this server* (signed in
   as an admin, it shows the exact trust command).

On each machine, in its Forgeyard folder:

1. Update Forgeyard the same way.
2. Stop the agent and run the trust command once:

   ```
   uv run --no-sync python -m forgeyard agent trust --server https://forge.lan:7860 \
       --ca-fingerprint sha256:3f9a…
   ```

   It checks the fingerprint, talks to the server through the pinned CA, then saves the new
   address and the CA. The machine **keeps its credential**: it needs no new join token, and its
   workloads and history stay.
3. Start the agent as before (without `--server`). It connects over `wss://`.

A wrong fingerprint changes nothing. If the server's CA ever has to change (`data/tls` lost or
replaced on purpose), use the same command with the new fingerprint.

## Programs: curl, Python and the OpenAI SDK

Programs that call the API or the gateway (`/v1`) need the CA file, `forgeyard-ca.pem`. Get it from
*Trust this server*, or copy `data/tls/ca.crt` from the server.

```bash
curl --cacert forgeyard-ca.pem https://forge.lan:7860/v1/models -H "Authorization: Bearer $FORGEYARD_API_KEY"
```

Python, with the official OpenAI SDK, through its `http_client`:

```python
import ssl

import httpx
from openai import OpenAI

ca = ssl.create_default_context(cafile="forgeyard-ca.pem")
client = OpenAI(base_url="https://forge.lan:7860/v1", api_key="fyk_…", http_client=httpx.Client(verify=ca))
print(client.chat.completions.create(model="qwen3-4b", messages=[{"role": "user", "content": "Hello"}]).choices[0].message.content)
```

Or, without changing code, with the variable both `httpx` and Python's `ssl` read:
`SSL_CERT_FILE=forgeyard-ca.pem python app.py`. Note that this replaces the system's list of
authorities for that process. The examples on the API keys page and on *Get started* already
contain the CA lines when the server uses its own CA.

## Your own certificate

To use a certificate from your company's CA, or one for a real domain, set:

```bash
TLS_CERT_FILE=/etc/forgeyard/tls/fullchain.pem   # the certificate, then its intermediates
TLS_KEY_FILE=/etc/forgeyard/tls/privkey.pem
TLS_CA_FILE=/etc/forgeyard/tls/company-ca.pem    # optional: the CA that issued it
```

- Forgeyard then makes no CA of its own and uses these files as they are. A running server checks
  them every 12 hours and switches to them when they changed, for example after certbot renewed
  them, without a restart. Restart the server to switch at once.
- **What agents pin.** When `TLS_CA_FILE` is set, the join command carries that CA's fingerprint
  and agents pin it. Pinning the issuer, not the certificate, means renewals keep working. When it
  is not set, the certificate is taken to be publicly trusted: the join command has no fingerprint
  and agents use the system's trust store.

  Pinning a public CA's intermediate would break agents at the next renewal, because Let's Encrypt
  rotates its intermediates.
- `Strict-Transport-Security: max-age=31536000` is sent: browsers trust this certificate from the
  start, so nothing has to stay clickable.
- *Trust this server* offers `TLS_CA_FILE` for download. Without it, the page says there is
  nothing to install.

## Behind a reverse proxy (nginx, Caddy)

If a proxy already handles TLS for you, let it, and have Forgeyard speak plain HTTP behind it on
loopback:

```bash
TLS=off
HOST=127.0.0.1
PUBLIC_URL=https://forge.example.com      # what people, agents and programs use
TRUSTED_PROXIES=127.0.0.1                 # believe the proxy's X-Forwarded-For / X-Forwarded-Proto
```

`TRUSTED_PROXIES` lists exact IP addresses (no ranges): the address the proxy connects from.

With these settings:

- Session cookies are `Secure`, because the proxy says `X-Forwarded-Proto: https`.
- Admins see no plain-HTTP warning.
- The gateway address is `https://forge.example.com/v1`.
- The join commands carry no fingerprint (the one-line install is
  `curl -fsS https://forge.example.com/install.sh | sudo sh -s -- --join fyj_…`): agents use the
  system's trust store. If the proxy's certificate comes from a
  company CA, add `--ca-file company-ca.pem` to the join command, or to `agent trust`.

nginx:

```nginx
server {
    listen 443 ssl;
    server_name forge.example.com;
    ssl_certificate     /etc/letsencrypt/live/forge.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/forge.example.com/privkey.pem;

    client_max_body_size 32m;          # GATEWAY_MAX_BODY_MB (audio for transcription)

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # The agents' connection (/agent/v1/stream) is a WebSocket.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        # Streamed answers from /v1 must not wait in a buffer; long generations take time.
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

Caddy (it gets and renews the certificate by itself, and passes WebSockets and streaming through):

```caddyfile
forge.example.com {
    request_body {
        max_size 32MB
    }
    reverse_proxy 127.0.0.1:7860 {
        flush_interval -1
        transport http {
            read_timeout 600s
        }
    }
}
```

Caddy sets `X-Forwarded-For` and `X-Forwarded-Proto` itself. HSTS is the proxy's job in this
setup: add `add_header Strict-Transport-Security "max-age=31536000" always;` in nginx, or
`header Strict-Transport-Security "max-age=31536000"` in Caddy, once everything works over HTTPS.

## Secrets encrypted at rest

Forgeyard encrypts the secrets it has to read back:

- Hugging Face tokens on deployments;
- model replicas' internal keys;
- the values of workload environment variables (the names stay readable);
- cloud providers' API keys (1.2).

They are encrypted with AES-256-GCM under a server key that is **not in the database**:
`data/secret.key` (32 random bytes, mode 0600, made on the first start), or the file named by
`SECRET_KEY_FILE`, or the base64 key in `FORGEYARD_SECRET_KEY` (never written anywhere). Each
value is bound to its column, so a value copied elsewhere does not decrypt.

Passwords, sessions, API keys and join tokens are not encrypted: they are hashed, because they
are only ever compared, never read back.

A copy of the database alone (a backup, a dump, a PostgreSQL server elsewhere) therefore reveals
none of these secrets. The database records which key its secrets belong to (a key id, never the
key), so the server notices a missing or wrong key at startup and refuses to start with a clear
message, instead of failing later or making a new key over the old secrets.

Upgrading from 1.0 encrypts the existing values once, in migration `0008_encrypt_secrets`. This
is the only place plain-text values are read; after it, a value that is not encrypted is an error.

**If the key is lost:** restore `data/secret.key` from your backup. If there is no backup, stop
the server and run:

```bash
uv run --no-sync python -m forgeyard secrets reset --yes
```

It makes a new key (the old file, if any, is kept as `secret.key.lost-<time>`) and then:

- drops the Hugging Face tokens: enter them again on each deployment of a gated model;
- places model replicas again, so they restart with new internal keys;
- empties lost workload environment values (the names stay), and says so in each workload's
  events: delete such a workload and create it again with its values;
- drops cloud providers' keys: enter them again on the **Providers** page.

It refuses to run when the key is not actually lost.

## What to back up

| What | Why |
|---|---|
| The database (`data/forgeyard.db`, or your PostgreSQL) | Everything Forgeyard knows |
| `data/secret.key` (or your `FORGEYARD_SECRET_KEY`) | Without it, the encrypted secrets above are gone. Keep it **apart** from the database backup: together they reveal those secrets. |
| `data/tls/` (above all `ca.key` and `ca.crt`) | Without them, a new CA is needed: every device and every agent has to trust it again (`agent trust` with the new fingerprint). The server refuses to make one silently while certificates are still there. |

## Settings

| Variable | Default | What it does |
|---|---|---|
| `TLS` | `on` | `off`: plain HTTP, only behind a TLS proxy |
| `TLS_CERT_FILE`, `TLS_KEY_FILE` | — | Your own certificate (both or neither) |
| `TLS_CA_FILE` | — | With your own certificate: the CA agents pin and devices download |
| `TLS_EXTRA_NAMES` | — | More names or IPs for Forgeyard's server certificate, comma-separated |
| `TRUSTED_PROXIES` | — | Proxies whose `X-Forwarded-For` / `X-Forwarded-Proto` are believed |
| `SECRET_KEY_FILE` | `data/secret.key` | Where the key that encrypts stored secrets is |
| `FORGEYARD_SECRET_KEY` | — | The key itself (base64, 32 bytes), instead of a file |
| `COOKIE_SECURE` | `auto` | `auto`: `Secure` cookies whenever the browser talks HTTPS |
