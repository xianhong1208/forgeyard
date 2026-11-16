import { Navigate, useLocation, useParams } from "react-router";

import type { DeploymentsResponse } from "../lib/types";
import { useApi } from "../lib/useApi";

// 1.4 (#88): apps left the console (deprecated; removed in 1.5). An old link to /apps… or /workloads…
// (1.3) opens the deployment when it names a model deployment's workload, and Machines otherwise: that
// is where an app holding a card is still named. The deployments say which workload is whose, so the
// deprecated apps API is not asked.
export function OldApps() {
  const rest = useParams()["*"] ?? "";
  const { hash } = useLocation();
  const id = /^(\d+)\/?$/.exec(rest)?.[1];
  const deps = useApi<DeploymentsResponse>(id ? "/deployments" : null);
  if (!id) return <Navigate to="/machines" replace />;
  if (!deps.data && !deps.error) return <p className="muted">Loading…</p>;
  const dep = deps.data?.deployments.find((d) => String(d.workload.id) === id);
  return <Navigate to={dep ? `/serving/${dep.id}${hash}` : "/machines"} replace />;
}
