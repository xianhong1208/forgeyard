import { describe, expect, it } from "vitest";

import { sessionLength } from "./Account";

describe("my account", () => {
  it("FY-WEB-139 says the idle limit the way people say it", () => {
    expect(sessionLength(120)).toBe("2 hours");
    expect(sessionLength(60)).toBe("1 hour");
    expect(sessionLength(90)).toBe("90 minutes");
    expect(sessionLength(1)).toBe("1 minute");
  });
});
