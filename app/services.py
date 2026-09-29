from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import Settings
from app.models import DeliveryAttempt, Endpoint, Event, utcnow
from app.security import UnsafeDestinationError, validate_destination_url
from app.transformations import TransformationError, transform_payload, validate_rules

logger = logging.getLogger("hookrelay.delivery")

DEMO_RULES: list[dict[str, Any]] = [
    {"op": "rename", "from": "type", "to": "event_type"},
    {"op": "copy", "from": "customer.email", "to": "email"},
    {"op": "remove", "path": "customer"},
    {"op": "add", "path": "source", "value": "hookrelay-demo"},
]
DEMO_PAYLOAD = {
    "id": "evt_demo_001",
    "type": "payment.completed",
    "customer": {"email": "demo@example.com"},
    "amount": 4900,
    "currency": "CHF",
}


def create_endpoint_record(
    session: Session,
    settings: Settings,
    *,
    name: str,
    destination_url: str,
    enabled: bool = True,
    signing_secret: str | None = None,
    transformation_rules: list[dict[str, Any]],
) -> Endpoint:
    if settings.public_demo_mode:
        raise PermissionError("endpoint creation is disabled in public demo mode")
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("name must not be blank")
    if signing_secret and len(signing_secret) < 16:
        raise ValueError("signing secrets must be at least 16 characters")
    validate_destination_url(destination_url, settings)
    validate_rules(transformation_rules)
    endpoint = Endpoint(
        name=cleaned_name,
        public_key=secrets.token_urlsafe(18),
        destination_url=destination_url,
        enabled=enabled,
        signing_secret=signing_secret or None,
        transformation_rules=transformation_rules,
    )
    session.add(endpoint)
    session.commit()
    session.refresh(endpoint)
    return endpoint


def prepare_event_replay(session: Session, event_id: int) -> Event:
    event = session.scalar(
        select(Event).options(selectinload(Event.endpoint)).where(Event.id == event_id)
    )
    if not event:
        raise LookupError("event not found")
    if not event.endpoint.enabled:
        raise PermissionError("endpoint is disabled")
    if event.signature_valid is False:
        raise PermissionError("rejected signatures cannot be replayed")
    event.transformed_payload = transform_payload(
        event.original_payload, event.endpoint.transformation_rules
    )
    event.status = "pending"
    event.next_retry_at = None
    session.commit()
    return event


def ensure_demo_endpoint(session: Session) -> Endpoint:
    endpoint = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
    if endpoint:
        return endpoint
    endpoint = Endpoint(
        name="Payment demo",
        public_key="demo",
        destination_url="demo://receiver",
        enabled=True,
        transformation_rules=DEMO_RULES,
        is_demo=True,
    )
    session.add(endpoint)
    session.commit()
    session.refresh(endpoint)
    return endpoint


def _schedule_retry(event: Event, settings: Settings) -> None:
    if event.retry_count >= settings.max_retries:
        event.status = "failed"
        event.next_retry_at = None
        return
    delay_index = min(event.retry_count, len(settings.retry_delays_seconds) - 1)
    event.status = "retrying"
    event.next_retry_at = utcnow() + timedelta(seconds=settings.retry_delays_seconds[delay_index])


def _record_attempt(
    session: Session,
    event: Event,
    *,
    destination: str,
    started: float,
    status_code: int | None,
    succeeded: bool,
    error: str | None,
    manual: bool,
) -> None:
    attempt_number = (
        session.scalar(
            select(func.count(DeliveryAttempt.id)).where(DeliveryAttempt.event_id == event.id)
        )
        or 0
    ) + 1
    session.add(
        DeliveryAttempt(
            event_id=event.id,
            attempt_number=attempt_number,
            destination_url=destination,
            status_code=status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            succeeded=succeeded,
            error=error,
            is_manual_replay=manual,
        )
    )


def deliver_event(
    session: Session,
    event: Event,
    settings: Settings,
    *,
    manual: bool = False,
    automated_retry: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> Event:
    endpoint = event.endpoint
    if automated_retry:
        event.retry_count += 1
    if manual:
        event.retry_count = 0
        event.transformed_payload = transform_payload(
            event.original_payload, endpoint.transformation_rules
        )

    started = time.perf_counter()
    status_code: int | None = None
    response_body: str | None = None
    error: str | None = None
    try:
        if endpoint.is_demo and endpoint.destination_url == "demo://receiver":
            status_code = 202
            response_body = '{"accepted":true,"receiver":"hookrelay-demo"}'
        else:
            destination = validate_destination_url(endpoint.destination_url, settings)
            timeout = httpx.Timeout(
                connect=settings.delivery_connect_timeout_seconds,
                read=settings.delivery_read_timeout_seconds,
                write=settings.delivery_read_timeout_seconds,
                pool=settings.delivery_connect_timeout_seconds,
            )
            with (
                httpx.Client(
                    timeout=timeout, follow_redirects=False, transport=transport
                ) as client,
                client.stream(
                    "POST",
                    destination,
                    json=event.transformed_payload,
                    headers={
                        "User-Agent": "HookRelay/1.0",
                        "X-HookRelay-Event": str(event.id),
                    },
                ) as response,
            ):
                status_code = response.status_code
                captured = bytearray()
                for chunk in response.iter_bytes():
                    remaining = settings.max_response_capture_bytes - len(captured)
                    captured.extend(chunk[:remaining])
                    if len(captured) >= settings.max_response_capture_bytes:
                        break
                response_body = bytes(captured).decode("utf-8", errors="replace")
        succeeded = status_code is not None and 200 <= status_code < 300
        if not succeeded:
            error = f"destination returned HTTP {status_code}"
    except (httpx.HTTPError, UnsafeDestinationError, TransformationError) as exc:
        succeeded = False
        error = str(exc)[:1000]

    event.latest_response_status = status_code
    event.latest_response_body = response_body
    event.latest_error = error
    if succeeded:
        event.status = "delivered"
        event.next_retry_at = None
    else:
        _schedule_retry(event, settings)
    _record_attempt(
        session,
        event,
        destination=endpoint.destination_url,
        started=started,
        status_code=status_code,
        succeeded=succeeded,
        error=error,
        manual=manual,
    )
    session.commit()
    session.refresh(event)
    logger.info(
        "delivery completed",
        extra={"event_id": event.id, "status": event.status, "status_code": status_code},
    )
    return event


def deliver_event_by_id(
    session_factory: sessionmaker[Session],
    event_id: int,
    settings: Settings,
    *,
    manual: bool = False,
    automated_retry: bool = False,
) -> None:
    with session_factory() as session:
        event = session.get(Event, event_id)
        if event:
            deliver_event(
                session,
                event,
                settings,
                manual=manual,
                automated_retry=automated_retry,
            )


def process_due_retries(session_factory: sessionmaker[Session], settings: Settings) -> int:
    now = utcnow()
    with session_factory() as session:
        event_ids = list(
            session.scalars(
                select(Event.id)
                .where(Event.status == "retrying", Event.next_retry_at <= now)
                .order_by(Event.next_retry_at)
                .limit(25)
            )
        )
    for event_id in event_ids:
        deliver_event_by_id(session_factory, event_id, settings, automated_retry=True)
    return len(event_ids)


async def retry_worker(session_factory: sessionmaker[Session], settings: Settings) -> None:
    while True:
        try:
            await asyncio.to_thread(process_due_retries, session_factory, settings)
        except (httpx.HTTPError, OSError, SQLAlchemyError):
            logger.exception("retry worker cycle failed")
        await asyncio.sleep(settings.retry_worker_interval_seconds)
