from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import __version__
from app.config import Settings, get_settings
from app.database import get_db
from app.models import Endpoint, Event, utcnow
from app.schemas import EndpointCreate, EndpointOut, EndpointUpdate, EventOut, HealthOut
from app.security import (
    UnsafeDestinationError,
    sanitize_headers,
    validate_destination_url,
    verify_signature,
)
from app.services import create_endpoint_record, deliver_event_by_id, prepare_event_replay
from app.transformations import TransformationError, transform_payload, validate_rules

router = APIRouter(prefix="/api")
hooks_router = APIRouter()


def endpoint_out(endpoint: Endpoint) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        name=endpoint.name,
        public_key=endpoint.public_key,
        destination_url=endpoint.destination_url,
        enabled=endpoint.enabled,
        signing_secret_configured=endpoint.signing_secret is not None,
        transformation_rules=endpoint.transformation_rules,
        is_demo=endpoint.is_demo,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", version=__version__)


@router.get("/endpoints", response_model=list[EndpointOut])
def list_endpoints(db: Session = Depends(get_db)) -> list[EndpointOut]:
    endpoints = db.scalars(select(Endpoint).order_by(Endpoint.created_at.desc())).all()
    return [endpoint_out(endpoint) for endpoint in endpoints]


@router.post("/endpoints", response_model=EndpointOut, status_code=201)
def create_endpoint(
    data: EndpointCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EndpointOut:
    try:
        endpoint = create_endpoint_record(
            db,
            settings,
            name=data.name,
            destination_url=data.destination_url,
            enabled=data.enabled,
            signing_secret=data.signing_secret,
            transformation_rules=data.transformation_rules,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (UnsafeDestinationError, TransformationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return endpoint_out(endpoint)


def _endpoint_or_404(db: Session, endpoint_id: int) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="endpoint not found")
    return endpoint


@router.get("/endpoints/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(endpoint_id: int, db: Session = Depends(get_db)) -> EndpointOut:
    return endpoint_out(_endpoint_or_404(db, endpoint_id))


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    endpoint_id: int,
    data: EndpointUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EndpointOut:
    endpoint = _endpoint_or_404(db, endpoint_id)
    if settings.public_demo_mode:
        raise HTTPException(
            status_code=403, detail="endpoint editing is disabled in public demo mode"
        )
    if endpoint.is_demo and data.destination_url is not None:
        raise HTTPException(status_code=409, detail="the demo destination cannot be changed")
    changes = data.model_dump(exclude_unset=True)
    if data.destination_url is not None:
        try:
            validate_destination_url(data.destination_url, settings)
        except UnsafeDestinationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if data.transformation_rules is not None:
        try:
            validate_rules(data.transformation_rules)
        except TransformationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if changes.pop("clear_signing_secret", False):
        endpoint.signing_secret = None
    for field, value in changes.items():
        setattr(endpoint, field, value)
    endpoint.updated_at = utcnow()
    db.commit()
    db.refresh(endpoint)
    return endpoint_out(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(
    endpoint_id: int,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if settings.public_demo_mode:
        raise HTTPException(
            status_code=403, detail="endpoint deletion is disabled in public demo mode"
        )
    endpoint = _endpoint_or_404(db, endpoint_id)
    if endpoint.is_demo:
        raise HTTPException(status_code=409, detail="the built-in demo endpoint cannot be deleted")
    db.delete(endpoint)
    db.commit()
    return Response(status_code=204)


@router.get("/events", response_model=list[EventOut])
def list_events(
    endpoint_id: int | None = None,
    status: str | None = None,
    signature_valid: bool | None = None,
    received_after: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Event]:
    statement = (
        select(Event)
        .options(selectinload(Event.attempts))
        .order_by(Event.received_at.desc())
        .limit(limit)
    )
    if endpoint_id is not None:
        statement = statement.where(Event.endpoint_id == endpoint_id)
    if status is not None:
        statement = statement.where(Event.status == status)
    if signature_valid is not None:
        statement = statement.where(Event.signature_valid == signature_valid)
    if received_after is not None:
        statement = statement.where(Event.received_at >= received_after)
    return list(db.scalars(statement).all())


@router.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)) -> Event:
    event = db.scalar(
        select(Event).options(selectinload(Event.attempts)).where(Event.id == event_id)
    )
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return event


@router.post("/events/{event_id}/replay", response_model=EventOut, status_code=202)
def replay_event(
    event_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Event:
    try:
        event = prepare_event_replay(db, event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TransformationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(
        deliver_event_by_id,
        request.app.state.session_factory,
        event.id,
        settings,
        manual=True,
    )
    return event


async def ingest_webhook(
    endpoint: Endpoint,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session,
    settings: Settings,
) -> tuple[Event, int]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > settings.max_payload_bytes
    ):
        raise HTTPException(status_code=413, detail="payload is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.max_payload_bytes:
            raise HTTPException(status_code=413, detail="payload is too large")
        body.extend(chunk)
    raw_body = bytes(body)
    try:
        payload: Any = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="request body must contain valid JSON") from exc

    signature_valid = (
        verify_signature(
            raw_body, endpoint.signing_secret, request.headers.get("x-hookrelay-signature")
        )
        if endpoint.signing_secret
        else None
    )
    event = Event(
        endpoint_id=endpoint.id,
        request_headers=sanitize_headers(request.headers, settings.max_stored_headers),
        original_payload=payload,
        signature_valid=signature_valid,
        status="rejected" if signature_valid is False else "pending",
    )
    if signature_valid is not False:
        try:
            event.transformed_payload = transform_payload(payload, endpoint.transformation_rules)
        except TransformationError as exc:
            event.status = "failed"
            event.latest_error = str(exc)
    db.add(event)
    db.commit()
    db.refresh(event)

    if signature_valid is False:
        return event, 401
    if event.status == "failed":
        return event, 422
    background_tasks.add_task(
        deliver_event_by_id, request.app.state.session_factory, event.id, settings
    )
    return event, 202


@hooks_router.post("/hooks/{endpoint_key}")
async def receive_webhook(
    endpoint_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    endpoint = db.scalar(select(Endpoint).where(Endpoint.public_key == endpoint_key))
    if not endpoint:
        raise HTTPException(status_code=404, detail="endpoint not found")
    if not endpoint.enabled:
        raise HTTPException(status_code=410, detail="endpoint is disabled")
    if not request.app.state.rate_limiter.allow(endpoint.public_key):
        raise HTTPException(
            status_code=429, detail="rate limit exceeded", headers={"Retry-After": "60"}
        )
    event, status_code = await ingest_webhook(endpoint, request, background_tasks, db, settings)
    content = {"event_id": event.id, "status": event.status}
    if status_code == 401:
        content["detail"] = "invalid webhook signature"
    elif status_code == 422:
        content["detail"] = event.latest_error
    return Response(
        content=json.dumps(content), status_code=status_code, media_type="application/json"
    )
