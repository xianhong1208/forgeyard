import { describe, expect, it } from "vitest";

import { paramApplies } from "../pages/ModelDeploy";
import { MAX_IMAGE_BYTES, chatContent, readImage } from "./gateway";
import { mediaText } from "./models";
import type { CatalogModel } from "./types";

const model = (modalities: CatalogModel["modalities"]) => ({ modalities }) as CatalogModel;

describe("images in chat (1.3)", () => {
  it("sends plain text alone, and text plus image parts once images are attached", () => {
    expect(chatContent("hi")).toBe("hi");
    expect(chatContent("hi", [])).toBe("hi");
    expect(chatContent("what is it?", [{ name: "a.png", uri: "data:image/png;base64,AAA" }])).toEqual([
      { type: "text", text: "what is it?" }, { type: "image_url", image_url: { url: "data:image/png;base64,AAA" } }]);
    expect(chatContent("", [{ name: "a.png", uri: "data:image/png;base64,AAA" }])).toEqual([
      { type: "image_url", image_url: { url: "data:image/png;base64,AAA" } }]);
  });

  it("reads an image as a data: URI and refuses other files and very large ones", async () => {
    const png = new File([new Uint8Array([137, 80, 78, 71])], "dot.png", { type: "image/png" });
    const read = await readImage(png);
    expect(read.name).toBe("dot.png");
    expect(read.uri.startsWith("data:image/png;base64,")).toBe(true);
    await expect(readImage(new File(["x"], "notes.txt", { type: "text/plain" }))).rejects.toThrow("not an image");
    const big = new File([new Uint8Array(1)], "huge.png", { type: "image/png" });
    Object.defineProperty(big, "size", { value: MAX_IMAGE_BYTES + 1 });
    await expect(readImage(big)).rejects.toThrow("larger than 20 MiB");
  });

  it("names what a model takes, and says nothing when it is unknown", () => {
    expect(mediaText(["image", "video"])).toBe("Images, video");
    expect(mediaText(["image", "audio", "video"])).toBe("Images, audio, video");
    expect(mediaText([])).toBe("Text only");
    expect(mediaText(null)).toBeNull();
    expect(mediaText(undefined)).toBeNull();
  });

  it("shows the media parameters only for a model that takes media (or is unknown)", () => {
    expect(paramApplies("images", model([]))).toBe(false);
    expect(paramApplies("videos", model(["image"]))).toBe(false);
    expect(paramApplies("images", model(["audio"]))).toBe(true);
    expect(paramApplies("videos", model(["image", "video"]))).toBe(true);
    expect(paramApplies("images", model(null))).toBe(true);
    expect(paramApplies("max_body_mb", model([]))).toBe(true);
    expect(paramApplies("image_tokens", model(["audio"]))).toBe(false);
    expect(paramApplies("image_tokens", model(["image"]))).toBe(true);
    expect(paramApplies("video_fps", model(["image"]))).toBe(false);
    expect(paramApplies("video_fps", model(["image", "video"]))).toBe(true);
    expect(paramApplies("context", undefined)).toBe(true);
  });
});
