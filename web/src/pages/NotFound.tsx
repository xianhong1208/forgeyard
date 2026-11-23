import { Link } from "react-router";

import { PageHeader } from "../components/ui";
import { useT } from "../i18n/react";

export function NotFoundPage() {
  const t = useT();
  return (
    <>
      <PageHeader index="404" title={t("shell.notFound.title")} lead={t("shell.notFound.lead")} />
      <Link className="btn" to="/machines">{t("shell.goToMachines")}</Link>
    </>
  );
}

export function AdminsOnly() {
  const t = useT();
  return (
    <>
      <PageHeader index="403" title={t("shell.adminsOnly.title")} lead={t("shell.adminsOnly.lead")} />
      <Link className="btn" to="/machines">{t("shell.goToMachines")}</Link>
    </>
  );
}
