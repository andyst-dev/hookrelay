from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    # SQLite stores naive values; create them from an explicit UTC clock.
    return datetime.now(UTC).replace(tzinfo=None)


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    public_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    destination_url: Mapped[str] = mapped_column(String(2048))
    signing_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    transformation_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    events: Mapped[list[Event]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("endpoints.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    request_headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    original_payload: Mapped[Any] = mapped_column(JSON)
    transformed_payload: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    signature_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    endpoint: Mapped[Endpoint] = relationship(back_populates="events")
    attempts: Mapped[list[DeliveryAttempt]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="DeliveryAttempt.attempt_number",
    )


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    destination_url: Mapped[str] = mapped_column(String(2048))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_manual_replay: Mapped[bool] = mapped_column(Boolean, default=False)

    event: Mapped[Event] = relationship(back_populates="attempts")
