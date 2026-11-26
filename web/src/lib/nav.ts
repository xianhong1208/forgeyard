import { labels } from "../i18n/core";

// Where to go after signing in. Only same-site paths are accepted, so a crafted
// ?next=https://evil.example (or //evil.example) cannot bounce people off the site.

// "/" shows the Get started list until it is done or hidden, then sends people on to MACHINES.
export const HOME = "/";
export const MACHINES = "/machines";
const AUTH_PAGES = ["/login", "/setup", "/set-password", "/register", "/sso/callback"];

export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return HOME;
  if ([...next].some((ch) => ch.charCodeAt(0) < 32)) return HOME;
  const path = next.split(/[?#]/)[0];
  if (AUTH_PAGES.includes(path)) return HOME;
  return next;
}

export function loginPath(current: string, reason?: string): string {
  const params = new URLSearchParams();
  if (current && safeNext(current) === current) params.set("next", current);
  if (reason) params.set("ended", reason);
  const qs = params.toString();
  return qs ? `/login?${qs}` : "/login";
}

// Why the last session ended (the server's reason code), in the reader's language.
export const ENDED_TEXT: Record<string, string> = labels({
  idle: "auth.ended.idle",
  expired: "auth.ended.expired",
  signed_out: "auth.ended.signed_out",
  signed_out_elsewhere: "auth.ended.signed_out_elsewhere",
  signed_out_by_admin: "auth.ended.signed_out_by_admin",
  password_changed: "auth.ended.password_changed",
  account_disabled: "auth.ended.account_disabled",
  mfa_reset: "auth.ended.mfa_reset",
});
