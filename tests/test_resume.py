"""Killing a run mid-flight and restarting it must not lose or duplicate work.

The ledger is what makes that possible: the task list is written before any
network call, each task's data and its ``done`` marker are committed in one
transaction, and a restart reclaims whatever the dead process left claimed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update

from jobradar.db import jobs, run_tasks, runs
from jobradar.models import JobPosting
from jobradar.pipeline import (
    _create_run,
    find_resumable_run,
    load_open_tasks,
    reclaim_orphaned_tasks,
)
from jobradar.sources.base import Task
from jobradar.storage import insert_quarantine, upsert_jobs


def make_tasks(n=5):
    return [
        Task(task_key=f"remoteok:t{i}", source="remoteok", url=f"https://remoteok.com/api?p={i}")
        for i in range(n)
    ]


def posting(external_id):
    return JobPosting(
        source="remoteok",
        external_id=external_id,
        url=f"https://remoteok.com/remote-jobs/{external_id}",
        company="Acme",
        title="Backend Engineer",
    )


def finish_task(engine, run_id, task_key, external_ids):
    """Do what a worker does on success: write data and completion together."""
    with engine.begin() as conn:
        upsert_jobs(conn, run_id, [posting(i) for i in external_ids])
        conn.execute(
            update(run_tasks)
            .where(run_tasks.c.run_id == run_id, run_tasks.c.task_key == task_key)
            .values(state="done", items_valid=len(external_ids))
        )


def states(engine, run_id) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(run_tasks.c.task_key, run_tasks.c.state).where(run_tasks.c.run_id == run_id)
        ).all()
    return {k: v for k, v in rows}


@pytest.fixture
def run_id(engine, settings):
    return _create_run(engine, settings, "test", make_tasks(5))


class TestPlanIsPersistedFirst:
    def test_all_tasks_are_pending_before_any_work_happens(self, engine, run_id):
        # Recovery reads the ledger rather than guessing, which only works if
        # the plan is durable before the first network call.
        assert set(states(engine, run_id).values()) == {"pending"}

    def test_the_run_records_its_task_total(self, engine, run_id):
        with engine.connect() as conn:
            total = conn.execute(
                select(runs.c.tasks_total).where(runs.c.id == run_id)
            ).scalar_one()
        assert total == 5

    def test_the_run_starts_marked_running(self, engine, run_id):
        with engine.connect() as conn:
            status = conn.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar_one()
        assert status == "running"


class TestResumeAfterAKill:
    def test_an_unfinished_run_is_found(self, engine, run_id):
        assert find_resumable_run(engine) == run_id

    def test_a_finished_run_is_not_resumed(self, engine, run_id):
        with engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(status="succeeded"))
        assert find_resumable_run(engine) is None

    def test_a_task_claimed_by_a_dead_process_is_reclaimed(self, engine, run_id):
        # Simulate: two tasks finished, one was in flight when the process died.
        finish_task(engine, run_id, "remoteok:t0", ["1", "2"])
        finish_task(engine, run_id, "remoteok:t1", ["3"])
        with engine.begin() as conn:
            conn.execute(
                update(run_tasks)
                .where(run_tasks.c.run_id == run_id, run_tasks.c.task_key == "remoteok:t2")
                .values(state="running")
            )

        assert reclaim_orphaned_tasks(engine, run_id) == 1
        assert states(engine, run_id)["remoteok:t2"] == "pending"

    def test_resume_only_picks_up_unfinished_work(self, engine, run_id):
        finish_task(engine, run_id, "remoteok:t0", ["1", "2"])
        finish_task(engine, run_id, "remoteok:t1", ["3"])
        reclaim_orphaned_tasks(engine, run_id)

        open_keys = {t["task_key"] for t in load_open_tasks(engine, run_id)}
        assert open_keys == {"remoteok:t2", "remoteok:t3", "remoteok:t4"}
        assert "remoteok:t0" not in open_keys, "finished work must not be redone"

    def test_completed_data_survives_the_kill(self, engine, run_id):
        finish_task(engine, run_id, "remoteok:t0", ["1", "2"])
        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 2

    def test_resuming_and_re_running_a_task_does_not_duplicate(self, engine, run_id):
        # The task that was in flight is re-processed on resume; because the
        # writes are idempotent that is harmless. This is why resume and
        # idempotency are one mechanism rather than two.
        finish_task(engine, run_id, "remoteok:t0", ["1", "2"])
        with engine.begin() as conn:
            conn.execute(
                update(run_tasks)
                .where(run_tasks.c.run_id == run_id, run_tasks.c.task_key == "remoteok:t1")
                .values(state="running")
            )
        reclaim_orphaned_tasks(engine, run_id)
        # Resume: t1 runs again and happens to return rows t0 already stored.
        finish_task(engine, run_id, "remoteok:t1", ["1", "2", "3"])

        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 3

    def test_a_run_older_than_the_resume_window_is_abandoned(self, engine, run_id, settings):
        from datetime import timedelta

        from jobradar.db import utcnow

        with engine.begin() as conn:
            conn.execute(
                update(runs)
                .where(runs.c.id == run_id)
                .values(started_at=utcnow() - timedelta(hours=48))
            )
        assert find_resumable_run(engine) is None


class TestTaskAtomicity:
    def test_a_failure_mid_task_writes_neither_data_nor_completion(self, engine, run_id):
        """The property the whole resume story depends on.

        There must be no state in which a task is marked done but its rows are
        missing -- otherwise a restart would skip work that never happened.
        """
        with pytest.raises(RuntimeError), engine.begin() as conn:
            upsert_jobs(conn, run_id, [posting("1"), posting("2")])
            insert_quarantine(conn, run_id, "remoteok", "remoteok:t0",
                              [{"reason": "bad", "raw": {}}])
            raise RuntimeError("process killed here")

        with engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(jobs)).scalar_one() == 0
        assert states(engine, run_id)["remoteok:t0"] == "pending"


class TestRunLock:
    def test_a_second_run_cannot_start_while_one_holds_the_lock(self, engine):
        from jobradar.db import LockNotAcquired, run_lock

        with (
            run_lock(engine, "jobradar:run", "holder-a"),
            pytest.raises(LockNotAcquired),
            run_lock(engine, "jobradar:run", "holder-b"),
        ):
            pass

    def test_the_lock_is_released_on_the_way_out(self, engine):
        from jobradar.db import run_lock

        with run_lock(engine, "jobradar:run", "holder-a"):
            pass
        with run_lock(engine, "jobradar:run", "holder-b"):
            pass  # must not raise

    def test_the_lock_is_released_even_if_the_run_raises(self, engine):
        from jobradar.db import run_lock

        with pytest.raises(ValueError), run_lock(engine, "jobradar:run", "holder-a"):
            raise ValueError("boom")
        with run_lock(engine, "jobradar:run", "holder-b"):
            pass

    def test_an_expired_lease_is_reclaimed(self, engine):
        # A process killed while holding the lock must not wedge the pipeline.
        from jobradar.db import run_lock

        with (
            pytest.raises(Exception),  # noqa: B017
            run_lock(engine, "jobradar:run", "dead-holder", ttl_seconds=-1),
        ):
            raise Exception("killed")
        # The lease has already lapsed, so the next run may take it immediately.
        with run_lock(engine, "jobradar:run", "next-holder"):
            pass
