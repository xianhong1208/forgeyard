// Where the identity provider sends the browser back (1.6.0, #39, PR 3). The provider's redirect is a cross-site
// navigation, so the server's SameSite=Strict session cookie cannot be set on it: this page sends the code and state
// to /api/auth/sso/finish from our own origin instead, and the session cookie is set on that answer.
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { useAuth } from "../auth";
import { AuthFrame } from "../components/AuthFrame";
import { Notice } from "../components/ui";
import { useT } from "../i18n/react";
import { api, ApiError, errorText } from "../lib/api";
import { safeNext } from "../lib/nav";
import type { FinishAnswer } from "../lib/sso";

type Outcome = { kind: "pending"; username: string } | { kind: "failed"; text: string } | { kind: "missing" };

export function SsoCallbackPage() {
  const t = useT();
  const navigate = useNavigate();
  const { setUser, reload } = useAuth();
  const location = useLocation();
  // Read once: the effect below takes the code out of the address bar.
  const [query] = useState(() => new URLSearchParams(location.search));
  const [outcome, setOutcome] = useState<Outcome | null>(() => (query.get("state") ? null : { kind: "missing" }));
  const sent = useRef(false); // once: a state works only once (React's development mode runs effects twice)

  useEffect(() => {
    // The code must not stay in the address bar, the history or a Referer.
    if (query.toString()) navigate("/sso/callback", { replace: true });
    const state = query.get("state");
    if (sent.current || !state) return;
    sent.current = true;
    const body = { state, code: query.get("code"), error: query.get("error"), error_description: query.get("error_description") };
    api<FinishAnswer>("/auth/sso/finish", { method: "POST", body, expect401: true }).then(
      (r) => {
        if (r.purpose === "login") setUser(r.user);
        void reload(); // the session's sign-in method and freshness
        navigate(safeNext(r.next), { replace: true, state: { sso: r.purpose } });
      },
      (err) => {
        if (err instanceof ApiError && err.code === "pending_approval") {
          setOutcome({ kind: "pending", username: String(err.detail.username ?? "") });
        } else {
          setOutcome({ kind: "failed", text: errorText(err) });
        }
      },
    );
  }, [navigate, query, reload, setUser]);

  if (!outcome) {
    return (
      <AuthFrame title={t("sso.callback.title")}>
        <p className="muted">{t("sso.callback.working")}</p>
      </AuthFrame>
    );
  }
  if (outcome.kind === "pending") {
    return (
      <AuthFrame title={t("sso.callback.pendingTitle")}>
        <Notice>{outcome.username ? t("sso.callback.pending", { username: outcome.username }) : t("server.code.pending_approval")}</Notice>
        <p className="auth-foot"><Link to="/login">{t("auth.backToSignIn")}</Link></p>
      </AuthFrame>
    );
  }
  return (
    <AuthFrame title={t("sso.callback.failedTitle")}>
      <Notice tone="error">{outcome.kind === "missing" ? t("sso.callback.missing") : outcome.text}</Notice>
      <p className="auth-foot"><Link to="/login">{t("auth.backToSignIn")}</Link></p>
      <p className="auth-foot"><Link to="/account">{t("sso.callback.backToAccount")}</Link></p>
    </AuthFrame>
  );
}
