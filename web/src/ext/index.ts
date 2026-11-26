// Extension slots (1.6.1, #190). The community console fills none of them. A separately distributed package (the
// enterprise edition) builds this console with its own copy of this file (a Vite alias on this path), whose lists
// hold its pages; the community pages render whatever the lists hold. Each entry names the feature it needs: it is
// shown only while GET /api/info lists that feature (server side: forgeyard/extensions.py), so a page whose server
// part is missing or switched off says so instead of failing.
//
// Internal and versioned to the exact release, like the server's extension interface: not a public plugin API.
import type { ReactNode } from "react";

import type { Translator } from "../i18n/react";
import type { Team } from "../lib/types";

/** A page of its own, under the signed-in frame. */
export type ExtRoute = { path: string; feature: string; admin?: boolean; element: ReactNode };
/** A link in the main menu, after the community's. */
export type ExtNavItem = { to: string; feature: string; admin?: boolean; label: (t: Translator) => string };
/** A tab on the Usage page, after Requests and Cost. */
export type ExtUsageTab = { id: string; feature: string; admin?: boolean; label: (t: Translator) => string; render: () => ReactNode };
/** A section at the end of a team's page (1.6.1: added with the Teams page), given the team as the page shows it. */
export type ExtTeamPageTab = { id: string; feature: string; admin?: boolean; label: (t: Translator) => string; render: (team: Team) => ReactNode };
/** A card at the end of Setup health (admins). */
export type ExtSetupHealthCard = { id: string; feature: string; render: () => ReactNode };

export const routes: ExtRoute[] = [];
export const navItems: ExtNavItem[] = [];
export const usageTabs: ExtUsageTab[] = [];
export const teamPageTabs: ExtTeamPageTab[] = [];
export const setupHealthCards: ExtSetupHealthCard[] = [];
