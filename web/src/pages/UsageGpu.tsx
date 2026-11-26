import { useState } from "react";

import { BreakdownTable, type BreakdownColumn } from "../components/BreakdownTable";
import { Notice } from "../components/ui";
import { labels, t as tNow } from "../i18n/core";
import { formatDateTime, formatNumber } from "../i18n/format";
import { getLang } from "../i18n/lang";
import { useT, type Translator } from "../i18n/react";
import { rowName } from "../lib/charts";
import type { GpuAmounts, GpuRow, GpuUsageResponse, TeamBrief } from "../lib/types";
import { useApi } from "../lib/useApi";
import { usePoll } from "../lib/poll";

// GPU hours (1.6.1, #190): the GPU time model replicas held, by deployment, team, machine or GPU model. Admins and
// operators see every deployment's (or one team's); a team's managers pick one of their teams.

const DAYS = [1, 7, 30];
const GROUP_LABEL = labels({ deployment: "usage.gpu.group.deployment", team: "usage.gpu.group.team", machine: "usage.gpu.group.machine",
  gpu_model: "usage.gpu.group.gpu_model" });
type GroupId = keyof typeof GROUP_LABEL;
const GROUPS: GroupId[] = ["deployment", "team", "machine", "gpu_model"];

const HOUR_MS = 3_600_000;

// GPU-milliseconds as GPU hours with one decimal ("0.2"); "—" for none.
export function gpuHours(ms: number | null | undefined): string {
  if (!ms) return "—";
  return formatNumber(ms / HOUR_MS, getLang(), { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

// MiB × ms as GiB-hours with one decimal.
export function gibHours(mibMs: number | null | undefined): string {
  return formatNumber((mibMs ?? 0) / 1024 / HOUR_MS, getLang(), { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

// A share 0..1 as a whole percentage ("25%"); "—" when there is nothing to share.
export function percent(x: number | null | undefined): string {
  if (x === null || x === undefined) return "—";
  return formatNumber(x, getLang(), { style: "percent", maximumFractionDigits: 0 });
}

export function UsageGpu({ days, onDays, teams: known, everything: canAll }: { days: number; onDays: (d: number) => void; teams: TeamBrief[]; everything: boolean }) {
  const t = useT();
  const [group, setGroup] = useState<GroupId>("deployment");
  // A team's manager (not an admin or operator) sees one of their teams: the first one until they pick another.
  const [team, setTeam] = useState<string | null>(null);
  const shown = team ?? (canAll ? null : known[0]?.name ?? null);
  const { data, error, reload } = useApi<GpuUsageResponse>(`/usage/gpu?days=${days}&group=${group}${shown ? `&team=${encodeURIComponent(shown)}` : ""}`);
  usePoll(reload, 10000);
  const teams = data?.teams ?? known;
  const sum = data?.total;
  const cpu = (sum?.ram_mib_ms ?? 0) > 0;
  const label = GROUP_LABEL[(data?.group_by as GroupId) ?? group] ?? GROUP_LABEL[group];
  return (
    <>
      <div className="usage-controls">
        <div className="segmented" role="group" aria-label={t("usage.period")}>
          {DAYS.map((d) => (
            <button key={d} type="button" className={days === d ? "on" : ""} aria-pressed={days === d} onClick={() => onDays(d)}>
              {d === 1 ? t("usage.today") : t("usage.days", { count: d })}
            </button>
          ))}
        </div>
        <div className="segmented" role="group" aria-label={t("usage.groupBy")}>
          {GROUPS.filter((g) => g !== "team" || !shown).map((g) => (
            <button key={g} type="button" className={group === g ? "on" : ""} aria-pressed={group === g} onClick={() => setGroup(g)}>
              {GROUP_LABEL[g]}
            </button>
          ))}
        </div>
        {teams.length > 0 && (
          <label className="field-inline small">{t("usage.team")}
            <select value={shown ?? ""} aria-label={t("usage.gpu.whose")} onChange={(e) => {
              setTeam(e.target.value || null);
              if (e.target.value && group === "team") setGroup("deployment");
            }}>
              {canAll && <option value="">{t("usage.gpu.all")}</option>}
              {teams.map((x) => <option key={x.id} value={x.name}>{x.title}</option>)}
            </select>
          </label>
        )}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {sum && (
        <dl className="fleet usage-totals usage-totals-gpu" aria-label={t("usage.gpu.totals")}>
          <div><dt>{t("usage.gpu.hours")}</dt><dd>{gpuHours(sum.gpu_ms)}</dd></div>
          <div><dt>{t("usage.gpu.busy")}</dt><dd>{percent(sum.busy_share)} <span className="muted small">{t("usage.gpu.busyDetail")}</span></dd></div>
          {cpu && <div><dt>{t("usage.gpu.ram")}</dt><dd>{t("usage.gpu.gibHours", { n: gibHours(sum.ram_mib_ms) })} <span className="muted small">{t("usage.gpu.ramDetail")}</span></dd></div>}
        </dl>
      )}
      {data && (data.rows.length === 0 ? (
        <div className="panel usage-empty"><p className="muted">{t("usage.gpu.empty")}</p></div>
      ) : (
        <BreakdownTable key={`gpu-${data.group_by}`} label={t("usage.gpu.by", { name: label.toLowerCase() })} nameLabel={label}
          rows={data.rows.map((r) => ({ ...r, name: rowName(r.id, r.name) }))} columns={gpuColumns(t, data.total.gpu_ms, cpu)} metric="gpu_ms"
          others={addUpGpu} nameClass={data.group_by === "deployment" ? "mono" : undefined}
          nameExtra={(r) => r.vendor === "cpu" && <span className="chip">{t("usage.gpu.chip.cpu")}</span>} />
      ))}
      {data && (
        <p className="muted small usage-foot">
          {t("usage.gpu.foot", { range: data.from === data.to ? data.from : t("usage.range", { from: data.from, to: data.to }),
            since: formatDateTime(data.counting_since) })}
        </p>
      )}
    </>
  );
}

function gpuColumns(t: Translator, total: number, cpu: boolean): BreakdownColumn<GpuRow>[] {
  const cols: (BreakdownColumn<GpuRow> | false)[] = [
    { id: "gpu_ms", label: t("usage.gpu.col.hours"), className: "num strong", value: (r) => r.gpu_ms, cell: (r) => gpuHours(r.gpu_ms) },
    { id: "share", label: t("usage.gpu.col.share"), className: "num", value: (r) => r.gpu_ms, cell: (r) => (total ? percent(r.gpu_ms / total) : "—") },
    { id: "busy", label: t("usage.gpu.col.busy"), className: "num", title: t("usage.gpu.col.busyTitle"), value: (r) => r.busy_share,
      cell: (r) => percent(r.busy_share) },
    cpu && { id: "ram", label: t("usage.gpu.col.ram"), className: "num", title: t("usage.gpu.col.ramTitle"), value: (r) => r.ram_mib_ms,
      cell: (r) => (r.ram_mib_ms ? gibHours(r.ram_mib_ms) : "—") },
  ];
  return cols.filter((c): c is BreakdownColumn<GpuRow> => !!c);
}

// The rows past the top ones as one: their amounts added up, the busy share worked out again.
export function addUpGpu(rows: GpuRow[]): GpuRow {
  const out: GpuAmounts = { gpu_ms: 0, ram_mib_ms: 0, busy_ms: 0, cpu_busy_ms: 0, busy_share: null };
  for (const r of rows) {
    out.gpu_ms += r.gpu_ms;
    out.ram_mib_ms += r.ram_mib_ms;
    out.busy_ms += r.busy_ms;
    out.cpu_busy_ms += r.cpu_busy_ms;
  }
  out.busy_share = out.gpu_ms ? out.busy_ms / out.gpu_ms : null;
  return { ...out, id: "others", name: tNow("usage.others") };
}
