import { IconChecklist, IconChevronDown, IconLock, IconLogout, IconMenu2, IconUserCircle, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth";
import { Mark } from "./Mark";
import { Notice } from "./ui";

export function Layout() {
  const { user, state, signOut } = useAuth();
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

  const links = [
    { to: "/machines", label: "Machines" },
    { to: "/workloads", label: "Workloads" },
    { to: "/models", label: "Models" },
    { to: "/playground", label: "Playground" },
    { to: "/keys", label: "API keys" },
    { to: "/usage", label: "Usage" },
    ...(user?.role === "admin"
      ? [
          { to: "/users", label: "Users" },
          { to: "/audit", label: "Audit log" },
        ]
      : []),
  ];

  return (
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
            aria-label={nav ? "Close menu" : "Open menu"}
            aria-expanded={nav}
            onClick={() => setNav((v) => !v)}
          >
            {nav ? <IconX size={20} /> : <IconMenu2 size={20} />}
          </button>
          <nav className={`mainnav${nav ? " open" : ""}`} aria-label="Main">
            {links.map((l) => (
              <NavLink key={l.to} to={l.to} onClick={() => setNav(false)} className={({ isActive }) => (isActive ? "active" : "")}>
                {l.label}
              </NavLink>
            ))}
            <NavLink to="/start" onClick={() => setNav(false)} className={({ isActive }) => `only-narrow${isActive ? " active" : ""}`}>
              Get started
            </NavLink>
            <NavLink to="/account" onClick={() => setNav(false)} className={({ isActive }) => `only-narrow${isActive ? " active" : ""}`}>
              My account
            </NavLink>
            <button type="button" className="only-narrow navbutton" onClick={() => void signOut()}>
              Sign out
            </button>
          </nav>
          <div className="usermenu" ref={menuRef}>
            <button type="button" className="usermenu-button" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((v) => !v)}>
              <IconUserCircle size={20} stroke={1.6} aria-hidden="true" />
              <span className="usermenu-name">{user?.username}</span>
              <span className="usermenu-role">{user?.role}</span>
              <IconChevronDown size={14} aria-hidden="true" />
            </button>
            {menu && (
              <div className="usermenu-panel" role="menu">
                <NavLink to="/start" role="menuitem" onClick={() => setMenu(false)}>
                  <IconChecklist size={16} aria-hidden="true" /> Get started
                </NavLink>
                <NavLink to="/account" role="menuitem" onClick={() => setMenu(false)}>
                  <IconUserCircle size={16} aria-hidden="true" /> My account and sessions
                </NavLink>
                <NavLink to="/trust" role="menuitem" onClick={() => setMenu(false)}>
                  <IconLock size={16} aria-hidden="true" /> Trust this server (HTTPS)
                </NavLink>
                <button type="button" role="menuitem" onClick={() => void signOut()}>
                  <IconLogout size={16} aria-hidden="true" /> Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="content">
        {plainHttp && (
          <div className="plain-http">
            <Notice tone="error">
              <strong>Forgeyard is running without HTTPS.</strong> It was started with <code>TLS=off</code> and this page came over plain HTTP,
              so passwords, session cookies and API keys cross the network unencrypted. Remove <code>TLS=off</code> to use Forgeyard's own
              certificate authority, or put a proxy that terminates TLS in front of it. <NavLink to="/trust">What to do</NavLink>
            </Notice>
          </div>
        )}
        <Outlet />
      </main>
    </div>
  );
}
