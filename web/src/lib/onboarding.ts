// The Get started checklist (GET /api/onboarding) and the example call it shows.

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
