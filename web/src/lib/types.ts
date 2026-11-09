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
