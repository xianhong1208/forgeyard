import { describe, expect, it } from "vitest";

import { HOME, loginPath, safeNext } from "./nav";

describe("safeNext", () => {
  it.each([
    ["/users", "/users"],
    ["/audit?action=auth.", "/audit?action=auth."],
    ["/account#sessions", "/account#sessions"],
  ])("FY-WEB-010 keeps same-site path %s", (input, out) => expect(safeNext(input)).toBe(out));

  it.each([
    null, "", "users", "https://evil.example", "//evil.example/x", "/\\evil.example", "javascript:alert(1)",
    "/login", "/login?next=/x", "/setup", "/set-password", "/users\n",
  ])("FY-WEB-011 refuses %s", (input) => expect(safeNext(input as string | null)).toBe(HOME));
});

describe("loginPath", () => {
  it("FY-WEB-012 carries the page and the reason", () => {
    expect(loginPath("/users?x=1", "idle")).toBe("/login?next=%2Fusers%3Fx%3D1&ended=idle");
    expect(loginPath("/login")).toBe("/login");
  });
});
