// The two dictionaries, one module per area of the console (i18n/en/<area>.ts and i18n/zh-TW/<area>.ts), so
// pages can be translated in parallel without touching one big file. Every key starts with its module's name.
// To add an area: write en/<area>.ts and zh-TW/<area>.ts, and add both here; the parity test (i18n.test.ts)
// finds every module file on its own and fails until the pair is complete and registered.
import enAddmodel from "./en/addmodel";
import enAdmin from "./en/admin";
import enAdvisor from "./en/advisor";
import enAuth from "./en/auth";
import enCloud from "./en/cloud";
import enCommon from "./en/common";
import enDeploy from "./en/deploy";
import enDeployment from "./en/deployment";
import enEnginepage from "./en/enginepage";
import enEngineupdates from "./en/engineupdates";
import enEnginetry from "./en/enginetry";
import enEnginetrial from "./en/enginetrial";
import enFleet from "./en/fleet";
import enGuide from "./en/guide";
import enLibrary from "./en/library";
import enKeys from "./en/keys";
import enMachine from "./en/machine";
import enMachines from "./en/machines";
import enMfa from "./en/mfa";
import enModelpage from "./en/modelpage";
import enPlayground from "./en/playground";
import enServer from "./en/server";
import enServing from "./en/serving";
import enShell from "./en/shell";
import enSso from "./en/sso";
import enTeams from "./en/teams";
import enStart from "./en/start";
import enUsage from "./en/usage";
import zhAddmodel from "./zh-TW/addmodel";
import zhAdmin from "./zh-TW/admin";
import zhAdvisor from "./zh-TW/advisor";
import zhAuth from "./zh-TW/auth";
import zhCloud from "./zh-TW/cloud";
import zhCommon from "./zh-TW/common";
import zhDeploy from "./zh-TW/deploy";
import zhDeployment from "./zh-TW/deployment";
import zhEnginepage from "./zh-TW/enginepage";
import zhEngineupdates from "./zh-TW/engineupdates";
import zhEnginetry from "./zh-TW/enginetry";
import zhEnginetrial from "./zh-TW/enginetrial";
import zhFleet from "./zh-TW/fleet";
import zhGuide from "./zh-TW/guide";
import zhLibrary from "./zh-TW/library";
import zhKeys from "./zh-TW/keys";
import zhMachine from "./zh-TW/machine";
import zhMachines from "./zh-TW/machines";
import zhMfa from "./zh-TW/mfa";
import zhModelpage from "./zh-TW/modelpage";
import zhPlayground from "./zh-TW/playground";
import zhServer from "./zh-TW/server";
import zhServing from "./zh-TW/serving";
import zhShell from "./zh-TW/shell";
import zhSso from "./zh-TW/sso";
import zhTeams from "./zh-TW/teams";
import zhStart from "./zh-TW/start";
import zhUsage from "./zh-TW/usage";

export const EN_MODULES = {
  admin: enAdmin, cloud: enCloud, keys: enKeys,
  addmodel: enAddmodel, advisor: enAdvisor, auth: enAuth, common: enCommon, enginepage: enEnginepage, engineupdates: enEngineupdates, enginetry: enEnginetry, enginetrial: enEnginetrial, fleet: enFleet, guide: enGuide, library: enLibrary, machines: enMachines, modelpage: enModelpage, server: enServer,
  serving: enServing, shell: enShell, start: enStart,
  // Machines and deployments (#189, group 1)
  deploy: enDeploy, deployment: enDeployment, machine: enMachine,
  playground: enPlayground, usage: enUsage,
  // Two-factor sign-in and single sign-on (1.6.0, #39)
  mfa: enMfa, sso: enSso,
  // Teams (1.6.1, #190)
  teams: enTeams,
};
export const ZH_TW_MODULES: Record<keyof typeof EN_MODULES, Record<string, string>> = {
  admin: zhAdmin, cloud: zhCloud, keys: zhKeys,
  addmodel: zhAddmodel, advisor: zhAdvisor, auth: zhAuth, common: zhCommon, enginepage: zhEnginepage, engineupdates: zhEngineupdates, enginetry: zhEnginetry, enginetrial: zhEnginetrial, fleet: zhFleet, guide: zhGuide, library: zhLibrary, machines: zhMachines, modelpage: zhModelpage, server: zhServer,
  serving: zhServing, shell: zhShell, start: zhStart,
  deploy: zhDeploy, deployment: zhDeployment, machine: zhMachine,
  playground: zhPlayground, usage: zhUsage,
  mfa: zhMfa, sso: zhSso,
  teams: zhTeams,
};

export const en = {
  ...enAdmin, ...enCloud, ...enKeys,
  ...enAddmodel, ...enAdvisor, ...enAuth, ...enCommon, ...enEnginepage, ...enEngineupdates, ...enEnginetry, ...enEnginetrial, ...enFleet, ...enGuide, ...enLibrary, ...enMachines, ...enModelpage, ...enServer, ...enServing, ...enShell, ...enStart,
  ...enDeploy, ...enDeployment, ...enMachine,
  ...enPlayground, ...enUsage,
  ...enMfa, ...enSso,
  ...enTeams,
};
export type Key = keyof typeof en;

// TypeScript already holds each zh-TW module to its English one (`satisfies`); this keeps the merge complete.
export const zhTW: Record<Key, string> = {
  ...zhAdmin, ...zhCloud, ...zhKeys,
  ...zhAddmodel, ...zhAdvisor, ...zhAuth, ...zhCommon, ...zhEnginepage, ...zhEngineupdates, ...zhEnginetry, ...zhEnginetrial, ...zhFleet, ...zhGuide, ...zhLibrary, ...zhMachines, ...zhModelpage, ...zhServer, ...zhServing, ...zhShell, ...zhStart,
  ...zhDeploy, ...zhDeployment, ...zhMachine,
  ...zhPlayground, ...zhUsage,
  ...zhMfa, ...zhSso,
  ...zhTeams,
};
