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
