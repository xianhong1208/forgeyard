import type { ReactNode } from "react";

import { Mark } from "./Mark";

// Sign-in screens: a yard of machine plates on the left (some hot), the form on the right.
const PLATES = [
  "cpu", "nv", "", "amd", "nv", "", "", "cpu", "", "nv", "amd", "", "", "", "cpu", "nv",
  "", "amd", "", "", "nv", "", "cpu", "", "amd", "", "nv", "", "", "cpu", "", "nv",
];

export function AuthFrame({ title, lead, children }: { title: string; lead?: ReactNode; children: ReactNode }) {
  return (
    <div className="auth">
      <aside className="auth-yard" aria-hidden="true">
        <div className="auth-brand">
          <Mark size={34} />
          <span>Forgeyard</span>
        </div>
        <div className="yard">
          {PLATES.map((kind, i) => (
            <span key={i} className={`plate ${kind ? `plate-${kind}` : ""} ${i % 7 === 3 ? "plate-hot" : ""}`} />
          ))}
        </div>
        <p className="auth-tagline">One control plane for the GPUs and CPUs you own.</p>
      </aside>
      <main className="auth-main">
        <div className="auth-card">
          <div className="auth-brand auth-brand-small">
            <Mark size={28} />
            <span>Forgeyard</span>
          </div>
          <h1>{title}</h1>
          {lead && <div className="lead">{lead}</div>}
          {children}
        </div>
      </main>
    </div>
  );
}
