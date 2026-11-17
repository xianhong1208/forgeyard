// The secret-key backup dialog's rules (1.4.2, issue #127). The server checks the same.

export const BACKUP_PASSPHRASE_MIN = 12;
export const BACKUP_PASSPHRASE_MAX = 1024;

// Why the passphrase cannot be used yet, or null.
export function passphraseProblem(passphrase: string, confirm: string, password: string): string | null {
  if (passphrase.length < BACKUP_PASSPHRASE_MIN) return `Use a passphrase of at least ${BACKUP_PASSPHRASE_MIN} characters.`;
  if (passphrase.length > BACKUP_PASSPHRASE_MAX) return `Use a passphrase of at most ${BACKUP_PASSPHRASE_MAX} characters.`;
  if (new Set(passphrase).size <= 2) return "Use more than two different characters in the passphrase.";
  if (password && passphrase === password) return "Choose a passphrase that is not your password.";
  if (confirm !== passphrase) return "The two passphrases are not the same.";
  return null;
}

// The file name from Content-Disposition (attachment; filename="…"), kept only when it is the
// backup's own pattern: never a path.
export function backupFilename(disposition: string | null): string | null {
  const m = /filename="([^"]+)"/.exec(disposition ?? "");
  return m && /^forgeyard-key-backup-[0-9a-f]{8}-\d{8}\.fykey$/.test(m[1]) ? m[1] : null;
}
