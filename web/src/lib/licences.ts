// Licences on the recommended list that come with terms beyond a permissive licence's (1.3): the
// model may be used commercially, but its licence forbids some uses or sets conditions. The card says
// so and links to the model's page, where the licence is. Hugging Face's licence ids.
import { t, type Key } from "../i18n/core";

const TERMS: Record<string, Key> = {
  "openrail": "addmodel.terms.openrail",
  "openrail++": "addmodel.terms.openrail",
  "creativeml-openrail-m": "addmodel.terms.openrail",
  "bigscience-openrail-m": "addmodel.terms.openrail",
  "gemma": "addmodel.terms.gemma",
  "llama3.1": "addmodel.terms.llama31",
  "llama3.2": "addmodel.terms.llama32",
};

/** What a licence asks beyond a permissive one, in the reader's language, or null (Apache-2.0, MIT, BSD, or not known). */
export function licenceTerms(licence: string | null | undefined): string | null {
  const key = licence ? TERMS[licence.toLowerCase()] : undefined;
  return key ? t(key as "addmodel.terms.openrail") : null;
}

/** The Hugging Face page that carries the licence: the original model's for a conversion. */
export function licencePage(source: string, licenseFrom?: string | null): string | null {
  const ref = licenseFrom || source;
  return ref.startsWith("hf://") ? `https://huggingface.co/${ref.slice("hf://".length)}` : null;
}
