// The Get started checklist (GET /api/onboarding) and the example call it shows.

import { translateMaybe } from "../i18n/core";
import { formatNumber } from "../i18n/format";
import { getLang, type Lang } from "../i18n/lang";
import { snippets } from "./gateway";

export type StepId = "machine" | "gpus" | "model" | "deploy" | "key" | "call" | "usage";

export type Step = {
  id: StepId;
  title: string;
  why: string;
  done: boolean;
  detail: string;
  action: { to: string; label: string } | null;
  // The earlier step this one waits for, if any.
  blocked_by: StepId | null;
  // Who does it, when the person looking cannot (operators: "An admin adds machines.").
  who: string | null;
};

export type Checklist = {
  kind: "setup" | "use";
  steps: Step[];
  done: number;
  total: number;
  complete: boolean;
  hidden: boolean;
  gateway_url: string;
  // Where the CA for /v1 clients is (null: nothing to trust).
  trust_url?: string | null;
  example: { model: string; api: string; ready: boolean } | null;
};

export const KEY_ENV = "FORGEYARD_API_KEY";

// The example call with the key read from an environment variable, so it can be copied as is.
export function exampleCall(gatewayUrl: string, model: string, api: string, trustUrl?: string | null): { curl: string; python: string } {
  const marker = "@@KEY@@";
  const s = snippets(gatewayUrl, marker, model, api, trustUrl);
  const python = s.python
    .replace(`"Bearer ${marker}"`, `"Bearer " + os.environ["${KEY_ENV}"]`)
    .replace(`"${marker}"`, `os.environ["${KEY_ENV}"]`);
  return {
    curl: `export ${KEY_ENV}=fyk_...   # your API key\n\n${s.curl.replace(marker, `$${KEY_ENV}`)}`,
    python: `import os\n${python}`,
  };
}

// The step to do next: the first one not done.
export function currentStep(c: Checklist): Step | undefined {
  return c.steps.find((s) => !s.done);
}

// The details that count or list things: their numbers and names go into the reader's sentence.
const DETAILS: [RegExp, string, (m: RegExpExecArray) => Record<string, string>][] = [
  [/^(\d+) machines? online\.$/, "online", (m) => ({ n: m[1] })],
  [/^(\d+) machines? online, (\d+) offline\.$/, "onlineOffline", (m) => ({ n: m[1], m: m[2] })],
  [/^(.+) joined but (?:is|are) offline: start the agent there again\.$/, "joinedOffline", (m) => ({ names: m[1] })],
  [/^Not chosen yet on (.+): until you choose, Forgeyard may use every GPU there\. Open the machine and use Change GPU access\.$/,
    "notChosen", (m) => ({ names: m[1] })],
  [/^Chosen on (\d+) machines? with GPUs\.$/, "chosen", (m) => ({ n: m[1] })],
  [/^(\d+) models? in the catalog\.$/, "models", (m) => ({ n: m[1] })],
  [/^Ready: (.+)\.$/, "ready", (m) => ({ names: m[1] })],
  [/^Not ready yet: (.+)\. The first start downloads the model and the engine; that can take a while\.$/, "notReady", (m) => ({ list: m[1] })],
  [/^You have (\d+) active keys?\.$/, "keys", (m) => ({ n: m[1] })],
  [/^You have sent (\d+) requests? with your keys\.$/, "sent", (m) => ({ n: m[1] })],
];

// A step's detail in the reader's language: a counting or listing sentence by its pattern, a fixed one by its
// text (start.sent.*), anything else as the server sent it.
export function detailText(text: string, lang: Lang = getLang()): string {
  if (lang === "en") return text;
  for (const [re, key, vars] of DETAILS) {
    const m = re.exec(text);
    if (!m) continue;
    const v = vars(m);
    if (v.n) v.n = formatNumber(Number(v.n), lang);
    if (v.names) v.names = v.names.split(" (asleep)").join(translateMaybe(lang, "start.detail.asleep", " (asleep)"));
    return translateMaybe(lang, `start.detail.${key}`, text, v);
  }
  return translateMaybe(lang, `start.sent.${text}`, text);
}
