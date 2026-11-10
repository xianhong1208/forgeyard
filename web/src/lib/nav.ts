// Where to go after signing in. Only same-site paths are accepted, so a crafted
// ?next=https://evil.example (or //evil.example) cannot bounce people off the site.

// "/" shows the Get started list until it is done or hidden, then sends people on to MACHINES.
export const HOME = "/";
export const MACHINES = "/machines";
const AUTH_PAGES = ["/login", "/setup", "/set-password", "/register"];

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

export const ENDED_TEXT: Record<string, string> = {
  idle: "You were signed out after 2 hours without activity.",
  expired: "Your session reached its 7-day limit. Sign in again.",
  signed_out: "You signed out.",
  signed_out_elsewhere: "This session was ended from another browser.",
  signed_out_by_admin: "An admin signed you out.",
  password_changed: "Your password changed, so this session ended.",
  account_disabled: "Your account was disabled.",
};
