// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { documentTitle, useDocumentTitle } from "./title";

function Page({ title }: { title?: string | null }) {
  useDocumentTitle(title);
  return null;
}

afterEach(cleanup);

describe("tab title", () => {
  it("FY-WEB-137 names the page before the product, and only the product without a page name", () => {
    expect(documentTitle("Machines")).toBe("Machines · Forgeyard");
    expect(documentTitle("  ")).toBe("Forgeyard");
    expect(documentTitle(null)).toBe("Forgeyard");
    expect(documentTitle("Forgeyard")).toBe("Forgeyard");
  });

  it("FY-WEB-138 sets the title while the page is shown and puts the product name back when it goes", () => {
    document.title = "Guide · Forgeyard";
    const view = render(<Page title="Usage" />);
    expect(document.title).toBe("Usage · Forgeyard");
    view.rerender(<Page title="API keys" />);
    expect(document.title).toBe("API keys · Forgeyard");
    view.unmount();
    expect(document.title).toBe("Forgeyard");
  });
});
