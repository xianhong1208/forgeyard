import { describe, expect, it } from "vitest";
import { engineCarry, engineImageFile, splitImage } from "./engineImages";

const DIGEST = "sha256:28a56d03c5cc4abeb16b2a5422c5286d9e48020a3cb3416434bff5fbf847bfa6";
const VULKAN = `ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242@${DIGEST}`;

describe("engine images for offline machines", () => {
  it("pulls a pinned image by digest, tags it, and saves it under the tag", () => {
    const c = engineCarry(VULKAN);
    expect(c.file).toBe("llama.cpp-server-vulkan-b12242.tar.gz");
    expect(c.save).toBe(
      `sudo docker pull ghcr.io/ggml-org/llama.cpp@${DIGEST} && \\\n` +
      `  sudo docker tag ghcr.io/ggml-org/llama.cpp@${DIGEST} ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 && \\\n` +
      "  sudo docker save ghcr.io/ggml-org/llama.cpp:server-vulkan-b12242 | gzip > llama.cpp-server-vulkan-b12242.tar.gz");
    expect(c.load).toBe("gunzip -c llama.cpp-server-vulkan-b12242.tar.gz | sudo docker load");
  });

  it("handles Docker Hub names, registry ports and unpinned images", () => {
    expect(splitImage(`vllm/vllm-openai:v0.32.0@${DIGEST}`)).toEqual(
      { name: "vllm/vllm-openai", tagRef: "vllm/vllm-openai:v0.32.0", digestRef: `vllm/vllm-openai@${DIGEST}` });
    expect(splitImage("localhost:5000/team/engine:1.2")).toEqual(
      { name: "localhost:5000/team/engine", tagRef: "localhost:5000/team/engine:1.2", digestRef: null });
    expect(engineImageFile("localhost:5000/team/engine")).toBe("engine.tar.gz");
    expect(engineCarry("python:3.13-slim").save).toBe("sudo docker pull python:3.13-slim && \\\n  sudo docker save python:3.13-slim | gzip > python-3.13-slim.tar.gz");
  });
});
