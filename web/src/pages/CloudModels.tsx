import { IconCloud } from "@tabler/icons-react";
import { Link } from "react-router";

import { Notice } from "../components/ui";
import { API_KIND } from "../lib/gateway";
import { ROUTE_KIND_LABEL, ROUTE_KIND_TONE, fallbackProviders, ownNames, routeHome, targetName, targetWhere, tiers } from "../lib/routing";
import type { RouteInfo, RoutesResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

// Cloud → Cloud models (1.4, #81): the model names an admin made. Those with a provider's model in
// them are cloud models; those with deployments only (several behind one name, or one under a second
// name) are listed after them, since they have no single deployment to live on. A deployment's own
// route is its cloud fallback, on its page: only named here, with a link.
export function CloudModels() {
  const { data, error } = useApi<RoutesResponse>("/routes");
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!data) return <p className="muted">Loading…</p>;
  const { cloud, other } = ownNames(data.routes);
  const fallbacks = data.routes.filter((r) => r.deployment_id !== null && fallbackProviders(data.routes, r.deployment_id).length > 0);
  return (
    <>
      <section className="panel" aria-labelledby="cloud-models-title">
        <div className="panel-head">
          <h2 id="cloud-models-title">Cloud models</h2>
          <span className="muted small">a provider's model under a name of its own at /v1</span>
        </div>
        <p className="muted small">
          Clients send the name as <code>model</code>; Forgeyard's keys, limits, budgets and usage apply. Only keys whose cloud setting is
          Allowed see a cloud model that has nothing local in it.
        </p>
        {cloud.length === 0 ? (
          <div className="empty-keys">
            <IconCloud size={22} aria-hidden="true" />
            <p className="muted">No cloud models yet. {data.is_admin
              ? <>Add a provider and its models, then <Link to="/cloud/models/new">make a cloud model</Link>.</>
              : "Admins make them."}</p>
          </div>
        ) : <div className="workloads">{cloud.map((r) => <NameCard key={r.id} r={r} admin={data.is_admin} />)}</div>}
      </section>
      {other.length > 0 && (
        <section className="panel" aria-labelledby="other-names-title">
          <div className="panel-head">
            <h2 id="other-names-title">Other names for deployments</h2>
            <span className="muted small">no cloud model in them</span>
          </div>
          <p className="muted small">
            Names an admin made for deployments: several behind one name, or one under a second name. Each deployment's page lists the
            names it also answers as.
          </p>
          <div className="workloads">{other.map((r) => <NameCard key={r.id} r={r} admin={data.is_admin} />)}</div>
        </section>
      )}
      {fallbacks.length > 0 && (
        <p className="muted small fallback-list">
          Deployments with a cloud fallback (on their own pages):{" "}
          {fallbacks.map((r, i) => (
            <span key={r.id}>{i ? ", " : ""}<Link to={routeHome(r)} className="mono">{r.served_name}</Link> → {fallbackProviders(data.routes, r.deployment_id!).join(", ")}</span>
          ))}
        </p>
      )}
    </>
  );
}

function NameCard({ r, admin }: { r: RouteInfo; admin: boolean }) {
  return (
    <article className="wl" aria-label={`Model name ${r.served_name}`}>
      <header className="wl-head">
        <div className="wl-title">
          {r.kind !== "local" && <IconCloud size={16} aria-hidden="true" />}
          <h3 className="mono"><Link to={routeHome(r)}>{r.served_name}</Link></h3>
          <span className={`chip chip-tone-${ROUTE_KIND_TONE[r.kind]}`}>{ROUTE_KIND_LABEL[r.kind]}</span>
          <span className="chip">{API_KIND[r.api] ?? r.api}</span>
          {r.local_only && <span className="chip chip-tone-ok">local only</span>}
        </div>
        <Link className="btn btn-quiet btn-small" to={routeHome(r)}>{admin ? "Edit" : "Details"}</Link>
      </header>
      <ol className="route-tiers">
        {tiers(r).map((tier, i) => (
          <li key={i}>
            <span className="tier-n">{i === 0 ? "First" : "Then"}</span>
            {tier.map((t) => (
              <span key={t.id} className={`route-target${t.enabled ? "" : " off"}`}>
                <span className="mono">{targetName(t)}</span> <span className="muted small">{targetWhere(t)}</span>
              </span>
            ))}
          </li>
        ))}
      </ol>
    </article>
  );
}
