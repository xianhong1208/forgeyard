// Single sign-on (1.6.0, #39, PR 3): "Sign in with <name>", My account's linked account, the fresh sign-in that
// stands in for a password, and the admins' provider settings on Users.
import { IconExternalLink, IconLink, IconPlugConnected } from "@tabler/icons-react";
import { useState, type FormEvent } from "react";
import { useLocation } from "react-router";

import { useAuth } from "../auth";
import { useT } from "../i18n/react";
import { api, ApiError, errorText } from "../lib/api";
import { exact, when } from "../lib/format";
import { reauthBody } from "../lib/mfa";
import {
  isFresh, listsToMapping, mappingToLists, splitList, startSso,
  type CheckAnswer, type LinkedIdentity, type ProviderAnswer, type ProviderSettings,
} from "../lib/sso";
import type { Role } from "../lib/types";
import { useApi } from "../lib/useApi";
import { HelpLink, HelpLinks } from "./HelpLink";
import { CopyBox, Field, Notice } from "./ui";

type Say = (m: { tone: "ok" | "error"; text: string }) => void;

/** The sign-in page's "Sign in with <name>". */
export function SignInWith({ name, next }: { name: string; next: string }) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await startSso("login", { next });
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  return (
    <div className="sso-signin">
      <p className="sso-or"><span>{t("sso.or")}</span></p>
      {error && <Notice tone="error">{error}</Notice>}
      <button type="button" className="btn btn-wide sso-button" disabled={busy} onClick={() => void go()}>
        <IconExternalLink size={16} aria-hidden="true" /> {busy ? t("sso.leaving", { name }) : t("sso.signInWith", { name })}
      </button>
    </div>
  );
}

/** For an account without a password: a fresh sign-in at the provider, in place of typing the password. */
export function ConfirmWithSso({ leadKey = "sso.fresh.lead" }: { leadKey?: "sso.fresh.lead" | "sso.fresh.leadMfa" | "sso.fresh.leadBackup" }) {
  const t = useT();
  const { state } = useAuth();
  const location = useLocation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = state?.sso?.name;
  if (isFresh(state?.session?.fresh_until)) return <Notice tone="ok">{t("sso.fresh.done", { name: name ?? "" })}</Notice>;
  if (!name) return <Notice tone="error">{t("sso.fresh.unavailable")}</Notice>;
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await startSso("reauth", { next: location.pathname + location.search });
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  return (
    <div className="sso-confirm stack">
      <p className="small">{t(leadKey, { name })}</p>
      {error && <Notice tone="error">{error}</Notice>}
      <div>
        <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void go()}>
          <IconExternalLink size={16} aria-hidden="true" /> {busy ? t("sso.leaving", { name }) : t("sso.fresh.button", { name })}
        </button>
      </div>
    </div>
  );
}

/** An account that can only sign in through the provider (no password), on a server where it is set up. */
export function useNoPassword(): boolean {
  const { user } = useAuth();
  return !!user && !user.has_password;
}

type Identities = { identities: LinkedIdentity[]; provider: { name: string } | null; fresh_until: string | null };

/** My account: the account at the identity provider, linked or not. */
export function SsoLinkPanel({ say }: { say: Say }) {
  const t = useT();
  const { user } = useAuth();
  const location = useLocation();
  const { data, error, reload } = useApi<Identities>("/auth/sso/identities");
  const [auth, setAuth] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!data || (!data.provider && data.identities.length === 0)) return error ? <Notice tone="error">{error}</Notice> : null;
  const name = data.provider?.name ?? data.identities[0]?.provider ?? "";
  const onlyWay = !!user && !user.has_password && data.identities.length === 1;

  const link = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await startSso("link", { next: location.pathname, ...reauthBody(auth) });
    } catch (err) {
      setProblem(errorText(err));
      if (err instanceof ApiError && (err.code === "wrong_password" || err.code === "mfa_code_invalid")) setAuth("");
      setBusy(false);
    }
  };

  const unlink = async (i: LinkedIdentity) => {
    if (!window.confirm(t("sso.panel.unlinkConfirm", { name: i.provider ?? name }))) return;
    try {
      await api("/auth/sso/unlink", { method: "POST", body: { id: i.id } });
      say({ tone: "ok", text: t("sso.panel.unlinked", { name: i.provider ?? name }) });
      reload();
    } catch (err) {
      say({ tone: "error", text: errorText(err) });
    }
  };

  return (
    <section className="panel sso-panel">
      <div className="panel-head">
        <h2><IconLink size={18} aria-hidden="true" /> {t("sso.panel.title")}</h2>
        <span className={`chip ${data.identities.length ? "chip-status-active" : ""}`}>
          {data.identities.length ? t("sso.panel.chipLinked") : t("sso.panel.chipNotLinked")}
        </span>
      </div>
      {data.identities.length > 0 ? (
        <>
          <p className="muted small">{t("sso.panel.leadLinked", { name })}</p>
          <ul className="mfa-factors">
            {data.identities.map((i) => (
              <li key={i.id}>
                <div className="mfa-factor-main">
                  <span className="strong">{i.provider ?? name}{i.email && <span className="muted"> · {i.email}</span>}</span>
                  <span className="muted small">
                    <span title={exact(i.created_at)}>{t("sso.panel.linkedAt", { when: when(i.created_at) })}</span>
                    {i.last_login_at && <> · <span title={exact(i.last_login_at)}>{t("sso.panel.lastUsed", { when: when(i.last_login_at) })}</span></>}
                  </span>
                </div>
                <button type="button" className="btn btn-quiet btn-small" disabled={onlyWay}
                  title={onlyWay ? t("sso.panel.onlyWay") : undefined} onClick={() => void unlink(i)}>
                  {t("sso.panel.unlink")}
                </button>
              </li>
            ))}
          </ul>
          {onlyWay && <p className="muted small">{t("sso.panel.onlyWay")}</p>}
        </>
      ) : (
        <form className="stack" onSubmit={(e) => void link(e)}>
          <p className="muted small">{t("sso.panel.leadUnlinked", { name })}</p>
          <Field label={t("sso.panel.auth")} name="sso-link-auth" type="password" autoComplete="current-password" required
            value={auth} onChange={(e) => setAuth(e.target.value)} />
          {problem && <Notice tone="error">{problem}</Notice>}
          <div>
            <button className="btn btn-primary" disabled={busy}>
              <IconExternalLink size={16} aria-hidden="true" /> {busy ? t("sso.leaving", { name }) : t("sso.panel.link", { name })}
            </button>
          </div>
        </form>
      )}
      <HelpLinks>
        <HelpLink page="how-to" anchor="sign-in-with-single-sign-on-oidc">{t("sso.help")}</HelpLink>
      </HelpLinks>
    </section>
  );
}

type Form = {
  name: string; issuer: string; client_id: string; client_secret: string; no_secret: boolean; scopes: string;
  role_claim: string; lists: Record<Role, string>; new_accounts: "approve" | "automatic"; domains: string; enabled: boolean;
};

function formOf(p: ProviderSettings | null): Form {
  return {
    name: p?.name ?? "", issuer: p?.issuer ?? "", client_id: p?.client_id ?? "", client_secret: "",
    no_secret: !!p && !p.has_client_secret, scopes: p?.scopes ?? "openid email profile", role_claim: p?.role_claim ?? "",
    lists: mappingToLists(p?.role_mapping ?? {}), new_accounts: p?.new_accounts ?? "approve",
    domains: (p?.allowed_domains ?? []).join(", "), enabled: p?.enabled ?? true,
  };
}

/** The body the API takes: the secret only when one was typed (or "" to remove it). */
export function providerBody(f: Form, stored: ProviderSettings | null) {
  const secret = f.no_secret ? "" : f.client_secret ? f.client_secret : stored ? undefined : null;
  return {
    name: f.name.trim(), issuer: f.issuer.trim(), client_id: f.client_id.trim(),
    ...(secret === undefined ? {} : { client_secret: secret }),
    scopes: f.scopes, role_claim: f.role_claim.trim() || null, role_mapping: listsToMapping(f.lists),
    new_accounts: f.new_accounts, allowed_domains: splitList(f.domains), enabled: f.enabled,
  };
}

/** Users (admins): the identity provider. */
export function SsoProviderPanel({ say }: { say: Say }) {
  const t = useT();
  const { reload: reloadAuth } = useAuth();
  const { data, error, reload } = useApi<ProviderAnswer>("/identity-provider");
  return (
    <section className="panel sso-admin">
      <div className="panel-head">
        <h2><IconPlugConnected size={18} aria-hidden="true" /> {t("sso.admin.title")}</h2>
        {data && <span className={`chip ${data.provider?.enabled ? "chip-status-active" : ""}`}>
          {data.provider?.enabled ? t("sso.admin.on") : t("sso.admin.off")}
        </span>}
      </div>
      <p className="muted small">{t("sso.admin.lead")}</p>
      <HelpLinks>
        <HelpLink page="how-to" anchor="sign-in-with-single-sign-on-oidc">{t("sso.help")}</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {data && !data.available && <Notice tone="warn">{t("sso.admin.notAvailable")}</Notice>}
      {data && (
        <ProviderForm key={JSON.stringify(data.provider)} answer={data}
          onSaved={(text) => { say({ tone: "ok", text }); reload(); void reloadAuth(); }}
          onError={(text) => say({ tone: "error", text })} />
      )}
    </section>
  );
}

function ProviderForm({ answer, onSaved, onError }: { answer: ProviderAnswer; onSaved: (text: string) => void; onError: (text: string) => void }) {
  const t = useT();
  const stored = answer.provider;
  const [f, setF] = useState<Form>(() => formOf(stored));
  const [busy, setBusy] = useState(false);
  const [check, setCheck] = useState<CheckAnswer | null>(null);
  const [checking, setChecking] = useState(false);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((old) => ({ ...old, [k]: v }));
  const setList = (role: Role, v: string) => setF((old) => ({ ...old, lists: { ...old.lists, [role]: v } }));

  const runCheck = async () => {
    setChecking(true);
    setCheck(null);
    try {
      setCheck(await api<CheckAnswer>("/identity-provider/check", { method: "POST", body: { issuer: f.issuer.trim() } }));
    } catch (err) {
      onError(errorText(err));
    } finally {
      setChecking(false);
    }
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/identity-provider", { method: "PUT", body: providerBody(f, stored) });
      onSaved(t("sso.admin.saved"));
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!stored || !window.confirm(t.plural("sso.admin.deleteConfirm", stored.linked_accounts, { name: stored.name }))) return;
    try {
      await api("/identity-provider", { method: "DELETE" });
      onSaved(t("sso.admin.deleted"));
    } catch (err) {
      onError(errorText(err));
    }
  };

  return (
    <form className="sso-form" onSubmit={(e) => void save(e)}>
      <div className="sso-redirect">
        <CopyBox label={t("sso.admin.redirect")} value={answer.redirect_uri} />
        {!answer.public_url_set && <p className="field-hint">{t("sso.admin.noPublicUrl")}</p>}
      </div>
      <Field label={t("sso.admin.name")} name="sso-name" required maxLength={64} value={f.name} placeholder="Keycloak"
        onChange={(e) => set("name", e.target.value)} hint={t("sso.admin.nameHint", { name: f.name || "Keycloak" })} />
      <div className="sso-issuer">
        <Field label={t("sso.admin.issuer")} name="sso-issuer" required maxLength={512} value={f.issuer} inputMode="url"
          placeholder="https://keycloak.example.com/realms/forgeyard" onChange={(e) => { set("issuer", e.target.value); setCheck(null); }}
          hint={t("sso.admin.issuerHint")} />
        <button type="button" className="btn btn-quiet" disabled={checking || !f.issuer.trim()} onClick={() => void runCheck()}>
          {checking ? t("sso.admin.checking") : t("sso.admin.check")}
        </button>
      </div>
      {check && (check.ok
        ? <Notice tone="ok">{t("sso.admin.checkOk", { issuer: check.issuer, algs: (check.algorithms ?? ["RS256"]).join(", ") })}</Notice>
        : <Notice tone="error">{t("sso.admin.checkFail", { problem: check.problem })}</Notice>)}
      <Field label={t("sso.admin.clientId")} name="sso-client-id" required maxLength={255} value={f.client_id}
        autoComplete="off" onChange={(e) => set("client_id", e.target.value)} />
      <div className="sso-secret">
        <Field label={t("sso.admin.secret")} name="sso-client-secret" type="password" autoComplete="new-password" maxLength={1024}
          value={f.client_secret} disabled={f.no_secret} onChange={(e) => set("client_secret", e.target.value)}
          hint={stored?.has_client_secret ? t("sso.admin.secretKeep") : t("sso.admin.secretHint")} />
        <label className="check small">
          <input type="checkbox" checked={f.no_secret} onChange={(e) => set("no_secret", e.target.checked)} /> {t("sso.admin.noSecret")}
        </label>
      </div>
      <Field label={t("sso.admin.scopes")} name="sso-scopes" maxLength={255} value={f.scopes}
        onChange={(e) => set("scopes", e.target.value)} hint={t("sso.admin.scopesHint")} />
      <fieldset className="sso-fieldset">
        <legend>{t("sso.admin.newAccounts")}</legend>
        <label className="check">
          <input type="radio" name="sso-new" checked={f.new_accounts === "approve"} onChange={() => set("new_accounts", "approve")} /> {t("sso.admin.approve")}
        </label>
        <label className="check">
          <input type="radio" name="sso-new" checked={f.new_accounts === "automatic"} onChange={() => set("new_accounts", "automatic")} /> {t("sso.admin.automatic")}
        </label>
        <Field label={t("sso.admin.domains")} name="sso-domains" value={f.domains} placeholder="example.com"
          required={f.new_accounts === "automatic"} onChange={(e) => set("domains", e.target.value)} hint={t("sso.admin.domainsHint")} />
      </fieldset>
      <details className="sso-roles" open={!!f.role_claim}>
        <summary>{t("sso.admin.roles")}</summary>
        <div className="stack">
          <Field label={t("sso.admin.roleClaim")} name="sso-role-claim" maxLength={64} value={f.role_claim} placeholder="groups"
            onChange={(e) => set("role_claim", e.target.value)} hint={t("sso.admin.roleClaimHint")} />
          <Field label={t("sso.admin.mapAdmin")} name="sso-map-admin" value={f.lists.admin} onChange={(e) => setList("admin", e.target.value)} />
          <Field label={t("sso.admin.mapOperator")} name="sso-map-operator" value={f.lists.operator} onChange={(e) => setList("operator", e.target.value)} />
          <Field label={t("sso.admin.mapUser")} name="sso-map-user" value={f.lists.user} onChange={(e) => setList("user", e.target.value)}
            hint={t("sso.admin.mapHint")} />
        </div>
      </details>
      <label className="check">
        <input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} /> {t("sso.admin.enabled")}
      </label>
      {stored && <p className="small muted">{t.plural("sso.admin.linkedCount", stored.linked_accounts)}</p>}
      <div className="row-gap">
        <button className="btn btn-primary" disabled={busy || !answer.available}>{busy ? t("sso.admin.saving") : t("sso.admin.save")}</button>
        {stored && <button type="button" className="btn btn-danger" onClick={() => void remove()}>{t("sso.admin.delete")}</button>}
      </div>
    </form>
  );
}
