// Licences on the recommended list that come with terms beyond a permissive licence's (1.3): the
// model may be used commercially, but its licence forbids some uses or sets conditions. The card says
// so and links to the model's page, where the licence is. Hugging Face's licence ids.

const OPENRAIL = "OpenRAIL: commercial use is allowed, but the licence forbids some uses. Read it before you deploy.";

const TERMS: Record<string, string> = {
  "openrail": OPENRAIL,
  "openrail++": OPENRAIL,
  "creativeml-openrail-m": OPENRAIL,
  "bigscience-openrail-m": OPENRAIL,
  "gemma": "Gemma terms: commercial use is allowed under Google's prohibited use policy. Read them before you deploy.",
  "llama3.1": "Llama 3.1 community licence: commercial use below 700 million monthly users, with attribution.",
  "llama3.2": "Llama 3.2 community licence: commercial use below 700 million monthly users, with attribution.",
};

/** What a licence asks beyond a permissive one, or null (Apache-2.0, MIT, BSD, or not known). */
export function licenceTerms(licence: string | null | undefined): string | null {
  return licence ? TERMS[licence.toLowerCase()] ?? null : null;
}

/** The Hugging Face page that carries the licence: the original model's for a conversion. */
export function licencePage(source: string, licenseFrom?: string | null): string | null {
  const ref = licenseFrom || source;
  return ref.startsWith("hf://") ? `https://huggingface.co/${ref.slice("hf://".length)}` : null;
}
