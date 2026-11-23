import { t } from "../i18n/core";
import { formatDateTime, formatRelative } from "../i18n/format";

// A server timestamp relative to now ("5 min ago"), and in full for a tooltip, in the reader's language.
export function when(value: string | null | undefined): string {
  return formatRelative(value);
}

export function exact(value: string | null | undefined): string {
  return formatDateTime(value);
}

// "Chrome on Linux" from a user agent string; good enough to tell sessions apart.
export function device(ua: string | null | undefined): string {
  if (!ua) return t("admin.device.unknown");
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
            : t("admin.device.browser");
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
  return os ? t("admin.device.on", { browser, os }) : browser;
}

// An engine image as people read it: "vllm/vllm-openai:v0.32.0" for "vllm/vllm-openai:v0.32.0@sha256:…".
// The agent runs the digest; the tag says which release that is.
export function imageTag(image: string | null | undefined): string {
  if (!image) return "";
  const at = image.indexOf("@sha256:");
  return at > 0 && image.slice(0, at).split("/").pop()!.includes(":") ? image.slice(0, at) : image;
}
