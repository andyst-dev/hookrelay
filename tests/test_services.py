from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import DeliveryAttempt, Endpoint, Event, utcnow
from app.services import (
    deliver_event,
    ensure_demo_endpoint,
    process_due_retries,
)


def make_event(
    session_factory: sessionmaker[Session],
    *,
    destination: str = "https://93.184.216.34/receive",
    rules: list[dict] | None = None,
) -> int:
    with session_factory() as session:
        endpoint = Endpoint(
            name="Destination",
            public_key=f"key-{utcnow().timestamp()}",
            destination_url=destination,
            transformation_rules=rules or [],
        )
        event = Event(
            endpoint=endpoint,
            request_headers={},
            original_payload={"id": 1},
            transformed_payload={"id": 1},
            status="pending",
        )
        session.add(event)
        session.commit()
        return event.id


def test_successful_delivery_records_bounded_response(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    event_id = make_event(session_factory)
    settings.max_response_capture_bytes = 5
    transport = httpx.MockTransport(lambda request: httpx.Response(201, content=b"abcdefgh"))
    with session_factory() as session:
        event = session.get(Event, event_id)
        deliver_event(session, event, settings, transport=transport)
        assert event.status == "delivered"
        assert event.latest_response_status == 201
        assert event.latest_response_body == "abcde"
        assert event.latest_error is None
        assert event.attempts[0].succeeded is True
        assert event.attempts[0].duration_ms >= 0


def test_non_2xx_schedules_retry(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    event_id = make_event(session_factory)
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="not ready"))
    with session_factory() as session:
        event = session.get(Event, event_id)
        deliver_event(session, event, settings, transport=transport)
        assert event.status == "retrying"
        assert event.next_retry_at is not None
        assert event.latest_error == "destination returned HTTP 503"


def test_network_failure_is_recorded(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    event_id = make_event(session_factory)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with session_factory() as session:
        event = session.get(Event, event_id)
        deliver_event(session, event, settings, transport=httpx.MockTransport(fail))
        assert event.status == "retrying"
        assert event.latest_response_status is None
        assert "connection refused" in event.latest_error
        assert event.attempts[0].succeeded is False


def test_retry_exhaustion(session_factory: sessionmaker[Session], settings: Settings) -> None:
    settings.max_retries = 1
    event_id = make_event(session_factory)
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with session_factory() as session:
        event = session.get(Event, event_id)
        deliver_event(session, event, settings, transport=transport)
        assert event.status == "retrying"
        deliver_event(session, event, settings, automated_retry=True, transport=transport)
        assert event.status == "failed"
        assert event.retry_count == 1
        assert event.next_retry_at is None
        assert len(event.attempts) == 2


def test_unsafe_destination_fails_at_delivery_time(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    event_id = make_event(session_factory, destination="http://127.0.0.1/admin")
    with session_factory() as session:
        event = session.get(Event, event_id)
        deliver_event(session, event, settings)
        assert event.status == "retrying"
        assert "disabled" in event.latest_error


def test_manual_replay_retransforms_and_is_labeled(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    event_id = make_event(
        session_factory,
        rules=[{"op": "add", "path": "current", "value": True}],
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    with session_factory() as session:
        event = session.get(Event, event_id)
        event.transformed_payload = {"stale": True}
        event.retry_count = 3
        deliver_event(session, event, settings, manual=True, transport=transport)
        assert event.transformed_payload == {"id": 1, "current": True}
        assert event.retry_count == 0
        assert event.attempts[0].is_manual_replay is True


def test_demo_seed_is_idempotent_and_delivery_is_internal(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as session:
        first = ensure_demo_endpoint(session)
        second = ensure_demo_endpoint(session)
        assert first.id == second.id
        event = Event(
            endpoint_id=first.id,
            request_headers={},
            original_payload={"type": "x", "customer": {"email": "a"}},
            transformed_payload={"event_type": "x"},
            status="pending",
        )
        session.add(event)
        session.commit()
        deliver_event(session, event, settings)
        assert event.status == "delivered"
        assert event.latest_response_status == 202


def test_due_retry_processor(session_factory: sessionmaker[Session], settings: Settings) -> None:
    with session_factory() as session:
        endpoint = ensure_demo_endpoint(session)
        due = Event(
            endpoint_id=endpoint.id,
            request_headers={},
            original_payload={"id": 1},
            transformed_payload={"id": 1},
            status="retrying",
            next_retry_at=utcnow() - timedelta(seconds=1),
        )
        future = Event(
            endpoint_id=endpoint.id,
            request_headers={},
            original_payload={"id": 2},
            transformed_payload={"id": 2},
            status="retrying",
            next_retry_at=utcnow() + timedelta(hours=1),
        )
        session.add_all([due, future])
        session.commit()
        due_id = due.id
        future_id = future.id
    assert process_due_retries(session_factory, settings) == 1
    with session_factory() as session:
        assert session.get(Event, due_id).status == "delivered"
        assert session.get(Event, due_id).retry_count == 1
        assert session.get(Event, future_id).status == "retrying"
        assert session.scalar(select(DeliveryAttempt).where(DeliveryAttempt.event_id == due_id))
