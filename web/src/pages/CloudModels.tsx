import { IconCloud } from "@tabler/icons-react";
import { Link } from "react-router";

import { Notice } from "../components/ui";
import { useT } from "../i18n/react";
import { API_KIND } from "../lib/gateway";
import { ROUTE_KIND_LABEL, ROUTE_KIND_TONE, fallbackProviders, ownNames, routeHome, targetName, targetWhere, tiers } from "../lib/routing";
import type { RouteInfo, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

// Cloud → Cloud models (1.4, #81): the model names an admin made. Those with a provider's model in
// them are cloud models; those with deployments only (several behind one name, or one under a second
// name) are listed after them, since they have no single deployment to live on. A deployment's own
// route is its cloud fallback, on its page: only named here, with a link.
export function CloudModels() {
  const t = useT();
  const { data, error } = useApi<RoutesResponse>("/routes");
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">{t("common.loading")}</p>;
  const { cloud, other } = ownNames(data.routes);
  const fallbacks = data.routes.filter((r) => r.deployment_id !== null && fallbackProviders(data.routes, r.deployment_id).length > 0);
  return (
    <>
      <section className="panel" aria-labelledby="cloud-models-title">
        <div className="panel-head">
          <h2 id="cloud-models-title">{t("cloud.models.page.title")}</h2>
          <span className="muted small">{t("cloud.models.page.what")}</span>
        </div>
        <p className="muted small">{t.rich("cloud.models.page.lead", { model: <code>model</code> })}</p>
        {cloud.length === 0 ? (
          <div className="empty-keys">
            <IconCloud size={22} aria-hidden="true" />
            <p className="muted">{data.is_admin
              ? t.rich("cloud.models.page.emptyAdmin", { link: (s) => <Link to="/cloud/models/new">{s}</Link> })
              : t("cloud.models.page.emptyUser")}</p>
          </div>
        ) : <div className="workloads">{cloud.map((r) => <NameCard key={r.id} r={r} admin={data.is_admin} />)}</div>}
      </section>
      {other.length > 0 && (
        <section className="panel" aria-labelledby="other-names-title">
          <div className="panel-head">
            <h2 id="other-names-title">{t("cloud.models.other.title")}</h2>
            <span className="muted small">{t("cloud.models.other.what")}</span>
          </div>
          <p className="muted small">{t("cloud.models.other.lead")}</p>
          <div className="workloads">{other.map((r) => <NameCard key={r.id} r={r} admin={data.is_admin} />)}</div>
        </section>
      )}
      {fallbacks.length > 0 && (
        <p className="muted small fallback-list">
          {t("cloud.models.fallbacks")}{" "}
          {fallbacks.map((r, i) => (
            <span key={r.id}>{i ? ", " : ""}<Link to={routeHome(r)} className="mono">{r.served_name}</Link> → {fallbackProviders(data.routes, r.deployment_id!).join(", ")}</span>
          ))}
        </p>
      )}
    </>
  );
}

function NameCard({ r, admin }: { r: RouteInfo; admin: boolean }) {
  const t = useT();
  return (
    <article className="wl" aria-label={t("cloud.models.cardAria", { name: r.served_name })}>
      <header className="wl-head">
        <div className="wl-title">
          {r.kind !== "local" && <IconCloud size={16} aria-hidden="true" />}
          <h3 className="mono"><Link to={routeHome(r)}>{r.served_name}</Link></h3>
          <span className={`chip chip-tone-${ROUTE_KIND_TONE[r.kind]}`}>{ROUTE_KIND_LABEL[r.kind]}</span>
          <span className="chip">{API_KIND[r.api] ?? r.api}</span>
          {r.local_only && <span className="chip chip-tone-ok">{t("cloud.models.localOnly")}</span>}
        </div>
        <Link className="btn btn-quiet btn-small" to={routeHome(r)}>{admin ? t("cloud.models.edit") : t("cloud.models.details")}</Link>
      </header>
      <ol className="route-tiers">
        {tiers(r).map((tier, i) => (
          <li key={i}>
            <span className="tier-n">{i === 0 ? t("cloud.models.first") : t("cloud.models.then")}</span>
            {tier.map((x) => (
              <span key={x.id} className={`route-target${x.enabled ? "" : " off"}`}>
                <span className="mono">{targetName(x)}</span> <span className="muted small">{targetWhere(x)}</span>
              </span>
            ))}
          </li>
        ))}
      </ol>
    </article>
  );
}
