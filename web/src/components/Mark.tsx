// The Forgeyard mark: an "F" plate with one hot ingot in the corner.
export function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="mark">
      <rect width="32" height="32" rx="7" fill="var(--mark-plate)" />
      <path d="M8 9h16v3.5H12v3h9v3.5h-9V24H8z" fill="var(--mark-ink)" />
      <rect x="18" y="20" width="6" height="4" rx="1" fill="var(--heat)" />
    </svg>
  );
}
