// The two dictionaries, one module per area of the console (i18n/en/<area>.ts and i18n/zh-TW/<area>.ts), so
// pages can be translated in parallel without touching one big file. Every key starts with its module's name.
// To add an area: write en/<area>.ts and zh-TW/<area>.ts, and add both here; the parity test (i18n.test.ts)
// finds every module file on its own and fails until the pair is complete and registered.
import enAdvisor from "./en/advisor";
import enAuth from "./en/auth";
import enCommon from "./en/common";
import enFleet from "./en/fleet";
import enGuide from "./en/guide";
import enMachines from "./en/machines";
import enPlayground from "./en/playground";
import enServer from "./en/server";
import enServing from "./en/serving";
import enShell from "./en/shell";
import enStart from "./en/start";
import enUsage from "./en/usage";
import zhAdvisor from "./zh-TW/advisor";
import zhAuth from "./zh-TW/auth";
import zhCommon from "./zh-TW/common";
import zhFleet from "./zh-TW/fleet";
import zhGuide from "./zh-TW/guide";
import zhMachines from "./zh-TW/machines";
import zhPlayground from "./zh-TW/playground";
import zhServer from "./zh-TW/server";
import zhServing from "./zh-TW/serving";
import zhShell from "./zh-TW/shell";
import zhStart from "./zh-TW/start";
import zhUsage from "./zh-TW/usage";

export const EN_MODULES = {
  advisor: enAdvisor, auth: enAuth, common: enCommon, fleet: enFleet, guide: enGuide, machines: enMachines, server: enServer,
  serving: enServing, shell: enShell, start: enStart,
  playground: enPlayground, usage: enUsage,
};
export const ZH_TW_MODULES: Record<keyof typeof EN_MODULES, Record<string, string>> = {
  advisor: zhAdvisor, auth: zhAuth, common: zhCommon, fleet: zhFleet, guide: zhGuide, machines: zhMachines, server: zhServer,
  serving: zhServing, shell: zhShell, start: zhStart,
  playground: zhPlayground, usage: zhUsage,
};

export const en = {
  ...enAdvisor, ...enAuth, ...enCommon, ...enFleet, ...enGuide, ...enMachines, ...enServer, ...enServing, ...enShell, ...enStart,
  ...enPlayground, ...enUsage,
};
export type Key = keyof typeof en;

// TypeScript already holds each zh-TW module to its English one (`satisfies`); this keeps the merge complete.
export const zhTW: Record<Key, string> = {
  ...zhAdvisor, ...zhAuth, ...zhCommon, ...zhFleet, ...zhGuide, ...zhMachines, ...zhServer, ...zhServing, ...zhShell, ...zhStart,
  ...zhPlayground, ...zhUsage,
};
