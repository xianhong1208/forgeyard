// The one place that talks to the server.
//
// Only a 401 means "you are not signed in": it calls the unauthorized handler, which sends
// the browser to the sign-in page. 403, 429 and 5xx are errors to show where they happen —
// a rate limit or a server hiccup must never sign anyone out.

import { hasKey, translate } from "../i18n/core";
import { getLang } from "../i18n/lang";
import { serverMessage } from "../i18n/server";

export type ErrorDetail = { code?: string; message?: string; reason?: string; retry_after?: string; [extra: string]: unknown };

export class ApiError extends Error {
  status: number;
  code: string;
  reason?: string;
  retryAfter?: number;
  detail: ErrorDetail;

  constructor(status: number, detail: ErrorDetail, retryAfter?: number) {
    super(detail.message || `Request failed (${status})`);
    this.detail = detail;
    this.status = status;
    this.code = detail.code || "error";
    this.reason = detail.reason;
    this.retryAfter = retryAfter;
  }
}

type Handler = (reason?: string) => void;
let unauthorized: Handler | null = null;

export function setUnauthorizedHandler(handler: Handler | null) {
  unauthorized = handler;
}

// For requests made without api() (the playground streams with fetch itself): a 401 still means
// "sign in again".
export function notifyUnauthorized(reason?: string) {
  unauthorized?.(reason);
}

export type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  // Sign-in style calls answer 401 for a wrong password: that is not an ended session.
  expect401?: boolean;
};

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") {
    headers["X-Forgeyard"] = "1"; // CSRF: the server refuses writes without it
    headers["Content-Type"] = "application/json";
  }
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch {
    throw new ApiError(0, { code: "network", message: "Cannot reach the Forgeyard server." });
  }
  const text = await res.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const raw = (data as { detail?: unknown } | null)?.detail;
    const detail: ErrorDetail =
      typeof raw === "object" && raw !== null && !Array.isArray(raw)
        ? (raw as ErrorDetail)
        : Array.isArray(raw) ? { code: "invalid_fields", message: "Some fields are not valid." } : {};
    const retry = Number(res.headers.get("Retry-After"));
    const error = new ApiError(res.status, detail, Number.isFinite(retry) && retry > 0 ? retry : undefined);
    if (res.status === 401 && !opts.expect401) unauthorized?.(detail.reason);
    throw error;
  }
  return data as T;
}

// What to show for a failed request, in the reader's language: English shows the server's message as it came;
// zh-TW translates the common codes (i18n/en/server.ts) and shows any other message as sent.
export function errorText(e: unknown, lang = getLang()): string {
  if (e instanceof ApiError) {
    const failed = translate(lang, "server.failed", { status: String(e.status) });
    const said = e.detail.message ? serverMessage(e.code, e.detail.message, lang)
      : lang !== "en" && e.detail.code && hasKey(`server.code.${e.code}`) ? serverMessage(e.code, null, lang) : failed;
    if (e.status === 429 && e.retryAfter) return translate(lang, "server.retryAbout", { message: said, seconds: e.retryAfter });
    if (e.status >= 500) return translate(lang, "server.serverProblem");
    return said;
  }
  return translate(lang, "server.generic");
}
