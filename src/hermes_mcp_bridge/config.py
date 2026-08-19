"""Environment-backed bridge configuration.

Secret-bearing settings support the Docker-secrets convention: ``<NAME>_FILE``
points at a mounted file and takes precedence over ``<NAME>``. Values are read
at settings construction; paths are never logged or echoed back.
"""

import os
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .secretfiles import read_secret


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    hermes_api_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr = SecretStr("")
    hermes_model: str = "hermes-agent"
    hermes_request_timeout_seconds: float = Field(default=30.0, gt=0)
    hermes_run_poll_interval_seconds: float = Field(default=1.0, gt=0)
    hermes_run_default_wait_seconds: float = Field(default=45.0, ge=0)
    hermes_run_max_wait_seconds: float = Field(default=7200.0, gt=0)
    hermes_progress_interval_seconds: float = Field(default=15.0, gt=0)
    hermes_event_stream_connect_timeout_seconds: float = Field(default=30.0, gt=0)

    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8765, ge=1, le=65535)
    mcp_path: str = "/mcp"
    log_level: str = "INFO"
    bridge_state_db_path: str = "/var/lib/hermes-mcp-bridge/state.sqlite3"
    bridge_version: str = "1.0.0"

    # Factory northbound is an additive, owner-controlled surface. It stays
    # absent by default so the stable 27-tool Bridge contract is unchanged.
    hermes_factory_northbound_enabled: bool = False
    hermes_factory_registry_path: str = ""

    # Resilience remains disabled by default. Enabling it is an explicit
    # operator decision and therefore does not change request behaviour during
    # an upgrade.
    bridge_retry_enabled: bool = False
    bridge_retry_max_attempts: int = Field(default=3, ge=1, le=10)
    bridge_retry_base_seconds: float = Field(default=0.5, gt=0, le=60.0)
    bridge_retry_max_seconds: float = Field(default=10.0, gt=0, le=300.0)
    bridge_retry_jitter_ratio: float = Field(default=0.1, ge=0.0, le=1.0)

    bridge_circuit_enabled: bool = False
    bridge_circuit_failure_threshold: int = Field(default=5, ge=1, le=1000)
    bridge_circuit_recovery_seconds: float = Field(default=30.0, gt=0, le=3600.0)
    bridge_circuit_half_open_max_calls: int = Field(default=1, ge=1, le=100)
    bridge_circuit_success_threshold: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def _resolve_file_backed_secrets(self) -> "Settings":
        """Apply file-backed secrets and enforce explicit Factory activation config."""

        if os.environ.get("HERMES_API_KEY_FILE"):
            value = read_secret("HERMES_API_KEY")
            if value:
                object.__setattr__(self, "hermes_api_key", SecretStr(value))
        if not self.hermes_api_key.get_secret_value():
            raise ValueError("HERMES_API_KEY (or HERMES_API_KEY_FILE) must be configured")
        if (
            self.hermes_factory_northbound_enabled
            and not self.hermes_factory_registry_path.strip()
        ):
            raise ValueError(
                "HERMES_FACTORY_REGISTRY_PATH must be configured when "
                "HERMES_FACTORY_NORTHBOUND_ENABLED is true"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance."""

    return Settings()  # type: ignore[call-arg]
