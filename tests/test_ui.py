from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Endpoint, Event, utcnow


def test_dashboard_and_navigation(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "See what happens" in response.text
    assert "Send sample event" in response.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_demo_flow_from_ui(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    response = client.post("/demo/send", follow_redirects=True)
    assert response.status_code == 200
    assert "Demo complete" in response.text
    assert "payment.completed" in response.text
    assert "hookrelay-demo" in response.text
    with session_factory() as session:
        event = session.scalar(select(Event).order_by(Event.id.desc()))
        assert event.status == "delivered"
        assert len(event.attempts) == 1


def test_endpoint_and_event_pages(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        demo = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        demo_id = demo.id
    endpoints = client.get("/endpoints")
    assert endpoints.status_code == 200
    assert "Payment demo" in endpoints.text
    detail = client.get(f"/endpoints/{demo_id}")
    assert detail.status_code == 200
    assert "http://testserver/hooks/demo" in detail.text
    assert client.get("/endpoints/9999").status_code == 404
    assert client.get("/events").status_code == 200
    assert client.get("/events/9999").status_code == 404


def test_create_endpoint_form_and_toggle(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    response = client.post(
        "/endpoints",
        data={
            "name": "Form endpoint",
            "destination_url": "https://93.184.216.34/hook",
            "signing_secret": "long-form-secret-value",
            "transformation_rules": '[{"op":"add","path":"ui","value":true}]',
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as session:
        endpoint = session.scalar(select(Endpoint).where(Endpoint.name == "Form endpoint"))
        endpoint_id = endpoint.id
        assert endpoint.signing_secret == "long-form-secret-value"
    assert (
        client.post(f"/endpoints/{endpoint_id}/toggle", follow_redirects=False).status_code == 303
    )
    with session_factory() as session:
        assert session.get(Endpoint, endpoint_id).enabled is False
    assert client.post("/endpoints/9999/toggle").status_code == 404


def test_endpoint_form_validation(client: TestClient) -> None:
    response = client.post(
        "/endpoints",
        data={
            "name": "Bad",
            "destination_url": "http://localhost:9000/hook",
            "signing_secret": "short",
            "transformation_rules": "not-json",
        },
    )
    assert response.status_code == 422
    assert "Expecting value" in response.text
    blank = client.post(
        "/endpoints",
        data={
            "name": "   ",
            "destination_url": "https://93.184.216.34/hook",
            "transformation_rules": "[]",
        },
    )
    assert blank.status_code == 422
    assert "must not be blank" in blank.text


def test_event_filter_and_replay_form(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    client.post("/demo/send")
    with session_factory() as session:
        event = session.scalar(select(Event).order_by(Event.id.desc()))
        event_id = event.id
        endpoint_id = event.endpoint_id
    listing = client.get(f"/events?endpoint_id={endpoint_id}&status=delivered")
    assert listing.status_code == 200
    assert f"EVT-{event_id:05d}" in listing.text
    replay = client.post(f"/events/{event_id}/replay", follow_redirects=True)
    assert replay.status_code == 200
    assert "Replay queued" in replay.text
    with session_factory() as session:
        assert len(session.get(Event, event_id).attempts) == 2
        assert session.get(Event, event_id).attempts[-1].is_manual_replay
    assert client.post("/events/9999/replay").status_code == 404


def test_ui_replay_resets_pending_retry_state(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.deliver_event_by_id", lambda *args, **kwargs: None)
    with session_factory() as session:
        endpoint = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        endpoint.transformation_rules = [{"op": "add", "path": "replayed", "value": True}]
        event = Event(
            endpoint_id=endpoint.id,
            request_headers={},
            original_payload={"id": 11},
            transformed_payload={"stale": True},
            status="retrying",
            next_retry_at=utcnow() + timedelta(hours=1),
        )
        session.add(event)
        session.commit()
        event_id = event.id

    response = client.post(f"/events/{event_id}/replay", follow_redirects=False)

    assert response.status_code == 303
    with session_factory() as session:
        replayed = session.get(Event, event_id)
        assert replayed.status == "pending"
        assert replayed.next_retry_at is None
        assert replayed.transformed_payload == {"id": 11, "replayed": True}


def test_disabled_event_cannot_be_replayed(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        demo = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        event = Event(
            endpoint=demo,
            request_headers={},
            original_payload={},
            transformed_payload={},
            status="failed",
        )
        demo.enabled = False
        session.add(event)
        session.commit()
        event_id = event.id
    assert client.post(f"/events/{event_id}/replay").status_code == 409


def test_rejected_signature_cannot_be_replayed(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        demo = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        event = Event(
            endpoint=demo,
            request_headers={},
            original_payload={},
            transformed_payload=None,
            signature_valid=False,
            status="rejected",
        )
        session.add(event)
        session.commit()
        event_id = event.id
    detail = client.get(f"/events/{event_id}")
    assert "Rejected signatures cannot be replayed" in detail.text
    assert client.post(f"/events/{event_id}/replay").status_code == 409


def test_public_demo_hides_ui_management(settings, session_factory) -> None:
    from app.main import create_app

    settings.public_demo_mode = True
    app = create_app(
        settings,
        session_factory=session_factory,
        database_engine=session_factory.kw["bind"],
    )
    with TestClient(app) as demo_client:
        assert "New endpoint" not in demo_client.get("/endpoints").text
        assert (
            demo_client.post(
                "/endpoints",
                data={"name": "x", "destination_url": "https://93.184.216.34/x"},
            ).status_code
            == 403
        )
        demo = next(item for item in demo_client.get("/api/endpoints").json() if item["is_demo"])
        assert demo_client.post(f"/endpoints/{demo['id']}/toggle").status_code == 403
