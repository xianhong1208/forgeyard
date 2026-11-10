import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";

import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Splash } from "./components/Splash";
import { loginPath } from "./lib/nav";
import { AccountPage } from "./pages/Account";
import { ApiKeysPage } from "./pages/ApiKeys";
import { AuditPage } from "./pages/Audit";
import { LoginPage } from "./pages/Login";
import { DeploymentDetailPage } from "./pages/DeploymentDetail";
import { GetStartedPage, HomePage } from "./pages/GetStarted";
import { MachinesPage } from "./pages/Machines";
import { ModelDeployPage } from "./pages/ModelDeploy";
import { ModelsPage } from "./pages/Models";
import { NotFoundPage, AdminsOnly } from "./pages/NotFound";
import { PlaygroundPage } from "./pages/Playground";
import { RegisterPage } from "./pages/Register";
import { SetPasswordPage } from "./pages/SetPassword";
import { SetupPage } from "./pages/Setup";
import { UsagePage } from "./pages/Usage";
import { UsersPage } from "./pages/Users";
import { WorkloadDetailPage } from "./pages/WorkloadDetail";
import { WorkloadNewPage } from "./pages/WorkloadNew";
import { WorkloadsPage } from "./pages/Workloads";

function RequireAuth({ children }: { children: ReactNode }) {
  const { state, loadError, user, ended, reload } = useAuth();
  const location = useLocation();
  if (!state) return <Splash error={loadError} onRetry={reload} />;
  if (state.needs_setup) return <Navigate to="/setup" replace />;
  if (!user) return <Navigate to={loginPath(location.pathname + location.search, ended ?? undefined)} replace />;
  return <>{children}</>;
}

function AdminOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  return user?.role === "admin" ? <>{children}</> : <AdminsOnly />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/set-password" element={<SetPasswordPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<HomePage />} />
        <Route path="/start" element={<GetStartedPage />} />
        <Route path="/machines" element={<MachinesPage />} />
        <Route path="/workloads" element={<WorkloadsPage />} />
        <Route path="/workloads/new" element={<WorkloadNewPage />} />
        <Route path="/workloads/:id" element={<WorkloadDetailPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/models/deploy" element={<ModelDeployPage />} />
        <Route path="/models/deployments/:id" element={<DeploymentDetailPage />} />
        {/* M0-M2 sent "Deployments" to the workloads; model deployments live under Models since M3. */}
        <Route path="/deployments" element={<Navigate to="/models" replace />} />
        <Route path="/playground" element={<PlaygroundPage />} />
        <Route path="/keys" element={<ApiKeysPage />} />
        <Route path="/usage" element={<UsagePage />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="/users" element={<AdminOnly><UsersPage /></AdminOnly>} />
        <Route path="/audit" element={<AdminOnly><AuditPage /></AdminOnly>} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
