// The secret-key backup dialog's rules (1.4.2, issue #127). The server checks the same.
import { t } from "../i18n/core";

export const BACKUP_PASSPHRASE_MIN = 12;
export const BACKUP_PASSPHRASE_MAX = 1024;

// Why the passphrase cannot be used yet, or null.
export function passphraseProblem(passphrase: string, confirm: string, password: string): string | null {
  if (passphrase.length < BACKUP_PASSPHRASE_MIN) return t("advisor.backup.tooShort", { min: BACKUP_PASSPHRASE_MIN });
  if (passphrase.length > BACKUP_PASSPHRASE_MAX) return t("advisor.backup.tooLong", { max: BACKUP_PASSPHRASE_MAX });
  if (new Set(passphrase).size <= 2) return t("advisor.backup.tooSimple");
  if (password && passphrase === password) return t("advisor.backup.isPassword");
  if (confirm !== passphrase) return t("advisor.backup.mismatch");
  return null;
}

// The file name from Content-Disposition (attachment; filename="…"), kept only when it is the
// backup's own pattern: never a path.
export function backupFilename(disposition: string | null): string | null {
  const m = /filename="([^"]+)"/.exec(disposition ?? "");
  return m && /^forgeyard-key-backup-[0-9a-f]{8}-\d{8}\.fykey$/.test(m[1]) ? m[1] : null;
}
