from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import DeliveryAttempt, Endpoint, Event, utcnow
from app.rate_limit import EndpointRateLimiter


def add_endpoint(
    session_factory: sessionmaker[Session],
    *,
    key: str = "test-key",
    enabled: bool = True,
    secret: str | None = None,
    rules: list[dict] | None = None,
) -> Endpoint:
    with session_factory() as session:
        endpoint = Endpoint(
            name="Test endpoint",
            public_key=key,
            destination_url="https://93.184.216.34/receive",
            enabled=enabled,
            signing_secret=secret,
            transformation_rules=rules or [],
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        return endpoint


def test_health_and_openapi(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok", "version": "1.0.0"}
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == "1.0.0"


def test_endpoint_crud_and_secret_non_disclosure(client: TestClient) -> None:
    response = client.post(
        "/api/endpoints",
        json={
            "name": "  Payments  ",
            "destination_url": "https://93.184.216.34/hook",
            "signing_secret": "this-is-a-long-secret",
            "transformation_rules": [{"op": "add", "path": "source", "value": "api"}],
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "Payments"
    assert created["signing_secret_configured"] is True
    assert "signing_secret" not in created

    endpoint_id = created["id"]
    assert any(item["id"] == endpoint_id for item in client.get("/api/endpoints").json())
    assert client.get(f"/api/endpoints/{endpoint_id}").json()["destination_url"].endswith("/hook")

    updated = client.patch(
        f"/api/endpoints/{endpoint_id}",
        json={"name": "New name", "enabled": False, "clear_signing_secret": True},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "New name"
    assert updated.json()["enabled"] is False
    assert updated.json()["signing_secret_configured"] is False
    assert client.delete(f"/api/endpoints/{endpoint_id}").status_code == 204
    assert client.get(f"/api/endpoints/{endpoint_id}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Unsafe", "destination_url": "http://127.0.0.1/hook"},
        {
            "name": "Bad rule",
            "destination_url": "https://93.184.216.34/hook",
            "transformation_rules": [{"op": "python"}],
        },
    ],
)
def test_endpoint_creation_validation(client: TestClient, body: dict) -> None:
    assert client.post("/api/endpoints", json=body).status_code == 422


def test_endpoint_not_found_and_demo_guards(client: TestClient) -> None:
    assert client.patch("/api/endpoints/9999", json={"enabled": False}).status_code == 404
    assert client.delete("/api/endpoints/9999").status_code == 404
    demo = next(item for item in client.get("/api/endpoints").json() if item["is_demo"])
    assert (
        client.patch(
            f"/api/endpoints/{demo['id']}", json={"destination_url": "https://93.184.216.34/x"}
        ).status_code
        == 409
    )
    assert client.delete(f"/api/endpoints/{demo['id']}").status_code == 409


def test_webhook_receipt_and_header_sanitization(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = add_endpoint(
        session_factory,
        rules=[{"op": "rename", "from": "type", "to": "event_type"}],
    )
    monkeypatch.setattr("app.api.deliver_event_by_id", lambda *args, **kwargs: None)
    response = client.post(
        f"/hooks/{endpoint.public_key}",
        json={"type": "created", "id": 7},
        headers={"Authorization": "Bearer no", "X-Webhook-Topic": "created"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        assert event.transformed_payload == {"event_type": "created", "id": 7}
        assert "authorization" not in event.request_headers
        assert event.request_headers["x-webhook-topic"] == "created"


def test_webhook_input_errors(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    endpoint = add_endpoint(session_factory)
    path = f"/hooks/{endpoint.public_key}"
    assert (
        client.post(path, content="{}", headers={"Content-Type": "text/plain"}).status_code == 415
    )
    assert (
        client.post(path, content="{", headers={"Content-Type": "application/json"}).status_code
        == 400
    )
    assert (
        client.post(
            path,
            content=b"x" * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert client.post("/hooks/missing", json={}).status_code == 404


def test_disabled_endpoint_is_gone(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    endpoint = add_endpoint(session_factory, enabled=False)
    assert client.post(f"/hooks/{endpoint.public_key}", json={}).status_code == 410


def test_valid_and_invalid_hmac(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "correct-horse-battery"
    endpoint = add_endpoint(session_factory, secret=secret)
    monkeypatch.setattr("app.api.deliver_event_by_id", lambda *args, **kwargs: None)
    body = b'{"amount":4900}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    valid = client.post(
        f"/hooks/{endpoint.public_key}",
        content=body,
        headers={"Content-Type": "application/json", "X-HookRelay-Signature": f"sha256={digest}"},
    )
    assert valid.status_code == 202
    invalid = client.post(
        f"/hooks/{endpoint.public_key}",
        content=body,
        headers={"Content-Type": "application/json", "X-HookRelay-Signature": "sha256=" + "0" * 64},
    )
    assert invalid.status_code == 401
    assert invalid.json()["status"] == "rejected"
    with session_factory() as session:
        rejected = session.get(Event, invalid.json()["event_id"])
        assert rejected.signature_valid is False
        assert rejected.attempts == []
    assert client.post(f"/api/events/{invalid.json()['event_id']}/replay").status_code == 409


def test_transformation_failure_is_persisted(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    endpoint = add_endpoint(session_factory, rules=[{"op": "remove", "path": "required"}])
    response = client.post(f"/hooks/{endpoint.public_key}", json={"other": True})
    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]


def test_rate_limit_returns_retry_after(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = add_endpoint(session_factory)
    monkeypatch.setattr("app.api.deliver_event_by_id", lambda *args, **kwargs: None)
    client.app.state.rate_limiter = EndpointRateLimiter(1)
    assert client.post(f"/hooks/{endpoint.public_key}", json={}).status_code == 202
    response = client.post(f"/hooks/{endpoint.public_key}", json={})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_event_history_filters_and_details(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    endpoint = add_endpoint(session_factory)
    with session_factory() as session:
        old = Event(
            endpoint_id=endpoint.id,
            original_payload={"id": 1},
            transformed_payload={"id": 1},
            request_headers={},
            signature_valid=True,
            status="failed",
            received_at=utcnow() - timedelta(days=1),
        )
        recent = Event(
            endpoint_id=endpoint.id,
            original_payload={"id": 2},
            transformed_payload={"id": 2},
            request_headers={},
            signature_valid=False,
            status="rejected",
        )
        session.add_all([old, recent])
        session.commit()
        recent_id = recent.id
    assert len(client.get(f"/api/events?endpoint_id={endpoint.id}").json()) == 2
    assert len(client.get("/api/events?status=failed").json()) == 1
    assert len(client.get("/api/events?signature_valid=false").json()) == 1
    assert len(client.get(f"/api/events?received_after={utcnow().date().isoformat()}").json()) == 1
    assert client.get(f"/api/events/{recent_id}").json()["status"] == "rejected"
    assert client.get("/api/events/99999").status_code == 404


def test_manual_replay_uses_current_rules(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        endpoint = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        endpoint.transformation_rules = [{"op": "add", "path": "replayed", "value": True}]
        event = Event(
            endpoint_id=endpoint.id,
            request_headers={},
            original_payload={"id": 9},
            transformed_payload={"id": 9},
            status="failed",
        )
        session.add(event)
        session.commit()
        event_id = event.id
    response = client.post(f"/api/events/{event_id}/replay")
    assert response.status_code == 202
    with session_factory() as session:
        replayed = session.get(Event, event_id)
        assert replayed.status == "delivered"
        assert replayed.transformed_payload == {"id": 9, "replayed": True}
        attempt = session.scalar(
            select(DeliveryAttempt).where(DeliveryAttempt.event_id == event_id)
        )
        assert attempt.is_manual_replay is True
    assert client.post("/api/events/9999/replay").status_code == 404


def test_api_replay_resets_pending_retry_state(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.deliver_event_by_id", lambda *args, **kwargs: None)
    with session_factory() as session:
        endpoint = session.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        endpoint.transformation_rules = [{"op": "add", "path": "replayed", "value": True}]
        event = Event(
            endpoint_id=endpoint.id,
            request_headers={},
            original_payload={"id": 10},
            transformed_payload={"stale": True},
            status="retrying",
            next_retry_at=utcnow() + timedelta(hours=1),
        )
        session.add(event)
        session.commit()
        event_id = event.id

    response = client.post(f"/api/events/{event_id}/replay")

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["next_retry_at"] is None
    with session_factory() as session:
        replayed = session.get(Event, event_id)
        assert replayed.status == "pending"
        assert replayed.next_retry_at is None
        assert replayed.transformed_payload == {"id": 10, "replayed": True}


def test_public_demo_mode_blocks_management(settings, session_factory) -> None:
    from app.main import create_app

    settings.public_demo_mode = True
    app = create_app(
        settings,
        session_factory=session_factory,
        database_engine=session_factory.kw["bind"],
    )
    with TestClient(app) as demo_client:
        assert (
            demo_client.post(
                "/api/endpoints",
                json={"name": "No", "destination_url": "https://93.184.216.34/x"},
            ).status_code
            == 403
        )
        demo = next(item for item in demo_client.get("/api/endpoints").json() if item["is_demo"])
        assert (
            demo_client.patch(f"/api/endpoints/{demo['id']}", json={"enabled": False}).status_code
            == 403
        )
        assert demo_client.delete(f"/api/endpoints/{demo['id']}").status_code == 403
