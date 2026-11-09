export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  const seconds = Math.round((Date.now() - d.getTime()) / 1000);
  const abs = Math.abs(seconds);
  const suffix = seconds >= 0 ? "ago" : "from now";
  if (abs < 45) return seconds >= 0 ? "just now" : "in a moment";
  if (abs < 3600) return `${Math.round(abs / 60)} min ${suffix}`;
  if (abs < 86400) return `${Math.round(abs / 3600)} h ${suffix}`;
  if (abs < 86400 * 14) return `${Math.round(abs / 86400)} d ${suffix}`;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function exact(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return d.toLocaleString();
}

// "Chrome on Linux" from a user agent string; good enough to tell sessions apart.
export function device(ua: string | null | undefined): string {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\//.test(ua)
      ? "Firefox"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : /curl\//i.test(ua)
            ? "curl"
            : "Browser";
  const os = /iPhone|iPad/.test(ua)
    ? "iOS"
    : /Android/.test(ua)
      ? "Android"
      : /Mac OS X/.test(ua)
        ? "macOS"
        : /Windows/.test(ua)
          ? "Windows"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  return os ? `${browser} on ${os}` : browser;
}
