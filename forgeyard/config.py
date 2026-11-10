"""Settings, read from the environment (and a `.env` file next to the working directory)."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 7860
    # Where Forgeyard keeps its state: the SQLite database and the secrets file.
    data_dir: Path = Path("data")
    # Empty: SQLite in data_dir. Set postgresql+psycopg://… for PostgreSQL.
    database_url: str = ""
    # The address people open Forgeyard at; its host is always allowed (see allowed_hosts).
    public_url: str = ""
    # Extra host names the server answers to, comma-separated. localhost and IP addresses
    # are always allowed; any other Host gets 421 (DNS-rebinding guard).
    allowed_hosts: str = ""
    # Proxies whose X-Forwarded-For is believed, comma-separated IPs. Empty: none.
    trusted_proxies: str = ""

    # First-run owner, created at startup instead of through /setup.
    admin_username: str = ""
    admin_password: str = ""
    # Pins the one-time setup token instead of generating one.
    forgeyard_setup_token: str = ""

    # Self-registration: "off" (default) or "approval" (new accounts wait for an admin).
    registration: str = "off"

    session_idle_minutes: int = 120
    session_max_days: int = 7
    # "auto": Secure cookies when the request came over https.
    cookie_secure: str = "auto"

    # Requests per minute per client IP across the API. 0 turns it off.
    rate_limit_per_minute: int = 600
    # Failed sign-ins per account before each further try must wait (doubling, max 5 min).
    login_free_failures: int = 5
    # Failed sign-ins per IP within 15 minutes before that IP must wait.
    login_ip_failures: int = 30

    # Machines: agents send a heartbeat this often, and a machine with nothing heard for
    # agent_offline_seconds is shown offline (a server-side check, so a silent network drop counts).
    agent_heartbeat_seconds: int = 10
    agent_offline_seconds: int = 30

    # Models (M3).
    # Where Hugging Face models are inspected and downloaded from (a mirror can be set here).
    hf_endpoint: str = "https://huggingface.co"
    # Allow model sources on private addresses (and plain http): only for a LAN mirror. Agents
    # need their own --allow-private-model-hosts as well.
    model_allow_private_hosts: bool = False
    # Initial allowlists; admins change them on the Models page (stored in the database).
    model_url_hosts: str = ""      # hosts allowed for https:// GGUF sources, comma-separated
    model_local_dirs: str = ""     # directories allowed for local:// sources, comma-separated
    # Per-process GPU memory a replica's CUDA context takes on top of its own (measured: ~0.4 GiB
    # on an A100), and memory always left free on a card when placing.
    gpu_context_mib: int = 400
    vram_headroom_mib: int = 512

    # Gateway (M4), /v1. Connecting to a replica that does not answer within this many seconds
    # moves on to the next one; once connected, it may take this long to send its answer's
    # headers or the next piece of a streamed answer (long generations, queued requests).
    gateway_connect_timeout: float = 5.0
    gateway_read_timeout: float = 600.0
    # The largest request body accepted (an audio file for transcription is the big one).
    gateway_max_body_mb: int = 32
    # Initial limits for API keys, per minute; admins change them on the API keys page (stored in
    # the database). default_*: what a key without its own limit gets; max_*: the most a user may
    # give their own key. 0: no limit / no cap.
    gateway_default_rpm: int = 0
    gateway_default_tpm: int = 0
    gateway_max_rpm: int = 0
    gateway_max_tpm: int = 0
    # How often gateway usage counted in memory is written to the database (seconds).
    usage_flush_seconds: float = 5.0

    @field_validator("registration")
    @classmethod
    def _registration(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("off", "approval"):
            raise ValueError("REGISTRATION must be 'off' or 'approval'")
        return v

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'forgeyard.db').as_posix()}"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_proxies.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
