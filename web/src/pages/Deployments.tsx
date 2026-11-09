import { IconBox, IconPlayerPlay, IconTerminal2 } from "@tabler/icons-react";

import { PageHeader } from "../components/ui";

const KINDS = [
  { icon: IconBox, name: "Services", detail: "Long-running: an API, a model server, a web app" },
  { icon: IconPlayerPlay, name: "Tasks", detail: "Run once to the end: a batch job, a fine-tune" },
  { icon: IconTerminal2, name: "Dev environments", detail: "A shell or notebook on a machine with a card" },
];

export function DeploymentsPage() {
  return (
    <>
      <PageHeader index="02 · Deployments" title="Deployments" lead="Work running on your machines: services, tasks and dev environments." />
      <section className="empty">
        <div className="empty-art empty-art-lanes" aria-hidden="true">
          <span className="lane" />
          <span className="lane lane-hot" />
          <span className="lane" />
        </div>
        <div className="empty-copy">
          <h2>Nothing deployed yet</h2>
          <p>Deployments need machines first. Once agents have joined, this is where you start and watch work.</p>
          <ul className="kinds">
            {KINDS.map(({ icon: Icon, name, detail }) => (
              <li key={name}>
                <Icon size={18} stroke={1.6} aria-hidden="true" />
                <span className="kind-name">{name}</span>
                <span className="kind-detail">{detail}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </>
  );
}
