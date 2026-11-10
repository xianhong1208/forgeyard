import { IconMinus, IconPlus } from "@tabler/icons-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { useAuth } from "../auth";
import { Notice, PageHeader } from "../components/ui";
import { api, errorText } from "../lib/api";
import { VENDOR_NAME } from "../lib/machines";
import type { Machine, MachinesResponse, WorkloadResponse } from "../lib/types";
import { useApi } from "../lib/useApi";
import { allowedCards, allowlistSummary, buildSpec, canRunWorkloads, emptyForm, gpuCards, type WorkloadForm } from "../lib/workloads";

function Segmented<T extends string>({ label, value, options, onChange }: {
  label: string; value: T; options: { value: T; label: string; hint?: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="field">
      <span className="field-label" id={`seg-${label}`}>{label}</span>
      <div className="segmented" role="radiogroup" aria-labelledby={`seg-${label}`}>
        {options.map((o) => (
          <button key={o.value} type="button" role="radio" aria-checked={value === o.value}
            className={value === o.value ? "on" : ""} onClick={() => onChange(o.value)} title={o.hint}>
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Input({ label, hint, value, onChange, ...rest }: {
  label: string; hint?: ReactNode; value: string; onChange: (v: string) => void; placeholder?: string;
  inputMode?: "numeric" | "decimal" | "text"; required?: boolean; id?: string; className?: string;
}) {
  const id = rest.id ?? `w-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className={`field ${rest.className ?? ""}`}>
      <label htmlFor={id}>{label}</label>
      <input id={id} value={value} onChange={(e) => onChange(e.target.value)} placeholder={rest.placeholder}
        inputMode={rest.inputMode} required={rest.required} autoComplete="off" spellCheck={false} />
      {hint && <p className="field-hint">{hint}</p>}
    </div>
  );
}

function Lines({ label, hint, value, onChange, placeholder }: {
  label: string; hint?: ReactNode; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  const id = `w-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <textarea id={id} rows={3} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        spellCheck={false} className="mono" />
      {hint && <p className="field-hint">{hint}</p>}
    </div>
  );
}

export function WorkloadNewPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { data: machines } = useApi<MachinesResponse>("/machines");
  const [f, setF] = useState<WorkloadForm>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof WorkloadForm>(k: K, v: WorkloadForm[K]) => setF((cur) => ({ ...cur, [k]: v }));

  if (user && user.role === "user") {
    return (
      <>
        <PageHeader index="02 · Workloads" title="New workload" />
        <Notice>Only admins and operators start workloads: creating one runs code on a machine.</Notice>
      </>
    );
  }

  const usable = (machines?.machines ?? []).filter((m) => m.status !== "revoked");
  const chosen: Machine | undefined = usable.find((m) => String(m.id) === f.machine);
  const spec = buildSpec(f);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<WorkloadResponse>("/workloads", { method: "POST", body: spec });
      navigate(`/workloads/${r.workload.id}`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleIndex = (i: number) =>
    set("gpuIndices", f.gpuIndices.includes(i) ? f.gpuIndices.filter((x) => x !== i) : [...f.gpuIndices, i]);

  return (
    <>
      <PageHeader index="02 · Workloads" title="New workload"
        lead="A container or a process, on a machine you pick or the first online machine with room. It starts as soon as it is placed." />
      <form className="wl-form" onSubmit={submit} aria-label="New workload">
        <section className="panel">
          <h2>What</h2>
          <div className="grid-2">
            <Segmented label="Kind" value={f.kind} onChange={(v) => set("kind", v)} options={[
              { value: "service", label: "Service", hint: "Runs until stopped" },
              { value: "task", label: "Task", hint: "Runs to the end" },
            ]} />
            <Input label="Name" value={f.name} onChange={(v) => set("name", v)} placeholder="web-demo" required
              hint="a-z, 0-9 and '-'; unique." />
          </div>
        </section>

        <section className="panel">
          <h2>Run</h2>
          <Segmented label="Runtime" value={f.runtime} onChange={(v) => set("runtime", v)} options={[
            { value: "container", label: "Container", hint: "Docker or Podman on the machine" },
            { value: "process", label: "Process", hint: "A program on the machine, as its workload user" },
          ]} />
          {f.runtime === "container" ? (
            <div className="grid-2">
              <Input label="Image" value={f.image} onChange={(v) => set("image", v)} placeholder="python:3.13-slim" required />
              <Lines label="Entrypoint (optional)" value={f.entrypoint} onChange={(v) => set("entrypoint", v)}
                hint="Replaces the image's entrypoint. One item per line." />
            </div>
          ) : (
            <div className="grid-2">
              <Input label="Command" value={f.command} onChange={(v) => set("command", v)} placeholder="python3" required
                hint="A program on the basic PATH, or an absolute path." />
              <Input label="Working directory (optional)" value={f.workdir} onChange={(v) => set("workdir", v)}
                placeholder="/srv/app" hint="Default: the replica's own empty directory (also its HOME)." />
            </div>
          )}
          <Lines label="Arguments" value={f.args} onChange={(v) => set("args", v)}
            placeholder={f.runtime === "container" ? "python\n-m\nhttp.server\n8000" : "-m\nhttp.server\n${PORT}"}
            hint={<>One argument per line, no shell quoting. {f.runtime === "process" && <><code>{"${PORT}"}</code> becomes the assigned host port.</>}</>} />
        </section>

        <section className="panel">
          <h2>Where</h2>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="w-machine">Machine</label>
              <select id="w-machine" value={f.machine} onChange={(e) => setF({ ...f, machine: e.target.value, gpuIndices: [],
                gpuMode: f.gpuMode === "indices" && !e.target.value ? "count" : f.gpuMode })}>
                <option value="">Any online machine with room</option>
                {usable.map((m) => (
                  <option key={m.id} value={m.id} disabled={!canRunWorkloads(m)}>
                    {m.name} · {m.status}{canRunWorkloads(m) ? ` · Forgeyard may use ${allowlistSummary(m)}` : " · agent too old for workloads"}
                  </option>
                ))}
              </select>
              <p className="field-hint">The first online machine that has the runtime, the free cards and the declared CPU and memory.</p>
            </div>
            <Segmented label="GPUs" value={f.gpuMode} onChange={(v) => set("gpuMode", v)} options={[
              { value: "none", label: "None" },
              { value: "count", label: "How many" },
              ...(chosen ? [{ value: "indices" as const, label: "These cards" }] : []),
            ]} />
          </div>
          {f.gpuMode === "count" && (
            <div className="grid-3">
              <Input label="GPU count" value={f.gpuCount} onChange={(v) => set("gpuCount", v)} inputMode="numeric" />
              <div className="field">
                <label htmlFor="w-vendor">Vendor</label>
                <select id="w-vendor" value={f.vendor} onChange={(e) => set("vendor", e.target.value as WorkloadForm["vendor"])}>
                  <option value="">Any</option>
                  <option value="nvidia">NVIDIA</option>
                  <option value="amd">AMD</option>
                </select>
              </div>
            </div>
          )}
          {f.gpuMode === "indices" && chosen && (
            <fieldset className="cards-pick">
              <legend>Cards on {chosen.name}</legend>
              {gpuCards(chosen).length === 0 && <p className="muted small">This machine has no GPU.</p>}
              {gpuCards(chosen).map((a) => {
                const blocked = a.allowed === false ? "not allowed for Forgeyard" : a.assigned_to ? `in use by ${a.assigned_to}` : null;
                return (
                  <label key={a.key} className={`card-pick${blocked ? " card-blocked" : ""}`}>
                    <input type="checkbox" checked={f.gpuIndices.includes(a.index)} disabled={!!blocked}
                      onChange={() => toggleIndex(a.index)} />
                    <span className="mono">#{a.index}</span>
                    <span>{a.model.toUpperCase().startsWith(VENDOR_NAME[a.vendor]?.toUpperCase() ?? "") ? a.model : `${VENDOR_NAME[a.vendor]} ${a.model}`}</span>
                    {blocked && <span className="muted small">{blocked}</span>}
                  </label>
                );
              })}
              {allowedCards(chosen).length < gpuCards(chosen).length && (
                <p className="field-hint">Cards outside this machine's allowlist are never assigned.</p>
              )}
            </fieldset>
          )}
          <div className="grid-3">
            <Input label="CPU cores" value={f.cpu} onChange={(v) => set("cpu", v)} inputMode="decimal" placeholder="2"
              hint="Counted for placement; capped with a systemd scope or in containers." />
            <Input label="Memory" value={f.memory} onChange={(v) => set("memory", v)} placeholder="4Gi" />
            <Input label="Replicas" value={f.replicas} onChange={(v) => set("replicas", v)} inputMode="numeric" />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Environment</h2>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => set("env", [...f.env, { key: "", value: "" }])}>
              <IconPlus size={14} aria-hidden="true" /> Add variable
            </button>
          </div>
          <p className="muted small">
            A workload starts from an empty environment: only what you list here
            {f.runtime === "process" ? " (plus PATH, HOME, LANG and the user)" : ""}, its GPU variables, its ports and
            FORGEYARD_* identity. Nothing of the agent's or the host's is passed on.
          </p>
          {f.env.map((row, i) => (
            <div className="kv" key={i}>
              <input aria-label={`Variable ${i + 1} name`} className="mono" placeholder="NAME" value={row.key}
                onChange={(e) => set("env", f.env.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))} />
              <input aria-label={`Variable ${i + 1} value`} className="mono" placeholder="value" value={row.value}
                onChange={(e) => set("env", f.env.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))} />
              <button type="button" className="btn btn-quiet btn-small" aria-label={`Remove variable ${i + 1}`}
                onClick={() => set("env", f.env.filter((_, j) => j !== i))}><IconMinus size={14} aria-hidden="true" /></button>
            </div>
          ))}
        </section>

        {f.kind === "service" ? (
          <section className="panel">
            <div className="panel-head">
              <h2>Ports and health</h2>
              <button type="button" className="btn btn-quiet btn-small"
                onClick={() => set("ports", [...f.ports, { name: `p${f.ports.length + 1}`, port: "" }])}>
                <IconPlus size={14} aria-hidden="true" /> Add port
              </button>
            </div>
            <p className="muted small">
              The agent picks a host port from its range and shows the address here.
              {f.runtime === "process" ? " A process must listen on it: $PORT (or ${PORT_<NAME>} in arguments)." : ""}
            </p>
            {f.ports.map((p, i) => (
              <div className="kv" key={i}>
                <input aria-label={`Port ${i + 1} name`} className="mono" value={p.name}
                  onChange={(e) => set("ports", f.ports.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))} />
                {f.runtime === "container" ? (
                  <input aria-label={`Port ${i + 1} container port`} className="mono" placeholder="container port" inputMode="numeric"
                    value={p.port} onChange={(e) => set("ports", f.ports.map((r, j) => (j === i ? { ...r, port: e.target.value } : r)))} />
                ) : <span className="muted small">host port assigned</span>}
                <button type="button" className="btn btn-quiet btn-small" aria-label={`Remove port ${i + 1}`}
                  onClick={() => set("ports", f.ports.filter((_, j) => j !== i))}><IconMinus size={14} aria-hidden="true" /></button>
              </div>
            ))}
            <div className="grid-3">
              <Segmented label="Health check" value={f.health} onChange={(v) => set("health", v)} options={[
                { value: "none", label: "None" }, { value: "http", label: "HTTP" }, { value: "tcp", label: "TCP" },
              ]} />
              {f.health === "http" && <Input label="Path" value={f.healthPath} onChange={(v) => set("healthPath", v)} />}
              {f.health !== "none" && (
                <Input label="Every (s)" value={f.healthInterval} onChange={(v) => set("healthInterval", v)} inputMode="numeric" />
              )}
            </div>
            <div className="field narrow">
              <label htmlFor="w-restart">Restart</label>
              <select id="w-restart" value={f.restart} onChange={(e) => set("restart", e.target.value as WorkloadForm["restart"])}>
                <option value="always">Always</option>
                <option value="on-failure">On failure</option>
                <option value="never">Never</option>
              </select>
            </div>
          </section>
        ) : (
          <section className="panel">
            <h2>Limits and retries</h2>
            <div className="grid-3">
              <Input label="Max duration (s)" value={f.maxDuration} onChange={(v) => set("maxDuration", v)} inputMode="numeric"
                placeholder="3600" hint="Stopped and failed when it runs longer; not retried." />
              <Input label="Retries" value={f.retries} onChange={(v) => set("retries", v)} inputMode="numeric"
                hint="After a non-zero exit." />
              <Input label="Retry backoff (s)" value={f.retryBackoff} onChange={(v) => set("retryBackoff", v)} inputMode="numeric"
                hint="Doubles after each retry, at most 5 min." />
            </div>
          </section>
        )}

        {error && <Notice tone="error">{error}</Notice>}
        <div className="form-foot">
          <details className="spec-preview">
            <summary>Spec that will be sent</summary>
            <pre className="mono" aria-label="Spec JSON">{JSON.stringify(spec, null, 2)}</pre>
          </details>
          <div className="row-gap">
            <button className="btn btn-primary" disabled={busy}>{busy ? "Creating…" : "Create and start"}</button>
            <Link to="/workloads" className="btn btn-quiet">Cancel</Link>
          </div>
        </div>
      </form>
    </>
  );
}
