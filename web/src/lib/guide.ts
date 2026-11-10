// The user guide (docs/guide, rendered by the server at /api/guide): which language to show, and
// links into it from the pages ("How do I…?").

export type GuideLang = "en" | "zh-TW";
export const GUIDE_LANGS: GuideLang[] = ["en", "zh-TW"];
const KEY = "forgeyard.guide.lang";

export function isGuideLang(v: string | undefined): v is GuideLang {
  return v === "en" || v === "zh-TW";
}

// The language chosen on the guide before, else the browser's (Chinese -> zh-TW), else English.
export function preferredLang(languages: readonly string[] = navigator.languages ?? [navigator.language]): GuideLang {
  try {
    const saved = localStorage.getItem(KEY) ?? undefined;
    if (isGuideLang(saved)) return saved;
  } catch {
    // storage blocked: fall back to the browser's language
  }
  return languages.some((l) => /^zh\b/i.test(l)) ? "zh-TW" : "en";
}

export function rememberLang(lang: GuideLang): void {
  try {
    localStorage.setItem(KEY, lang);
  } catch {
    // not remembered; nothing else depends on it
  }
}

export function guideHref(page: string, anchor?: string, lang: GuideLang = preferredLang()): string {
  return `/guide/${lang}/${page}${anchor ? `#${anchor}` : ""}`;
}

export type GuidePageData = {
  lang: GuideLang;
  slug: string;
  title: string;
  html: string;
  toc: { id: string; text: string; level: number }[];
  pages: { slug: string; title: string }[];
  languages: { code: GuideLang; name: string; available: boolean }[];
};
