"""Database schema and storage primitives.

The same schema runs on SQLite (local dev, CI tests) and Postgres (production
on Neon). Only three things actually differ between the dialects -- the upsert
construct, date truncation in the dashboard aggregates, and timestamp timezone
handling -- and each is isolated behind a helper here.

Table roles
-----------
``runs``         one row per pipeline execution; the dashboard health indicator
                 reads the newest ``succeeded`` row.
``run_tasks``    the resume ledger. The full task list is written *before* any
                 network call happens, so a killed process leaves behind an
                 accurate record of what was and was not finished.
``jobs``         the canonical posting store, keyed by ``dedup_key``.
``quarantine``   rows that failed schema validation, kept with a readable
                 reason and the original payload. Nothing is ever dropped.
``source_state`` per-URL ETag / Last-Modified for conditional requests.
``run_locks``    mutual exclusion so two overlapping runs cannot interleave.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine


def utcnow() -> datetime:
    return datetime.now(UTC)


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on both dialects.

    Postgres stores ``timestamptz`` natively. SQLite has no timezone concept, so
    values are converted to UTC and stored naive, then re-tagged as UTC on read.
    Every datetime that enters or leaves the database is therefore aware and in
    UTC, which keeps comparisons and chart bucketing honest.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        if dialect.name == "postgresql":
            return value
        return value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


metadata = MetaData()


runs = Table(
    "runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("started_at", TZDateTime, nullable=False),
    Column("finished_at", TZDateTime),
    # running | succeeded | failed | partial
    Column("status", String(16), nullable=False),
    Column("trigger", String(32), nullable=False, default="manual"),
    Column("git_sha", String(40)),
    Column("host", String(120)),
    Column("error", Text),
    Column("tasks_total", Integer, nullable=False, default=0),
    Column("tasks_done", Integer, nullable=False, default=0),
    Column("tasks_failed", Integer, nullable=False, default=0),
    Column("items_seen", Integer, nullable=False, default=0),
    Column("items_inserted", Integer, nullable=False, default=0),
    Column("items_updated", Integer, nullable=False, default=0),
    Column("items_unchanged", Integer, nullable=False, default=0),
    Column("items_quarantined", Integer, nullable=False, default=0),
    Column("requests_made", Integer, nullable=False, default=0),
    Column("retries_made", Integer, nullable=False, default=0),
    Column("resumed_count", Integer, nullable=False, default=0),
    Column("duration_seconds", Float),
    Column("health", JSON),
    Index("ix_runs_status_finished", "status", "finished_at"),
    Index("ix_runs_started", "started_at"),
)


run_tasks = Table(
    "run_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    # deterministic identifier for this unit of work within the run
    Column("task_key", String(200), nullable=False),
    Column("source", String(32), nullable=False),
    Column("url", Text, nullable=False),
    Column("params", JSON),
    # pending | running | done | failed
    Column("state", String(16), nullable=False, default="pending"),
    Column("attempts", Integer, nullable=False, default=0),
    Column("started_at", TZDateTime),
    Column("finished_at", TZDateTime),
    Column("error", Text),
    Column("http_status", Integer),
    Column("not_modified", Boolean, nullable=False, default=False),
    Column("items_seen", Integer, nullable=False, default=0),
    Column("items_valid", Integer, nullable=False, default=0),
    Column("items_quarantined", Integer, nullable=False, default=0),
    Index("ux_run_tasks_run_key", "run_id", "task_key", unique=True),
    Index("ix_run_tasks_state", "run_id", "state"),
)


jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # sha256(source | external_id) -- the idempotency anchor
    Column("dedup_key", String(64), nullable=False, unique=True),
    # sha256(normalised company | title | location) -- advisory, cross-source
    Column("content_fingerprint", String(64), nullable=False),
    Column("source", String(32), nullable=False),
    Column("external_id", String(200), nullable=False),
    Column("url", Text, nullable=False),
    Column("company", String(300), nullable=False),
    Column("title", String(400), nullable=False),
    Column("location", String(300)),
    Column("employment_type", String(60)),
    Column("salary_min", Integer),
    Column("salary_max", Integer),
    Column("salary_currency", String(8)),
    Column("tags", JSON),
    Column("description_text", Text),
    Column("posted_at", TZDateTime),
    Column("first_seen_at", TZDateTime, nullable=False),
    Column("last_seen_at", TZDateTime, nullable=False),
    Column("last_seen_run_id", String(36)),
    # Set only when the content actually changed. Lets a run's "updated" count
    # be derived from the table itself rather than from per-task tallies, which
    # can double-count when two concurrent tasks carry the same posting.
    Column("last_changed_run_id", String(36)),
    Column("content_hash", String(64), nullable=False),
    Column("revision", Integer, nullable=False, default=1),
    Index("ix_jobs_fingerprint", "content_fingerprint"),
    Index("ix_jobs_source", "source"),
    Index("ix_jobs_posted_at", "posted_at"),
    Index("ix_jobs_first_seen", "first_seen_at"),
    Index("ix_jobs_last_seen", "last_seen_at"),
)


quarantine = Table(
    "quarantine",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    Column("source", String(32), nullable=False),
    Column("task_key", String(200)),
    Column("external_id", String(200)),
    # one-line human-readable explanation, e.g.
    # "title: String should have at least 2 characters (got '')"
    Column("reason", Text, nullable=False),
    # The same failure with the offending value stripped out, e.g.
    # "title:string_too_short". The full reason embeds the bad value and so is
    # unique per row; this is what the dashboard groups on to show which
    # failure modes actually dominate.
    Column("reason_code", String(120), nullable=False, default="unknown"),
    Column("error_count", Integer, nullable=False, default=1),
    Column("errors", JSON),
    Column("raw", JSON),
    Column("created_at", TZDateTime, nullable=False),
    Index("ix_quarantine_run", "run_id"),
    Index("ix_quarantine_created", "created_at"),
    Index("ix_quarantine_source", "source"),
)


source_state = Table(
    "source_state",
    metadata,
    Column("url", Text, primary_key=True),
    Column("etag", String(300)),
    Column("last_modified", String(120)),
    Column("last_fetched_at", TZDateTime),
    Column("last_status", Integer),
    Column("last_item_count", Integer),
)


run_locks = Table(
    "run_locks",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("holder", String(120), nullable=False),
    Column("acquired_at", TZDateTime, nullable=False),
    Column("expires_at", TZDateTime, nullable=False),
)


# ---------------------------------------------------------------- engine ----


def make_engine(database_url: str, echo: bool = False) -> Engine:
    """Build an engine, creating the parent directory for SQLite files."""
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        return create_engine(database_url, echo=echo, future=True)
    # Neon closes idle connections; pre-ping avoids handing out a dead one.
    return create_engine(
        database_url,
        echo=echo,
        future=True,
        pool_pre_ping=True,
        pool_recycle=280,
        connect_args={"sslmode": "require"} if "sslmode" not in database_url else {},
    )


def ensure_schema(engine: Engine) -> None:
    metadata.create_all(engine)


def dialect_insert(engine_or_conn, table: Table):
    """Return the dialect-specific INSERT construct that supports ON CONFLICT."""
    name = (
        engine_or_conn.dialect.name
        if hasattr(engine_or_conn, "dialect")
        else engine_or_conn.engine.dialect.name
    )
    return pg_insert(table) if name == "postgresql" else sqlite_insert(table)


def day_bucket(engine_or_conn, column):
    """Truncate a timestamp column to a day, portably."""
    from sqlalchemy import func

    name = engine_or_conn.dialect.name
    if name == "postgresql":
        return func.date_trunc("day", column)
    return func.date(column)


# ------------------------------------------------------------------ lock ----


class LockNotAcquired(RuntimeError):
    pass


#: Lease length for the run lock. It only has to outlive a couple of missed
#: heartbeats, not a whole run: a live run renews the lease every 60s, so a
#: short TTL costs nothing while the process is healthy. It is what a *killed*
#: process costs that matters -- the lease is the delay before recovery can
#: start, and an hour of that would mean a crash wedges the pipeline until
#: someone notices by hand.
DEFAULT_LOCK_TTL = 300


@contextlib.contextmanager
def run_lock(engine: Engine, name: str, holder: str, ttl_seconds: int = DEFAULT_LOCK_TTL):
    """Cross-dialect advisory lock with an expiry.

    Guards against two scheduled runs overlapping (a slow run still going when
    the next cron fires). The TTL means a process killed while holding the lock
    does not wedge the pipeline forever -- the next run reclaims it once the
    lease lapses, which is the behaviour the resume path depends on.
    """
    now = utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    with engine.begin() as conn:
        # Clear any lease that has already lapsed.
        conn.execute(delete(run_locks).where(run_locks.c.expires_at < now))
        stmt = (
            dialect_insert(conn, run_locks)
            .values(name=name, holder=holder, acquired_at=now, expires_at=expires)
            .on_conflict_do_nothing(index_elements=["name"])
        )
        result = conn.execute(stmt)
        if result.rowcount == 0:
            row = conn.execute(
                select(run_locks.c.holder, run_locks.c.expires_at).where(
                    run_locks.c.name == name
                )
            ).first()
            raise LockNotAcquired(
                f"lock {name!r} is held by {row.holder if row else '?'} "
                f"until {row.expires_at if row else '?'}"
            )
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(
                delete(run_locks).where(
                    run_locks.c.name == name, run_locks.c.holder == holder
                )
            )


def heartbeat_lock(
    engine: Engine, name: str, holder: str, ttl_seconds: int = DEFAULT_LOCK_TTL
) -> None:
    """Extend a held lease so a long but healthy run is not reclaimed."""
    with engine.begin() as conn:
        conn.execute(
            update(run_locks)
            .where(run_locks.c.name == name, run_locks.c.holder == holder)
            .values(expires_at=utcnow() + timedelta(seconds=ttl_seconds))
        )


__all__ = [
    "metadata",
    "runs",
    "run_tasks",
    "jobs",
    "quarantine",
    "source_state",
    "run_locks",
    "make_engine",
    "ensure_schema",
    "dialect_insert",
    "day_bucket",
    "run_lock",
    "heartbeat_lock",
    "LockNotAcquired",
    "utcnow",
    "Connection",
    "Engine",
    "insert",
    "select",
    "update",
    "delete",
]
