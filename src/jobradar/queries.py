"""Read-side aggregates for the dashboard.

Kept separate from the write path so the dashboard can be deployed and scaled
independently of the scraper, and so these can be tested without a pipeline run.

Every query is portable across SQLite and Postgres; the only dialect-sensitive
piece is day bucketing, which goes through :func:`jobradar.db.day_bucket`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.engine import Connection

from .db import day_bucket, jobs, quarantine, run_tasks, runs, utcnow

SOURCE_LABELS = {
    "hackernews": "Hacker News",
    "weworkremotely": "We Work Remotely",
    "remoteok": "Remote OK",
}

#: Fixed slot order, so a source keeps its colour even when a filter removes
#: another source from the chart.
SOURCE_ORDER = ("hackernews", "weworkremotely", "remoteok")


def _as_day(value: Any) -> str:
    """Normalise a bucketed day to ``YYYY-MM-DD`` on either dialect."""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def health(conn: Connection, stale_after_hours: int = 30) -> dict[str, Any]:
    """The health indicator: when did this last work, and is that recent enough?"""
    last_ok = conn.execute(
        select(runs)
        .where(runs.c.status.in_(["succeeded", "partial"]), runs.c.finished_at.isnot(None))
        .order_by(desc(runs.c.finished_at))
        .limit(1)
    ).mappings().first()

    last_any = conn.execute(
        select(runs).order_by(desc(runs.c.started_at)).limit(1)
    ).mappings().first()

    now = utcnow()
    age_hours = None
    if last_ok:
        age_hours = (now - last_ok["finished_at"]).total_seconds() / 3600.0

    if last_ok is None:
        state, label = "critical", "Never completed a run"
    elif age_hours is not None and age_hours > stale_after_hours:
        state, label = "critical", f"Stale — last success {_humanise(age_hours)} ago"
    elif last_any is not None and last_any["status"] == "failed":
        state, label = "warning", "Last run failed; previous data still fresh"
    elif last_ok["status"] == "partial":
        state, label = "warning", "Last run completed with failed tasks"
    else:
        state, label = "good", f"Healthy — last success {_humanise(age_hours or 0)} ago"

    # Consecutive days with at least one successful run, counting back from today.
    bucket = day_bucket(conn, runs.c.finished_at)
    ok_days = {
        _as_day(r[0])
        for r in conn.execute(
            select(bucket)
            .where(runs.c.status.in_(["succeeded", "partial"]))
            .group_by(bucket)
        ).all()
    }
    streak = 0
    cursor = now.date()
    # Today may not have run yet; that must not reset the streak.
    if cursor.strftime("%Y-%m-%d") not in ok_days:
        cursor -= timedelta(days=1)
    while cursor.strftime("%Y-%m-%d") in ok_days:
        streak += 1
        cursor -= timedelta(days=1)

    return {
        "state": state,
        "label": label,
        "last_success_at": last_ok["finished_at"] if last_ok else None,
        "last_success_age_hours": age_hours,
        "last_run": dict(last_any) if last_any else None,
        "consecutive_days": streak,
        "stale_after_hours": stale_after_hours,
    }


def _humanise(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


def totals(conn: Connection) -> dict[str, Any]:
    total = conn.execute(select(func.count()).select_from(jobs)).scalar_one()
    companies = conn.execute(
        select(func.count(func.distinct(jobs.c.company))).select_from(jobs)
    ).scalar_one()
    quarantined = conn.execute(select(func.count()).select_from(quarantine)).scalar_one()
    with_salary = conn.execute(
        select(func.count()).select_from(jobs).where(jobs.c.salary_min.isnot(None))
    ).scalar_one()

    # Distinct jobs after cross-source grouping, and how many groups span
    # more than one source -- the payoff of the fingerprint.
    distinct_groups = conn.execute(
        select(func.count(func.distinct(jobs.c.content_fingerprint))).select_from(jobs)
    ).scalar_one()

    multi = conn.execute(
        select(func.count()).select_from(
            select(jobs.c.content_fingerprint)
            .group_by(jobs.c.content_fingerprint)
            .having(func.count(func.distinct(jobs.c.source)) > 1)
            .subquery()
        )
    ).scalar_one()

    added_24h = conn.execute(
        select(func.count())
        .select_from(jobs)
        .where(jobs.c.first_seen_at >= utcnow() - timedelta(hours=24))
    ).scalar_one()

    # Quarantine accumulates one row per run, so the all-time total grows
    # without bound and reads as alarming next to a live posting count. The
    # latest run's figure is the one that means something.
    latest_run = conn.execute(
        select(runs.c.id).order_by(desc(runs.c.started_at)).limit(1)
    ).scalar_one_or_none()
    quarantined_last_run = (
        conn.execute(
            select(func.count()).select_from(quarantine).where(quarantine.c.run_id == latest_run)
        ).scalar_one()
        if latest_run
        else 0
    )

    return {
        "jobs": total,
        "companies": companies,
        "quarantined": quarantined,
        "quarantined_last_run": quarantined_last_run,
        "with_salary": with_salary,
        "distinct_groups": distinct_groups,
        "cross_source_groups": multi,
        "added_24h": added_24h,
        "duplicates_collapsed": max(0, total - distinct_groups),
    }


def by_source(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(jobs.c.source, func.count().label("n")).group_by(jobs.c.source)
    ).all()
    counts = {r.source: r.n for r in rows}
    return [
        {"source": s, "label": SOURCE_LABELS.get(s, s), "count": counts.get(s, 0)}
        for s in SOURCE_ORDER
        if counts.get(s)
    ]


def daily_discoveries(conn: Connection, days: int = 30) -> dict[str, Any]:
    """New postings per day per source, from ``first_seen_at``."""
    since = utcnow() - timedelta(days=days)
    bucket = day_bucket(conn, jobs.c.first_seen_at)
    rows = conn.execute(
        select(bucket.label("day"), jobs.c.source, func.count().label("n"))
        .where(jobs.c.first_seen_at >= since)
        .group_by(bucket, jobs.c.source)
        .order_by(bucket)
    ).all()

    per_day: dict[str, dict[str, int]] = {}
    for r in rows:
        per_day.setdefault(_as_day(r.day), {})[r.source] = r.n

    # A day with no run is a real gap and must render as one, so the axis is
    # built from the calendar rather than from the rows that happen to exist.
    today = utcnow().date()
    axis = [(today - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(days - 1, -1, -1)]
    present = [s for s in SOURCE_ORDER if any(s in v for v in per_day.values())]
    return {
        "days": axis,
        "sources": [{"key": s, "label": SOURCE_LABELS.get(s, s)} for s in present],
        "series": {s: [per_day.get(d, {}).get(s, 0) for d in axis] for s in present},
    }


def run_history(conn: Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(runs).order_by(desc(runs.c.started_at)).limit(limit)
    ).mappings().all()
    return [dict(r) for r in reversed(rows)]


def top_companies(conn: Connection, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            jobs.c.company,
            func.count().label("n"),
            func.count(func.distinct(jobs.c.source)).label("sources"),
        )
        .group_by(jobs.c.company)
        .order_by(desc(func.count()), jobs.c.company)
        .limit(limit)
    ).all()
    return [{"company": r.company, "count": r.n, "sources": r.sources} for r in rows]


def quarantine_breakdown(conn: Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(
            quarantine.c.reason_code,
            quarantine.c.source,
            func.count().label("n"),
            func.min(quarantine.c.reason).label("example"),
        )
        .group_by(quarantine.c.reason_code, quarantine.c.source)
        .order_by(desc(func.count()))
        .limit(limit)
    ).all()
    return [
        {
            "code": r.reason_code,
            "source": r.source,
            "label": SOURCE_LABELS.get(r.source, r.source),
            "count": r.n,
            "example": r.example,
        }
        for r in rows
    ]


def recent_quarantine(conn: Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(quarantine).order_by(desc(quarantine.c.created_at), desc(quarantine.c.id)).limit(limit)
    ).mappings().all()
    return [dict(r) for r in rows]


def duplicate_groups(conn: Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Postings the fingerprint identified as the same job.

    Duplicates arrive from two directions, and both matter:

    * *across sources* -- one employer listing the same role on Remote OK and
      We Work Remotely;
    * *across time* -- the same company reposting into consecutive months'
      Hacker News threads, which produces different comment ids and therefore
      different ``dedup_key`` values for what is plainly one job.

    Every row is kept either way. The fingerprint groups them for counting; it
    never decides that one of them should not exist.
    """
    grouped = (
        select(jobs.c.content_fingerprint)
        .group_by(jobs.c.content_fingerprint)
        .having(func.count() > 1)
        # Show the ones spanning several sources first: they are the harder
        # match and the more interesting evidence.
        .order_by(desc(func.count(func.distinct(jobs.c.source))), desc(func.count()))
        .limit(limit)
        .subquery()
    )
    rows = conn.execute(
        select(
            jobs.c.content_fingerprint, jobs.c.source, jobs.c.company,
            jobs.c.title, jobs.c.url, jobs.c.first_seen_at,
        )
        .where(jobs.c.content_fingerprint.in_(select(grouped.c.content_fingerprint)))
        .order_by(jobs.c.content_fingerprint, jobs.c.source)
    ).all()

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(
            r.content_fingerprint,
            {"fingerprint": r.content_fingerprint[:12], "company": r.company,
             "title": r.title, "rows": []},
        )
        g["rows"].append(
            {"source": r.source, "label": SOURCE_LABELS.get(r.source, r.source),
             "company": r.company, "title": r.title, "url": r.url}
        )
    for g in groups.values():
        sources = {r["source"] for r in g["rows"]}
        g["spans_sources"] = len(sources) > 1
        g["kind"] = "across sources" if len(sources) > 1 else "same source, repeated"
    return sorted(groups.values(), key=lambda g: not g["spans_sources"])


def recent_jobs(conn: Connection, limit: int = 40, source: str | None = None,
                search: str | None = None) -> list[dict[str, Any]]:
    stmt = select(jobs)
    if source:
        stmt = stmt.where(jobs.c.source == source)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(jobs.c.title).like(pattern) | func.lower(jobs.c.company).like(pattern)
        )
    rows = conn.execute(
        stmt.order_by(desc(jobs.c.first_seen_at), desc(jobs.c.id)).limit(limit)
    ).mappings().all()
    return [dict(r) for r in rows]


def task_timings(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(run_tasks).where(run_tasks.c.run_id == run_id).order_by(run_tasks.c.id)
    ).mappings().all()
    return [dict(r) for r in rows]


def snapshot(conn: Connection, stale_after_hours: int = 30) -> dict[str, Any]:
    """Everything the dashboard renders, in one call."""
    return {
        "generated_at": utcnow(),
        "health": health(conn, stale_after_hours),
        "totals": totals(conn),
        "by_source": by_source(conn),
        "daily": daily_discoveries(conn),
        "runs": run_history(conn),
        "top_companies": top_companies(conn),
        "quarantine_breakdown": quarantine_breakdown(conn),
        "recent_quarantine": recent_quarantine(conn),
        "duplicates": duplicate_groups(conn),
    }
