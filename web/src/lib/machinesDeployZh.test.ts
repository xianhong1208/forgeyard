// @vitest-environment jsdom
// Machines and deployments in Traditional Chinese (#189, group 1): the lib functions that word a machine's page and
// the deploy forms follow the language, English stays as it was, and the server's own words are kept.
import { afterEach, describe, expect, it } from "vitest";

import { setLang } from "../i18n/lang";
import { behindDetail, methodLine, METHOD_LABEL } from "./agentUpdate";
import { cacheRows, diskLine, removeBlocked, removeQuestion } from "./cache";
import { FETCH_VIA_HINT, FETCH_VIA_LABEL, TARGET_LABEL, downloadLine, targetDetail } from "./downloads";
import { matchLine, removedLine, uploadBlocked, uploadLine } from "./imageUpload";
import { replicaAddressNote } from "./machines";
import { tlsChip, tlsLine } from "./replicaTls";
import { loadText, scaleSummary } from "./scale";
import { idleProblem, seconds, startHint, wakeWaitProblem } from "./sleep";
import type { AgentUpdate, Deployment, DownloadTarget, Machine, MachineCache, ModelDownload, StartTime } from "./types";
import { allowlistSummary, runtimeLine } from "./workloads";

const GIB = 1024 ** 3;

afterEach(() => {
  localStorage.clear();
  setLang("en");
  localStorage.clear();
});

const machine = (over: Partial<Machine> = {}): Machine => ({
  id: 4, name: "gpu-01", status: "online", features: ["image_load"], gpu_allowlist: null,
  accelerators: [0, 1, 2].map((i) => ({ key: `nvidia:GPU-${i}`, index: i, vendor: "nvidia", model: "NVIDIA A100", allowed: true })),
  ...over,
} as unknown as Machine);

describe("a machine's page in zh-TW", () => {
  it("FY-WEB-700 replica traffic and its chip: Chinese in zh-TW, the ISO date and the server's reason kept; English as before", () => {
    const expiring = { state: "expiring", not_after: "2026-12-01T00:00:00Z", reason: "renewal refused" } as Machine["replica_tls"];
    expect(tlsLine(expiring)).toBe("TLS, certificate valid until 2026-12-01 and not being renewed (renewal refused)");
    expect(tlsLine({ state: "ok", not_after: null } as Machine["replica_tls"])).toBe("Encrypted (TLS)");
    expect(tlsChip({ status: "online", replica_tls: { state: "plain" } as Machine["replica_tls"] })?.text).toBe("not encrypted: update the agent");
    setLang("zh-TW");
    expect(tlsLine(expiring)).toBe("TLS，憑證有效至 2026-12-01，而且沒有在更新（renewal refused）");
    expect(tlsLine({ state: "expired", not_after: "2026-11-01T00:00:00Z" } as Machine["replica_tls"])).toBe("TLS，憑證已在 2026-11-01 過期：連不到它的副本");
    expect(tlsChip({ status: "online", replica_tls: { state: "missing" } as Machine["replica_tls"] })?.text).toBe("沒有副本憑證");
  });

  it("FY-WEB-701 the agent update: why it is behind and how it runs, in the reader's language", () => {
    const u = { method: "container", known: true, offline: true, image: "docker.io/x/forgeyard-agent:1.5.1" } as AgentUpdate;
    setLang("zh-TW");
    expect(behindDetail({ agent: "1.4.0", server: "1.5.1", older: true, old_protocol: false, missing: [] }))
      .toBe("它的 agent 是 1.4.0，伺服器是 1.5.1。請更新它，取得這一版的修正和功能。");
    expect(behindDetail({ agent: null, server: "1.5.1", older: false, old_protocol: true, missing: [] })).toMatch(/^它的 agent（版本不明）使用的協定/);
    expect(methodLine(u)).toBe("它在容器中執行，並經由伺服器取得檔案：把映像檔 docker.io/x/forgeyard-agent:1.5.1 帶到這台機器，再用新的映像檔啟動同一個容器。");
    expect(methodLine({ ...u, method: "manual" })).toBe("它是手動安裝的。");
    expect(METHOD_LABEL["install.sh"]).toBe("以 install.sh 安裝");
    setLang("en");
    expect(METHOD_LABEL["install.sh"]).toBe("installed with install.sh");
  });

  it("FY-WEB-702 the cache rows and questions: plurals in English, Chinese in zh-TW", () => {
    const c = {
      models: [{ key: "hub/x@r", kind: "hf", repo: "acme/x", revision: "abcdef1234567890", files: 1, missing: 2, bytes: GIB, catalog: [], used_by: [] }],
      engines: [{ key: "engine/e", id: "e1", name: null, bytes: 1024 ** 2, used_by: [] }], used_bytes: 7.5 * GIB, free_bytes: 50 * GIB, total_bytes: 100 * GIB,
    } as unknown as MachineCache;
    const rows = cacheRows(c);
    expect(rows[0].sub).toBe("revision abcdef123456 · 1 file · 2 missing on disk");
    expect(rows[1].title).toBe("engine e1");
    const used = (n: number) => ({ ...rows[0], usedBy: Array.from({ length: n }, (_, i) => ({ name: `d${i}`, kind: "deployment" as const, id: i })) });
    expect(removeBlocked({ remove_note: null } as MachineCache, used(1))).toBe("In use by d0. Stop it first.");
    expect(removeBlocked({ remove_note: null } as MachineCache, used(2))).toBe("In use by d0, d1. Stop them first.");
    expect(diskLine(c)).toBe("7.5 GiB in the cache · 50 GiB free of 100 GiB");
    setLang("zh-TW");
    expect(cacheRows(c)[0].sub).toBe("修訂版 abcdef123456 · 1 個檔案 · 2 個不在磁碟上");
    expect(removeBlocked({ remove_note: null } as MachineCache, used(2))).toBe("d0, d1 正在使用。請先停止它們。");
    expect(removeQuestion("gpu-01", rows[0])).toBe("要從 gpu-01 的快取移除 acme/x (1 GiB) 嗎？和其他修訂版共用的檔案會保留。需要它的部署會再下載一次。");
    expect(diskLine(c)).toBe("快取中有 7.5 GiB · 可用 50 GiB，共 100 GiB");
  });

  it("FY-WEB-703 downloads: target labels and details, a download's line; the server's reason as sent", () => {
    const target = { machine_id: 4, name: "gpu-01", state: "ok", need_bytes: GIB, free_bytes: 48 * GIB, model_cached: false, via: "server",
      from_machine: "gpu-02", engine: { kind: "archive", name: "llama.cpp-1.0.0", cached: true }, variant: "cpu-process" } as unknown as DownloadTarget;
    const done = { state: "done", via: "machine", source: { name: "gpu-02" }, recipe: "llama-cpp", finished_at: null } as unknown as ModelDownload;
    const cancelled = { state: "cancelled", reason: null, finished_at: null } as unknown as ModelDownload;
    expect(targetDetail(target)).toBe("1 GiB to download · 48 GiB free · engine: llama.cpp-1.0.0 archive (already here) · cpu-process · via the server, from gpu-02");
    expect(downloadLine(done)).toBe("Downloaded — via the server, from gpu-02, with the llama-cpp engine");
    expect(downloadLine(cancelled)).toBe("Cancelled —");
    expect(downloadLine({ ...cancelled, reason: "stopped by owner" })).toBe("Stopped by owner —");
    setLang("zh-TW");
    expect(TARGET_LABEL.no_space).toBe("磁碟空間不足");
    expect(FETCH_VIA_LABEL.server).toBe("一律經由伺服器");
    expect(FETCH_VIA_HINT.direct).toBe("一律直接下載，從不經由伺服器。");
    expect(targetDetail(target)).toBe("要下載 1 GiB · 可用 48 GiB · 引擎：llama.cpp-1.0.0 封存檔（已經在這裡） · cpu-process · 經由伺服器，來自 gpu-02");
    expect(downloadLine(done)).toBe("—下載完成，經由伺服器，來自 gpu-02，搭配 llama-cpp 引擎");
    expect(downloadLine(cancelled)).toBe("—已取消");
    expect(downloadLine({ ...cancelled, reason: "stopped by owner" })).toBe("Stopped by owner —");
    expect(downloadLine({ state: "failed", reason: null } as unknown as ModelDownload)).toBe("失敗：原因不明");
  });

  it("FY-WEB-704 engine images: why an upload is refused, the progress and what was loaded", () => {
    const loaded = { id: "sha256:" + "a".repeat(64), tags: ["x:1"], content: null, os: "linux", architecture: "amd64",
      matches: [{ recipe: "llama-cpp", variant: "cpu", image: "ghcr.io/ggml-org/llama.cpp:server@sha256:1", how: "checked" as const }] };
    setLang("zh-TW");
    expect(uploadBlocked(machine({ status: "offline" }))).toBe("gpu-01 離線中：它的 agent 連線後才能上傳映像檔。");
    expect(uploadLine("sending", 512 * 1024 ** 2, GIB, "gpu-01")).toBe("已傳送 512 MiB／1 GiB（50%）");
    expect(matchLine(loaded)).toBe("llama-cpp (cpu) 釘選的映像檔，已用記錄的 ID 核對。它的標籤不是配方用的標籤：在機器上執行 docker tag aaaaaaaaaaaa ghcr.io/ggml-org/llama.cpp:server 就能使用。");
    expect(removedLine("gpu-01", { ok: true, image: { ...loaded, size: null, created: null }, freed_bytes: GIB, free_bytes: 40 * GIB, total_bytes: 100 * GIB }))
      .toBe("已從 gpu-01 移除 x:1：釋出最多 1 GiB（和其他映像檔共用的層會保留）；現在可用 40 GiB，共 100 GiB。");
  });

  it("FY-WEB-705 GPU access and runtimes, and the loopback note, in zh-TW", () => {
    const m = machine({ gpu_allowlist: ["nvidia:GPU-1", "nvidia:GPU-2"],
      accelerators: [0, 1, 2].map((i) => ({ key: `nvidia:GPU-${i}`, index: i, vendor: "nvidia", model: "NVIDIA A100", allowed: i > 0 })) as Machine["accelerators"] });
    expect(allowlistSummary(m)).toBe("#1, #2 of 3");
    expect(runtimeLine({ ok: true, user: "forgeyard-run", limits: "systemd" } as never, "process")).toBe("process as forgeyard-run (systemd limits)");
    setLang("zh-TW");
    expect(allowlistSummary(m)).toBe("3 張中的 #1, #2");
    expect(allowlistSummary(machine())).toBe("全部 3 張 GPU");
    expect(runtimeLine({ ok: true, engine: "docker", version: "27.1", nvidia: true } as never, "container")).toBe("容器：docker 27.1，可用 NVIDIA GPU");
    expect(runtimeLine(undefined, "process")).toBe("行程：不明");
    expect(replicaAddressNote(machine({ runtime_info: { address: "127.0.0.1", same_host: false } } as never))).toBe("迴路位址：閘道會在伺服器的主機上連它");
  });
});

describe("the deploy forms in zh-TW", () => {
  it("FY-WEB-706 start times and the sleep hints: seconds and minutes in Chinese", () => {
    expect(seconds(200)).toBe("3 min 20 s");
    expect(startHint({ seconds: 74, at: "2026-11-20T00:00:00Z" } as StartTime, "chat-a")).toBe("chat-a (the same model and engine) last started in 74 s: the first request after a sleep waits about that long.");
    setLang("zh-TW");
    expect(seconds(74)).toBe("74 秒");
    expect(seconds(200)).toBe("3 分 20 秒");
    expect(seconds(180)).toBe("3 分鐘");
    expect(startHint({ seconds: 74, at: "2026-11-20T00:00:00Z" } as StartTime)).toBe("上次啟動花了 74 秒：休眠後的第一個請求大約要等這麼久。");
  });

  it("FY-WEB-707 the idle and wake-wait checks say what is wrong in the reader's language", () => {
    expect(idleProblem("abc")).toBe("A whole number of minutes.");
    expect(idleProblem("5000")).toBe("1 to 1,440 minutes.");
    expect(wakeWaitProblem("5")).toBe("10 to 1,800 seconds.");
    expect(idleProblem("30")).toBeNull();
    setLang("zh-TW");
    expect(idleProblem("abc")).toBe("請填整數分鐘。");
    expect(wakeWaitProblem("x")).toBe("請填整數秒數。");
    expect(wakeWaitProblem("5")).toBe("10 到 1,800 秒。");
    expect(wakeWaitProblem("300")).toBeNull();
  });

  it("FY-WEB-708 automatic replicas: the summary and the load, one and many requests", () => {
    const d = (capacity: number) => ({ autoscale: { on: true, min: 0, max: 4, replicas: 2, load: 3.5, scale_up_seconds: 60, per_replica: 1.75,
      capacity, target: 80 } }) as unknown as Deployment;
    expect(scaleSummary(d(1))).toBe("Automatic, 0 (asleep) to 4 · 2 now");
    expect(loadText(d(1))).toBe("Load 3.5 over the last 60 s, 1.75 per ready replica (target 0.8 of 1 concurrent request each)");
    expect(loadText(d(4))).toBe("Load 3.5 over the last 60 s, 1.75 per ready replica (target 3.2 of 4 concurrent requests each)");
    setLang("zh-TW");
    expect(scaleSummary(d(4))).toBe("自動，0（休眠）～4 個 · 目前 2 個");
    expect(loadText(d(4))).toBe("最近 60 秒的負載為 3.5，每個就緒副本 1.75（目標：每個副本 4 個同時請求中的 3.2 個）");
  });
});
