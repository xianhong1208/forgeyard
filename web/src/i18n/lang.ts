// The console's language (1.5.1, #189): English or Traditional Chinese. One choice for the whole console and
// the guide, kept in this browser only (no account setting, no migration). The first time, the browser's
// language decides: any Chinese -> zh-TW, anything else -> English.

export type Lang = "en" | "zh-TW";
export const LANGS: readonly Lang[] = ["en", "zh-TW"];
// Each language named in itself, for the switch.
export const LANG_NAME: Record<Lang, string> = { en: "English", "zh-TW": "繁體中文" };

export const LANG_KEY = "forgeyard.lang";
// Before 1.5.1 only the guide had a language choice; it carries over.
export const LEGACY_GUIDE_KEY = "forgeyard.guide.lang";

export function isLang(v: unknown): v is Lang {
  return v === "en" || v === "zh-TW";
}

// The browser's preference: the first Chinese entry wins over later ones, English over nothing.
export function detectLang(languages: readonly string[] | undefined = browserLanguages()): Lang {
  return (languages ?? []).some((l) => /^zh\b/i.test(l)) ? "zh-TW" : "en";
}

function browserLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages?.length ? navigator.languages : navigator.language ? [navigator.language] : [];
}

// When storage is blocked (some private windows), the choice lives in memory for this page only.
let memoryChoice: Lang | null = null;

export function storedLang(): Lang | null {
  try {
    const v = localStorage.getItem(LANG_KEY) ?? localStorage.getItem(LEGACY_GUIDE_KEY);
    return isLang(v) ? v : null;
  } catch {
    return memoryChoice;
  }
}

// The language in use: chosen before, else the browser's.
export function getLang(languages?: readonly string[]): Lang {
  return storedLang() ?? detectLang(languages ?? browserLanguages());
}

const listeners = new Set<() => void>();

export function subscribeLang(fn: () => void): () => void {
  listeners.add(fn);
  // Another tab changed it.
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key === LANG_KEY || e.key === LEGACY_GUIDE_KEY) fn();
  };
  if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(fn);
    if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
  };
}

export function setLang(lang: Lang): void {
  memoryChoice = lang;
  try {
    localStorage.setItem(LANG_KEY, lang);
    localStorage.removeItem(LEGACY_GUIDE_KEY);
  } catch {
    // kept in memory only
  }
  applyHtmlLang(lang);
  for (const fn of [...listeners]) fn();
}

// <html lang>: screen readers pick their voice by it, and the browser its CJK fonts.
export function applyHtmlLang(lang: Lang = getLang()): void {
  if (typeof document !== "undefined") document.documentElement.lang = lang;
}

// Intl wants a locale; ours are already BCP 47 tags.
export function locale(lang: Lang): string {
  return lang;
}
