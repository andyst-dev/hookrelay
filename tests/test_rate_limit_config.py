from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.rate_limit import EndpointRateLimiter


def test_rate_limiter_window() -> None:
    limiter = EndpointRateLimiter(2, window_seconds=10)
    assert limiter.allow("key", now=1)
    assert limiter.allow("key", now=2)
    assert not limiter.allow("key", now=3)
    assert limiter.allow("key", now=12)
    assert limiter.allow("other", now=3)


def test_settings_payload_size_and_data_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "events.db"
    settings = Settings(database_url=f"sqlite:///{path}", max_payload_size_mb=0.5)
    settings.ensure_data_directory()
    assert path.parent.is_dir()
    assert settings.max_payload_bytes == 524288
    assert settings.public_base_url == "http://localhost:8000"
    settings.render_external_hostname = "hookrelay.example.onrender.com"
    assert settings.public_base_url == "https://hookrelay.example.onrender.com"
