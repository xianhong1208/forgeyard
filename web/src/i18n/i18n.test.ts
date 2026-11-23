import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, errorText } from "../lib/api";
import { findingTitle, fixLabel, summary } from "../lib/advisor";
import { MACHINE_KIND, machineCount } from "../lib/fleet";
import { guideHref, preferredLang, rememberLang } from "../lib/guide";
import { bytes } from "../lib/machines";
import { hasKey, interpolate, labels, t, tPlural, translate, translatePlural } from "./core";
import { EN_MODULES, ZH_TW_MODULES, en, zhTW } from "./dictionaries";
import { formatAgo, formatBytes, formatDate, formatDuration, formatNumber, formatRelative, pluralForm } from "./format";
import { LANG_KEY, LEGACY_GUIDE_KEY, detectLang, getLang, setLang, subscribeLang } from "./lang";
import { SAME_IN_BOTH } from "./sameInBoth";
import { eventKindLabel, serverMessage } from "./server";

type Dict = Record<string, string>;
// Every module file on disk, found on its own: a page group added later is checked without editing this test.
const EN_FILES = import.meta.glob<{ default: Dict }>("./en/*.ts", { eager: true });
const ZH_FILES = import.meta.glob<{ default: Dict }>("./zh-TW/*.ts", { eager: true });
const name = (path: string) => path.replace(/^.*\/([^/]+)\.ts$/, "$1");
const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
const tags = (s: string) => [...s.matchAll(/<(\w+)>/g)].map((m) => m[1]).sort();
// A zh-TW text with Latin words must also have Chinese in it (outside its placeholders and tags), or it was left
// in English; punctuation alone ("、") is fine.
const HAN = /\p{Script=Han}/u;
const LATIN = /[A-Za-z]/;
const bareText = (s: string) => s.replace(/\{\w+\}/g, "").replace(/<\/?\w+>/g, "");

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("dictionaries", () => {
  it("FY-WEB-656 every area has an English and a Traditional Chinese module, both registered", () => {
    const enNames = Object.keys(EN_FILES).map(name).sort();
    const zhNames = Object.keys(ZH_FILES).map(name).sort();
    expect(zhNames).toEqual(enNames);
    expect(Object.keys(EN_MODULES).sort()).toEqual(enNames);
    expect(Object.keys(ZH_TW_MODULES).sort()).toEqual(enNames);
    for (const [path, mod] of Object.entries(EN_FILES)) expect(EN_MODULES[name(path) as keyof typeof EN_MODULES]).toBe(mod.default);
    for (const [path, mod] of Object.entries(ZH_FILES)) expect(ZH_TW_MODULES[name(path) as keyof typeof ZH_TW_MODULES]).toBe(mod.default);
  });

  it("FY-WEB-657 the two languages have the same keys, each key in its own module's namespace, none twice", () => {
    const seen = new Map<string, string>();
    for (const [path, mod] of Object.entries(EN_FILES)) {
      const area = name(path);
      const zh = ZH_FILES[path.replace("/en/", "/zh-TW/")].default;
      const missing = Object.keys(mod.default).filter((k) => !(k in zh));
      const extra = Object.keys(zh).filter((k) => !(k in mod.default));
      expect({ area, missing, extra }).toEqual({ area, missing: [], extra: [] });
      for (const k of Object.keys(mod.default)) {
        expect(k.startsWith(`${area}.`), `${k} is not in ${area}.*`).toBe(true);
        expect(seen.get(k), `${k} is defined twice`).toBeUndefined();
        seen.set(k, area);
      }
    }
    expect(Object.keys(en).length).toBe(seen.size);
    expect(Object.keys(zhTW).sort()).toEqual(Object.keys(en).sort());
  });

  it("FY-WEB-658 a translation keeps every {placeholder} and <tag>, is not empty, and is not left in English", () => {
    const problems: string[] = [];
    for (const [k, enText] of Object.entries(en) as [string, string][]) {
      const zh = (zhTW as Dict)[k];
      if (!zh.trim()) problems.push(`${k}: empty`);
      if (placeholders(zh).join() !== placeholders(enText).join()) problems.push(`${k}: placeholders ${placeholders(zh)} vs ${placeholders(enText)}`);
      if (tags(zh).join() !== tags(enText).join()) problems.push(`${k}: tags ${tags(zh)} vs ${tags(enText)}`);
      const bare = bareText(zh);
      if (!SAME_IN_BOTH.includes(k) && LATIN.test(bare) && !HAN.test(bare)) problems.push(`${k}: no Chinese in "${zh}"`);
    }
    // The exceptions stay real: each one exists and would fail the rule without its entry.
    for (const k of SAME_IN_BOTH) {
      const bare = hasKey(k) ? bareText((zhTW as Dict)[k]) : "";
      if (!hasKey(k)) problems.push(`${k}: listed in SAME_IN_BOTH but not a key`);
      else if (HAN.test(bare) || !LATIN.test(bare)) problems.push(`${k}: listed in SAME_IN_BOTH but needs no exception`);
    }
    expect(problems).toEqual([]);
  });

  it("FY-WEB-659 every plural has its .other form in both languages", () => {
    for (const k of Object.keys(en)) {
      if (/\.(zero|one|two|few|many)$/.test(k)) expect(hasKey(k.replace(/\.\w+$/, ".other")), k).toBe(true);
    }
  });
});

describe("t()", () => {
  it("FY-WEB-660 fills {placeholders}, numbers in the reader's digits; an unknown name stays visible", () => {
    expect(translate("en", "machines.joined", { names: "gpu-01, gpu-02" })).toBe("gpu-01, gpu-02 joined.");
    expect(translate("zh-TW", "machines.joined", { names: "gpu-01" })).toBe("gpu-01 已加入。");
    expect(translate("en", "machines.running", { count: 12345 })).toBe("12,345 running");
    expect(interpolate("{a} and {b}", { a: "x" }, "en")).toBe("x and {b}");
    expect(translate("zh-TW", "shell.nav.machines")).toBe("機器");
  });

  it("FY-WEB-661 plurals follow Intl.PluralRules: one and other in English, one form in Chinese", () => {
    expect(translatePlural("en", "machines.unused", 1)).toBe("1 unused join token:");
    expect(translatePlural("en", "machines.unused", 2)).toBe("2 unused join tokens:");
    expect(translatePlural("zh-TW", "machines.unused", 1)).toBe("1 個未使用的加入權杖：");
    expect(pluralForm(1, "en")).toBe("one");
    expect(pluralForm(1, "zh-TW")).toBe("other");
    expect(machineCount("cpu", 1)).toBe("CPU-only machine");
    expect(machineCount("cpu", 2)).toBe("CPU-only machines");
    expect(summary({ error: 1, warning: 2, info: 0 })).toBe("1 error, 2 warnings");
    setLang("zh-TW");
    expect(summary({ error: 1, warning: 2, info: 0 })).toBe("1 個錯誤、2 個警告");
    expect(tPlural("machines.unused", 3)).toBe("3 個未使用的加入權杖：");
  });

  it("FY-WEB-662 tables of labels follow the language each time they are read", () => {
    const table = labels({ a: "shell.nav.machines", b: "shell.nav.serving" });
    expect(Object.values(table)).toEqual(["Machines", "Serving"]);
    expect(MACHINE_KIND.cpu.label).toBe("CPU only");
    setLang("zh-TW");
    expect(Object.values(table)).toEqual(["機器", "服務中"]);
    expect(MACHINE_KIND.cpu.label).toBe("僅 CPU");
    expect(t("common.copy")).toBe("複製");
  });
});

describe("the language", () => {
  it("FY-WEB-663 the first time, any Chinese browser language means zh-TW, anything else English", () => {
    expect(detectLang(["zh-TW", "en"])).toBe("zh-TW");
    expect(detectLang(["en-US", "zh-CN"])).toBe("zh-TW");
    expect(detectLang(["zh"])).toBe("zh-TW");
    expect(detectLang(["ZH-hk"])).toBe("zh-TW");
    expect(detectLang(["en-GB", "ja"])).toBe("en");
    expect(detectLang(["zhx"])).toBe("en");
    expect(detectLang([])).toBe("en");
    expect(getLang(["zh-TW"])).toBe("zh-TW");
    expect(getLang(["fr"])).toBe("en");
  });

  it("FY-WEB-664 a choice wins over the browser, is kept in this browser, and tells whoever listens", () => {
    const heard = vi.fn();
    const stop = subscribeLang(heard);
    setLang("zh-TW");
    expect(localStorage.getItem(LANG_KEY)).toBe("zh-TW");
    expect(getLang(["en-US"])).toBe("zh-TW");
    expect(document.documentElement.lang).toBe("zh-TW");
    expect(heard).toHaveBeenCalledTimes(1);
    setLang("en");
    expect(getLang(["zh-TW"])).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    stop();
    setLang("zh-TW");
    expect(heard).toHaveBeenCalledTimes(2);
  });

  it("FY-WEB-665 the guide and the console share one choice; the guide's choice from before 1.5.1 carries over", () => {
    setLang("en"); // chosen in the console before
    rememberLang("zh-TW"); // then the guide's language links
    expect(getLang(["en-US"])).toBe("zh-TW");
    expect(localStorage.getItem(LANG_KEY)).toBe("zh-TW");
    expect(guideHref("how-to")).toBe("/guide/zh-TW/how-to");
    setLang("en"); // the console's switch
    expect(preferredLang(["zh-TW"])).toBe("en");
    expect(guideHref("how-to")).toBe("/guide/en/how-to");
    localStorage.clear();
    localStorage.setItem(LEGACY_GUIDE_KEY, "zh-TW");
    expect(getLang(["en-US"])).toBe("zh-TW");
    setLang("en");
    expect(localStorage.getItem(LEGACY_GUIDE_KEY)).toBeNull();
    expect(localStorage.getItem(LANG_KEY)).toBe("en");
  });

  it("FY-WEB-666 with storage blocked the choice still holds for the page", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    setLang("zh-TW");
    expect(getLang(["en-US"])).toBe("zh-TW");
    setLang("en");
    expect(getLang(["zh-TW"])).toBe("en");
  });
});

describe("Intl", () => {
  it("FY-WEB-667 sizes, numbers, durations and times in each language", () => {
    expect(formatBytes(85899345920, "en")).toBe("80 GiB");
    expect(formatBytes(4023779328, "en")).toBe("3.7 GiB");
    expect(formatBytes(536870912, "zh-TW")).toBe("512 MiB");
    expect(formatBytes(1000 * 1024 ** 2, "en")).toBe("1000 MiB");
    expect(formatBytes(null, "en")).toBe("—");
    expect(bytes(4023779328)).toBe("3.7 GiB");
    expect(formatNumber(1234567, "zh-TW")).toBe("1,234,567");
    expect(formatDuration(41, "en")).toBe("41 s");
    expect(formatDuration(300, "en")).toBe("5 min");
    expect(formatDuration(300, "zh-TW")).toBe("5 分鐘");
    expect(formatDuration(7200, "zh-TW")).toBe("2 小時");
    expect(formatAgo(41, "en")).toBe("41 s ago");
    expect(formatAgo(41, "zh-TW")).toBe("41 秒前");
    expect(formatAgo(3 * 86400, "zh-TW")).toBe("3 天前");
    expect(formatAgo(null, "en")).toBe("never");
    expect(formatAgo(null, "zh-TW")).toBe("從未");
  });

  it("FY-WEB-668 times relative to now: the console's English words, Intl's Chinese ones, the date past two weeks", () => {
    const now = Date.parse("2026-11-23T12:00:00Z");
    const ago = (s: number) => new Date(now - s * 1000).toISOString();
    expect(formatRelative(ago(10), "en", now)).toBe("just now");
    expect(formatRelative(ago(10), "zh-TW", now)).toBe("剛剛");
    expect(formatRelative(ago(50), "en", now)).toBe("1 min ago");
    expect(formatRelative(ago(600), "en", now)).toBe("10 min ago");
    expect(formatRelative(ago(600), "zh-TW", now)).toBe("10 分鐘前");
    expect(formatRelative(ago(-7200), "en", now)).toBe("2 h from now");
    expect(formatRelative(ago(-7200), "zh-TW", now)).toBe("2 小時後");
    expect(formatRelative(ago(30 * 86400), "zh-TW", now)).toBe(formatDate(ago(30 * 86400), "zh-TW"));
    expect(formatDate("2026-11-09T08:00:00Z", "zh-TW")).toContain("2026年11月");
    expect(formatRelative(null, "en", now)).toBe("—");
  });
});

describe("server messages", () => {
  it("FY-WEB-669 English shows the server's message; zh-TW translates a known code and keeps its specifics", () => {
    expect(serverMessage("machine_offline", "gpu-01's agent is not connected.", "en")).toBe("gpu-01's agent is not connected.");
    expect(serverMessage("machine_offline", "gpu-01's agent is not connected.", "zh-TW"))
      .toBe("那台機器的 agent 沒有連線，等它上線後再試一次：gpu-01's agent is not connected.");
    expect(serverMessage("invalid_credentials", "Wrong username or password.", "zh-TW")).toBe("使用者名稱或密碼錯誤。");
    expect(serverMessage("model_waking", null, "zh-TW")).toBe("模型正在喚醒，請稍後再試");
  });

  it("FY-WEB-670 a code without a translation is shown as the server sent it, and nothing at all gets a generic line", () => {
    expect(serverMessage("some_new_code", "A brand-new problem.", "zh-TW")).toBe("A brand-new problem.");
    expect(serverMessage(undefined, null, "zh-TW")).toBe("發生了問題。");
    expect(serverMessage(undefined, null, "en")).toBe("Something went wrong.");
    expect(eventKindLabel("woken", "zh-TW")).toBe("已喚醒");
    expect(eventKindLabel("something_new", "zh-TW")).toBe("something_new");
  });

  it("FY-WEB-671 errorText: unchanged in English, translated by code in zh-TW", () => {
    const limited = new ApiError(429, { code: "rate_limited", message: "Too many tries. Wait a moment." }, 30);
    const busy = new ApiError(503, { code: "x", message: "boom" });
    const offline = new ApiError(0, { code: "network", message: "Cannot reach the Forgeyard server." });
    const bare = new ApiError(404, {});
    expect(errorText(limited, "en")).toBe("Too many tries. Wait a moment. (about 30 s)");
    expect(errorText(limited, "zh-TW")).toBe("請求太多次了，請稍等一下。（約 30 秒後）");
    expect(errorText(busy, "en")).toBe("The server had a problem. Try again in a moment.");
    expect(errorText(busy, "zh-TW")).toBe("伺服器發生問題，請稍後再試。");
    expect(errorText(offline, "zh-TW")).toBe("連不到 Forgeyard 伺服器。");
    expect(errorText(bare, "en")).toBe("Request failed (404)");
    expect(errorText(new Error("x"), "zh-TW")).toBe("發生了問題。");
  });

  it("FY-WEB-672 a setup-health finding: the server's title in English, its kind named in zh-TW with the server's words kept", () => {
    const f = { id: "machine-offline-with-replicas", title: "gpu-01 is offline with replicas on it" };
    expect(findingTitle(f, "en")).toEqual({ title: f.title, translated: false });
    expect(findingTitle(f, "zh-TW")).toEqual({ title: "有機器離線，但上面還有副本", translated: true });
    expect(findingTitle({ id: "a-check-from-a-newer-server", title: "Something new" }, "zh-TW")).toEqual({ title: "Something new", translated: false });
    expect(fixLabel("Open Machines")).toBe("Open Machines");
    setLang("zh-TW");
    expect(fixLabel("Open Machines")).toBe("開啟機器");
    expect(fixLabel("Open the new page")).toBe("Open the new page");
  });
});
