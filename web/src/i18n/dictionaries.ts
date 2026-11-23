// The two dictionaries, one module per area of the console (i18n/en/<area>.ts and i18n/zh-TW/<area>.ts), so
// pages can be translated in parallel without touching one big file. Every key starts with its module's name.
// To add an area: write en/<area>.ts and zh-TW/<area>.ts, and add both here; the parity test (i18n.test.ts)
// finds every module file on its own and fails until the pair is complete and registered.
import enAdmin from "./en/admin";
import enAdvisor from "./en/advisor";
import enAuth from "./en/auth";
import enCloud from "./en/cloud";
import enCommon from "./en/common";
import enFleet from "./en/fleet";
import enGuide from "./en/guide";
import enKeys from "./en/keys";
import enMachines from "./en/machines";
import enServer from "./en/server";
import enServing from "./en/serving";
import enShell from "./en/shell";
import enStart from "./en/start";
import zhAdmin from "./zh-TW/admin";
import zhAdvisor from "./zh-TW/advisor";
import zhAuth from "./zh-TW/auth";
import zhCloud from "./zh-TW/cloud";
import zhCommon from "./zh-TW/common";
import zhFleet from "./zh-TW/fleet";
import zhGuide from "./zh-TW/guide";
import zhKeys from "./zh-TW/keys";
import zhMachines from "./zh-TW/machines";
import zhServer from "./zh-TW/server";
import zhServing from "./zh-TW/serving";
import zhShell from "./zh-TW/shell";
import zhStart from "./zh-TW/start";

export const EN_MODULES = {
  admin: enAdmin, cloud: enCloud, keys: enKeys,
  advisor: enAdvisor, auth: enAuth, common: enCommon, fleet: enFleet, guide: enGuide, machines: enMachines, server: enServer,
  serving: enServing, shell: enShell, start: enStart,
};
export const ZH_TW_MODULES: Record<keyof typeof EN_MODULES, Record<string, string>> = {
  admin: zhAdmin, cloud: zhCloud, keys: zhKeys,
  advisor: zhAdvisor, auth: zhAuth, common: zhCommon, fleet: zhFleet, guide: zhGuide, machines: zhMachines, server: zhServer,
  serving: zhServing, shell: zhShell, start: zhStart,
};

export const en = {
  ...enAdmin, ...enCloud, ...enKeys,
  ...enAdvisor, ...enAuth, ...enCommon, ...enFleet, ...enGuide, ...enMachines, ...enServer, ...enServing, ...enShell, ...enStart,
};
export type Key = keyof typeof en;

// TypeScript already holds each zh-TW module to its English one (`satisfies`); this keeps the merge complete.
export const zhTW: Record<Key, string> = {
  ...zhAdmin, ...zhCloud, ...zhKeys,
  ...zhAdvisor, ...zhAuth, ...zhCommon, ...zhFleet, ...zhGuide, ...zhMachines, ...zhServer, ...zhServing, ...zhShell, ...zhStart,
};
