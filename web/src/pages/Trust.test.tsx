// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { imageTag } from "../lib/format";
import { snippets } from "../lib/gateway";
import { exampleCall } from "../lib/onboarding";
import { platformOf } from "../lib/trust";
import type { TlsInfo } from "../lib/types";

const ADMIN = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };
const USER = { ...ADMIN, id: 2, username: "alice", role: "user", is_owner: false };
const FP = "sha256:" + "ab".repeat(32);

const INFO: TlsInfo = {
  mode: "forgeyard-ca", https: true, hsts: false, server_url: "https://forge.lan:7860",
  ca: { name: "CN=Forgeyard CA forge a1b2c3,O=Forgeyard", expires_at: "2036-11-08T00:00:00Z", fingerprint: FP,
    fingerprint_colons: Array(32).fill("AB").join(":"), own: true },
};

function server(tls: { mode: string; https: boolean }, user: typeof ADMIN | null, info: TlsInfo = INFO) {
  const fetch = vi.fn(async (url: string) => {
    const body = url === "/api/auth/state" ? { needs_setup: false, registration: "off", user, tls }
      : url === "/api/tls" ? info
        : url === "/api/onboarding" ? { steps: [], hidden: true, gateway_url: "https://forge.lan:7860/v1", example: null }
          : url === "/api/machines" ? { machines: [], server_time: "2026-11-10T00:00:00Z", heartbeat_interval: 10, offline_after: 30, can_manage: true }
            : url === "/api/machines/join-tokens" ? { tokens: [] } : {};
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("trust this server", () => {
  it("FY-WEB-113 is public: explains the warning, shows the fingerprint and both downloads", async () => {
    server({ mode: "forgeyard-ca", https: true }, null);
    mount("/trust");
    expect(await screen.findByRole("heading", { name: "Trust this server" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: /Why your browser warns/ })).toBeTruthy();
    expect(screen.getByText(/made its own small certificate authority/)).toBeTruthy();
    expect(screen.getByText(Array(32).fill("AB").join(":"))).toBeTruthy();
    expect(screen.getByRole("link", { name: /forgeyard-ca\.crt/ }).getAttribute("href")).toBe("/api/tls/ca.crt");
    expect(screen.getByRole("link", { name: /forgeyard-ca\.pem/ }).getAttribute("href")).toBe("/api/tls/ca.pem");
    for (const p of ["Windows", "macOS", "Linux (Chrome, system)", "Firefox (any system)", "iPhone and iPad", "Android"]) {
      expect(screen.getByText(p, { selector: "summary" })).toBeTruthy();
    }
    expect(screen.getByLabelText("curl").textContent).toBe("curl --cacert forgeyard-ca.pem https://forge.lan:7860/api/health");
    expect(screen.queryByRole("heading", { name: "For admins" })).toBeNull();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeTruthy();
  });

  it("FY-WEB-114 opens the instructions for the device it is on", () => {
    expect(platformOf("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1")).toBe("ios");
    expect(platformOf("Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36")).toBe("android");
    expect(platformOf("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0")).toBe("firefox");
    expect(platformOf("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36")).toBe("windows");
    expect(platformOf("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15")).toBe("macos");
    expect(platformOf("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36")).toBe("linux");
  });

  it("FY-WEB-115 admins also get the server certificate and the trust command for old agents", async () => {
    const cmd = `uv run --no-sync python -m forgeyard agent trust --server https://forge.lan:7860 --ca-fingerprint ${FP}`;
    server({ mode: "forgeyard-ca", https: true }, ADMIN, { ...INFO, admin: { trust_command: cmd,
      server_certificate: { names: ["localhost", "forge.lan"], ips: ["127.0.0.1"], expires_at: "2027-12-01T00:00:00Z", issuer: "CN=x" } } });
    mount("/trust");
    expect(await screen.findByRole("heading", { name: "For admins" })).toBeTruthy();
    expect(screen.getByLabelText("Trust command").textContent).toBe(cmd);
    expect(screen.getByText("localhost, forge.lan, 127.0.0.1")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to Forgeyard" })).toBeTruthy();
  });

  it("FY-WEB-116 plain HTTP: admins see the warning on every page, users do not", async () => {
    server({ mode: "off", https: false }, ADMIN);
    mount("/machines");
    expect(await screen.findByText("Forgeyard is running without HTTPS.")).toBeTruthy();
    expect(screen.getByRole("link", { name: "What to do" }).getAttribute("href")).toBe("/trust");
    cleanup();
    server({ mode: "off", https: true }, ADMIN); // behind a TLS proxy
    mount("/machines");
    expect(await screen.findByRole("heading", { name: "Machines" })).toBeTruthy();
    expect(screen.queryByText("Forgeyard is running without HTTPS.")).toBeNull();
    cleanup();
    server({ mode: "off", https: false }, USER);
    mount("/machines");
    expect(await screen.findByRole("heading", { name: "Machines" })).toBeTruthy();
    expect(screen.queryByText("Forgeyard is running without HTTPS.")).toBeNull();
    cleanup();
    server({ mode: "forgeyard-ca", https: true }, ADMIN);
    mount("/machines");
    expect(await screen.findByRole("heading", { name: "Machines" })).toBeTruthy();
    expect(screen.queryByText("Forgeyard is running without HTTPS.")).toBeNull();
  });

  it("FY-WEB-117 /v1 snippets trust the CA file when the server has its own CA", () => {
    const s = snippets("https://forge.lan:7860/v1", "fyk_x", "qwen", "openai-chat", "https://forge.lan:7860/trust");
    expect(s.python).toContain('ca = ssl.create_default_context(cafile="forgeyard-ca.pem")');
    expect(s.python).toContain('OpenAI(base_url="https://forge.lan:7860/v1", api_key="fyk_x", http_client=httpx.Client(verify=ca))');
    expect(s.python.startsWith("import ssl\n\nimport httpx\nfrom openai import OpenAI\n")).toBe(true);
    expect(s.curl).toContain("curl --cacert forgeyard-ca.pem https://forge.lan:7860/v1/chat/completions");
    const rr = snippets("https://h/v1", "fyk_x", "r", "rerank", "https://h/trust");
    expect(rr.python).toContain("verify=ca)");
    expect(rr.python).toContain("import ssl");
    const plain = snippets("https://h/v1", "fyk_x", "e", "openai-embeddings");
    expect(plain.python).not.toContain("ssl");
    expect(plain.curl).not.toContain("--cacert");
    expect(exampleCall("https://h/v1", "m", "openai-chat", "https://h/trust").python.startsWith("import os\nimport ssl\n")).toBe(true);
  });

  it("FY-WEB-118 images show their tag; the digest is what runs", () => {
    expect(imageTag("vllm/vllm-openai:v0.32.0@sha256:" + "a".repeat(64))).toBe("vllm/vllm-openai:v0.32.0");
    expect(imageTag("ghcr.io/ggml-org/llama.cpp:server-b11977@sha256:" + "b".repeat(64))).toBe("ghcr.io/ggml-org/llama.cpp:server-b11977");
    expect(imageTag("registry:5000/app@sha256:" + "c".repeat(64))).toBe("registry:5000/app@sha256:" + "c".repeat(64)); // no tag: keep it whole
    expect(imageTag("python:3.13-slim")).toBe("python:3.13-slim");
  });
});
