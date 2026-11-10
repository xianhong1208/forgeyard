import { IconBook2 } from "@tabler/icons-react";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router";

import { useAuth } from "../auth";
import { Mark } from "../components/Mark";
import { Notice } from "../components/ui";
import { api, errorText } from "../lib/api";
import { type GuideLang, type GuidePageData, isGuideLang, preferredLang, rememberLang } from "../lib/guide";

// The user guide, public like "Trust this server": it explains how to install the server and its
// agents, before anyone can sign in. The server renders the Markdown (no raw HTML, nothing from
// elsewhere), so it works offline.

const UI: Record<GuideLang, { guide: string; contents: string; onThisPage: string; back: string; signIn: string; copy: string; copied: string }> = {
  en: { guide: "Guide", contents: "Contents", onThisPage: "On this page", back: "Back to Forgeyard", signIn: "Sign in", copy: "Copy", copied: "Copied" },
  "zh-TW": { guide: "使用指南", contents: "目錄", onThisPage: "本頁內容", back: "回到 Forgeyard", signIn: "登入", copy: "複製", copied: "已複製" },
};

export function GuideRedirect() {
  const { lang } = useParams();
  return <Navigate to={`/guide/${isGuideLang(lang) ? lang : preferredLang()}/index`} replace />;
}

export function GuidePage() {
  const { lang: rawLang, slug = "index" } = useParams();
  const lang: GuideLang = isGuideLang(rawLang) ? rawLang : "en";
  const { user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [data, setData] = useState<GuidePageData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const article = useRef<HTMLDivElement>(null);
  const t = UI[lang];

  useEffect(() => {
    let alive = true;
    api<GuidePageData>(`/guide/${lang}/${encodeURIComponent(slug)}`).then(
      (d) => {
        if (!alive) return;
        setData(d);
        setError(null);
        document.title = `${d.title} · Forgeyard`;
      },
      (e) => alive && setError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [lang, slug]);

  // Copy buttons on every code block, and the section in the address jumped to once it is there.
  useEffect(() => {
    const root = article.current;
    if (!root || !data) return;
    for (const pre of Array.from(root.querySelectorAll("pre"))) {
      if (pre.parentElement?.classList.contains("guide-code")) continue;
      const wrap = document.createElement("div");
      wrap.className = "guide-code";
      pre.replaceWith(wrap);
      wrap.appendChild(pre);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-quiet btn-small guide-copy";
      button.textContent = t.copy;
      button.addEventListener("click", () => {
        void navigator.clipboard?.writeText(pre.textContent ?? "").then(() => {
          button.textContent = t.copied;
          window.setTimeout(() => (button.textContent = t.copy), 1600);
        });
      });
      wrap.appendChild(button);
    }
    const id = decodeURIComponent(location.hash.slice(1));
    if (id) document.getElementById(id)?.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [data, location.hash, t]);

  // Links between guide pages stay in the app (no reload).
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href");
    if (!a || !href || !href.startsWith("/") || a.target || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    navigate(href);
  };

  const switchTo = (to: GuideLang) => rememberLang(to);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="brand">
            <Mark size={26} />
            <span>Forgeyard</span>
          </Link>
          <span className="topbar-title"><IconBook2 size={18} stroke={1.7} aria-hidden="true" /> {t.guide}</span>
          <span className="topbar-spacer" />
          <nav className="guide-langs" aria-label="Language">
            {(data?.languages ?? [{ code: "en", name: "English", available: true }, { code: "zh-TW", name: "繁體中文", available: true }]).map((l) => (
              <Link
                key={l.code}
                to={`/guide/${l.code}/${l.available ? slug : "index"}${l.available ? location.hash : ""}`}
                className={l.code === lang ? "active" : ""}
                aria-current={l.code === lang ? "true" : undefined}
                lang={l.code}
                onClick={() => switchTo(l.code)}
              >
                {l.name}
              </Link>
            ))}
          </nav>
          <Link to={user ? "/" : "/login"} className="topbar-link">{user ? t.back : t.signIn}</Link>
        </div>
      </header>
      <main className="content guide" lang={lang}>
        {error && <Notice tone="error">{error}</Notice>}
        {data && (
          <div className="guide-layout">
            <aside className="guide-side" aria-label={t.contents}>
              <p className="eyebrow">{t.contents}</p>
              <ol className="guide-pages">
                {data.pages.map((p) => (
                  <li key={p.slug}>
                    <Link to={`/guide/${lang}/${p.slug}`} className={p.slug === slug ? "active" : ""} aria-current={p.slug === slug ? "page" : undefined}>
                      {p.title}
                    </Link>
                    {p.slug === slug && data.toc.length > 0 && (
                      <ol className="guide-toc" aria-label={t.onThisPage}>
                        {data.toc.filter((h) => h.level === 2).map((h) => (
                          <li key={h.id}>
                            <a href={`#${h.id}`} onClick={(e) => { e.preventDefault(); navigate({ hash: h.id }); }}>{h.text}</a>
                          </li>
                        ))}
                      </ol>
                    )}
                  </li>
                ))}
              </ol>
            </aside>
            <article className="guide-article" ref={article} onClick={onClick} dangerouslySetInnerHTML={{ __html: data.html }} />
          </div>
        )}
      </main>
    </div>
  );
}
