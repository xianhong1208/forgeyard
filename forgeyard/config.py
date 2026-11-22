"""Settings, read from the environment (and a `.env` file next to the working directory)."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from forgeyard.install.images import reference_error


MIN_HEARTBEAT_SECONDS = 2

# Deprecated in 1.3.1 and removed in 1.4: no longer read. The setting, the one that replaces it
# (None: nothing does), and what happens instead. A server that still has one set says so at start
# and in Setup health (forgeyard/settings_check.py), and SECRET_KEY_FILE that would now mean another
# key file stops the start (forgeyard/security/crypto.py).
REMOVED: dict[str, tuple[Optional[str], str]] = {
    "OPENROUTER_MODELS_URL": (None, "Refresh prices from OpenRouter always reads OpenRouter's public list"),
    "USAGE_FLUSH_SECONDS": (None, "usage is written to the database every 5 seconds"),
    "PROVIDER_COOLDOWN_SECONDS": (None, "a failed provider target is tried last for 30 seconds (longer when its "
                                        "Retry-After says so, up to 5 minutes)"),
    "PROVIDER_RETRY_AFTER_CAP": (None, "a Retry-After of up to 2 seconds is waited for once"),
    "FORGEYARD_SETUP_TOKEN": ("ADMIN_USERNAME and ADMIN_PASSWORD", "for an unattended first start; otherwise use the "
                              "setup token the server prints"),
    "AGENT_HEARTBEAT_SECONDS": ("AGENT_OFFLINE_SECONDS", "agents send a heartbeat every third of it, at least every "
                                "2 seconds"),
    "SECRET_KEY_FILE": ("FORGEYARD_SECRET_KEY_FILE", "the same setting under its new name"),
}
REMOVED_IN = "1.4"


def removed_in_use(environ: Optional[Mapping[str, str]] = None,
                   dotenv: Optional[Mapping[str, Optional[str]]] = None) -> dict[str, str]:
    """{NAME: value} for each removed setting that is still set, with a value, in the environment or
    in `.env` (the one the server reads: next to the working directory). Names are matched without
    regard to case, as settings are; the environment wins over `.env`, as it does for settings."""
    environ = os.environ if environ is None else environ
    if dotenv is None:
        dotenv = dotenv_values()
    out: dict[str, str] = {}
    for source in (dotenv, environ):  # the environment last, so it wins
        for name, value in source.items():
            upper = (name or "").upper()
            if upper in REMOVED and value is not None and str(value).strip():
                out[upper] = str(value).strip()
    return out


def dotenv_values(path: Path | str = ".env") -> dict[str, Optional[str]]:
    """The names and values a `.env` file sets ({} when there is none or it cannot be read)."""
    try:
        from dotenv import dotenv_values as read

        return {k: v for k, v in read(path).items() if k}
    except (OSError, UnicodeDecodeError):
        return {}


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
    # GET /metrics (Prometheus, 1.4.5, #165). Empty: only a direct request from this host (loopback, no proxy in
    # between) may read it; set: every scrape needs `Authorization: Bearer <token>`, from anywhere.
    metrics_token: str = ""

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
    forgeyard_secret_key_file: str = ""
    # Or the key itself (base64 of 32 bytes), e.g. from a secrets manager; never written to disk.
    forgeyard_secret_key: str = ""

    # First-run owner, created at startup instead of through /setup.
    admin_username: str = ""
    admin_password: str = ""

    # Self-registration: "off" (default) or "approval" (new accounts wait for an admin).
    registration: str = "off"

    session_idle_minutes: int = 120
    session_max_days: int = 7
    # "auto": Secure cookies when the request came over https; "true" / "false" to force. Anything else
    # stops the start (it used to act as "auto" without a word).
    cookie_secure: str = "auto"

    # Requests per minute per client IP across the API. 0 turns it off.
    rate_limit_per_minute: int = 600
    # Failed sign-ins per account before each further try must wait (doubling, max 5 min).
    login_free_failures: int = 5
    # Failed sign-ins per IP within 15 minutes before that IP must wait.
    login_ip_failures: int = 30

    # Machines: a machine with nothing heard for this long is shown offline (a server-side check, so a
    # silent network drop counts). Agents send a heartbeat every third of it (at least 2 s).
    agent_offline_seconds: int = 30
    # The agent image the Machines page puts in its `docker run` (forgeyard/install/images.py). Empty:
    # the published one, docker.io/<namespace>/forgeyard-agent, or a local build while none is
    # published. Without :TAG or @sha256:DIGEST the tag is this server's version. A mirror or a
    # private registry goes here, e.g. registry.example.org/forgeyard/forgeyard-agent.
    agent_image: str = ""

    # Models (M3).
    # Where Hugging Face models are inspected and downloaded from (a mirror can be set here).
    hf_endpoint: str = "https://huggingface.co"
    # Allow model sources on private addresses (and plain http): only for a LAN mirror. Agents
    # need their own --allow-private-model-hosts as well.
    model_allow_private_hosts: bool = False
    # The largest image file (`docker save`, optionally gzip) an admin may upload to a machine from its page
    # (1.4); it is streamed through the server to the agent, never stored.
    image_upload_max_gb: int = Field(default=64, ge=1, le=4096)
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
    # Images, audio and video in chat (1.3). The gateway fetches http(s) media URLs itself, through the
    # egress rules (public addresses only, every redirect checked), and sends them on as data: URIs;
    # engines never fetch. At most this many MiB per fetched item.
    media_fetch_max_mb: int = Field(default=20, ge=1, le=1024)
    # Let media URLs point at private or loopback addresses (an image server on your LAN). Link-local
    # and metadata addresses stay refused.
    media_allow_private_hosts: bool = False

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
    # Budgets: a cloud request is reserved at its estimated cost before it is sent. Without
    # `max_tokens` in the request, its answer is estimated at this many tokens.
    budget_default_max_tokens: int = Field(default=4096, ge=1, le=10_000_000)

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

    @field_validator("agent_image")
    @classmethod
    def _agent_image(cls, v: str) -> str:
        v = v.strip()
        problem = reference_error(v) if v else None
        if problem:
            raise ValueError(problem)
        return v

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

    @field_validator("cookie_secure")
    @classmethod
    def _cookie_secure(cls, v: str) -> str:
        v = v.strip().lower()
        v = {"1": "true", "yes": "true", "on": "true", "0": "false", "no": "false", "off": "false"}.get(v, v)
        if v not in ("auto", "true", "false"):
            raise ValueError("COOKIE_SECURE must be 'auto', 'true' or 'false'")
        return v

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
        """FORGEYARD_SECRET_KEY_FILE, else data_dir/secret.key."""
        if self.forgeyard_secret_key_file.strip():
            return Path(self.forgeyard_secret_key_file.strip())
        return self.data_dir / "secret.key"

    @property
    def heartbeat_seconds(self) -> int:
        """How often agents send a heartbeat: a third of the offline time, at least 2 s."""
        return max(MIN_HEARTBEAT_SECONDS, int(self.agent_offline_seconds) // 3)

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
