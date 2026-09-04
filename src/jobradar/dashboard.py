"""The live dashboard: a FastAPI app over the same renderer the static build uses.

This module is only the web layer -- routing, status codes, response types.
Everything that turns data into a page lives in :mod:`jobradar.render`, which
imports no web framework, so the static export can render the identical page
with only the scraper's dependencies installed.

Used for local development (``jobradar serve``) and for any ASGI host. The
published site is built by ``jobradar export`` instead; see ``export.py`` for
why that is the better fit for a pipeline that updates once a day.

Attribution: Remote OK's API terms require a followed link back to the posting
and a credit naming Remote OK. Both are honoured in the rendered page; the
outbound links carry ``rel="noopener"`` only, deliberately not ``nofollow``.
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from . import queries
from .config import load_settings
from .db import ensure_schema, make_engine
from .render import _iso, _jsonable, render_page

settings = load_settings()
engine = make_engine(settings.database_url)

# The schema is created by the scraper. Only bootstrap it for a local SQLite
# file that may not exist yet -- against Postgres the dashboard may well be
# running as a read-only role, and a read path must never require DDL rights.
if settings.database_url.startswith("sqlite"):
    ensure_schema(engine)

app = FastAPI(title="jobradar", docs_url="/api/docs", redoc_url=None)


@app.get("/api/health")
def api_health() -> JSONResponse:
    """Machine-readable health. 200 when fresh, 503 when stale."""
    with engine.connect() as conn:
        data = queries.health(conn, settings.stale_after_hours)
    payload = {
        "state": data["state"],
        "detail": data["label"],
        "last_success_at": _iso(data["last_success_at"]),
        "last_success_age_hours": round(data["last_success_age_hours"], 2)
        if data["last_success_age_hours"] is not None
        else None,
        "consecutive_days": data["consecutive_days"],
        "stale_after_hours": data["stale_after_hours"],
    }
    return JSONResponse(payload, status_code=200 if data["state"] != "critical" else 503)


@app.get("/api/stats")
def api_stats() -> JSONResponse:
    with engine.connect() as conn:
        snap = queries.snapshot(conn, settings.stale_after_hours)
    return JSONResponse(_jsonable(snap))


@app.get("/api/jobs")
def api_jobs(
    limit: int = Query(50, ge=1, le=500),
    source: str | None = None,
    q: str | None = None,
) -> JSONResponse:
    with engine.connect() as conn:
        rows = queries.recent_jobs(conn, limit=limit, source=source, search=q)
    return JSONResponse(_jsonable({"count": len(rows), "jobs": rows}))


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    with engine.connect() as conn:
        snap = queries.snapshot(conn, settings.stale_after_hours)
        recent = queries.recent_jobs(conn, limit=25)
    return HTMLResponse(render_page(snap, recent))
