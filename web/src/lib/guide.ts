// The user guide (docs/guide, rendered by the server at /api/guide): which language to show, and
// links into it from the pages ("How do I…?"). Since 1.5.1 the guide and the console share one language
// choice (i18n/lang.ts).
import { getLang, isLang, LANGS, setLang, type Lang } from "../i18n/lang";

export type GuideLang = Lang;
export const GUIDE_LANGS: GuideLang[] = [...LANGS];

export function isGuideLang(v: string | undefined): v is GuideLang {
  return isLang(v);
}

// The language chosen before (on the guide or in the console), else the browser's (Chinese -> zh-TW), else English.
export function preferredLang(languages?: readonly string[]): GuideLang {
  return getLang(languages);
}

export function rememberLang(lang: GuideLang): void {
  setLang(lang);
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
