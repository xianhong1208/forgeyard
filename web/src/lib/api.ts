// The one place that talks to the server.
//
// Only a 401 means "you are not signed in": it calls the unauthorized handler, which sends
// the browser to the sign-in page. 403, 429 and 5xx are errors to show where they happen —
// a rate limit or a server hiccup must never sign anyone out.

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
        : { message: Array.isArray(raw) ? "Some fields are not valid." : undefined };
    const retry = Number(res.headers.get("Retry-After"));
    const error = new ApiError(res.status, detail, Number.isFinite(retry) && retry > 0 ? retry : undefined);
    if (res.status === 401 && !opts.expect401) unauthorized?.(detail.reason);
    throw error;
  }
  return data as T;
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429 && e.retryAfter) return `${e.message} (about ${e.retryAfter} s)`;
    if (e.status >= 500) return "The server had a problem. Try again in a moment.";
    return e.message;
  }
  return "Something went wrong.";
}
