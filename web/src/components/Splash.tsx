import { useT } from "../i18n/react";
import { Mark } from "./Mark";

export function Splash({ error, onRetry }: { error?: string | null; onRetry?: () => void }) {
  const t = useT();
  return (
    <div className="splash">
      <Mark size={40} />
      {error ? (
        <>
          <p>{error}</p>
          <button className="btn" type="button" onClick={onRetry}>{t("common.tryAgain")}</button>
        </>
      ) : (
        <p className="muted">{t("common.loading")}</p>
      )}
    </div>
  );
}
