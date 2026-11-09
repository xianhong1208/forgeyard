// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router";

import App from "./App";
import { AuthProvider } from "./auth";

const OWNER = { id: 1, username: "owner", email: null, role: "admin", status: "active", is_owner: true, has_password: true, created_at: "2026-11-09T00:00:00Z", last_login_at: null };

type Route = (url: string, init?: RequestInit) => { status: number; body: unknown; headers?: Record<string, string> };

function server(route: Route) {
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const r = route(url, init);
    return new Response(JSON.stringify(r.body), { status: r.status, headers: r.headers });
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search}</div>;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
        <Where />
      </AuthProvider>
    </MemoryRouter>,
  );
}

const where = () => screen.getByTestId("where").textContent;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("routing and sign-in", () => {
  it("FY-WEB-020 a signed-out visit goes to sign-in and comes back after it", async () => {
    let signedIn = false;
    server((url, init) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: signedIn ? OWNER : null } };
      if (url === "/api/auth/login" && init?.method === "POST") {
        signedIn = true;
        return { status: 200, body: { user: OWNER } };
      }
      if (url === "/api/audit?limit=50") return { status: 200, body: { events: [], next_before: null } };
      return { status: 404, body: {} };
    });
    mount("/audit?x=1");
    await waitFor(() => expect(where()).toBe("/login?next=%2Faudit%3Fx%3D1"));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "harbour-lantern-71" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
    await waitFor(() => expect(where()).toBe("/audit?x=1"));
    expect(await screen.findByRole("heading", { name: "Audit log" })).toBeTruthy();
  });

  it("FY-WEB-021 a 401 from any call sends you to sign-in with the reason and the page", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } };
      if (url === "/api/users") return { status: 401, body: { detail: { code: "session_ended", reason: "signed_out_by_admin" } } };
      return { status: 404, body: {} };
    });
    mount("/users");
    await waitFor(() => expect(where()).toBe("/login?next=%2Fusers&ended=signed_out_by_admin"));
    expect(await screen.findByText("An admin signed you out.")).toBeTruthy();
  });

  it("FY-WEB-022 a 429 or 500 on a page keeps you there, signed in", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: OWNER } };
      if (url === "/api/users") return { status: 429, body: { detail: { code: "rate_limited", message: "Too many requests." } }, headers: { "Retry-After": "30" } };
      if (url === "/api/auth/sessions") return { status: 500, body: {} };
      return { status: 404, body: {} };
    });
    mount("/users");
    expect(await screen.findByText(/Too many requests/)).toBeTruthy();
    expect(where()).toBe("/users");
    expect(screen.getByRole("heading", { name: "Users" })).toBeTruthy();
  });

  it("FY-WEB-023 a rate-limited sign-in says wait and stays on the page", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: null } };
      if (url === "/api/auth/login") return { status: 429, body: { detail: { code: "rate_limited", message: "Too many failed sign-ins." } }, headers: { "Retry-After": "8" } };
      return { status: 404, body: {} };
    });
    mount("/login?next=%2Fusers");
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "x" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
    expect(await screen.findByText(/about 8 s/)).toBeTruthy();
    expect(where()).toBe("/login?next=%2Fusers");
  });

  it("FY-WEB-024 an unsafe next is ignored after sign-in", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: null } };
      if (url === "/api/auth/login") return { status: 200, body: { user: OWNER } };
      return { status: 404, body: {} };
    });
    mount("/login?next=%2F%2Fevil.example");
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "owner" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "harbour-lantern-71" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign in" })));
    await waitFor(() => expect(where()).toBe("/machines"));
  });

  it("FY-WEB-025 a fresh server goes to setup; a non-admin sees 'Admins only'", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: true, registration: "off", user: null } };
      return { status: 404, body: {} };
    });
    mount("/machines");
    await waitFor(() => expect(where()).toBe("/setup"));
    cleanup();
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: { ...OWNER, role: "user", is_owner: false } } };
      return { status: 404, body: {} };
    });
    mount("/users");
    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeTruthy();
  });

  it("FY-WEB-026 the server being down shows a retry, not the sign-in page", async () => {
    server(() => ({ status: 503, body: {} }));
    mount("/machines");
    expect(await screen.findByText("Forgeyard is not answering right now.")).toBeTruthy();
    expect(where()).toBe("/machines");
  });
});

describe("ended sessions", () => {
  it("FY-WEB-027 opening the app with an ended session shows why on sign-in", async () => {
    server((url) => {
      if (url === "/api/auth/state") return { status: 200, body: { needs_setup: false, registration: "off", user: null, ended: "idle" } };
      return { status: 404, body: {} };
    });
    mount("/account");
    await waitFor(() => expect(where()).toBe("/login?next=%2Faccount&ended=idle"));
    expect(await screen.findByText(/2 hours without activity/)).toBeTruthy();
  });
});
