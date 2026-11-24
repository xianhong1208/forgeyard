import { describe, expect, it } from "vitest";

import { compatibleRecipes, downloadFraction, downloadText } from "./models";
import type { CatalogModel, Recipe, Replica } from "./types";

const GIB = 1024 ** 3;
const rep = (download: Replica["download"]) => ({ download }) as Replica;

describe("download and pull progress", () => {
  it("FY-WEB-205: a model download never reads past its total, even when an agent reports more", () => {
    const r = rep({ phase: "model", done: 14.54 * GIB, total: 12.93 * GIB });
    expect(downloadFraction(r)).toBe(1);
    expect(downloadText(r)).toBe("Downloading the model: 12.9 GiB of 12.9 GiB");
    expect(downloadText(rep({ phase: "engine", done: 300 * 1024 ** 2, total: 0 }))).toBe("Downloading the engine: 300 MiB");
    expect(downloadFraction(rep({ phase: "engine", done: 3, total: 0 }))).toBeNull();
  });

  it("FY-WEB-206: an image pull reads like a download, with its layers while sizes are unknown", () => {
    expect(downloadText(rep({ phase: "image", done: 0.8 * GIB, total: 1.9 * GIB, layers: 7, layers_done: 2 })))
      .toBe("Pulling the image: 819 MiB of 1.9 GiB");
    expect(downloadFraction(rep({ phase: "image", done: 0.8 * GIB, total: 1.9 * GIB })))
      .toBeCloseTo(0.42, 2);
    expect(downloadText(rep({ phase: "image", done: GIB / 2, total: 0, layers: 7, layers_done: 3 })))
      .toBe("Pulling the image: 512 MiB, 3 of 7 layers");
    expect(downloadText(rep({ phase: "image", done: 0, total: 0, layers: 7, layers_done: 3 })))
      .toBe("Pulling the image: 3 of 7 layers");
    expect(downloadText(rep({ phase: "image", done: 9, total: 9, layers: 7, layers_done: 5, unpacking: true })))
      .toBe("Pulling the image: unpacking, 5 of 7 layers");
    expect(downloadText(rep({ phase: "image", done: 0, total: 0 }))).toBe("Pulling the image");
    expect(downloadText(rep(null))).toBeNull();
  });
});

describe("the recipes a model is offered", () => {
  const recipe = (name: string, tasks: string[]) => ({ name, tasks, formats: ["safetensors"] }) as unknown as Recipe;
  const all = [recipe("transformers", ["embed", "classify"]), recipe("vision", ["detect"]), recipe("vllm-pooling", ["embed", "rerank", "classify"])];
  const model = (recipes?: string[]) => ({ name: "m", format: "safetensors", task: "embed", recipes }) as unknown as CatalogModel;

  it("FY-WEB-899: the server's list and order win (1.5.4: the transformers server last, only for the models it runs)", () => {
    expect(compatibleRecipes(model(["vllm-pooling", "transformers"]), all).map((r) => r.name)).toEqual(["vllm-pooling", "transformers"]);
    expect(compatibleRecipes(model(["vllm-pooling"]), all).map((r) => r.name)).toEqual(["vllm-pooling"]);
    // A name the catalog no longer has, or one that does not serve the model's task, is dropped.
    expect(compatibleRecipes(model(["gone", "vision", "vllm-pooling"]), all).map((r) => r.name)).toEqual(["vllm-pooling"]);
    // Without the server's list (an older answer), every recipe serving the format and task.
    expect(compatibleRecipes(model(undefined), all).map((r) => r.name)).toEqual(["transformers", "vllm-pooling"]);
    expect(compatibleRecipes(undefined, all)).toEqual([]);
  });
});
