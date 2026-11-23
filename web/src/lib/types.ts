import type { Budget } from "./budgets";

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
  // forgeyard-ca | own | off, and whether this page came over HTTPS (directly or through a trusted proxy).
  tls?: { mode: TlsMode; https: boolean };
};

export type TlsMode = "forgeyard-ca" | "own" | "off";

// GET /api/secret-key (admins): what a backup of the secret key holds (1.4.2, #127).
export type SecretKeyInfo = {
  kid: string;
  source: "file" | "env";
  includes_ca: boolean;
  tls_mode: TlsMode;
  has_password: boolean;
  passphrase_min: number;
  backed_up: { kid: string; by: string; at: string; how?: string } | null;
};

export type TlsInfo = {
  mode: TlsMode;
  https: boolean;
  hsts: boolean;
  server_url: string;
  ca: { name: string; expires_at: string; fingerprint: string; fingerprint_colons: string; own: boolean } | null;
  admin?: {
    trust_command: string | null;
    server_certificate: { names: string[]; ips: string[]; expires_at: string; issuer: string } | null;
  };
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
  // 1.4: how it fetches models, engines and images (admins set it): directly, through the server, or both.
  fetch_via?: FetchVia;
  // 1.4: the largest image file an admin may upload to it (IMAGE_UPLOAD_MAX_GB).
  image_upload_max_bytes?: number;
  // 1.4.5 (#171): `address`, where its replicas are published (what the gateway dials); `same_host`, whether its
  // agent runs on the server's own host (agents before 1.4.5 report neither).
  runtime_info?: Partial<Record<"process" | "container", RuntimeInfo>> & { address?: string; same_host?: boolean };
  // 1.4.2 (#122): why its agent should be updated (null: it need not be), and the server's version.
  agent_behind?: AgentBehind | null;
  server_version?: string;
  // 1.4.5 (#163): how the gateway reaches its replicas (null: it runs no workloads).
  replica_tls?: ReplicaTls | null;
  model_cache?: { ok?: boolean; free_bytes?: number | null; cached?: string[]; reason?: string | null };
  accelerators: Accelerator[];
};

// A machine's model cache (1.4): GET /api/machines/{id}/cache.
export type CacheUser = { name: string; kind: "deployment" | "workload"; id: number };
export type CacheCatalogRef = { id: number; name: string };
export type CachePart = { key: string; file: string; bytes: number; catalog: CacheCatalogRef[]; used_by: CacheUser[] };
export type CacheModel = {
  key: string;
  kind: "hf" | "url";
  repo?: string | null;
  revision?: string;
  file?: string | null;
  bytes: number | null;
  files?: number;
  completed_at?: number | null;
  missing?: number;
  parts?: CachePart[];
  catalog: CacheCatalogRef[];
  used_by: CacheUser[];
};
export type CacheEngine = { key: string; id: string; name: string | null; bytes: number; used_by: CacheUser[] };
export type MachineCache = {
  machine_id: number;
  connected: boolean;
  reported: boolean;
  sizes: boolean;
  ok: boolean | null;
  reason: string | null;
  free_bytes: number | null;
  total_bytes: number | null;
  used_bytes: number | null;
  partial_bytes: number | null;
  partial_files: number;
  truncated: boolean;
  models: CacheModel[];
  engines: CacheEngine[];
  remove_note: string | null;
};

export type MachinesResponse = {
  machines: Machine[];
  server_time: string;
  heartbeat_interval: number;
  offline_after: number;
  can_manage: boolean;
};

export type JoinToken = { id: string; created_at: string; expires_at: string; created_by: string | null };

export type NewJoinToken = JoinToken & {
  token: string; command: string; server_url: string; valid_minutes: number;
  // The CA the join command pins (null: plain HTTP, or a publicly trusted certificate), and the
  // one-time command for agents that joined before.
  ca_fingerprint?: string | null; trust_command?: string | null;
  // The three ways to add the machine (forgeyard/install/commands.py): null install_command when the
  // server is not reached over HTTPS (install.sh is served over HTTPS only).
  install_command?: string | null;
  // The agent image the Docker way runs: published (pulled), or a local build while none is published.
  agent_image?: { reference: string; published: boolean; local_tag: string };
  docker_build_command?: string;
  docker_commands?: { nvidia: string; amd: string; cpu: string };
  config_file?: string;
  manual_commands?: string[];
  version?: string;
};

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
  // An image pull (1.3.1) is reported the same way: phase "image", with its layers; total 0 while
  // some layer's size is not known yet.
  download?: {
    phase: "model" | "engine" | "image"; done: number; total: number; file?: string;
    layers?: number; layers_done?: number; unpacking?: boolean;
    // 1.4: "server" while the bytes come through the server's relay.
    via?: "direct" | "server";
    // 1.4.1: the machine whose cache the model file comes from, through the server.
    source?: string;
  } | null;
  ready_at?: string | null;
  runs?: { type: string; image: string | null; command: string | string[] | null } | null;
  // 1.3.1: placed on a connected machine whose agent has not taken it for two minutes: why, and what to do.
  not_picked_up?: string | null;
  // 1.4: why it is on this machine, when one that could not fetch what it needs decided it.
  placement_note?: string | null;
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

// 1.5.0: "asleep": a deployment stopped because nobody used it (sleep when idle); Start wakes it.
export type WorkloadStatus = "running" | "starting" | "degraded" | "pending" | "succeeded" | "failed" | "stopping" | "stopped" | "asleep";

export type Workload = {
  id: number;
  name: string;
  kind: WorkloadKind;
  runtime: "container" | "process" | "model";
  desired_state: "running" | "stopped" | "asleep";
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
  // 1.4: the deployment that created this (model) workload.
  // and the GPUs it gives each replica (0: the CPU); a model workload's own spec asks for none.
  deployment?: { id: number; name: string; gpus?: number } | null;
  // 1.3: exposed behind the gateway at /x/<name> (null for a model deployment's workload).
  gateway?: WorkloadGateway | null;
};

export type WorkloadGatewaySettings = {
  expose: boolean; port: string | null; read_timeout: number; max_body_mb: number;
  concurrency: number | null; key_concurrency: number | null;
};

export type WorkloadGateway = WorkloadGatewaySettings & {
  exposed: boolean; path: string; port_name: string | null;
  guarded: number[];     // replicas behind the agent's key guard
  unguarded: number[];   // placed replicas that are not (an agent too old, or not restarted yet)
};

export type WorkloadEvent = { at: string; kind: string; replica: number | null; message: string };

export type WorkloadsResponse = { workloads: Workload[]; server_time: string; can_manage: boolean };

export type LogsResponse = { source: "live" | "stored"; text: string; truncated: boolean; state: string; note?: string };

// ---------------------------------------------------------------- models (M3)
// 1.4 (#86): detect, segment and depth are vision models (Forgeyard's vision server, /v1/vision/<task>).
// 1.4.4 (#144): classify is text classification (vLLM's pooling runner, /v1/classify); image-classify (#141) is
// image classification on the vision server (/v1/vision/classify).
export type Task = "chat" | "embed" | "rerank" | "asr" | "tts" | "image" | "video" | "detect" | "segment" | "depth" | "classify" | "image-classify";

export type RecipeParam = {
  type: "int" | "choice" | "size" | "float"; default: number | string; min?: number; max?: number; choices?: string[]; label?: string; help?: string;
  // Shown in the deploy form's collapsed Advanced section: its default works untouched (1.3).
  advanced?: boolean;
};

// What a model takes in chat besides text (1.3).
export type Medium = "image" | "audio" | "video";

export type RecipeVariant = { name: string; vendor: "nvidia" | "amd" | "cpu"; runtime: "container" | "process"; image: string | null; arch: string[] | null; compute_capability: string | null; driver: string | null; engine: string | null;
  // Ids are recorded for the image: a copy loaded with `docker load` is checked and used offline.
  image_check?: boolean };

export type Recipe = {
  name: string; title: string; description: string; engine: string; version: string; tasks: Task[]; formats: string[];
  api: Record<string, string>; share: "exclusive" | "vram"; builtin: boolean; estimate: string;
  params: Record<string, RecipeParam>; devices: ("gpu" | "cpu")[]; variants: RecipeVariant[]; yaml?: string;
  media?: { inputs: Medium[]; files: boolean } | null;
  // 1.3, text to speech: the formats the engine makes, how it streams, its default voice.
  tts?: { formats: string[]; default_format: string; stream_formats: string[]; voices: string | null; default_voice: string | null; cjk: boolean } | null;
  // 1.3, image generation: how the gateway sends the request, the formats it makes, its most images.
  image?: { request: "fields" | "sd-cpp"; output_formats: string[]; max_n: number } | null;
  // 1.3, video generation: how the gateway asks the engine, and the frame counts it makes (k x step + 1).
  video?: { request: "vllm-omni"; frames_step: number } | null;
  // 1.5.2 (#95): a copy of a built-in engine ("Try this version"): the built-in it came from, and what the copy did.
  based_on?: string | null;
  copy?: RecipeCopy | null;
};

// 1.5.2 (#95): what the server verified about an image or archive of a copy (forgeyard/models/signatures.py).
export type EngineSignature = {
  status: "verified" | "unsigned"; note?: string | null; source?: string; repository?: string; workflow?: string;
  ref?: string | null; commit?: string | null; run?: string | null; predicate?: string; issuer?: string;
  log_index?: number | null; signed_at?: string | null;
};
export type RecipeCopy = {
  from_version: string; kind: "release" | "build"; made_at: string; made_by: string;
  left_out: { variant: string; code: string; text: string; params: Record<string, string> }[];
  archives: { variant: string; release: string | null; signature: EngineSignature | Record<string, never> }[];
};
export type EnginePin = { signature: EngineSignature | null; recorded_at: string | null; recorded_by: string };
// What a built-in engine's project signs (GET /api/engine-updates: signatures).
export type EngineSigned = { signed: true; kind: string; repository: string } | { signed: false; text: string };
export type EngineTryResponse = {
  recipe: Recipe; left_out: RecipeCopy["left_out"]; signatures: Record<string, EngineSignature>;
};

export type CatalogModel = {
  id: number; name: string; title: string | null; source: string; kind: "hf" | "url" | "local"; ref: string | null;
  revision: string | null; file: string | null; format: string; task: Task; task_label: string; size_bytes: number;
  engine_args: Record<string, string[]>; recipes: string[]; deployments: number; created_by: string | null; created_at: string;
  summary: { params?: number; layers?: number; kv_heads?: number; head_dim?: number; max_context?: number; dtype?: string; architecture?: string; weights_bytes?: number };
  gguf_files: { path: string; size: number; parts: number }[];
  // 1.3: the licence Hugging Face shows, gated, the media it takes (null: unknown, added before 1.3)
  // and a GGUF vision model's projector file.
  license?: string | null; gated?: boolean; modalities?: Medium[] | null;
  // A conversion without a licence of its own (1.3): the original model whose licence it is.
  license_from?: string | null;
  mmproj?: { path: string; size: number } | null; mmproj_files?: { path: string; size: number }[];
  // 1.3, text to speech: the built-in voices its config names (Qwen3-TTS), if any.
  voices?: string[];
  // 1.3, image models: the native size, and the pipeline's components or the files beside the model file.
  image?: ImageModel | null;
  // 1.4, vision models: the task, architecture and (detection) the classes it knows.
  vision?: { task: "detect" | "segment" | "depth" | "image-classify"; architecture?: string; labels?: number; label_examples?: string[]; problem?: string;
    // 1.4.4: a zero-shot detector (#142), which finds what each request names, or a zero-shot image classifier (#141),
    // which chooses among the labels each request sends (no classes of their own)
    zero_shot?: boolean } | null;
  // 1.4.4, text classifiers: the architecture, how many labels, their names in index order, the problem type
  // (multi_label_classification: each label's probability on its own).
  classify?: TextClassifier | null;
  // 1.3, video models: the native size and frame rate, and the pipeline's components.
  video?: VideoModel | null;
};

export type TextClassifier = { architecture?: string; labels?: number; label_names?: string[]; problem?: string };

// /v1/classify's answer (vLLM's shape): each input's most likely label and every label's probability.
export type ClassifyAnswer = {
  model?: string;
  data: { index: number; label: string | null; probs: number[]; num_classes: number }[];
  usage?: { prompt_tokens?: number; total_tokens?: number };
};

export type VideoModel = {
  size: [number, number] | null; multiple: number | null; fps: number | null; pipeline: string | null;
  parts: Record<string, { class: string | null; params: number | null }>;
};

export type ImageModel = {
  size: number | null; multiple: number | null; single: boolean | null; components: Record<string, string>;
  pipeline: string | null; parts: Record<string, { class: string | null; params: number | null }>;
};

// 1.3.1: the server's Hugging Face token, as the UI may see it (never the token itself).
export type HfTokenInfo = { set: boolean; last4: string | null; updated_by: string | null; updated_at: string | null };

export type ModelSources = {
  url_hosts: string[]; local_dirs: string[]; hf_endpoint: string; allow_private_hosts: boolean;
  // Environment variables the lists still come from (nobody saved them yet), and the server's token.
  env_seeds?: string[]; hf_token?: HfTokenInfo;
};

export type CatalogResponse = {
  models: CatalogModel[]; recipes: Recipe[]; can_manage: boolean; can_deploy: boolean; sources?: ModelSources;
  hf_token_set?: boolean;
};

export type EstimatePart = { label: string; bytes: number; detail: string };

export type Estimate = { method: string; device: "gpu" | "cpu"; gpus: number; per_gpu: number; ram: number; amount: number; parts: EstimatePart[]; notes: string[]; kv_tokens: number };

export type MachineVerdict = {
  machine_id: number; name: string; status: string; fits: boolean; reason: string; short_by: number | null;
  cards: { index: number | null; available: number; total: number | null; shared_with: string[] }[];
  variant?: string; runtime?: string; image?: string | null; gpus?: number[]; cached?: boolean; leftover?: number;
  // 1.3.1: exclusive cards are in the way, but the memory is there if they are shared.
  share_fits?: { gpus: number[]; holders: string[]; exclusive: string[]; both: boolean };
};

export type PlacedReplica = { replica: number; machine: string; machine_id: number; variant: string; runtime: string; image: string | null; gpus: number[]; cached: boolean; leftover: number; online: boolean; gpu_memory_utilization?: string;
  // 1.3.1 (a change): it goes back to the machine it runs on now.
  previous?: boolean };

// 1.3.1: an exclusive GPU deployment that needs half its card or less could share it.
export type ShareHint = { need: number; card_total: number; card_model: string | null; machine: string | null; gpu: number | null; text: string };

export type Placement = { fits: boolean; placed: PlacedReplica[]; machines: MachineVerdict[]; summary?: string; failed_replica?: number; share_hint?: ShareHint | null };

// 1.3.1 (D9): the model's longest context and the largest that fits right now, offered as one click.
export type ContextHint = { param: string; model_max: number | null; fits_max: number | null };

export type EstimateResponse = {
  estimate: Estimate; placement: Placement; params: Record<string, unknown>; recipe: string; model: string;
  context_hint?: ContextHint | null;
};

export type Endpoint = {
  replica: number; machine: string | null; state: string; ready: boolean; url: string;
  // M4: whether the replica requires its internal key (guarded: on every path, by its agent's key
  // guard), and what the gateway sent it since the server started.
  protected?: boolean; guarded?: boolean; served?: number; in_flight?: number;
};

export type LiveCard = { replica: number; machine: string | null; key: string; index: number | null; vendor: string; model: string; total: number | null; free: number | null; used: number | null; reserved: number | null; updated_at: string };

export type Deployment = {
  id: number; name: string; recipe: string; served_name: string; params: Record<string, unknown>;
  model: { id: number; name: string; source: string; task: Task; format: string; revision: string | null; file: string | null; size_bytes: number;
    // 1.4: what it takes in chat besides text (null: unknown), so a chat model can be listed as multimodal.
    modalities?: Medium[] | null;
    // 1.4.4: a text classifier's labels (names in index order), for the Playground.
    classify?: TextClassifier | null };
  estimate: Estimate; has_token: boolean; created_by: string | null; created_at: string;
  route: { served_name: string; api: string; upstream_model: string } | null;
  endpoints: Endpoint[]; workload: Workload; cards?: LiveCard[];
  // 1.2: the deployment's own route with its tiers, and other routes it is a target of.
  routing?: RouteInfo | null; also_in?: string[];
  usage?: { days: number; total: UsageRow; by_day: UsageRow[] };
  // 1.3.1: the machine a person chose (null: Forgeyard chooses), and an edit's restarts in progress.
  pinned?: { id: number; name: string | null } | null;
  rollout?: { generation: number; updated: number; total: number; next: number } | null;
  // 1.5.0 (#37): sleep when idle, the last request the server saw, and when it would go to sleep.
  sleep?: DeploymentSleep;
  // 1.5.0 (#37): how long its latest start took (source "server": measured by the server for an older agent).
  last_start?: StartTime | null;
  // 1.5.0 (#37): automatic replicas: the bounds, the load and the last decision.
  autoscale?: DeploymentAutoscale;
};

export type StartTime = {
  seconds: number;
  phases: Partial<Record<"model" | "waiting" | "image" | "engine", number>>;
  source: "agent" | "server";
  at: string;
  replica: number | null;
};

export type DeploymentAutoscale = {
  on: boolean; min: number; max: number; target: number; scale_up_seconds: number; scale_down_minutes: number;
  capacity: number; replicas: number; ready: number; load: number | null; per_replica: number | null;
  wanted: number | null; draining: { replica: number; since: string; in_flight: number } | null;
  room: string | null; paused: string | null; last: { at: string; text: string } | null;
};

export type DeploymentSleep = {
  on: boolean; idle_minutes: number; asleep: boolean; last_request_at: string | null; since: string;
  sleeps_at: string | null; held_by: string | null;
  // 1.5.0 PR 3: how long a request waits for a wake (seconds), and the wake under way (a request's or Wake now's).
  wake_wait?: number; waking?: Waking | null;
  // 1.5.0 PR 4 (the console): since when it is asleep, and how often it was woken in the last day.
  asleep_since?: string | null; wakes?: number;
};

export type Waking = {
  since: string; by: string; waiting: number;
  expected_seconds: number;   // its last start, or 30 when none was measured
};

export type RestartMode = "none" | "on_start" | "on_wake" | "rolling" | "all";

// PATCH /api/deployments/{id} (1.3.1); dry_run answers the same without `deployment`.
export type EditResponse = {
  estimate: Estimate; placement: Placement; params: Record<string, unknown>;
  changes: Record<string, { before: unknown; after: unknown }>; restart: RestartMode; restart_text: string;
  deployment?: Deployment;
};

export type DeploymentsResponse = { deployments: Deployment[]; server_time: string; can_manage: boolean };

export type DeploymentResponse = { deployment: Deployment; events: WorkloadEvent[]; server_time: string; can_manage: boolean };

export type TryResponse = { status: number; ok: boolean; api: string; endpoint: string; request: Record<string, unknown>; summary: string; answer: unknown; ms: number };

// ---------------------------------------------------------------- gateway (M4)
export type KeyLimits = { default_rpm: number | null; default_tpm: number | null; max_rpm: number | null; max_tpm: number | null };

// 1.2: may a key's requests reach cloud providers? off, only when a route spills (fallback), or on.
export type CloudLevel = "off" | "fallback" | "on";
export type CloudSettings = { default: CloudLevel; max: CloudLevel };

export type ApiKey = {
  id: string; name: string; prefix: string; models: string[] | null; rpm: number | null; tpm: number | null;
  // The admin who set that limit on someone else's key: locked for its owner.
  rpm_locked_by?: string | null; tpm_locked_by?: string | null;
  cloud: CloudLevel; cloud_locked_by?: string | null; effective_cloud?: CloudLevel;
  effective_rpm: number | null; effective_tpm: number | null; expires_at: string | null; created_at: string;
  last_used_at: string | null; revoked_at: string | null; revoked_by: string | null; status: "active" | "revoked" | "expired";
  user: { id: number; username: string | null }; this_minute: { requests: number; tokens: number };
  // Monthly cloud budget (1.2) and this month's spend; budget_locked_by: an admin set it on someone else's key.
  budget?: Budget; budget_locked_by?: string | null;
  // Exposed workloads at /x/ (1.3): none (the default), listed (workload_list) or all.
  workloads?: WorkloadAccess; workload_list?: string[] | null; workloads_locked_by?: string | null;
};

export type WorkloadAccess = "none" | "listed" | "all";

export type KeysResponse = {
  keys: ApiKey[]; limits: KeyLimits; is_admin: boolean; gateway_url: string; trust_url?: string | null; cloud: CloudSettings;
  // Admins: environment variables the panels' values still come from (D1).
  env_seeds?: { limits: string[]; cloud: string[] };
  models: { name: string; api: string; kind?: RouteKind }[];
  // Budgets (1.2): this month (UTC), when it resets, and the signed-in person's own budget.
  month?: string; resets_at?: string; person_budget?: Budget;
};

export type NewKeyResponse = { key: string; api_key: ApiKey; gateway_url: string; trust_url?: string | null };

export type UsageRow = {
  id: string; name: string; requests: number; errors: number; limited: number; prompt_tokens: number; completion_tokens: number;
  tokens: number; latency_avg_ms: number | null; latency_p50_ms: number | null; latency_p95_ms: number | null;
  cached_tokens?: number; reasoning_tokens?: number; spilled?: number;
  // Cost (1.2): micro-USD; unpriced requests are not in cost_micros.
  cost_micros?: number; unpriced?: number; cost_reported?: number; cost_estimated?: number;
  // 1.3 units: media items sent in chat (images, audio clips, videos); the others are filled by
  // later model kinds.
  media_items?: number; characters?: number; images?: number; kilopixels?: number; media_ms?: number;
  bytes_in?: number; bytes_out?: number; busy_ms?: number;
  cloud?: boolean;   // grouped by target or provider: outside your network
};

export type UsageTotal = UsageRow & { split?: { local: UsageRow; cloud: UsageRow } };

export type UsageResponse = {
  from: string; to: string; days: number; group_by: string; total: UsageTotal; rows: UsageRow[]; everyone: boolean; is_admin: boolean;
};

// ---------------------------------------------------------------- routes and providers (1.2)
export type RouteKind = "local" | "mixed" | "cloud";

export type ProviderRef = { id: number; name: string; kind: string; external: boolean; enabled: boolean };

export type RouteTarget = {
  id: number; tier: number; kind: "local" | "provider"; weight: number; enabled: boolean; upstream_model: string | null; cloud: boolean;
  deployment: { id: number; name: string; served_name: string; ready: number; replicas: number; concurrency: number | null; context: number | null } | null;
  provider_model: { id: number; upstream_model: string; display_name: string | null; api: string; context_length: number | null; provider: ProviderRef } | null;
};

export type RouteInfo = {
  id: number; served_name: string; api: string; upstream_model: string | null; deployment_id: number | null;
  local_only: boolean; spill_on_long_prompt: boolean; wait_ms: number; kind: RouteKind; has_local: boolean; has_cloud: boolean;
  created_by?: string | null; endpoints: Endpoint[]; targets: RouteTarget[]; usable?: boolean;
};

export type RoutesResponse = { routes: RouteInfo[]; cloud: CloudLevel; is_admin: boolean };

export type RouteOptions = {
  deployments: { id: number; name: string; served_name: string; api: string | null }[];
  provider_models: { id: number; upstream_model: string; display_name: string | null; api: string; provider: ProviderRef }[];
};

// 1.3.1 (D4): whether a provider looks outside your network, and why.
export type ProviderWhere = { external: boolean; basis: "kind" | "address" | "name" | "resolved" | "unknown"; reason: string };

export type ProviderKind = {
  kind: string; label: string; base_url: string; base_url_hint: string; auth: string; embeddings: boolean; models_endpoint: boolean;
  default_external: boolean; key_required: boolean; note: string;
};

export type ProviderModel = {
  id: number; provider_id: number; upstream_model: string; api: string; display_name: string | null; context_length: number | null;
  drop_params: string[]; price_input: number | null; price_output: number | null; price_cache_read: number | null; price_cache_write: number | null;
  price_source: string | null; price_checked_at?: string | null; price_ref?: string | null; cooldown_s: number; in_flight: number; sent: number;
  created_at: string; routes: string[];
};

export type PriceFields = { price_input: number | null; price_output: number | null; price_cache_read: number | null; price_cache_write: number | null };

export type PriceMatch = "exact" | "confirmed" | "suggested" | "not_listed" | "none";

export type PriceRow = {
  model_id: number; provider_id: number; provider: string; kind: string; upstream_model: string; api: string; ref: string | null;
  match: PriceMatch; manual: boolean; source: string | null; checked_at: string | null; before: PriceFields; after: PriceFields | null; changed: boolean;
};

export type PricePreview = { checked_at: string; listed: number; rows: PriceRow[]; matched: number; changed: number };

export type PriceApplied = { applied: number[]; skipped: { model_id: number; reason: string }[] };

export type Provider = {
  id: number; name: string; kind: string; kind_label: string; base_url: string; has_key: boolean; key_hint: string | null;
  external: boolean; allow_http: boolean; enabled: boolean; timeout_s: number | null; created_by: string | null; created_at: string;
  updated_at: string; models: ProviderModel[]; headers?: Record<string, string>;
  budget?: Budget;   // outside your network only
};

export type Egress = { proxy: boolean; allowed_hosts: string[]; ca_file: boolean; private_hosts: boolean; cooldown_s: number; read_timeout_s: number };

export type ProvidersResponse = { providers: Provider[]; kinds: ProviderKind[]; egress: Egress; is_admin: boolean };

export type ProviderResponse = { provider: Provider; kinds: ProviderKind[]; egress: Egress; is_admin: boolean };

export type TestResult = { ok: boolean; status: number; ms: number; models: number; sample: string[]; message: string | null };

export type UpstreamModel = { id: string; owned_by: string | null; context_length: number | null; api: string; display_name: string | null; added: boolean };

// 1.4, "Download to…" (issue #35): a model (and by default its engine) downloaded to a machine ahead of a deployment.
export type FetchVia = "auto" | "server" | "direct";

export type DownloadProgress = {
  phase: "model" | "engine" | "image"; done: number; total: number; file?: string;
  layers?: number; layers_done?: number; unpacking?: boolean;
  // 1.4 ("relay"): "server" while the bytes come through the server.
  via?: "direct" | "server";
  // 1.4.1: the machine whose cache the model file comes from, through the server.
  source?: string;
};

export type ModelDownload = {
  id: number;
  model: { id: number; name: string; title: string | null } | null;
  machine: { id: number; name: string; connected: boolean } | null;
  file: string | null;
  recipe: string | null;
  state: "queued" | "downloading" | "done" | "failed" | "cancelled";
  via: string;
  download: DownloadProgress | null;
  reason: string | null;
  requested_by: string;
  created_at: string;
  finished_at: string | null;
  // Under way on a machine that is offline: it goes on when the agent is back.
  waiting: string | null;
  // 1.4.1: via "machine": the machine whose cache the model came from, through the server.
  source?: { id: number; name: string } | null;
};

export type DownloadsResponse = { downloads: ModelDownload[]; can_download: boolean };

export type DownloadTargetState = "ok" | "cached" | "downloading" | "too_old" | "no_cache" | "offline" | "no_space" | "revoked";

export type DownloadTarget = {
  machine_id: number; name: string; status: string; connected: boolean; state: DownloadTargetState; note: string;
  model_cached: boolean; need_bytes: number; free_bytes: number | null; variant: string | null;
  engine: { kind: "archive" | "image"; name: string; cached: boolean | null } | null;
  engine_note: string | null;
  download: ModelDownload | null;
  // 1.4 ("relay"): how the model would come: "server" (through the server), "auto" (directly, through the
  // server after a network failure) or "direct". Absent from older servers.
  via?: FetchVia;
  // 1.4.1: through the server, a machine whose cache has the model sends it.
  from_machine?: string | null;
};

export type DownloadTargets = {
  model: { id: number; name: string; title: string | null; size_bytes: number; kind: string; file: string | null };
  recipes: string[];
  recipe: string | null;
  machines: DownloadTarget[];
  can_download: boolean;
};

// 1.4.2 (#122): an agent older than the server, of an older protocol, or without a feature such an agent declares.
// 1.4.5 (#163): plain = an agent before 1.4.5 (not encrypted); the rest are TLS and its certificate.
export type ReplicaTls = {
  state: "plain" | "ok" | "pending" | "missing" | "expiring" | "expired";
  not_after: string | null;
  reason: string | null;
};

export type AgentBehind = { agent: string | null; server: string; older: boolean; old_protocol: boolean; missing: string[] };

// 1.4.2 (#121): GET /api/machines/{id}/agent-update (admins): the command that updates a machine's agent.
export type UpdateStep = { title: string; command: string };
export type AgentUpdate = {
  machine_id: number;
  name: string;
  agent_version: string | null;
  server_version: string;
  behind: AgentBehind | null;
  method: "container" | "install.sh" | "manual" | "unknown" | null;
  known: boolean;
  reason: string | null;
  image: string;
  offline: boolean;
  steps: UpdateStep[];
  alternative: { title: string; steps: UpdateStep[] } | null;
  notes: string[];
  guide: string;
};

// 1.4.3 (#140): a catalog model's page and an engine's page.
// The engine a machine would run, and whether its image (the machine's last image listing) or program (its
// cache report) is there: true / false, null when not known (never listed, no report).
export type EngineOn = {
  variant: string | null; runtime: "container" | "process" | null; kind: "image" | "program" | "command" | null;
  engine_name: string | null; present: boolean | null; listed_at: string | null; note: string | null;
};

export type ModelFile = { path: string; size: number | null; role: string; downloaded: boolean };

export type ModelPageMachine = {
  machine_id: number; name: string; status: string; connected: boolean;
  cache: { reported: boolean; cached: boolean; bytes: number | null; files: number | null; used_by: { kind: string; id: number; name: string }[] };
  download: ModelDownload | null;
  engines: Record<string, EngineOn>;
};

// Where the model fits with an engine's defaults, per device it runs on.
export type DeviceFit = { estimate?: Estimate; fits?: boolean; machines?: MachineVerdict[]; error?: string };

export type ModelPage = {
  model: CatalogModel; recipes: Recipe[]; can_manage: boolean; can_deploy: boolean;
  files: { recorded: boolean; files: ModelFile[]; downloaded_count: number; downloaded_bytes: number };
  machines: ModelPageMachine[];
  fits: Record<string, Partial<Record<"gpu" | "cpu", DeviceFit>>>;
  deployments: { id: number; name: string; recipe: string }[];
};

export type EnginePageMachine = EngineOn & { machine_id: number; name: string; status: string; connected: boolean; can_list: boolean };

export type EnginePage = {
  recipe: Recipe; can_manage: boolean; machines: EnginePageMachine[];
  deployments: { id: number; name: string; model_id: number }[];
  models: { id: number; name: string; title: string | null }[];
  // 1.5.2 (#95): per variant, what the server recorded for its pinned image (a copy's).
  pins?: Record<string, EnginePin>;
};

// 1.5.2 (#95): newer versions of the built-in engines, as the last check found them (GET /api/engine-updates).
export type EngineUpdateWhy = { code: string; text: string } & Record<string, string | string[] | number | null | undefined>;
export type EngineUpdateVariant = {
  name: string; runtime: "container" | "process"; vendor: string; available: boolean;
  tag?: string; digest?: string; image?: string; url?: string; sha256?: string; archive?: string; release?: string;
  why?: EngineUpdateWhy;
};
export type EngineUpdateCandidate = {
  version: string; kind: "release" | "build"; revision?: string | null; created?: string | null;
  release?: { tag: string; name: string | null; url: string | null; published_at: string | null };
  variants: EngineUpdateVariant[];
  mentions?: { flag: string; lines: number; excerpts: string[] }[];
  notes?: EngineUpdateWhy;
};
export type EngineUpdateStatus = "newer" | "newer_build" | "up_to_date" | "partly_checked" | "could_not_check" | "not_checked";
export type EngineUpdate = {
  recipe: string; title: string; engine: string; version: string; kind: "release" | "rolling" | "own";
  status: EngineUpdateStatus; newest?: string | null; candidates: EngineUpdateCandidate[];
  errors: { where: string; text: string }[]; upstream?: EngineUpdateWhy; why?: EngineUpdateWhy;
};
export type EngineUpdatesResult = {
  checked_at: string; checked_by: string; seconds: number; github_requests: number; engines: Record<string, EngineUpdate>;
};
export type EngineUpdatesResponse = { result: EngineUpdatesResult | null; can_check: boolean; signatures?: Record<string, EngineSigned> };
