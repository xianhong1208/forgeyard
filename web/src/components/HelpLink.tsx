import { IconHelpCircle } from "@tabler/icons-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { guideHref } from "../lib/guide";

// "How do I…?": a link into the user guide, in the language the reader chose there.
export function HelpLink({ page, anchor, children }: { page: string; anchor?: string; children: ReactNode }) {
  return (
    <Link className="help-link" to={guideHref(page, anchor)}>
      <IconHelpCircle size={15} stroke={1.8} aria-hidden="true" />
      <span>{children}</span>
    </Link>
  );
}

export function HelpLinks({ children }: { children: ReactNode }) {
  return (
    <nav className="help-links" aria-label="Help">
      {children}
    </nav>
  );
}
