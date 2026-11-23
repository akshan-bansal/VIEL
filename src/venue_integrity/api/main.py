"""FastAPI application factory.

    uvicorn venue_integrity.api.main:create_app --factory

Environment:
    VIA_MODE                    replay | simulation (anything else refuses to start)
    VIA_API_KEYS                JSON list of {key, principal, roles}
    VIA_RAW_STORE_PATH          SQLite raw store (default: in-memory)
    VIA_RATE_LIMIT_PER_MINUTE   per-principal request budget (default 600)
    VIA_BOOTSTRAP               "simulation" to preload the synthetic scenario
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from venue_integrity import __version__
from venue_integrity.api.monitoring import Metrics
from venue_integrity.api.routes import router
from venue_integrity.api.security import KeyStore, RateLimiter
from venue_integrity.api.service import AuditService
from venue_integrity.config import assert_supported_mode


def create_app(
    *,
    keys: KeyStore | None = None,
    service: AuditService | None = None,
    rate_limit_per_minute: int | None = None,
) -> FastAPI:
    assert_supported_mode(os.environ.get("VIA_MODE", "replay"))
    app = FastAPI(title="Venue Integrity Audit", version=__version__,
                  description="Replay-mode audit API. It has no order-entry capability.")
    app.state.keys = keys or KeyStore.from_env()
    app.state.rate_limiter = RateLimiter(rate_limit_per_minute or int(os.environ.get("VIA_RATE_LIMIT_PER_MINUTE", "600")))
    app.state.service = service or AuditService(os.environ.get("VIA_RAW_STORE_PATH", ":memory:"),
                                                bootstrap=os.environ.get("VIA_BOOTSTRAP"))
    app.state.metrics = Metrics()
    app.include_router(router)
    return app
