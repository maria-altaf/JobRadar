"""Writes: the upsert, the quarantine, and the run/task ledger.

Idempotency
-----------
Every posting is written with ``INSERT ... ON CONFLICT (dedup_key) DO UPDATE``.
``dedup_key`` is derived from the identifier the *source* assigns, so re-running
the pipeline over data it has already seen can only update rows -- there is no
code path that inserts a second copy of a posting. This holds for the three ways
duplicates actually arrive:

* running the whole pipeline twice on the same day;
* resuming a killed run, which re-processes the task that was in flight;
* Remote OK's tag endpoints returning the same posting under ``dev`` *and*
  ``engineer`` *and* the unfiltered feed, within a single run.

``content_hash`` covers the mutable fields. When it is unchanged the row's
``revision`` is left alone, so ``revision`` counts genuine edits by the employer
rather than counting how many times we looked.

Atomicity
---------
:func:`complete_task` writes the postings, the quarantine rows, the fetch state
and the task's ``done`` marker **in one transaction**. That is the property the
resume path rests on: a process killed mid-task leaves either all of the task's
effects or none of them, never a half-written task marked finished.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.engine import Connection, Engine

from .db import (
    dialect_insert,
    jobs,
    quarantine,
    run_tasks,
    runs,
    source_state,
    utcnow,
)
from .models import JobPosting
from .normalize import make_content_fingerprint, make_content_hash, make_dedup_key

log = logging.getLogger(__name__)

#: Rows per INSERT. Keeps statements well inside parameter limits on both
#: dialects without making the write chatty.
BATCH_SIZE = 200

#: The fields whose change constitutes a genuine revision of a posting.
_MUTABLE_FIELDS = (
    "url",
    "company",
    "title",
    "location",
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "description_text",
    "posted_at",
)


def chunked(items: Sequence[Any], size: int = BATCH_SIZE) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _mutable_payload(posting: JobPosting) -> dict[str, Any]:
    data = {f: getattr(posting, f) for f in _MUTABLE_FIELDS}
    data["tags"] = ",".join(sorted(posting.tags))
    return data


def upsert_jobs(
    conn: Connection, run_id: str, postings: Iterable[JobPosting]
) -> tuple[int, int, int]:
    """Insert or update postings. Returns ``(inserted, updated, unchanged)``.

    The counts are computed by reading the existing ``content_hash`` values
    first. That read is only for reporting -- the write itself is an upsert, so
    a concurrent writer racing between the read and the write still cannot
    produce a duplicate.
    """
    postings = list(postings)
    if not postings:
        return (0, 0, 0)

    now = utcnow()

    # Collapse duplicates inside the batch before touching the database: a
    # single Remote OK response can carry the same posting more than once, and
    # ON CONFLICT cannot resolve a conflict against a row in its own statement.
    by_key: dict[str, JobPosting] = {}
    for posting in postings:
        by_key[make_dedup_key(posting.source, posting.external_id)] = posting

    keys = list(by_key)
    existing: dict[str, str] = {}
    for chunk in chunked(keys):
        rows = conn.execute(
            select(jobs.c.dedup_key, jobs.c.content_hash).where(jobs.c.dedup_key.in_(chunk))
        )
        existing.update({r.dedup_key: r.content_hash for r in rows})

    inserted = updated = unchanged = 0
    values: list[dict[str, Any]] = []

    for key, posting in by_key.items():
        content_hash = make_content_hash(_mutable_payload(posting))
        previous = existing.get(key)
        if previous is None:
            inserted += 1
        elif previous == content_hash:
            unchanged += 1
        else:
            updated += 1

        values.append(
            {
                "dedup_key": key,
                "content_fingerprint": make_content_fingerprint(
                    posting.company, posting.title, posting.location
                ),
                "source": posting.source,
                "external_id": posting.external_id,
                "url": posting.url,
                "company": posting.company,
                "title": posting.title,
                "location": posting.location,
                "employment_type": posting.employment_type,
                "salary_min": posting.salary_min,
                "salary_max": posting.salary_max,
                "salary_currency": posting.salary_currency,
                "tags": posting.tags,
                "description_text": posting.description_text,
                "posted_at": posting.posted_at,
                "first_seen_at": now,
                "last_seen_at": now,
                "last_seen_run_id": run_id,
                "last_changed_run_id": run_id,
                "content_hash": content_hash,
                "revision": 1,
            }
        )

    for chunk in chunked(values):
        stmt = dialect_insert(conn, jobs)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["dedup_key"],
            set_={
                "content_fingerprint": excluded.content_fingerprint,
                "url": excluded.url,
                "company": excluded.company,
                "title": excluded.title,
                "location": excluded.location,
                "employment_type": excluded.employment_type,
                "salary_min": excluded.salary_min,
                "salary_max": excluded.salary_max,
                "salary_currency": excluded.salary_currency,
                "tags": excluded.tags,
                "description_text": excluded.description_text,
                "posted_at": excluded.posted_at,
                # first_seen_at is deliberately absent: it must survive updates.
                "last_seen_at": excluded.last_seen_at,
                "last_seen_run_id": excluded.last_seen_run_id,
                "content_hash": excluded.content_hash,
                # Only count a revision when the content actually moved.
                "revision": case(
                    (jobs.c.content_hash != excluded.content_hash, jobs.c.revision + 1),
                    else_=jobs.c.revision,
                ),
                # Likewise, only stamp the changing run when something changed.
                "last_changed_run_id": case(
                    (
                        jobs.c.content_hash != excluded.content_hash,
                        excluded.last_changed_run_id,
                    ),
                    else_=jobs.c.last_changed_run_id,
                ),
            },
        )
        conn.execute(stmt, list(chunk))

    return (inserted, updated, unchanged)


def reason_code_for(entry: dict[str, Any]) -> str:
    """A stable, groupable label for a quarantine reason.

    The human-readable reason embeds the offending value, which makes it unique
    per row and useless to ``GROUP BY``. The code keeps only the shape of the
    failure -- ``title:string_too_short`` -- so the dashboard can show which
    failure modes actually dominate, and so a *change* in the mix is visible.
    """
    # A parser that raised supplies its own slug, which is the clearest label.
    explicit = entry.get("code")
    if explicit:
        return str(explicit)[:120]

    errors = entry.get("errors") or []
    if errors:
        first = errors[0]
        field = str(first.get("field") or "?")
        kind = first.get("type")
        if kind:
            return f"{field}:{kind}"[:120]
        message = str(first.get("message") or entry.get("reason") or "unknown")
        return f"{field}:{'_'.join(message.split()[:5])}"[:120]
    return str(entry.get("reason", "unknown")).split("(got")[0].strip()[:120] or "unknown"


def insert_quarantine(
    conn: Connection,
    run_id: str,
    source: str,
    task_key: str,
    entries: Iterable[dict[str, Any]],
) -> int:
    """Persist rows that could not be validated, with their reasons."""
    entries = list(entries)
    if not entries:
        return 0
    now = utcnow()
    values = [
        {
            "run_id": run_id,
            "source": source,
            "task_key": task_key,
            "external_id": (e.get("external_id") or None),
            "reason": e["reason"],
            "reason_code": reason_code_for(e),
            "error_count": len(e.get("errors") or []) or 1,
            "errors": e.get("errors"),
            "raw": e.get("raw"),
            "created_at": now,
        }
        for e in entries
    ]
    for chunk in chunked(values):
        conn.execute(quarantine.insert(), list(chunk))
    return len(values)


def record_source_state(
    conn: Connection,
    url: str,
    *,
    etag: str | None,
    last_modified: str | None,
    status: int,
    item_count: int | None,
) -> None:
    """Remember ETag / Last-Modified so the next run can ask conditionally.

    ``last_item_count`` is also the input to the parser health check: it is what
    "this feed usually returns" is measured against.
    """
    stmt = dialect_insert(conn, source_state)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["url"],
        set_={
            "etag": excluded.etag,
            "last_modified": excluded.last_modified,
            "last_fetched_at": excluded.last_fetched_at,
            "last_status": excluded.last_status,
            # A 304 carries no items; keep the previous count as the baseline.
            "last_item_count": case(
                (excluded.last_item_count.is_(None), source_state.c.last_item_count),
                else_=excluded.last_item_count,
            ),
        },
    )
    conn.execute(
        stmt,
        [
            {
                "url": url,
                "etag": etag,
                "last_modified": last_modified,
                "last_fetched_at": utcnow(),
                "last_status": status,
                "last_item_count": item_count,
            }
        ],
    )


def get_source_state(conn: Connection, url: str) -> dict[str, Any] | None:
    row = conn.execute(select(source_state).where(source_state.c.url == url)).mappings().first()
    return dict(row) if row else None


# ------------------------------------------------------------- ledger ops ----


def mark_task_running(engine: Engine, task_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(run_tasks)
            .where(run_tasks.c.id == task_id)
            .values(
                state="running",
                started_at=utcnow(),
                attempts=run_tasks.c.attempts + 1,
            )
        )


def mark_task_failed(engine: Engine, task_id: int, error: str, *, terminal: bool) -> None:
    """Park a failed task.

    A non-terminal failure goes back to ``pending`` so a later attempt in this
    same run (or a resume) can pick it up; a terminal one is left ``failed`` and
    reported.
    """
    with engine.begin() as conn:
        conn.execute(
            update(run_tasks)
            .where(run_tasks.c.id == task_id)
            .values(
                state="failed" if terminal else "pending",
                finished_at=utcnow() if terminal else None,
                error=error[:4000],
            )
        )


def bump_run_counters(conn: Connection, run_id: str, **deltas: int) -> None:
    if not deltas:
        return
    conn.execute(
        update(runs)
        .where(runs.c.id == run_id)
        .values({k: getattr(runs.c, k) + v for k, v in deltas.items()})
    )


def count_tasks_by_state(engine: Engine, run_id: str) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(run_tasks.c.state, func.count())
            .where(run_tasks.c.run_id == run_id)
            .group_by(run_tasks.c.state)
        ).all()
    return {state: n for state, n in rows}
