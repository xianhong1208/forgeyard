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
  model_cache?: { ok?: boolean; free_bytes?: number | null; cached?: string[]; reason?: string | null };
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
  // Model deployments (M3).
  vram?: number | null;
  download?: { phase: "model" | "engine"; done: number; total: number; file?: string } | null;
  ready_at?: string | null;
  runs?: { type: string; image: string | null; command: string | string[] | null } | null;
};

export type ContainerRuntimeSpec = { type: "container"; image: string; command: string[] | null; args: string[] };
export type ProcessRuntimeSpec = { type: "process"; command: string; args: string[]; workdir: string | null };
export type ModelRuntimeSpec = { type: "model"; recipe: string; model: string };

export type WorkloadSpec = {
  name: string;
  kind: WorkloadKind;
  runtime: ContainerRuntimeSpec | ProcessRuntimeSpec | ModelRuntimeSpec;
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
  runtime: "container" | "process" | "model";
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
  deployment_id?: number | null;
};

export type WorkloadEvent = { at: string; kind: string; replica: number | null; message: string };

export type WorkloadsResponse = { workloads: Workload[]; server_time: string; can_manage: boolean };

export type WorkloadResponse = { workload: Workload; events: WorkloadEvent[]; server_time: string; can_manage: boolean };

export type LogsResponse = { source: "live" | "stored"; text: string; truncated: boolean; state: string; note?: string };

// ---------------------------------------------------------------- models (M3)
export type Task = "chat" | "embed" | "rerank" | "asr";

export type RecipeParam = { type: "int" | "choice" | "size" | "float"; default: number | string; min?: number; max?: number; choices?: string[]; label?: string; help?: string };

export type RecipeVariant = { name: string; vendor: "nvidia" | "amd" | "cpu"; runtime: "container" | "process"; image: string | null; arch: string[] | null; compute_capability: string | null; driver: string | null; engine: string | null };

export type Recipe = {
  name: string; title: string; description: string; engine: string; version: string; tasks: Task[]; formats: string[];
  api: Record<string, string>; share: "exclusive" | "vram"; builtin: boolean; estimate: string;
  params: Record<string, RecipeParam>; devices: ("gpu" | "cpu")[]; variants: RecipeVariant[]; yaml?: string;
};

export type CatalogModel = {
  id: number; name: string; title: string | null; source: string; kind: "hf" | "url" | "local"; ref: string | null;
  revision: string | null; file: string | null; format: string; task: Task; task_label: string; size_bytes: number;
  engine_args: Record<string, string[]>; recipes: string[]; deployments: number; created_by: string | null; created_at: string;
  summary: { params?: number; layers?: number; kv_heads?: number; head_dim?: number; max_context?: number; dtype?: string; architecture?: string; weights_bytes?: number };
  gguf_files: { path: string; size: number; parts: number }[];
};

export type ModelSources = { url_hosts: string[]; local_dirs: string[]; hf_endpoint: string; allow_private_hosts: boolean };

export type CatalogResponse = { models: CatalogModel[]; recipes: Recipe[]; can_manage: boolean; can_deploy: boolean; sources?: ModelSources };

export type EstimatePart = { label: string; bytes: number; detail: string };

export type Estimate = { method: string; device: "gpu" | "cpu"; gpus: number; per_gpu: number; ram: number; amount: number; parts: EstimatePart[]; notes: string[]; kv_tokens: number };

export type MachineVerdict = {
  machine_id: number; name: string; status: string; fits: boolean; reason: string; short_by: number | null;
  cards: { index: number | null; available: number; total: number | null; shared_with: string[] }[];
  variant?: string; runtime?: string; image?: string | null; gpus?: number[]; cached?: boolean; leftover?: number;
};

export type PlacedReplica = { replica: number; machine: string; machine_id: number; variant: string; runtime: string; image: string | null; gpus: number[]; cached: boolean; leftover: number; online: boolean; gpu_memory_utilization?: string };

export type Placement = { fits: boolean; placed: PlacedReplica[]; machines: MachineVerdict[]; summary?: string; failed_replica?: number };

export type EstimateResponse = { estimate: Estimate; placement: Placement; params: Record<string, unknown>; recipe: string; model: string };

export type Endpoint = { replica: number; machine: string | null; state: string; ready: boolean; url: string };

export type LiveCard = { replica: number; machine: string | null; key: string; index: number | null; vendor: string; model: string; total: number | null; free: number | null; used: number | null; reserved: number | null; updated_at: string };

export type Deployment = {
  id: number; name: string; recipe: string; served_name: string; params: Record<string, unknown>;
  model: { id: number; name: string; source: string; task: Task; format: string; revision: string | null; file: string | null; size_bytes: number };
  estimate: Estimate; has_token: boolean; created_by: string | null; created_at: string;
  route: { served_name: string; api: string; upstream_model: string } | null;
  endpoints: Endpoint[]; workload: Workload; cards?: LiveCard[];
};

export type DeploymentsResponse = { deployments: Deployment[]; server_time: string; can_manage: boolean };

export type DeploymentResponse = { deployment: Deployment; events: WorkloadEvent[]; server_time: string; can_manage: boolean };

export type TryResponse = { status: number; ok: boolean; api: string; endpoint: string; request: Record<string, unknown>; summary: string; answer: unknown; ms: number };
