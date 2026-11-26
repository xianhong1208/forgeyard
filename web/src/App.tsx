import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";

import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { ExtPage } from "./components/ExtPage";
import { Splash } from "./components/Splash";
import { routes as extRoutes } from "./ext";
import { HtmlLang } from "./i18n/react";
import { loginPath } from "./lib/nav";
import { AccountPage } from "./pages/Account";
import { ApiKeysPage } from "./pages/ApiKeys";
import { AuditPage } from "./pages/Audit";
import { LoginPage } from "./pages/Login";
import { DeploymentDetailPage } from "./pages/DeploymentDetail";
import { DeploymentEditPage } from "./pages/DeploymentEdit";
import { GetStartedPage, HomePage } from "./pages/GetStarted";
import { GuidePage, GuideRedirect } from "./pages/Guide";
import { MachineDetailPage } from "./pages/MachineDetail";
import { MachinesPage } from "./pages/Machines";
import { ModelDeployPage } from "./pages/ModelDeploy";
import { EnginePage } from "./pages/EnginePage";
import { ModelPage } from "./pages/ModelPage";
import { ModelsPage } from "./pages/Models";
import { NotFoundPage, AdminsOnly } from "./pages/NotFound";
import { OldApps } from "./pages/OldApps";
import { PlaygroundPage } from "./pages/Playground";
import { CloudPage, ProviderDetailPage } from "./pages/Providers";
import { OldProviders, OldRoute, RouteDetailPage } from "./pages/RouteDetail";
import { RegisterPage } from "./pages/Register";
import { SetPasswordPage } from "./pages/SetPassword";
import { OldDeploy, OldDeployment, ServingPage } from "./pages/Serving";
import { SetupPage } from "./pages/Setup";
import { SsoCallbackPage } from "./pages/SsoCallback";
import { TrustPage } from "./pages/Trust";
import { UsagePage } from "./pages/Usage";
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
    <>
    <HtmlLang />
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/set-password" element={<SetPasswordPage />} />
      {/* 1.6.0 (#39): the identity provider sends the browser back here; the page finishes the sign-in. */}
      <Route path="/sso/callback" element={<SsoCallbackPage />} />
      {/* Public: a device gets the CA before anyone signs in on it. */}
      <Route path="/trust" element={<TrustPage />} />
      {/* Public too: the guide explains how to install the server and add machines. */}
      <Route path="/guide" element={<GuideRedirect />} />
      <Route path="/guide/:lang" element={<GuideRedirect />} />
      <Route path="/guide/:lang/:slug" element={<GuidePage />} />
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
        <Route path="/machines/:id" element={<MachineDetailPage />} />
        {/* 1.4 (#88): apps left the console. 1.3's /workloads… and 1.4's /apps… open the model's
            deployment, or Machines (where an app holding a card is still named). */}
        <Route path="/apps/*" element={<OldApps />} />
        <Route path="/workloads/*" element={<OldApps />} />
        {/* 1.4 (#92): Serving is what runs now, with Deploy a model and each deployment's page; Models
            is the library (Catalog, Engines, Sources). 1.3's addresses under /models open the new places. */}
        <Route path="/serving" element={<ServingPage />} />
        <Route path="/serving/deploy" element={<ModelDeployPage />} />
        <Route path="/serving/:id" element={<DeploymentDetailPage />} />
        <Route path="/serving/:id/edit" element={<DeploymentEditPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/models/deploy" element={<OldDeploy />} />
        <Route path="/models/deployments/*" element={<OldDeployment />} />
        {/* 1.4 (#81): routes live where they belong (a deployment's page, or Cloud → Cloud models), and
            Providers became Cloud. The old addresses open the new places. */}
        <Route path="/models/routes/:id" element={<OldRoute />} />
        {/* 1.4.3 (#140): a page per catalog model and per engine. The words of the routes above (and engines) are
            refused as model names by the API, so a model's page never hides one of them. */}
        <Route path="/models/engines" element={<Navigate to="/models?tab=engines" replace />} />
        <Route path="/models/engines/:name" element={<EnginePage />} />
        <Route path="/models/:name" element={<ModelPage />} />
        <Route path="/cloud" element={<AdminOnly><CloudPage /></AdminOnly>} />
        <Route path="/cloud/providers/:id" element={<AdminOnly><ProviderDetailPage /></AdminOnly>} />
        <Route path="/cloud/models/:id" element={<RouteDetailPage />} />
        <Route path="/providers/*" element={<OldProviders />} />
        {/* M0-M2 sent "Deployments" to the workloads; model deployments were under Models from M3 to 1.3. */}
        <Route path="/deployments" element={<Navigate to="/serving" replace />} />
        <Route path="/playground" element={<PlaygroundPage />} />
        <Route path="/keys" element={<ApiKeysPage />} />
        <Route path="/usage" element={<UsagePage />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="/users" element={<AdminOnly><UsersPage /></AdminOnly>} />
        <Route path="/audit" element={<AdminOnly><AuditPage /></AdminOnly>} />
        {/* 1.6.1 (#190): an extension's pages (web/src/ext; none in the community edition). */}
        {extRoutes.map((r) => <Route key={r.path} path={r.path} element={<ExtPage route={r}>{r.element}</ExtPage>} />)}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
    </>
  );
}
