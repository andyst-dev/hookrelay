# HookRelay repository guide

## Project

HookRelay is a compact Python 3.12+ webhook gateway. FastAPI serves the REST API,
public hook receiver, and Jinja UI; SQLAlchemy persists to SQLite by default; httpx
delivers outbound events. Keep the architecture local and avoid queue, frontend, or
authentication frameworks unless a future requirement genuinely needs them.

## Map

- `app/main.py`: application factory, lifespan/retry-worker startup, static/templates,
  and server-rendered UI routes.
- `app/api.py`: typed management API, public `/hooks/{endpoint_key}` ingestion, replay,
  filtering, validation, and background-delivery scheduling.
- `app/models.py`, `database.py`, `schemas.py`, `config.py`: persisted domain, database
  setup, API contracts, and environment settings.
- `app/services.py`: shared endpoint/replay operations, demo seed/receiver, outbound
  delivery, attempt recording, retry scheduling, and due-retry processing.
- `app/security.py`, `transformations.py`, `rate_limit.py`: SSRF/HMAC/header controls,
  deterministic JSON rules, and the in-memory endpoint limiter.
- `app/templates/` and `app/static/`: Jinja pages plus dependency-free CSS/JavaScript.
- `tests/`: behavior-oriented API, security, service, transformation, UI, and config
  coverage. Shared app/database fixtures live in `tests/conftest.py`.
- `scripts/`: local mock destination and synthetic webhook sender.
- `Dockerfile`, `render.yaml`, `.github/workflows/ci.yml`: release and deployment paths.
- `README.md`: detailed product behavior, examples, security model, and operator docs;
  search its relevant section instead of loading it wholesale for ordinary changes.

## Data flow and invariants

The receive path is hook lookup → rate/content/JSON/HMAC checks → event persistence →
deterministic transformation → background delivery → attempt/retry persistence. Manual
replay keeps the original payload and reapplies current rules.

- HMAC signs the raw body and uses constant-time comparison. Rejected signatures must
  never be forwarded or replayed, and stored secrets must never enter responses or logs.
- Validate destinations on configuration and again immediately before delivery. Preserve
  private/reserved-address blocking, disabled redirects, and the explicit local-dev escape
  hatch; document rather than overstate DNS-level SSRF guarantees.
- Keep request/response capture bounded and streamed, sensitive headers redacted, retry
  counts capped, and destination timeouts explicit.
- The internal `demo://receiver` transport is valid only for the seeded demo endpoint.
  `PUBLIC_DEMO_MODE` must continue to block endpoint-configuration mutations.
- Keep transformations deterministic and data-only; never execute user-supplied code.
- Preserve Jinja autoescaping, accessible controls, responsive layouts, and dependency-free
  frontend behavior.

## Working rules

Identify the owning module above, then search symbols and read its immediate dependencies
and relevant tests before expanding scope. Search for existing behavior before adding a
parallel path. Prefer the standard library and installed stack; add dependencies only with
a demonstrated need. Split code by cohesive responsibility, not file length, and avoid
forwarding wrappers, catch-all helpers, speculative abstractions, and unrelated cleanup.

Make the smallest correct change while preserving security, validation, data safety,
accessibility, and documented behavior. Start with focused tests, then broaden verification
for shared or security-sensitive behavior. Update this nearest map only when an important
path, ownership boundary, command, or dependency flow changes.

## Verified commands

```bash
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
./.venv/bin/pytest -q
docker build -t hookrelay:local .
./.venv/bin/uvicorn app.main:app --reload
```

The full pytest command enforces 90% branch coverage. For a targeted change, run the
smallest relevant test module first, then the full suite before release-facing handoff.
