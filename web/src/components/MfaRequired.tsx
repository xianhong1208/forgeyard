// Required two-factor sign-in (1.6.0, #39, PR 2): the banner during the grace period, the only page left after it,
// and the admins' setting on Users.
import { useState, type FormEvent } from "react";
import { NavLink } from "react-router";

import { useAuth } from "../auth";
import { formatDate, parseServerTime } from "../i18n/format";
import { useT } from "../i18n/react";
import { api, errorText } from "../lib/api";
import type { MfaLevel, MfaPolicy, User } from "../lib/types";
import { useApi } from "../lib/useApi";
import { HelpLink, HelpLinks } from "./HelpLink";
import { TwoFactorPanel } from "./TwoFactor";
import { Field, Notice, PageHeader } from "./ui";

type Say = (m: { tone: "ok" | "error"; text: string }) => void;

/** Whole days left until `by`, at least 1 while any time is left. */
export function daysLeft(by: string, now: number = Date.now()): number {
  return Math.max(1, Math.ceil((parseServerTime(by).getTime() - now) / 86_400_000));
}

/** The grace period is over and no authenticator yet: the session reaches only the setup. */
export function setupOverdue(user: User | null): boolean {
  return !!user?.mfa_enrolment?.due && !user.has_mfa;
}

export function MfaBanner() {
  const t = useT();
  const { user } = useAuth();
  const e = user?.mfa_enrolment;
  if (!e || e.due || user?.has_mfa) return null;
  return (
    <div className="mfa-banner">
      <Notice tone="warn">
        {t.plural("mfa.required.banner", daysLeft(e.by), { date: formatDate(e.by) })}{" "}
        <NavLink to="/account">{t("mfa.required.bannerLink")}</NavLink>
      </Notice>
    </div>
  );
}

export function EnrolmentRequiredPage() {
  const t = useT();
  const { user } = useAuth();
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  return (
    <>
      <PageHeader index={t("mfa.required.index")} title={t("mfa.required.title")}
        lead={user?.mfa_enrolment ? t("mfa.required.lead", { date: formatDate(user.mfa_enrolment.by) }) : undefined} />
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      <div className="mfa-required-page">
        <TwoFactorPanel say={setMessage} />
      </div>
    </>
  );
}

export function MfaPolicyPanel({ say }: { say: Say }) {
  const t = useT();
  const { data, error, reload } = useApi<MfaPolicy>("/users/mfa-policy");
  const on = data && data.mfa_required !== "off";
  return (
    <section className="panel mfa-policy">
      <h2>{t("admin.mfaPolicy.title")}</h2>
      <p className="muted small">{t("admin.mfaPolicy.lead")}</p>
      <HelpLinks>
        <HelpLink page="how-to" anchor="require-two-factor-for-everyone">{t("mfa.help")}</HelpLink>
      </HelpLinks>
      {error && <Notice tone="error">{error}</Notice>}
      {data && (
        <PolicyForm key={`${data.mfa_required}/${data.mfa_grace_days}`} saved={data}
          onSaved={(text) => { say({ tone: "ok", text }); reload(); }} onError={(text) => say({ tone: "error", text })} />
      )}
      {on && (
        <p className="small mfa-policy-status">
          {data.in_grace_period + data.setup_overdue === 0
            ? t("admin.mfaPolicy.allSet")
            : <>
                {data.in_grace_period > 0 && <span>{t.plural("admin.mfaPolicy.waiting", data.in_grace_period)} </span>}
                {data.setup_overdue > 0 && <span className="strong">{t.plural("admin.mfaPolicy.overdue", data.setup_overdue)}</span>}
              </>}
        </p>
      )}
    </section>
  );
}

function PolicyForm({ saved, onSaved, onError }: { saved: MfaPolicy; onSaved: (text: string) => void; onError: (text: string) => void }) {
  const t = useT();
  const { user } = useAuth();
  const [level, setLevel] = useState<MfaLevel>(saved.mfa_required);
  const [days, setDays] = useState(String(saved.mfa_grace_days));
  const [busy, setBusy] = useState(false);
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api<MfaPolicy>("/users/mfa-policy", { method: "PUT", body: { mfa_required: level, mfa_grace_days: Number(days) } });
      onSaved(t("admin.mfaPolicy.saved"));
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="mfa-policy-form" onSubmit={(e) => void save(e)}>
      <div className="field">
        <label htmlFor="mfa-level">{t("admin.mfaPolicy.level")}</label>
        <select id="mfa-level" value={level} onChange={(e) => setLevel(e.target.value as MfaLevel)}>
          <option value="off">{t("admin.mfaPolicy.off")}</option>
          <option value="admins">{t("admin.mfaPolicy.admins")}</option>
          <option value="everyone">{t("admin.mfaPolicy.everyone")}</option>
        </select>
      </div>
      <Field label={t("admin.mfaPolicy.grace")} name="mfa-grace" type="number" inputMode="numeric" min={0} max={saved.max_grace_days}
        step={1} required value={days} onChange={(e) => setDays(e.target.value)}
        hint={t("admin.mfaPolicy.graceHint", { max: String(saved.max_grace_days) })} />
      {level !== "off" && user && !user.has_mfa && <Notice tone="warn">{t("admin.mfaPolicy.selfNote")}</Notice>}
      <div><button className="btn btn-primary" disabled={busy}>{busy ? t("admin.mfaPolicy.saving") : t("admin.mfaPolicy.save")}</button></div>
    </form>
  );
}
