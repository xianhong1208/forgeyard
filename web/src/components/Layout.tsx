import { IconChevronDown, IconLogout, IconMenu2, IconUserCircle, IconX } from "@tabler/icons-react";
import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth";
import { Mark } from "./Mark";

export function Layout() {
  const { user, signOut } = useAuth();
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
    { to: "/deployments", label: "Deployments" },
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
          <NavLink to="/machines" className="brand" onClick={() => setNav(false)}>
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
                <NavLink to="/account" role="menuitem" onClick={() => setMenu(false)}>
                  <IconUserCircle size={16} aria-hidden="true" /> My account and sessions
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
        <Outlet />
      </main>
    </div>
  );
}
