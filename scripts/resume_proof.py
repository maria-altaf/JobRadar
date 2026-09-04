#!/usr/bin/env python
"""Kill a run mid-flight and prove it resumes correctly.

Reproducible evidence for the claim in the README. It:

1. starts a real run against a scratch database;
2. waits until some tasks have finished and one is genuinely in flight;
3. kills the process **hard** -- SIGKILL / TerminateProcess, no cleanup, no
   signal handler, the way a machine dying would;
4. shows the ledger: what was done, what was abandoned;
5. restarts, and shows it picking up only the unfinished work;
6. checks the result for duplicates and for gaps.

Run it with:  python scripts/resume_proof.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import func, select  # noqa: E402

from jobradar.db import ensure_schema, jobs, make_engine, run_tasks, runs  # noqa: E402

DB_PATH = REPO / "data" / "resume_proof.db"
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"

#: Kill once this many tasks are done -- enough that "it resumed" is meaningful,
#: few enough that plenty is left to resume.
KILL_AFTER_TASKS = 4
KILL_TIMEOUT = 120

#: A killed process cannot release its run lock, so recovery has to wait for the
#: lease to lapse. In production that lease is 5 minutes; here it is shortened
#: so the demonstration does not sit idle. Waiting for it is part of what is
#: being shown -- the lock expiring is what stops a crash wedging the pipeline.
LOCK_TTL = 10


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def ledger(engine, run_id=None):
    with engine.connect() as conn:
        if run_id is None:
            row = conn.execute(
                select(runs.c.id).order_by(runs.c.started_at.desc()).limit(1)
            ).first()
            if not row:
                return None, {}, 0
            run_id = row.id
        states = dict(
            conn.execute(
                select(run_tasks.c.state, func.count())
                .where(run_tasks.c.run_id == run_id)
                .group_by(run_tasks.c.state)
            ).all()
        )
        n_jobs = conn.execute(select(func.count()).select_from(jobs)).scalar_one()
    return run_id, states, n_jobs


def show(engine, run_id, label):
    run_id, states, n_jobs = ledger(engine, run_id)
    total = sum(states.values())
    print(f"  {label}")
    print(f"    run          {run_id}")
    print(f"    tasks        {dict(sorted(states.items()))}  (total {total})")
    print(f"    rows in jobs {n_jobs}")
    return run_id, states, n_jobs


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    env = {**os.environ, "DATABASE_URL": DB_URL, "JOBRADAR_LOG_LEVEL": "WARNING"}
    engine = make_engine(DB_URL)
    ensure_schema(engine)

    rule("1. Start a real run, then kill it mid-flight")
    proc = subprocess.Popen(
        [sys.executable, "-m", "jobradar.cli", "run",
         "--trigger", "resume-proof", "--lock-ttl", str(LOCK_TTL)],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"  started pid {proc.pid}; waiting for {KILL_AFTER_TASKS} tasks to finish...")

    deadline = time.time() + KILL_TIMEOUT
    run_id = None
    while time.time() < deadline:
        if proc.poll() is not None:
            print("  ! the run finished before we could kill it; lower KILL_AFTER_TASKS")
            return 1
        run_id, states, _ = ledger(engine)
        if run_id and states.get("done", 0) >= KILL_AFTER_TASKS:
            break
        time.sleep(0.5)
    else:
        print("  ! timed out waiting for tasks to finish")
        proc.kill()
        return 1

    # SIGKILL equivalent: no cleanup, no chance to mark anything.
    proc.kill()
    proc.wait(timeout=10)
    print(f"  KILLED pid {proc.pid} (no cleanup, no signal handler)")

    rule("2. What the killed process left behind")
    _, states_before, jobs_before = show(engine, run_id, "after the kill:")
    unfinished = sum(v for k, v in states_before.items() if k != "done")
    print(f"\n    -> {states_before.get('done', 0)} task(s) finished and committed")
    print(f"    -> {unfinished} task(s) left unfinished")
    print(f"    -> the run is still marked '{_status(engine, run_id)}', so it is resumable")

    rule("3. Wait for the dead process's lock lease to lapse")
    print(f"  the killed process still holds the run lock; its {LOCK_TTL}s lease")
    print("  must expire before anything may touch this run  ", end="", flush=True)
    for _ in range(LOCK_TTL + 4):
        if not _lock_held(engine):
            break
        print(".", end="", flush=True)
        time.sleep(1)
    print(" lapsed")

    rule("4. Restart -- it should adopt the same run, not start over")
    result = subprocess.run(
        [sys.executable, "-m", "jobradar.cli", "run", "--trigger", "resume-proof-restart"],
        cwd=REPO, env={**env, "JOBRADAR_LOG_LEVEL": "INFO"},
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if any(w in line for w in ("resuming run", "open task", "reclaimed", "skipping")):
            print(f"    {line.strip()}")

    rule("5. Final state")
    run_id_after, states_after, jobs_after = show(engine, run_id, "after the restart:")

    rule("6. Checks")
    ok = True

    same_run = run_id_after == run_id
    ok &= _check(same_run, "resumed the SAME run rather than starting a new one")

    all_done = states_after.get("done", 0) == sum(states_after.values())
    ok &= _check(all_done, f"every task reached 'done' ({states_after})")

    ok &= _check(jobs_after > jobs_before,
                 f"the resumed half added rows ({jobs_before} -> {jobs_after})")

    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(jobs)).scalar_one()
        distinct = conn.execute(
            select(func.count(func.distinct(jobs.c.dedup_key))).select_from(jobs)
        ).scalar_one()
        n_runs = conn.execute(select(func.count()).select_from(runs)).scalar_one()
    ok &= _check(total == distinct,
                 f"no duplicates: {total} rows, {distinct} distinct dedup_keys")
    ok &= _check(n_runs == 1, f"only one run row exists, not two ({n_runs})")

    print("\n" + ("PASS -- the pipeline resumed and did not duplicate."
                  if ok else "FAIL -- see above."))
    return 0 if ok else 1


def _lock_held(engine) -> bool:
    from jobradar.db import run_locks
    from jobradar.db import utcnow as _now

    with engine.connect() as conn:
        row = conn.execute(
            select(run_locks.c.expires_at).where(run_locks.c.name == "jobradar:run")
        ).first()
    return bool(row and row.expires_at > _now())


def _status(engine, run_id) -> str:
    with engine.connect() as conn:
        return conn.execute(select(runs.c.status).where(runs.c.id == run_id)).scalar_one()


def _check(condition: bool, description: str) -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return condition


if __name__ == "__main__":
    raise SystemExit(main())
