"""Settings, read from the environment (and a `.env` file next to the working directory)."""

from functools import lru_cache
from pathlib import Path

from pydantic import PrivateAttr, field_validator, model_validator
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
    # Proxies whose X-Forwarded-For (and X-Forwarded-Proto) is believed, comma-separated IPs. Empty: none.
    trusted_proxies: str = ""

    # HTTPS (docs/https-and-trust.md). "on" (the default): the server speaks HTTPS with a certificate
    # from Forgeyard's own certificate authority, made on first start in data_dir/tls, or with
    # TLS_CERT_FILE / TLS_KEY_FILE when both are set. "off": plain HTTP, only behind a proxy that
    # terminates TLS (admins are warned in the web UI when requests still arrive over plain HTTP).
    tls: str = "on"
    tls_cert_file: str = ""
    tls_key_file: str = ""
    # With your own certificate: the CA that issued it (a company CA). Agents then pin this CA
    # instead of Forgeyard's. Leave empty for a publicly trusted certificate (the system store).
    tls_ca_file: str = ""
    # Extra names or IP addresses for Forgeyard's server certificate, comma-separated (an address
    # behind NAT, a DNS alias). PUBLIC_URL's host, ALLOWED_HOSTS, the host name and the machine's
    # own addresses are always included.
    tls_extra_names: str = ""

    # The key that encrypts stored secrets (Hugging Face tokens, replica keys, workload environment
    # values). Empty: data_dir/secret.key, made on first start (mode 0600). Back it up apart from
    # the database: without it those secrets cannot be read (docs/https-and-trust.md).
    secret_key_file: str = ""
    # Or the key itself (base64 of 32 bytes), e.g. from a secrets manager; never written to disk.
    forgeyard_secret_key: str = ""

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

    # Cloud providers (1.2). Initial values of a key's `cloud` setting; admins change them on the API
    # keys page (stored in the database). default: what a new key gets; max: the most a user may give
    # their own key (off < fallback < on). Keys made before 1.2 are "off".
    gateway_cloud_default: str = "off"
    gateway_cloud_max: str = "off"
    # Requests to providers go through this proxy (http://host:port) when set; never through a
    # proxy from the environment.
    egress_proxy: str = ""
    # Only these provider hosts, comma-separated (`*.example.org` for subdomains). Empty: any host.
    egress_allowed_hosts: str = ""
    # A CA file to trust for providers on top of the system's (a company proxy that inspects TLS).
    egress_ca_file: str = ""
    # Providers on private or loopback addresses, and plain http, also when marked as outside your
    # network: for testing against a stand-in only. Link-local and metadata addresses stay refused.
    provider_allow_private_hosts: bool = False
    provider_connect_timeout: float = 10.0
    # Waiting for a provider's answer headers or the next piece of a stream (a provider can lower it).
    provider_read_timeout: float = 300.0
    # A provider target that failed (429, 5xx, no connection) is tried last for this long.
    provider_cooldown_seconds: float = 30.0
    # A provider's Retry-After up to this many seconds is waited for once, when no other target is left.
    provider_retry_after_cap: float = 2.0

    @field_validator("gateway_cloud_default", "gateway_cloud_max")
    @classmethod
    def _cloud(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("off", "fallback", "on"):
            raise ValueError("GATEWAY_CLOUD_DEFAULT and GATEWAY_CLOUD_MAX: off, fallback or on")
        return v

    @property
    def egress_allowed_host_list(self) -> list[str]:
        return [h.strip().lower() for h in self.egress_allowed_hosts.split(",") if h.strip()]

    @field_validator("registration")
    @classmethod
    def _registration(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("off", "approval"):
            raise ValueError("REGISTRATION must be 'off' or 'approval'")
        return v

    # PUBLIC_URL said http:// while the server itself speaks HTTPS (a 1.0 setting): fixed, and logged.
    _public_url_upgraded: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _public_url_scheme(self) -> "Settings":
        """With TLS on, the server only speaks HTTPS, so an http:// PUBLIC_URL (kept from 1.0) would
        put dead links in join commands, set-password links and the gateway address."""
        if self.tls == "on" and self.public_url.strip().lower().startswith("http://"):
            self.public_url = "https://" + self.public_url.strip()[len("http://"):]
            self._public_url_upgraded = True
        return self

    @property
    def public_url_upgraded(self) -> bool:
        return self._public_url_upgraded

    @field_validator("tls")
    @classmethod
    def _tls(cls, v: str) -> str:
        v = v.strip().lower()
        v = {"true": "on", "1": "on", "yes": "on", "false": "off", "0": "off", "no": "off"}.get(v, v)
        if v not in ("on", "off"):
            raise ValueError("TLS must be 'on' or 'off'")
        return v

    @property
    def tls_on(self) -> bool:
        return self.tls == "on"

    @property
    def own_certificate(self) -> bool:
        return bool(self.tls_cert_file.strip() and self.tls_key_file.strip())

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def secret_key_path(self) -> Path:
        return Path(self.secret_key_file) if self.secret_key_file.strip() else self.data_dir / "secret.key"

    @property
    def tls_extra_name_list(self) -> list[str]:
        return [h.strip() for h in self.tls_extra_names.split(",") if h.strip()]

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
