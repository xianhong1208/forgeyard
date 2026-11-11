import { useEffect } from "react";

export const APP_NAME = "Forgeyard";

/** The browser tab's title for a page: "Machines · Forgeyard", or just "Forgeyard". */
export function documentTitle(page?: string | null): string {
  const p = (page ?? "").trim();
  return p && p !== APP_NAME ? `${p} · ${APP_NAME}` : APP_NAME;
}

/**
 * Sets the tab's title while the calling page is shown and puts the plain name back when it goes,
 * so a page never keeps the title of the one before it (the guide's, for example).
 */
export function useDocumentTitle(page?: string | null): void {
  useEffect(() => {
    document.title = documentTitle(page);
    return () => {
      document.title = APP_NAME;
    };
  }, [page]);
}
