from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app import __version__
from app.api import hooks_router, router
from app.config import Settings, get_settings
from app.database import Base, SessionLocal, engine, get_db
from app.models import Endpoint, Event, utcnow
from app.rate_limit import EndpointRateLimiter
from app.security import UnsafeDestinationError
from app.services import (
    DEMO_PAYLOAD,
    create_endpoint_record,
    deliver_event_by_id,
    ensure_demo_endpoint,
    prepare_event_replay,
    retry_worker,
)
from app.transformations import TransformationError, transform_payload

APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


templates.env.filters["prettyjson"] = _json


def create_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    database_engine=None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_factory = session_factory or SessionLocal
    active_engine = database_engine or engine

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        Base.metadata.create_all(active_engine)
        with active_factory() as session:
            ensure_demo_endpoint(session)
        worker: asyncio.Task[None] | None = None
        if active_settings.enable_retry_worker:
            worker = asyncio.create_task(retry_worker(active_factory, active_settings))
        yield
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(
        title="HookRelay",
        description="Receive, inspect, transform and replay webhooks without the plumbing.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.session_factory = active_factory
    app.state.rate_limiter = EndpointRateLimiter(active_settings.rate_limit_per_minute)
    app.state.settings = active_settings
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(hooks_router)

    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: active_settings

        def override_db():
            with active_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_db

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: Session = Depends(get_db)):
        counts = {
            "endpoints": db.scalar(select(func.count(Endpoint.id))) or 0,
            "events": db.scalar(select(func.count(Event.id))) or 0,
            "delivered": db.scalar(select(func.count(Event.id)).where(Event.status == "delivered"))
            or 0,
            "attention": db.scalar(
                select(func.count(Event.id)).where(
                    Event.status.in_(["failed", "retrying", "rejected"])
                )
            )
            or 0,
        }
        recent = db.scalars(
            select(Event)
            .options(selectinload(Event.endpoint))
            .order_by(Event.received_at.desc())
            .limit(8)
        ).all()
        demo = db.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "recent": recent,
                "demo": demo,
                "sample": _json(DEMO_PAYLOAD),
                "settings": active_settings,
            },
        )

    @app.get("/endpoints", response_class=HTMLResponse)
    def endpoints_page(request: Request, db: Session = Depends(get_db)):
        endpoints = db.scalars(select(Endpoint).order_by(Endpoint.created_at.desc())).all()
        return templates.TemplateResponse(
            request,
            "endpoints.html",
            {"endpoints": endpoints, "settings": active_settings},
        )

    @app.post("/endpoints")
    def create_endpoint_form(
        request: Request,
        name: str = Form(),
        destination_url: str = Form(),
        signing_secret: str = Form(default=""),
        transformation_rules: str = Form(default="[]"),
        db: Session = Depends(get_db),
    ):
        try:
            rules = json.loads(transformation_rules)
            if not isinstance(rules, list):
                raise TransformationError("transformation rules must be a JSON array")
            endpoint = create_endpoint_record(
                db,
                active_settings,
                name=name,
                destination_url=destination_url,
                signing_secret=signing_secret,
                transformation_rules=rules,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=403, detail="editing is disabled in public demo mode"
            ) from exc
        except (
            json.JSONDecodeError,
            TransformationError,
            UnsafeDestinationError,
            ValueError,
        ) as exc:
            endpoints = db.scalars(select(Endpoint).order_by(Endpoint.created_at.desc())).all()
            return templates.TemplateResponse(
                request,
                "endpoints.html",
                {"endpoints": endpoints, "settings": active_settings, "form_error": str(exc)},
                status_code=422,
            )
        return RedirectResponse(f"/endpoints/{endpoint.id}", status_code=303)

    @app.get("/endpoints/{endpoint_id}", response_class=HTMLResponse)
    def endpoint_page(endpoint_id: int, request: Request, db: Session = Depends(get_db)):
        endpoint = db.get(Endpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="endpoint not found")
        recent = db.scalars(
            select(Event)
            .where(Event.endpoint_id == endpoint.id)
            .order_by(Event.received_at.desc())
            .limit(10)
        ).all()
        hook_url = f"{active_settings.public_base_url}/hooks/{endpoint.public_key}"
        return templates.TemplateResponse(
            request,
            "endpoint_detail.html",
            {
                "endpoint": endpoint,
                "recent": recent,
                "hook_url": hook_url,
                "curl_command": (
                    f"curl -X POST '{hook_url}' -H 'Content-Type: application/json' "
                    f"-d '{json.dumps(DEMO_PAYLOAD, separators=(',', ':'))}'"
                ),
                "settings": active_settings,
            },
        )

    @app.post("/endpoints/{endpoint_id}/toggle")
    def toggle_endpoint(endpoint_id: int, db: Session = Depends(get_db)):
        if active_settings.public_demo_mode:
            raise HTTPException(status_code=403, detail="editing is disabled in public demo mode")
        endpoint = db.get(Endpoint, endpoint_id)
        if not endpoint:
            raise HTTPException(status_code=404, detail="endpoint not found")
        endpoint.enabled = not endpoint.enabled
        endpoint.updated_at = utcnow()
        db.commit()
        return RedirectResponse(f"/endpoints/{endpoint_id}", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    def events_page(
        request: Request,
        endpoint_id: int | None = None,
        status: str | None = None,
        db: Session = Depends(get_db),
    ):
        statement = (
            select(Event)
            .options(selectinload(Event.endpoint))
            .order_by(Event.received_at.desc())
            .limit(200)
        )
        if endpoint_id:
            statement = statement.where(Event.endpoint_id == endpoint_id)
        if status:
            statement = statement.where(Event.status == status)
        return templates.TemplateResponse(
            request,
            "events.html",
            {
                "events": db.scalars(statement).all(),
                "endpoints": db.scalars(select(Endpoint).order_by(Endpoint.name)).all(),
                "selected_endpoint": endpoint_id,
                "selected_status": status,
            },
        )

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_page(event_id: int, request: Request, db: Session = Depends(get_db)):
        event = db.scalar(
            select(Event)
            .options(selectinload(Event.endpoint), selectinload(Event.attempts))
            .where(Event.id == event_id)
        )
        if not event:
            raise HTTPException(status_code=404, detail="event not found")
        return templates.TemplateResponse(request, "event_detail.html", {"event": event})

    @app.post("/events/{event_id}/replay")
    def replay_form(
        event_id: int,
        request: Request,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
    ):
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
            active_settings,
            manual=True,
        )
        return RedirectResponse(f"/events/{event.id}?replayed=1", status_code=303)

    @app.post("/demo/send")
    def send_demo(
        request: Request,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
    ):
        endpoint = db.scalar(select(Endpoint).where(Endpoint.is_demo.is_(True)))
        if not endpoint:
            raise HTTPException(status_code=503, detail="demo endpoint is unavailable")
        event = Event(
            endpoint_id=endpoint.id,
            request_headers={"content-type": "application/json", "x-request-id": "demo-ui"},
            original_payload=DEMO_PAYLOAD,
            transformed_payload=transform_payload(DEMO_PAYLOAD, endpoint.transformation_rules),
            status="pending",
        )
        db.add(event)
        db.commit()
        background_tasks.add_task(
            deliver_event_by_id,
            request.app.state.session_factory,
            event.id,
            active_settings,
        )
        return RedirectResponse(f"/events/{event.id}?demo=1", status_code=303)

    return app


app = create_app()
