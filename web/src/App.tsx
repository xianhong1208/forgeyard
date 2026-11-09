import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";

import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Splash } from "./components/Splash";
import { loginPath } from "./lib/nav";
import { AccountPage } from "./pages/Account";
import { AuditPage } from "./pages/Audit";
import { DeploymentsPage } from "./pages/Deployments";
import { LoginPage } from "./pages/Login";
import { MachinesPage } from "./pages/Machines";
import { NotFoundPage, AdminsOnly } from "./pages/NotFound";
import { RegisterPage } from "./pages/Register";
import { SetPasswordPage } from "./pages/SetPassword";
import { SetupPage } from "./pages/Setup";
import { UsersPage } from "./pages/Users";

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
        <Route index element={<Navigate to="/machines" replace />} />
        <Route path="/machines" element={<MachinesPage />} />
        <Route path="/deployments" element={<DeploymentsPage />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="/users" element={<AdminOnly><UsersPage /></AdminOnly>} />
        <Route path="/audit" element={<AdminOnly><AuditPage /></AdminOnly>} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
