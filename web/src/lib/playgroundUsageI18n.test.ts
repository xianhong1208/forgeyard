// The Playground's and Usage's own words in Traditional Chinese (1.5.1, #189): the lib helpers behind the pages,
// with the English output unchanged (FY-WEB-780 to 789).
import { afterEach, describe, expect, it } from "vitest";

import { setLang } from "../i18n/lang";
import { METRICS, SPLITS, STEP_HEAD, STEP_WORD, VIEWS, formatTick, formatValue, periodLabel, periodTitle, rowName, textUnits } from "./charts";
import { answerError, elapsed } from "./gateway";
import { clock, imageProblem, pickerDetail, playError } from "./playground";
import { TASK_HELP, ZERO_SHOT_HELP, checkImage, parseLabels, targets } from "./vision";
import type { RouteInfo } from "./types";
import { visionError } from "../pages/PlaygroundVision";
import { audioLength, busyTime, megapixels } from "../pages/Usage";

afterEach(() => {
  localStorage.clear();
  setLang("en");
  localStorage.clear();
});

const file = (name: string, type: string, size: number) => {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
};

describe("the Playground's helpers", () => {
  it("FY-WEB-780 a request's error reads like answerError in English, and in Chinese by its code with the server's specifics", () => {
    const bodies: [number, unknown][] = [
      [503, { error: { code: "no_ready_replica", message: "no replica of chat is ready" } }],
      [429, { detail: { code: "rate_limited", message: "Too many requests" } }],
      [400, { error: { code: "something_new", message: "a sentence only the server has" } }],
      [500, null], [404, {}], [502, { error: {} }],
    ];
    for (const [status, body] of bodies) expect(playError(status, body, "en")).toBe(answerError(status, body));
    expect(playError(503, bodies[0][1], "zh-TW")).toBe("503：這個模型沒有就緒的副本：no replica of chat is ready");
    expect(playError(429, bodies[1][1], "zh-TW")).toMatch(/^429：\p{Script=Han}/u);
    expect(playError(400, bodies[2][1], "zh-TW")).toBe("400：a sentence only the server has");   // no translation: as sent
    expect(playError(500, null, "zh-TW")).toBe("500：伺服器發生問題。");
    expect(playError(404, {}, "zh-TW")).toBe("請求失敗（404）。");
  });

  it("FY-WEB-781 an image that cannot be attached, and a video's elapsed time, in the reader's words", () => {
    expect(imageProblem(file("a.txt", "text/plain", 10))).toBe("a.txt is not an image.");
    expect(imageProblem(file("big.png", "image/png", 21 * 1024 * 1024))).toBe("big.png is larger than 20 MiB.");
    expect(imageProblem(file("ok.png", "image/png", 1000))).toBeNull();
    for (const s of [0, 9.6, 59, 60, 65, 3599, 3725]) expect(clock(s)).toBe(elapsed(s));
    setLang("zh-TW");
    expect(imageProblem(file("a.txt", "text/plain", 10))).toBe("a.txt 不是圖片。");
    expect(imageProblem(file("big.png", "image/png", 21 * 1024 * 1024))).toBe("big.png 超過 20 MiB。");
    expect(clock(45)).toBe("45 秒");
    expect(clock(65)).toBe("1 分 05 秒");
  });

  it("FY-WEB-782 the picker's small print names where answers come from, in the language in use", () => {
    const r = (api: string, kind: string) => ({ api, kind, served_name: "m", endpoints: [] }) as unknown as RouteInfo;
    expect(pickerDetail(r("openai-chat", "mixed"))).toBe("local, then cloud");
    expect(pickerDetail(r("vision-detect", "cloud"))).toBe("object detection · cloud only");
    expect(pickerDetail(r("openai-chat", "local"))).toBe("");
    setLang("zh-TW");
    expect(pickerDetail(r("openai-chat", "mixed"))).toBe("先本機，再雲端");
    expect(pickerDetail(r("vision-detect", "cloud"))).toBe("物件偵測 · 僅雲端");
  });

  it("FY-WEB-783 the vision panel's checks and help follow the language; the call line keeps the path and model", () => {
    expect(parseLabels(" ").error).toBe("Name at least one thing to find, e.g. cat, remote control.");
    expect(parseLabels(Array.from({ length: 33 }, (_, i) => `x${i}`).join(",")).error).toBe("At most 32 things at once.");
    expect(parseLabels("a".repeat(70)).error).toBe(`"${"a".repeat(20)}…" is longer than 64 characters.`);
    expect(checkImage(file("a.gif", "image/gif", 10), 20)).toBe("a.gif is not a PNG, JPEG or WebP image.");
    expect(checkImage(file("", "image/png", 30 * 1024 * 1024), 20)).toBe("The image is 30.0 MiB: the most it takes is 20 MiB.");
    const opts = { tasks: [], models: [{ name: "det", task: "detect" as const, title: "Object detection", endpoint: "/v1/vision/detect", deployment_id: 4, model: "m",
      licence: null, ready: 1, running: true, reason: null }] };
    expect(targets(opts)[0].call).toBe("POST /v1/vision/detect with model=det");
    expect(TASK_HELP.depth).toBe("Estimates how near each pixel is and shows it as a depth map beside the photo.");
    setLang("zh-TW");
    expect(parseLabels("").error).toBe("請至少指定一樣要找的東西，例如 cat, remote control。");
    expect(parseLabels(Array.from({ length: 33 }, (_, i) => `x${i}`).join(",")).error).toBe("一次最多 32 個。");
    expect(checkImage(file("a.gif", "image/gif", 10), 20)).toBe("a.gif 不是 PNG、JPEG 或 WebP 圖片。");
    expect(checkImage(file("", "image/png", 30 * 1024 * 1024), 20)).toBe("這張圖片 有 30.0 MiB：最多只能 20 MiB。");
    expect(targets(opts)[0].call).toBe("POST /v1/vision/detect（參數 model=det）");
    expect(targets(opts)[0].link?.label).toBe("部署");
    expect(TASK_HELP.depth).toBe("估計每個像素的遠近，並在照片旁顯示成深度圖。");
    expect(ZERO_SHOT_HELP.detect).toBe("找出你指定的東西，並為每一個畫出方框、標籤和分數。");
  });

  it("FY-WEB-784 the vision server's error codes in Chinese; English unchanged, and an unknown code as answerError says it", () => {
    const err = (code: string, message?: string) => ({ error: { code, message } });
    expect(visionError(413, err("image_too_large"), 20, "model")).toBe("The image is too large for this model: it takes at most 20 MiB.");
    expect(visionError(503, err("no_ready_replica"), 20, "model")).toBe("The model is not ready: no replica is running.");
    expect(visionError(400, err("labels_required", "This model classifies among labels"), 20, "model"))
      .toBe("This model chooses among the labels you give: type two or more, separated by commas.");
    expect(visionError(418, err("teapot", "short and stout"), 20, "model")).toBe("418: short and stout");
    setLang("zh-TW");
    expect(visionError(413, err("image_too_large"), 20, "model")).toBe("圖片對這個模型來說太大：最多只能 20 MiB。");
    expect(visionError(503, err("key_busy"), 20, "app")).toBe("這個應用程式正忙著處理其他圖片。請稍後再試。");
    expect(visionError(503, err("no_ready_replica"), 20, "model")).toBe("這個模型尚未就緒：沒有執行中的副本。");
    expect(visionError(400, err("labels_not_supported", "This classifier has fixed classes"), 20, "model")).toBe("這個分類器只認得固定的類別，不能指定標籤。");
    expect(visionError(400, err("labels_not_supported", "This detector has fixed classes"), 20, "model")).toBe("這個偵測器只認得固定的類別，不能指定要找什麼。");
    expect(visionError(404, err("model_not_found"), 20, "model")).toBe("這個模型已不再部署。重新載入頁面即可看到目前部署的模型。");
  });
});

describe("Usage's charts", () => {
  it("FY-WEB-785 periods in Chinese come from Intl (month, day, weekday, a week as a range); English is unchanged", () => {
    const day = { start: "2026-11-12", end: "2026-11-12", partial: false };
    const week = { start: "2026-12-28", end: "2027-01-03", partial: false };
    const month = { start: "2026-11-01", end: "2026-11-15", partial: true };
    expect([periodLabel(day, "day", "en"), periodTitle(day, "day", "en")]).toEqual(["Nov 12", "Thu, Nov 12, 2026"]);
    expect(periodTitle(week, "week", "en")).toBe("Dec 28 – Jan 3, 2027");
    expect([periodLabel(month, "month", "en"), periodTitle(month, "month", "en")]).toEqual(["Nov 2026", "November 2026 (so far)"]);
    expect(periodLabel(day, "day", "zh-TW")).toBe("11月12日");
    expect(periodTitle(day, "day", "zh-TW")).toMatch(/^2026年11月12日 ?週四$/);   // ICU versions differ on the space
    expect(periodTitle({ start: "2026-11-09", end: "2026-11-15", partial: true }, "week", "zh-TW")).toBe("2026年11月9日至11月15日（至今）");
    expect(periodTitle(week, "week", "zh-TW")).toBe("2026年12月28日至2027年1月3日");   // across a year: the year twice
    expect(periodLabel(month, "month", "zh-TW")).toBe("2026年11月");
    expect(periodTitle(month, "month", "zh-TW")).toBe("2026年11月（至今）");
  });

  it("FY-WEB-786 axis and table values in Chinese: compact numbers and Intl's time units; dollars as the tables write them; bytes stay", () => {
    const en = [formatTick(1500, "count"), formatTick(90_000, "duration"), formatValue(7_200_000, "duration"), formatValue(12345, "count"), formatValue(0, "duration")];
    expect(en).toEqual(["1.5K", "1.5 min", "2 h", "12,345", "0 s"]);
    setLang("zh-TW");
    expect(formatTick(15000, "count")).toBe("1.5萬");
    expect(formatTick(90_000, "duration")).toBe("1.5 分鐘");
    expect(formatValue(7_200_000, "duration")).toBe("2 小時");
    expect(formatValue(12_300, "duration")).toBe("12 秒");
    expect(formatValue(0, "duration")).toBe("0 秒");
    expect(formatValue(12345, "count")).toBe("12,345");
    expect(formatTick(250_000, "usd")).toBe("US$0.25");   // as the tables write dollars in Chinese
    expect(formatTick(0, "usd")).toBe("US$0");
    expect(formatValue(3 * 1024 * 1024, "bytes")).toBe("3 MiB");
  });

  it("FY-WEB-787 the names the server gives some groups are translated by their id; every other name stays as it is", () => {
    const named: [string, string][] = [["total", "Total"], ["console", "Console playground"], ["api", "API keys"], ["local", "Local replicas"],
      ["deleted", "Deleted providers"], ["other", "Other (3)"], ["9", "deleted key #9"], ["12", "user #12"], ["pm:4", "deleted provider model #4"],
      ["qwen3-4b", "qwen3-4b"], ["Total", "Total"], ["console", "Console"]];
    for (const [id, name] of named) expect(rowName(id, name, "en")).toBe(name);
    expect(named.map(([id, name]) => rowName(id, name, "zh-TW"))).toEqual(["總計", "主控台試用區", "API 金鑰", "本機副本", "已刪除的供應商", "其他（3）",
      "已刪除的金鑰 #9", "使用者 #12", "已刪除的供應商模型 #4", "qwen3-4b", "Total", "Console"]);
    expect(rowName("7", "Other (3)", "zh-TW")).toBe("Other (3)");   // a key someone named so
  });

  it("FY-WEB-788 the views, splits, metrics and steps follow the language when read; CJK text is measured wider", () => {
    expect(VIEWS.map((v) => v.label)).toEqual(["Daily", "Weekly", "Monthly"]);
    expect(VIEWS[1].range).toBe("the last 13 weeks");
    expect(SPLITS.map((s) => s.label)).toEqual(["Total", "Key", "Model", "Provider", "Person"]);
    expect(SPLITS.find((s) => s.id === "user")?.admin).toBe(true);
    expect(METRICS.find((m) => m.id === "tokens")?.label).toBe("Tokens (in + out)");
    expect([STEP_WORD.week, STEP_HEAD.week]).toEqual(["week", "Week"]);
    setLang("zh-TW");
    expect(VIEWS.map((v) => v.label)).toEqual(["每日", "每週", "每月"]);
    expect(VIEWS[1].range).toBe("最近 13 週");
    expect(SPLITS.map((s) => s.label)).toEqual(["總計", "金鑰", "模型", "供應商", "使用者"]);
    expect(METRICS.find((m) => m.id === "busy_ms")?.label).toBe("忙碌時間");
    expect([STEP_WORD.month, STEP_HEAD.day]).toEqual(["個月", "日期"]);
    expect(textUnits("Nov 12")).toBe(6);
    expect(textUnits("11月12日")).toBe(7);
    expect(textUnits("1.5萬")).toBe(4.5);
  });

  it("FY-WEB-789 lengths of audio and busy time in Chinese words; megapixels keep their unit", () => {
    expect([audioLength(12_300), audioLength(65_000), busyTime(7_500_000), megapixels(12_600)]).toEqual(["12.3 s", "1 min 05 s", "2 h 05 min", "12.6 MP"]);
    setLang("zh-TW");
    expect([audioLength(12_300), audioLength(65_000), busyTime(7_500_000), busyTime(0), megapixels(262)]).toEqual(["12.3 秒", "1 分 05 秒", "2 小時 05 分", "—", "0.26 MP"]);
  });
});
