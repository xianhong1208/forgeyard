// The 1.5.1 wording pass (#189): server sentences the console now words in Chinese, and units and money the way
// the rest of the page writes them.
import { afterEach, describe, expect, it } from "vitest";

import { translateMaybe } from "../i18n/core";
import { setLang } from "../i18n/lang";
import { formatTick } from "./charts";
import { ms } from "./gateway";
import { detailText } from "./onboarding";

afterEach(() => {
  setLang("en");
});

describe("the 1.5.1 wording pass", () => {
  it("FY-WEB-837 Get started's counting and listing details in Chinese, names kept; English as the server sent it", () => {
    const cases: [string, string][] = [
      ["1 machine online.", "1 台機器上線。"],
      ["3 machines online, 1 offline.", "3 台機器上線，1 台離線。"],
      ["gpu-01, cpu-01 joined but are offline: start the agent there again.", "gpu-01, cpu-01 已加入，但目前離線：請在那裡重新啟動 agent。"],
      ["Not chosen yet on gpu-01: until you choose, Forgeyard may use every GPU there. Open the machine and use Change GPU access.",
        "gpu-01 還沒有選擇：選擇之前，Forgeyard 可以使用那裡的每一張 GPU。請開啟那台機器，使用「變更 GPU 存取」。"],
      ["Chosen on 2 machines with GPUs.", "已在 2 台有 GPU 的機器上選好。"],
      ["20 models in the catalog.", "目錄裡有 20 個模型。"],
      ["Ready: chat-a, embed (asleep).", "已就緒：chat-a, embed（休眠中）。"],
      ["Not ready yet: chat-b (starting). The first start downloads the model and the engine; that can take a while.",
        "還沒有就緒：chat-b (starting)。第一次啟動要下載模型和推論引擎，可能需要一段時間。"],
      ["You have 2 active keys.", "你有 2 把啟用中的金鑰。"],
      ["You have sent 10127 requests with your keys.", "你已經用你的金鑰送出 10,127 個請求。"],
      ["The catalog is empty.", "目錄是空的。"],
      ["Something the console has no words for.", "Something the console has no words for."],
    ];
    for (const [sent, zh] of cases) {
      expect(detailText(sent, "en")).toBe(sent);
      expect(detailText(sent, "zh-TW")).toBe(zh);
    }
  });

  it("FY-WEB-838 the replica output panel's notes in Chinese by their exact text; another note as sent", () => {
    const note = "Live output is not available: not placed on a machine. Showing the lines kept when it last ended.";
    expect(translateMaybe("zh-TW", `deployment.out.sent.${note}`, note)).toBe("無法顯示即時輸出：它還沒有放置到機器上。下面是它上次結束時保留的輸出。");
    expect(translateMaybe("en", `deployment.out.sent.${note}`, note)).toBe(note);
    const other = "No such container: fy-chat-a";
    expect(translateMaybe("zh-TW", `deployment.out.sent.${other}`, other)).toBe(other);
  });

  it("FY-WEB-839 times in milliseconds and seconds in Chinese words; English as before; the cost axis writes US$ like the tables", () => {
    expect([ms(350), ms(1234), ms(12_345), ms(null)]).toEqual(["350 ms", "1.2 s", "12 s", "—"]);
    expect(formatTick(250_000, "usd")).toBe("$0.25");
    setLang("zh-TW");
    expect([ms(350), ms(1234), ms(12_345)]).toEqual(["350 毫秒", "1.2 秒", "12 秒"]);
    expect([formatTick(0, "usd"), formatTick(60_000_000, "usd")]).toEqual(["US$0", "US$60"]);
  });
});

describe("Chinese numbers with 萬 and 億", () => {
  it("FY-WEB-841 a space between the digits and 萬/億, none between 萬/億 and the next word; English unchanged", async () => {
    const { spaceCjkNumbers } = await import("../i18n/format");
    const { translate } = await import("../i18n/core");
    expect(spaceCjkNumbers("10萬")).toBe("10 萬");
    expect(spaceCjkNumbers("每分鐘 10萬 個 token")).toBe("每分鐘 10 萬個 token");
    expect(spaceCjkNumbers("20.3億 參數")).toBe("20.3 億參數");
    expect(spaceCjkNumbers("每 100 萬個 token")).toBe("每 100 萬個 token");
    expect(spaceCjkNumbers("7 億時")).toBe("7 億時");
    expect(spaceCjkNumbers("10 萬 tokens")).toBe("10 萬 tokens");
    expect(translate("zh-TW", "keys.tokensPerMin.other", { n: "10萬" })).toBe("每分鐘 10 萬個 token");
    expect(translate("zh-TW", "keys.tokensPerMin.other", { n: "600" })).toBe("每分鐘 600 個 token");
    expect(translate("en", "keys.tokensPerMin.other", { n: "10k" })).toBe("10k tokens/min");
  });
});
