// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import App from "../App";
import { AuthProvider } from "../auth";
import { guideHref, preferredLang, rememberLang } from "../lib/guide";

const PAGES = [{ slug: "index", title: "Forgeyard guide" }, { slug: "quick-start", title: "Quick start" }, { slug: "how-to", title: "How-to" }];
const LANGS = [{ code: "en", name: "English", available: true }, { code: "zh-TW", name: "繁體中文", available: true }];

function page(lang: string, slug: string) {
  const title = slug === "how-to" ? (lang === "en" ? "How-to" : "操作指南") : "Quick start";
  return {
    lang, slug, title, pages: PAGES, languages: LANGS,
    toc: [{ id: "run-the-agent-in-docker", text: "Run the agent in Docker", level: 2 }],
    html: `<h1 id="t">${title}</h1><h2 id="run-the-agent-in-docker">Run the agent in Docker</h2>`
      + `<pre><code class="language-bash">docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .\n</code></pre>`
      + `<p><a href="/guide/${lang}/quick-start#3-add-a-machine">Add a machine</a> <a href="https://docs.astral.sh/uv/" target="_blank" rel="noopener noreferrer">uv</a></p>`,
  };
}

function server(user: unknown = null) {
  const fetch = vi.fn(async (url: string) => {
    if (url === "/api/auth/state") return new Response(JSON.stringify({ needs_setup: false, registration: "off", user }));
    const m = /^\/api\/guide\/([^/]+)\/([^/]+)$/.exec(url);
    if (m) return new Response(JSON.stringify(page(m[1], m[2])));
    return new Response("{}", { status: 404 });
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
  localStorage.clear();
});

describe("guide", () => {
  it("FY-WEB-120 shows a page without signing in, with copy buttons, contents and a language switch", async () => {
    const fetch = server();
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    mount("/guide/en/how-to");
    expect(await screen.findByRole("heading", { level: 1, name: "How-to" })).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => u === "/api/guide/en/how-to")).toBe(true);
    const contents = screen.getByRole("complementary", { name: "Contents" });
    expect(within(contents).getByRole("link", { name: "How-to" }).getAttribute("aria-current")).toBe("page");
    expect(within(contents).getByRole("link", { name: "Run the agent in Docker" })).toBeTruthy();
    // every code block gets a Copy button that copies the block as it is
    fireEvent.click(await screen.findByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("docker build -f Dockerfile.agent -t forgeyard-agent:1.1.0 .\n"));
    // the same page in the other language, and a way back to sign in
    const zh = screen.getByRole("link", { name: "繁體中文" });
    expect(zh.getAttribute("href")).toBe("/guide/zh-TW/how-to");
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/login");
    fireEvent.click(zh);
    expect(await screen.findByRole("heading", { level: 1, name: "操作指南" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "複製" })).toBeTruthy();
    expect(preferredLang(["en-US"])).toBe("zh-TW"); // the choice is remembered
  });

  it("FY-WEB-121 links between guide pages stay in the app; /guide opens the reader's language", async () => {
    const fetch = server();
    mount("/guide");
    expect(await screen.findByRole("heading", { level: 1 })).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => u === "/api/guide/en/index")).toBe(true);
    fireEvent.click(screen.getByRole("link", { name: "Add a machine" }));
    await waitFor(() => expect(fetch.mock.calls.some(([u]) => u === "/api/guide/en/quick-start")).toBe(true));
    expect(screen.getByRole("link", { name: "uv" }).getAttribute("target")).toBe("_blank");
  });

  it("FY-WEB-122 help links follow the reader's language: chosen, else the browser's", () => {
    expect(preferredLang(["zh-TW", "en"])).toBe("zh-TW");
    expect(preferredLang(["zh-CN"])).toBe("zh-TW");
    expect(preferredLang(["en-GB"])).toBe("en");
    rememberLang("en");
    expect(preferredLang(["zh-TW"])).toBe("en");
    expect(guideHref("troubleshooting", "an-agent-shows-offline")).toBe("/guide/en/troubleshooting#an-agent-shows-offline");
    rememberLang("zh-TW");
    expect(guideHref("quick-start")).toBe("/guide/zh-TW/quick-start");
  });
});
