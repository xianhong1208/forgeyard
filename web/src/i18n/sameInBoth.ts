// Keys whose Traditional Chinese text is rightly the same as the English one, or has no Chinese character:
// product and vendor names, units, protocol words. The parity test lets these through; every other zh-TW value
// must contain Chinese, so an English string pasted in by mistake (or left untranslated) fails it.
export const SAME_IN_BOTH: readonly string[] = [
  "fleet.machineKind.nvidia",
  "fleet.machineKind.amd-gpu",
  "fleet.machineKind.amd-apu",
  "fleet.legend.forgeyard",
  "fleet.cpu",
  "machines.way.docker",
  "machines.ram",
  "machines.agent",
  "serving.perGpu",
  "serving.ram",
  "enginetrial.gpu",
];
