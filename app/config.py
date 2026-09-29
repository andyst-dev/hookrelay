from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/hookrelay.db"
    max_payload_size_mb: float = Field(default=1.0, gt=0, le=10)
    max_response_capture_bytes: int = Field(default=4096, ge=128, le=65_536)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delays_seconds: tuple[int, ...] = (10, 30, 120, 600)
    event_retention_hours: int = Field(default=168, ge=1)
    allow_private_destinations_for_dev: bool = False
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_stored_headers: int = Field(default=40, ge=1, le=100)
    delivery_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    delivery_read_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    retry_worker_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    enable_retry_worker: bool = True
    public_demo_mode: bool = False
    base_url: str = "http://localhost:8000"
    render_external_hostname: str | None = None

    @property
    def max_payload_bytes(self) -> int:
        return int(self.max_payload_size_mb * 1024 * 1024)

    @property
    def public_base_url(self) -> str:
        if self.render_external_hostname:
            return f"https://{self.render_external_hostname}"
        return self.base_url.rstrip("/")

    def ensure_data_directory(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            path = self.database_url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
