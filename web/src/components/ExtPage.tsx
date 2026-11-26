import type { ReactNode } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth";
import type { ExtRoute } from "../ext";
import { useT } from "../i18n/react";
import { useInfo } from "../lib/info";
import { AdminsOnly } from "../pages/NotFound";
import { PageHeader } from "./ui";

// An extension's page (1.6.1, #190; web/src/ext): shown while /api/info lists its feature; otherwise it says the
// page is not available on this server (its server part is missing or switched off) instead of failing.
export function ExtPage({ route, children }: { route: ExtRoute; children: ReactNode }) {
  const t = useT();
  const { user } = useAuth();
  const info = useInfo();
  if (route.admin && user?.role !== "admin") return <AdminsOnly />;
  if (!info) return null;
  if (!info.features.includes(route.feature)) {
    return (
      <>
        <PageHeader index="—" title={t("shell.unavailable.title")} lead={t("shell.unavailable.lead")} />
        <Link className="btn" to="/machines">{t("shell.goToMachines")}</Link>
      </>
    );
  }
  return <>{children}</>;
}
