// 1.5.2 (#189): the engine is 推論引擎 in Traditional Chinese (a bare 引擎 read as unclear), and its image is shown
// beside its name. See GLOSSARY.md, "Engine, image, program".
import { describe, expect, it } from "vitest";

import { ZH_TW_MODULES } from "./dictionaries";
import { deploymentRuns, engineRuns, engineRunsTitle, splitImage } from "../lib/engines";
import type { RecipeVariant } from "../lib/types";

// The compounds that keep a bare 引擎, each for a reason.
const ALLOWED = [
  "推論引擎", // the engine itself
  "容器引擎", // Docker or Podman on a machine: another thing, named as Docker names it
];

// Every bare 引擎 left once the allowed compounds are taken out, as "<key>: <text>".
function bareEngine(dict: Record<string, string>): string[] {
  const out: string[] = [];
  for (const [key, text] of Object.entries(dict)) {
    let rest = text;
    for (const ok of ALLOWED) rest = rest.split(ok).join("");
    if (rest.includes("引擎")) out.push(`${key}: ${text}`);
  }
  return out;
}

const V = (name: string, vendor: RecipeVariant["vendor"], image: string | null, engine: string | null = null): RecipeVariant => ({
  name, vendor, runtime: image ? "container" : "process", image, engine, arch: null, compute_capability: null, driver: null });

describe("the engine's name and image (1.5.2)", () => {
  it("FY-WEB-870 no zh-TW dictionary says a bare 引擎: the engine is 推論引擎, Docker's is 容器引擎", () => {
    const all = Object.values(ZH_TW_MODULES).flatMap((m) => bareEngine(m));
    expect(all).toEqual([]);
    // The rule itself finds one.
    expect(bareEngine({ "x.a": "加入自訂引擎", "x.b": "推論引擎的映像檔", "x.c": "它沒有容器引擎" })).toEqual(["x.a: 加入自訂引擎"]);
    // The words people read most.
    expect(ZH_TW_MODULES.library["library.tab.engines"]).toBe("推論引擎");
    expect(ZH_TW_MODULES.machine["machine.dlg.engine"]).toBe("推論引擎");
    expect(ZH_TW_MODULES.enginepage["enginepage.eyebrow"]).toBe("推論引擎 · {kind}");
  });

  it("FY-WEB-871 an engine's images: each once by its tag, programs by file name, per device; a deployment's from its replicas", () => {
    const cuda = "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:a8cf841eef4e9d6ea28bb4d88eea1004032a2bf0d1302bff7b8d2100e801ed16";
    const zip = "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-bin.zip";
    const variants = [V("cuda", "nvidia", cuda), V("cuda-amd", "amd", cuda), V("vulkan", "amd", "ghcr.io/ggml-org/llama.cpp:server-vulkan"),
      V("cpu-process", "cpu", null, zip), V("none", "cpu", null)];
    const runs = engineRuns(variants);
    expect(runs.map((r) => r.label)).toEqual(["ghcr.io/ggml-org/llama.cpp:server-cuda", "ghcr.io/ggml-org/llama.cpp:server-vulkan", "llama-bin.zip"]);
    expect(runs[0].vendors).toEqual(["nvidia", "amd"]);
    expect(engineRunsTitle(runs).split("\n")).toEqual([`NVIDIA, AMD: ${cuda}`, "AMD: ghcr.io/ggml-org/llama.cpp:server-vulkan", `CPU: ${zip}`]);
    expect(engineRuns(variants, "cpu").map((r) => r.label)).toEqual(["llama-bin.zip"]);
    expect(engineRuns(variants, "gpu").map((r) => r.label)).toEqual(["ghcr.io/ggml-org/llama.cpp:server-cuda", "ghcr.io/ggml-org/llama.cpp:server-vulkan"]);
    // A deployment: what its replicas run, each once; none placed (or process replicas): the engine's for its device.
    expect(deploymentRuns([cuda, cuda, null], { variants }, "gpu").map((r) => [r.label, r.full])).toEqual([["ghcr.io/ggml-org/llama.cpp:server-cuda", cuda]]);
    expect(deploymentRuns([null], { variants }, "cpu").map((r) => r.label)).toEqual(["llama-bin.zip"]);
    expect(deploymentRuns([], undefined, "gpu")).toEqual([]);
    // Split before the tag, so a short line cuts the repository first; a registry port is not a tag; a program stays whole.
    expect(splitImage("vllm/vllm-openai:v0.32.0")).toEqual(["vllm/vllm-openai", ":v0.32.0"]);
    expect(splitImage("registry.local:5000/llama.cpp")).toEqual(["registry.local:5000/llama.cpp", ""]);
    expect(splitImage("llama-bin.zip")).toEqual(["llama-bin.zip", ""]);
  });
});
