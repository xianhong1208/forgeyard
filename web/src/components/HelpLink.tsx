import { IconHelpCircle } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { useLang, useT } from "../i18n/react";
import { guideHref } from "../lib/guide";

// "How do I…?": a link into the user guide, in the language the reader chose there.
export function HelpLink({ page, anchor, children }: { page: string; anchor?: string; children: ReactNode }) {
  const lang = useLang();
  return (
    <Link className="help-link" to={guideHref(page, anchor, lang)}>
      <IconHelpCircle size={15} stroke={1.8} aria-hidden="true" />
      <span>{children}</span>
    </Link>
  );
}

export function HelpLinks({ children }: { children: ReactNode }) {
  const t = useT();
  return (
    <nav className="help-links" aria-label={t("common.help")}>
      {children}
    </nav>
  );
}
