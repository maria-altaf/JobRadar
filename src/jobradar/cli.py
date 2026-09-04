"""Command line entry point.

``run`` is what the scheduler invokes. Its exit code is the alerting mechanism:
a non-zero exit fails the GitHub Actions job, which is what sends the failure
email. A run therefore exits non-zero when its health checks fail, and zero when
it merely degraded (some tasks failed but the data is still sound).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import timedelta

from sqlalchemy import func, select

from .config import load_settings
from .db import (
    DEFAULT_LOCK_TTL,
    LockNotAcquired,
    ensure_schema,
    jobs,
    make_engine,
    quarantine,
    run_lock,
    runs,
    utcnow,
)
from .pipeline import LOCK_NAME, execute_run


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else os.getenv("JOBRADAR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # httpx logs every request at INFO; ours already covers that.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def render_summary(report) -> str:
    """A Markdown run summary, shown in the GitHub Actions job page."""
    icon = {"succeeded": "✅", "partial": "⚠️", "failed": "❌"}.get(report.status, "•")
    lines = [
        f"## {icon} jobradar run `{report.run_id[:8]}` — **{report.status}**",
        "",
        "| | |",
        "|---|---|",
        f"| Duration | {report.duration_seconds:.1f}s |",
        f"| Resumed | {'yes' if report.resumed else 'no'} |",
        f"| Tasks | {report.tasks_done} done, {report.tasks_failed} failed of {report.tasks_total} |",
        f"| Records seen | {report.items_seen} |",
        f"| New | {report.items_inserted} |",
        f"| Updated | {report.items_updated} |",
        f"| Unchanged | {report.items_unchanged} |",
        f"| Quarantined | {report.items_quarantined} |",
        f"| HTTP requests | {report.requests_made} ({report.retries_made} retried) |",
        "",
        "### Health checks",
        "",
        "| Check | Result | Value | Detail |",
        "|---|---|---|---|",
    ]
    for check in report.health.get("checks", []):
        mark = "✅" if check["ok"] else "❌"
        lines.append(
            f"| `{check['name']}` | {mark} | {check.get('value')} | {check.get('detail', '')} |"
        )
    failed_tasks = report.health.get("failed_tasks") or []
    if failed_tasks:
        lines += ["", "### Failed tasks", ""]
        for t in failed_tasks:
            lines.append(f"- `{t['task']}` — {t['error']}")
    return "\n".join(lines)


def write_step_summary(text: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("could not write step summary: %s", exc)


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.sources:
        object.__setattr__(settings, "enabled_sources", tuple(args.sources))
    engine = make_engine(settings.database_url)
    ensure_schema(engine)

    log = logging.getLogger("jobradar")
    log.info("database: %s", _redact(settings.database_url))
    log.info("sources: %s", ", ".join(settings.enabled_sources))

    holder = f"cli/{os.getpid()}"
    try:
        with run_lock(engine, LOCK_NAME, holder, ttl_seconds=args.lock_ttl):
            report = asyncio.run(
                execute_run(
                    engine,
                    settings,
                    trigger=args.trigger,
                    allow_resume=not args.no_resume,
                )
            )
    except LockNotAcquired as exc:
        # Another run is genuinely in flight. Not an error worth alerting on:
        # the scheduler simply overlapped with a slow run.
        log.warning("skipping: %s", exc)
        return 0

    summary = render_summary(report)
    print("\n" + summary)
    write_step_summary(summary)

    if args.json:
        print(json.dumps({"run_id": report.run_id, "status": report.status,
                          "health": report.health}, default=str, indent=2))

    if report.status == "failed":
        log.error("run FAILED health checks: %s", ", ".join(report.health["failed_checks"]))
        return 1
    if report.status == "partial":
        log.warning("run completed with %d failed task(s)", report.tasks_failed)
    return 0


def cmd_initdb(args: argparse.Namespace) -> int:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    print(f"schema ready on {_redact(settings.database_url)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    ensure_schema(engine)
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(jobs)).scalar_one()
        quarantined = conn.execute(select(func.count()).select_from(quarantine)).scalar_one()
        last_ok = conn.execute(
            select(runs)
            .where(runs.c.status.in_(["succeeded", "partial"]))
            .order_by(runs.c.finished_at.desc())
            .limit(1)
        ).mappings().first()
        recent = conn.execute(
            select(runs).order_by(runs.c.started_at.desc()).limit(args.limit)
        ).mappings().all()

    print(f"jobs stored     : {total}")
    print(f"quarantined     : {quarantined}")
    if last_ok:
        age = utcnow() - last_ok["finished_at"]
        fresh = age < timedelta(hours=settings.stale_after_hours)
        print(
            f"last good run   : {last_ok['finished_at']:%Y-%m-%d %H:%M UTC} "
            f"({_humanise(age)} ago) {'OK' if fresh else 'STALE'}"
        )
    else:
        print("last good run   : never")
    print("\nrecent runs:")
    for r in recent:
        print(
            f"  {r['started_at']:%Y-%m-%d %H:%M} {r['status']:<10} "
            f"tasks {r['tasks_done']}/{r['tasks_total']} "
            f"new {r['items_inserted']:<5} quarantined {r['items_quarantined']:<4} "
            f"{(r['duration_seconds'] or 0):.0f}s"
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Render the dashboard to a directory of static files."""
    from .export import export_site

    settings = load_settings()
    engine = make_engine(settings.database_url)
    # Deliberately no ensure_schema(): exporting only reads. The build that
    # renders this site runs with a read-only database role, so issuing DDL
    # here would fail -- and a read path that demands CREATE rights is a bug
    # regardless of who happens to be running it.
    if settings.database_url.startswith("sqlite"):
        ensure_schema(engine)

    summary = export_site(engine, settings, args.out)
    print(
        f"exported {len(summary['files'])} file(s) "
        f"({summary['bytes'] / 1024:.1f} KB) to {summary['out_dir']} "
        f"— {summary['jobs']} postings, health: {summary['health']}"
    )
    # A red dashboard is the health indicator doing its job, so an unhealthy
    # state is emphatically *not* a reason to withhold the page -- showing that
    # the pipeline has stopped is the single most useful thing it can do.
    #
    # The only genuinely useless page is one with no data behind it at all,
    # which is what exporting from an empty database produces.
    if summary["jobs"] == 0 and not args.allow_empty:
        logging.getLogger("jobradar").error(
            "refusing to publish: the database holds no postings, so the page would "
            "say nothing. Pass --allow-empty to override."
        )
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("jobradar.dashboard:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _humanise(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def _redact(url: str) -> str:
    """Never print credentials, including into CI logs."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.partition("@")
    user = creds.split(":")[0]
    return f"{scheme}://{user}:***@{host}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobradar", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="execute a scrape run (resuming one if open)")
    r.add_argument("--trigger", default=os.getenv("JOBRADAR_TRIGGER", "manual"))
    r.add_argument(
        "--no-resume",
        action="store_true",
        help="start a fresh run instead of continuing an unfinished one",
    )
    r.add_argument("--sources", nargs="*", help="limit to these sources")
    r.add_argument(
        "--lock-ttl",
        type=int,
        default=DEFAULT_LOCK_TTL,
        help="seconds before an unrenewed run lock lapses and can be reclaimed",
    )
    r.add_argument("--json", action="store_true", help="also print the health JSON")
    r.set_defaults(func=cmd_run)

    i = sub.add_parser("initdb", help="create tables if they do not exist")
    i.set_defaults(func=cmd_initdb)

    s = sub.add_parser("status", help="print pipeline health")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_status)

    e = sub.add_parser("export", help="render the dashboard to a static site")
    e.add_argument("--out", default="dist", help="output directory (default: dist)")
    e.add_argument(
        "--allow-empty",
        action="store_true",
        help="export even when there are no postings to show",
    )
    e.set_defaults(func=cmd_export)

    v = sub.add_parser("serve", help="run the dashboard locally")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--reload", action="store_true")
    v.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
