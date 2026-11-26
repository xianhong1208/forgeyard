// Single sign-on through an OpenID Connect provider (1.6.0, #39, PR 3): what the browser needs.
import { parseServerTime } from "../i18n/format";
import { api } from "./api";
import type { Role } from "./types";

export type SsoPurpose = "login" | "link" | "reauth";

export type LinkedIdentity = { id: number; provider: string | null; email: string | null; created_at: string; last_login_at: string | null };

export type ProviderSettings = {
  id: number;
  kind: "oidc";
  name: string;
  issuer: string;
  client_id: string;
  has_client_secret: boolean;
  scopes: string;
  role_claim: string | null;
  role_mapping: Record<string, Role>;
  new_accounts: "approve" | "automatic";
  allowed_domains: string[];
  enabled: boolean;
  linked_accounts: number;
};

export type ProviderAnswer = {
  provider: ProviderSettings | null;
  redirect_uri: string;
  public_url_set: boolean;
  available: boolean;
  ca_file: boolean;
};

export type CheckAnswer =
  | { ok: true; issuer: string; authorization_endpoint: string; token_endpoint: string; jwks_uri: string; algorithms?: string[] | null }
  | { ok: false; issuer: string; code: string; reason: string; problem: string };

export type FinishAnswer = { user: import("./types").User; next: string; purpose: SsoPurpose; fresh_until?: string | null };

/** How the page leaves for the provider (tests replace it: jsdom cannot navigate). */
export const away = { go: (url: string) => window.location.assign(url) };

/** Where the browser goes to sign in at the provider; the page leaves for it. */
export async function startSso(purpose: SsoPurpose, extra: { next?: string; password?: string; code?: string } = {}): Promise<void> {
  const r = await api<{ url: string }>("/auth/sso/start", { method: "POST", body: { purpose, ...extra } });
  away.go(r.url);
}

/** The session signed in again through the provider within the last few minutes. */
export function isFresh(until: string | null | undefined, now: number = Date.now()): boolean {
  return !!until && parseServerTime(until).getTime() > now;
}

/** The role mapping as three comma-separated lists (one per role), and back. */
export function mappingToLists(mapping: Record<string, Role>): Record<Role, string> {
  const out: Record<Role, string[]> = { admin: [], operator: [], user: [] };
  for (const [value, role] of Object.entries(mapping)) out[role]?.push(value);
  return { admin: out.admin.join(", "), operator: out.operator.join(", "), user: out.user.join(", ") };
}

export function listsToMapping(lists: Record<Role, string>): Record<string, Role> {
  const out: Record<string, Role> = {};
  // Lower roles first, so a value typed under two roles ends up with the higher one.
  for (const role of ["user", "operator", "admin"] as Role[]) {
    for (const v of lists[role].split(",").map((x) => x.trim()).filter(Boolean)) out[v] = role;
  }
  return out;
}

export function splitList(raw: string): string[] {
  return raw.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
}
