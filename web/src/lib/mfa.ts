// Two-factor sign-in (1.6.0, #39): what the browser needs to know about codes.
import { encode } from "uqr";

export type Factor = { id: number; kind: "totp"; name: string; created_at: string; last_used_at: string | null };
export type MfaStatus = { factors: Factor[]; recovery_codes_left: number; max_factors: number };
export type Enrolment = { secret: string; uri: string; enrolment: string; expires_in: number };

/** Six digits (spaces allowed): a code from the app. Anything else typed in the same box is a password. */
export function isAppCode(value: string): boolean {
  return /^\d{6}$/.test(value.replace(/\s+/g, ""));
}

/** A setup key in groups of four, as apps show it: "ABCD EFGH …". */
export function groupKey(secret: string): string {
  return secret.replace(/\s+/g, "").replace(/(.{4})(?=.)/g, "$1 ");
}

/** The body for "your password or a current code": one box, sent as whichever it is. */
export function reauthBody(value: string): { password?: string; code?: string } {
  return isAppCode(value) ? { code: value.replace(/\s+/g, "") } : { password: value };
}

/** The QR code's modules (true = dark), with a quiet zone of 4 modules, at error correction level M. */
export function qrModules(text: string): boolean[][] {
  return encode(text, { ecc: "M", border: 4 }).data;
}

/** The recovery codes as a text file: a header line, then one code per line. */
export function recoveryFile(header: string, codes: string[]): string {
  return [header, "", ...codes, ""].join("\n");
}
