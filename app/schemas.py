from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EndpointCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    destination_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    signing_secret: str | None = Field(default=None, min_length=16, max_length=512)
    transformation_rules: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    destination_url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    signing_secret: str | None = Field(default=None, min_length=16, max_length=512)
    clear_signing_secret: bool = False
    transformation_rules: list[dict[str, Any]] | None = None

    @field_validator("name")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank")
        return value.strip() if value is not None else None


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    public_key: str
    destination_url: str
    enabled: bool
    signing_secret_configured: bool
    transformation_rules: list[dict[str, Any]]
    is_demo: bool
    created_at: datetime
    updated_at: datetime


class DeliveryAttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    attempt_number: int
    attempted_at: datetime
    destination_url: str
    status_code: int | None
    duration_ms: float
    succeeded: bool
    error: str | None
    is_manual_replay: bool


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    endpoint_id: int
    received_at: datetime
    request_headers: dict[str, str]
    original_payload: Any
    transformed_payload: Any | None
    signature_valid: bool | None
    status: str
    retry_count: int
    latest_response_status: int | None
    latest_response_body: str | None
    latest_error: str | None
    next_retry_at: datetime | None
    attempts: list[DeliveryAttemptOut] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    version: str
