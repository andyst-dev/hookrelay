from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        enable_retry_worker=False,
        retry_delays_seconds=(0, 0, 0),
        base_url="http://testserver",
        rate_limit_per_minute=50,
    )


@pytest.fixture
def session_factory(settings: Settings) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.fixture
def client(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Generator[TestClient, None, None]:
    app = create_app(
        settings,
        session_factory=session_factory,
        database_engine=session_factory.kw["bind"],
    )
    with TestClient(app) as test_client:
        yield test_client
