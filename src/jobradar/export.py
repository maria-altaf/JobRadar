"""Render the dashboard to a static site.

Netlify has no Python runtime -- its Functions are JS/TS/Go only -- so the
dashboard cannot be served there as a live app. It is exported instead: after
each scrape, the same ``render_page`` used by the live server writes an
``index.html`` plus JSON files, and that directory is published.

For this pipeline that is a better fit than a server, not a worse one:

* the data changes exactly once a day, so a request-time database query would
  return the same bytes to every visitor until the next run;
* the published site holds no credentials at all -- the database URL never
  leaves GitHub Actions, so the public web tier has nothing worth stealing;
* nothing to keep warm, nothing to cold-start, and the page cannot break
  because the database is briefly unreachable.

The one thing a static build loses is *freshness of the page itself*, and that
matters here because the whole point is a health indicator. Ages are therefore
recomputed in the browser from ISO timestamps, and the banner re-derives its
own state against the staleness threshold -- see the script in ``dashboard.py``.
A page built by a run that later stopped happening turns red on its own.
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine

from . import queries
from .config import Settings

# From render, not dashboard: the exporter must not drag a web framework in.
# The static build installs only the scraper's dependencies, and importing the
# FastAPI app here is what made `jobradar export` die on Netlify with
# ModuleNotFoundError: No module named 'fastapi'.
from .render import _jsonable, render_page

log = logging.getLogger(__name__)

#: How many postings to embed in the page and in jobs.json. The page shows the
#: most recent slice; the full table lives in the database.
PAGE_JOBS = 30
API_JOBS = 500


def export_site(engine: Engine, settings: Settings, out_dir: pathlib.Path) -> dict[str, Any]:
    """Write the whole site to ``out_dir``. Returns a summary of what was written."""
    out_dir = pathlib.Path(out_dir)
    (out_dir / "api").mkdir(parents=True, exist_ok=True)

    with engine.connect() as conn:
        snap = queries.snapshot(conn, settings.stale_after_hours)
        page_jobs = queries.recent_jobs(conn, limit=PAGE_JOBS)
        api_jobs = queries.recent_jobs(conn, limit=API_JOBS)

    written: list[tuple[str, int]] = []

    def write(relative: str, text: str) -> None:
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append((relative, len(text.encode("utf-8"))))

    write("index.html", render_page(snap, page_jobs))

    health = snap["health"]
    write(
        "api/health.json",
        json.dumps(
            {
                "state": health["state"],
                "detail": health["label"],
                "last_success_at": _iso(health["last_success_at"]),
                "last_success_age_hours": round(health["last_success_age_hours"], 2)
                if health["last_success_age_hours"] is not None
                else None,
                "consecutive_days": health["consecutive_days"],
                "stale_after_hours": health["stale_after_hours"],
                # So a consumer can judge staleness itself rather than trusting
                # a value that was true when the file was written.
                "generated_at": _iso(snap["generated_at"]),
            },
            indent=2,
        ),
    )

    write("api/stats.json", json.dumps(_jsonable(snap), indent=2))
    write(
        "api/jobs.json",
        json.dumps(
            _jsonable(
                {
                    "count": len(api_jobs),
                    "note": (
                        "Descriptions are excerpted here to keep this file small; "
                        "follow each posting's url for the full text."
                    ),
                    "jobs": [_slim(job) for job in api_jobs],
                }
            ),
            indent=2,
        ),
    )

    # Extensionless paths for the JSON, matching the live server's routes, so
    # the same URLs work against either deployment.
    write(
        "_redirects",
        "\n".join(
            [
                "# Keep the live server's API paths working on the static build.",
                "/api/health  /api/health.json  200",
                "/api/stats   /api/stats.json   200",
                "/api/jobs    /api/jobs.json    200",
                "",
            ]
        ),
    )

    write(
        "_headers",
        "\n".join(
            [
                "/*",
                "  X-Content-Type-Options: nosniff",
                "  Referrer-Policy: strict-origin-when-cross-origin",
                # Rebuilt after every run, so a short cache with revalidation
                # keeps a visitor from seeing yesterday's numbers.
                "  Cache-Control: public, max-age=300, must-revalidate",
                "/api/*",
                "  Access-Control-Allow-Origin: *",
                "  Cache-Control: public, max-age=300, must-revalidate",
                "",
            ]
        ),
    )

    total = sum(size for _, size in written)
    log.info("exported %d file(s), %.1f KB to %s", len(written), total / 1024, out_dir)
    for name, size in written:
        log.info("  %-22s %6.1f KB", name, size / 1024)

    return {
        "out_dir": str(out_dir),
        "files": [name for name, _ in written],
        "bytes": total,
        "jobs": snap["totals"]["jobs"],
        "health": health["state"],
    }


#: Descriptions run to 8 KB each, which turned a 500-posting export into a 2 MB
#: file that almost nobody downloads in full. An excerpt plus the canonical URL
#: carries the same value at 1% of the weight.
EXCERPT_CHARS = 280


def _slim(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if k != "description_text"}
    text = job.get("description_text")
    if isinstance(text, str) and text:
        out["description_excerpt"] = text[:EXCERPT_CHARS]
    return out


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value
