// The server's messages in the reader's language, by their code (#189). The API never changes: English
// readers see what the server sent, and a code without a translation is shown as sent too.
import { hasKey, interpolate, lookup, translate } from "./core";
import { getLang, type Lang } from "./lang";

// The text for an error code. `sent` is the server's own message, if any.
export function serverMessage(code: string | undefined, sent?: string | null, lang: Lang = getLang()): string {
  const key = `server.code.${code}`;
  const known = Boolean(code) && hasKey(key);
  if (lang === "en" && sent) return sent;
  if (!known) return sent || translate(lang, "server.generic");
  const template = lookup(lang, key);
  // "{message}" carries the server's specifics; without them, the sentence ends before it.
  if (!sent) return template.replace(/\s*[:：]?\s*\{message\}/, "").trim();
  return interpolate(template, { message: sent }, lang);
}

// A deployment event's kind ("placed", "woken"), or the kind as sent.
export function eventKindLabel(kind: string, lang: Lang = getLang()): string {
  const key = `server.event.${kind}`;
  return hasKey(key) ? lookup(lang, key) : kind;
}
