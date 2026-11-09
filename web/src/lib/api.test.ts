import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, setUnauthorizedHandler } from "./api";

function reply(status: number, body: unknown, headers: Record<string, string> = {}) {
  return vi.fn().mockImplementation(async () => new Response(JSON.stringify(body), { status, headers }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("api()", () => {
  it("FY-WEB-001 sends the CSRF header on writes only", async () => {
    const fetch = reply(200, {});
    vi.stubGlobal("fetch", fetch);
    await api("/x", { method: "POST", body: { a: 1 } });
    await api("/x");
    expect(fetch.mock.calls[0][1].headers["X-Forgeyard"]).toBe("1");
    expect(fetch.mock.calls[1][1].headers["X-Forgeyard"]).toBeUndefined();
  });

  it("FY-WEB-002 a 401 calls the sign-in handler with the reason", async () => {
    vi.stubGlobal("fetch", reply(401, { detail: { code: "session_ended", reason: "idle" } }));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(api("/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledWith("idle");
  });

  it.each([403, 429, 500, 502, 503])("FY-WEB-003 a %i never signs out", async (status) => {
    vi.stubGlobal("fetch", reply(status, { detail: { code: "x", message: "nope" } }, { "Retry-After": "7" }));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const err = (await api("/users").catch((e) => e)) as ApiError;
    expect(err.status).toBe(status);
    expect(handler).not.toHaveBeenCalled();
  });

  it("FY-WEB-004 sign-in calls opt out of the handler (a wrong password is not an ended session)", async () => {
    vi.stubGlobal("fetch", reply(401, { detail: { code: "invalid_credentials" } }));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await api("/auth/login", { method: "POST", body: {}, expect401: true }).catch(() => undefined);
    expect(handler).not.toHaveBeenCalled();
  });

  it("FY-WEB-005 network failure is an error, not a sign-out", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const err = (await api("/auth/me").catch((e) => e)) as ApiError;
    expect(err.status).toBe(0);
    expect(handler).not.toHaveBeenCalled();
  });

  it("FY-WEB-006 reads Retry-After", async () => {
    vi.stubGlobal("fetch", reply(429, { detail: { code: "rate_limited", message: "Slow down" } }, { "Retry-After": "12" }));
    const err = (await api("/auth/login", { method: "POST", body: {}, expect401: true }).catch((e) => e)) as ApiError;
    expect(err.retryAfter).toBe(12);
  });
});
