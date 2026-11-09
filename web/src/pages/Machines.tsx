import { IconCpu, IconCloud, IconServer2 } from "@tabler/icons-react";

import { PageHeader } from "../components/ui";

const KINDS = [
  { icon: IconServer2, name: "NVIDIA", detail: "CUDA cards, read through NVML" },
  { icon: IconServer2, name: "AMD", detail: "ROCm cards, read through amd-smi" },
  { icon: IconCpu, name: "CPU only", detail: "Machines without a GPU" },
  { icon: IconCloud, name: "Cloud", detail: "Rented machines, added later" },
];

export function MachinesPage() {
  return (
    <>
      <PageHeader index="01 · Machines" title="Machines" lead="Every computer Forgeyard can run work on, with its cards, memory and load." />
      <section className="empty">
        <div className="empty-art" aria-hidden="true">
          {Array.from({ length: 12 }, (_, i) => (
            <span key={i} className={`slot${i === 5 ? " slot-hot" : ""}`} />
          ))}
        </div>
        <div className="empty-copy">
          <h2>No machines yet</h2>
          <p>
            A machine joins when the Forgeyard agent runs on it and reports its hardware. Adding agents is the next
            milestone; this page lists them once they can join.
          </p>
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
