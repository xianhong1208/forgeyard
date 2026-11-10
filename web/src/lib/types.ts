export type Role = "admin" | "operator" | "user";

export type User = {
  id: number;
  username: string;
  email: string | null;
  role: Role;
  status: "active" | "pending" | "disabled";
  is_owner: boolean;
  has_password: boolean;
  created_at: string;
  last_login_at: string | null;
};

export type AuthState = {
  needs_setup: boolean;
  registration: "off" | "approval";
  user: User | null;
  ended?: string | null;
};

export type Vendor = "nvidia" | "amd" | "cpu";

export type Accelerator = {
  key: string;
  vendor: Vendor;
  index: number;
  model: string;
  arch: string | null;
  uuid: string | null;
  // Bytes. For the CPU row: system RAM. For a unified-memory APU: the GPU-usable pool.
  vram_total: number | null;
  vram_free: number | null;
  vram_used: number | null;
  unified_memory: boolean;
  driver: string | null;
  utilization: number | null;
  temperature: number | null;
  detail: Record<string, unknown>;
  updated_at: string;
  // May Forgeyard assign this card (the machine's allowlist)? Never true for the CPU row.
  allowed?: boolean;
  // The workload holding it, if any.
  assigned_to?: string | null;
};

export type RuntimeInfo = {
  ok: boolean;
  reason?: string;
  user?: string;
  uid?: number;
  limits?: string;
  engine?: string;
  version?: string | null;
  nvidia?: boolean;
  gpu_mode?: string | null;
};

export type CollectorStatus = { ok: boolean; count?: number; source?: string; reason?: string };

export type Machine = {
  id: number;
  name: string;
  hostname: string | null;
  status: "online" | "offline" | "revoked";
  connected: boolean;
  simulated: boolean;
  agent_version: string | null;
  protocol: number | null;
  labels: Record<string, string>;
  runtimes: string[];
  platform: Record<string, string>;
  collectors: Record<string, CollectorStatus>;
  ip: string | null;
  joined_at: string;
  connected_at: string | null;
  last_heartbeat_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  // Accelerator keys Forgeyard may assign; null = all of them.
  gpu_allowlist?: string[] | null;
  features?: string[];
  runtime_info?: Partial<Record<"process" | "container", RuntimeInfo>>;
  accelerators: Accelerator[];
};

export type MachinesResponse = {
  machines: Machine[];
  server_time: string;
  heartbeat_interval: number;
  offline_after: number;
  can_manage: boolean;
};

export type JoinToken = { id: string; created_at: string; expires_at: string; created_by: string | null };

export type NewJoinToken = JoinToken & { token: string; command: string; server_url: string; valid_minutes: number };

export type WorkloadKind = "service" | "task";

export type ReplicaState =
  | "pending" | "starting" | "running" | "healthy" | "unhealthy" | "backoff"
  | "exited" | "failed" | "stopping" | "stopped";

export type PortStatus = { name: string; port: number | null; host_port: number | null; address: string | null };

export type Replica = {
  ordinal: number;
  id: string;
  generation: number;
  machine: { id: number; name: string; status: string; connected: boolean } | null;
  gpus: { index: number; vendor: string; model: string | null; key: string }[];
  state: ReplicaState;
  reason: string | null;
  exit_code: number | null;
  attempt: number;
  restarts: number;
  final: boolean;
  pid: number | null;
  container: string | null;
  ports: PortStatus[];
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
};

export type ContainerRuntimeSpec = { type: "container"; image: string; command: string[] | null; args: string[] };
export type ProcessRuntimeSpec = { type: "process"; command: string; args: string[]; workdir: string | null };

export type WorkloadSpec = {
  name: string;
  kind: WorkloadKind;
  runtime: ContainerRuntimeSpec | ProcessRuntimeSpec;
  env: Record<string, string>;
  resources: { cpu: number | null; memory: number | null; gpus: { count: number | null; indices: number[] | null; vendor: string | null } };
  ports: { name: string; port: number | null }[];
  health: { type: "http" | "tcp"; port: string | null; path: string; interval: number; timeout: number; failure_threshold: number; initial_delay: number } | null;
  restart: "always" | "on-failure" | "never";
  max_duration: number | null;
  retries: number;
  retry_backoff: number;
  replicas: number;
  machine_id: number | null;
};

export type WorkloadStatus = "running" | "starting" | "degraded" | "pending" | "succeeded" | "failed" | "stopping" | "stopped";

export type Workload = {
  id: number;
  name: string;
  kind: WorkloadKind;
  runtime: "container" | "process";
  desired_state: "running" | "stopped";
  generation: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  spec: WorkloadSpec;
  status: WorkloadStatus;
  ready: number;
  replicas: number;
  replica_list: Replica[];
};

export type WorkloadEvent = { at: string; kind: string; replica: number | null; message: string };

export type WorkloadsResponse = { workloads: Workload[]; server_time: string; can_manage: boolean };

export type WorkloadResponse = { workload: Workload; events: WorkloadEvent[]; server_time: string; can_manage: boolean };

export type LogsResponse = { source: "live" | "stored"; text: string; truncated: boolean; state: string; note?: string };
