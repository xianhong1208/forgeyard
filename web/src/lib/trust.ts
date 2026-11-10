// The "Trust this server" page: which instructions to open first.

export type Platform = "windows" | "macos" | "linux" | "firefox" | "ios" | "android";

export function platformOf(ua: string): Platform {
  if (/iPhone|iPad|iPod/.test(ua)) return "ios";
  if (/Android/.test(ua)) return "android";
  if (/Firefox\//.test(ua)) return "firefox"; // Firefox keeps its own list of authorities on every desktop system
  if (/Windows/.test(ua)) return "windows";
  if (/Mac OS X|Macintosh/.test(ua)) return "macos";
  return "linux";
}
