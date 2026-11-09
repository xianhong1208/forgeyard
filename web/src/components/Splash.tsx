import { Mark } from "./Mark";

export function Splash({ error, onRetry }: { error?: string | null; onRetry?: () => void }) {
  return (
    <div className="splash">
      <Mark size={40} />
      {error ? (
        <>
          <p>{error}</p>
          <button className="btn" type="button" onClick={onRetry}>Try again</button>
        </>
      ) : (
        <p className="muted">Loading…</p>
      )}
    </div>
  );
}
