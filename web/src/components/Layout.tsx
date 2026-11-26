import { IconBook2, IconCheck, IconChecklist, IconChevronDown, IconLanguage, IconLock, IconLogout, IconMenu2, IconUserCircle, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth";
import { navItems as extNav } from "../ext";
import { LANG_NAME, LANGS, setLang } from "../i18n/lang";
import { useT } from "../i18n/react";
import { shown, useInfo } from "../lib/info";
import type { TeamsResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { AdvisorProvider, HealthBadge } from "./Advisor";
import { Mark } from "./Mark";
import { EnrolmentRequiredPage, MfaBanner, setupOverdue } from "./MfaRequired";
import { Notice } from "./ui";

export function Layout() {
  const { user, state, signOut } = useAuth();
  const t = useT();
  // TLS=off and this page came over plain HTTP (not through a TLS proxy): admins must know.
  const plainHttp = user?.role === "admin" && state?.tls?.mode === "off" && !state.tls.https;
  const [menu, setMenu] = useState(false);
  const [nav, setNav] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return;
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);

  // 1.4 (#81, #92): Machines, Serving (what runs now), Models (the library), Cloud (cloud providers and
  // cloud models, admins only, as Providers was), the everyday pages, then the admin pages. Apps left
  // the console in 1.4 (#88).
  // 1.6.0 (#39): two-factor sign-in is required of this account and its grace period is over. Only the setup is
  // shown (the server refuses everything else with 403 mfa_enrolment_required), with the guide, language and sign-out.
  const overdue = setupOverdue(user);
  const admin = user?.role === "admin" && !overdue;
  const info = useInfo(extNav.length > 0 && !overdue);
  // 1.6.1 (#190): Teams is in the menu for admins, and for everyone else once there is a team they can see.
  const teams = useApi<TeamsResponse>(user && !admin && !overdue ? "/teams" : null);
  const showTeams = admin || (teams.data?.teams.length ?? 0) > 0;
  const links = overdue ? [] : [
    { to: "/machines", label: t("shell.nav.machines") },
    { to: "/serving", label: t("shell.nav.serving") },
    { to: "/models", label: t("shell.nav.models") },
    ...(admin ? [{ to: "/cloud", label: t("shell.nav.cloud") }] : []),
    { to: "/playground", label: t("shell.nav.playground") },
    { to: "/keys", label: t("shell.nav.keys") },
    { to: "/usage", label: t("shell.nav.usage") },
    ...(showTeams ? [{ to: "/teams", label: t("shell.nav.teams") }] : []),
    ...(admin
      ? [
          { to: "/users", label: t("shell.nav.users") },
          { to: "/audit", label: t("shell.nav.audit") },
        ]
      : []),
    // 1.6.1 (#190): an extension's pages, once /api/info lists their features (none in the community edition).
    ...extNav.filter((n) => shown(info, n, admin)).map((n) => ({ to: n.to, label: n.label(t) })),
  ];

  return (
    <AdvisorProvider enabled={admin}>
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand" onClick={() => setNav(false)}>
            <Mark size={26} />
            <span>Forgeyard</span>
          </NavLink>
          <button
            type="button"
            className="nav-toggle"
            aria-label={nav ? t("shell.menu.close") : t("shell.menu.open")}
            aria-expanded={nav}
            onClick={() => setNav((v) => !v)}
          >
            {nav ? <IconX size={20} /> : <IconMenu2 size={20} />}
          </button>
          <nav className={`mainnav${nav ? " open" : ""}`} aria-label={t("shell.nav.main")}>
            {links.map((l) => (
              <NavLink key={l.to} to={l.to} onClick={() => setNav(false)} className={({ isActive }) => (isActive ? "active" : "")}>
                {l.label}
              </NavLink>
            ))}
            {!overdue && (
              <NavLink to="/start" onClick={() => setNav(false)} className={({ isActive }) => `only-narrow${isActive ? " active" : ""}`}>
                {t("shell.nav.start")}
              </NavLink>
            )}
            <NavLink to="/guide" onClick={() => setNav(false)} className="only-narrow">
              {t("shell.nav.guide")}
            </NavLink>
            {!overdue && (
              <NavLink to="/account" onClick={() => setNav(false)} className={({ isActive }) => `only-narrow${isActive ? " active" : ""}`}>
                {t("shell.nav.account")}
              </NavLink>
            )}
            <button type="button" className="only-narrow navbutton" onClick={() => void signOut()}>
              {t("shell.menu.signOut")}
            </button>
            <LangSwitch className="only-narrow nav-langs" onPick={() => setNav(false)} />
          </nav>
          {!overdue && <HealthBadge onClick={() => setNav(false)} />}
          <div className="usermenu" ref={menuRef}>
            <button type="button" className="usermenu-button" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
              <IconUserCircle size={20} stroke={1.6} aria-hidden="true" />
              <span className="usermenu-name">{user?.username}</span>
              <span className="usermenu-role">{user ? t.maybe(`common.role.${user.role}`, user.role) : null}</span>
              <IconChevronDown size={14} aria-hidden="true" />
            </button>
            {menu && (
              <div className="usermenu-panel" role="menu">
                {!overdue && (
                  <NavLink to="/start" role="menuitem" onClick={() => setMenu(false)}>
                    <IconChecklist size={16} aria-hidden="true" /> {t("shell.menu.start")}
                  </NavLink>
                )}
                <NavLink to="/guide" role="menuitem" onClick={() => setMenu(false)}>
                  <IconBook2 size={16} aria-hidden="true" /> {t("shell.menu.guide")}
                </NavLink>
                {!overdue && (
                  <NavLink to="/account" role="menuitem" onClick={() => setMenu(false)}>
                    <IconUserCircle size={16} aria-hidden="true" /> {t("shell.menu.account")}
                  </NavLink>
                )}
                <NavLink to="/trust" role="menuitem" onClick={() => setMenu(false)}>
                  <IconLock size={16} aria-hidden="true" /> {t("shell.menu.trust")}
                </NavLink>
                <button type="button" role="menuitem" onClick={() => void signOut()}>
                  <IconLogout size={16} aria-hidden="true" /> {t("shell.menu.signOut")}
                </button>
                <LangSwitch className="usermenu-langs" inMenu />
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="content">
        {plainHttp && (
          <div className="plain-http">
            <Notice tone="error">
              {t.rich("shell.plainHttp", {
                off: <code>TLS=off</code>,
                strong: (text) => <strong>{text}</strong>,
                link: (text) => <NavLink to="/trust">{text}</NavLink>,
              })}
            </Notice>
          </div>
        )}
        {overdue ? <EnrolmentRequiredPage /> : <><MfaBanner /><Outlet /></>}
      </main>
    </div>
    </AdvisorProvider>
  );
}

// English / 繁體中文, each named in itself: in the account menu, and in the narrow screens' menu. The choice is
// kept in this browser and is the guide's too.
function LangSwitch({ className, onPick, inMenu }: { className: string; onPick?: () => void; inMenu?: boolean }) {
  const t = useT();
  return (
    <div className={`langswitch ${className}`} role="group" aria-label={t("shell.menu.language")}>
      <span className="langswitch-label"><IconLanguage size={16} aria-hidden="true" /> {t("shell.menu.language")}</span>
      <span className="langswitch-options">
        {LANGS.map((l) => (
          <button key={l} type="button" lang={l} className={t.lang === l ? "on" : ""}
            {...(inMenu ? { role: "menuitemradio", "aria-checked": t.lang === l } : { "aria-pressed": t.lang === l })}
            onClick={() => { setLang(l); onPick?.(); }}>
            {t.lang === l && <IconCheck size={13} aria-hidden="true" />}{LANG_NAME[l]}
          </button>
        ))}
      </span>
    </div>
  );
}
