"""Running the pipeline twice must never duplicate data.

These tests exercise the upsert directly, covering the three ways duplicates
actually arrive: the same run seeing a posting twice, two runs seeing the same
posting, and a resumed run re-processing a task that was already in flight.
"""

from __future__ import annotations

from sqlalchemy import func, select

from jobradar.db import jobs, quarantine
from jobradar.models import JobPosting
from jobradar.storage import insert_quarantine, upsert_jobs


def posting(external_id="1", **overrides):
    data = {
        "source": "remoteok",
        "external_id": external_id,
        "url": f"https://remoteok.com/remote-jobs/{external_id}",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Remote",
        "tags": ["python"],
    }
    data.update(overrides)
    return JobPosting(**data)


def count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(jobs)).scalar_one()


def row(engine, external_id="1"):
    with engine.connect() as conn:
        return conn.execute(
            select(jobs).where(jobs.c.external_id == external_id)
        ).mappings().one()


class TestRepeatedRuns:
    def test_the_same_batch_twice_leaves_one_row(self, engine):
        with engine.begin() as conn:
            first = upsert_jobs(conn, "run-1", [posting()])
        with engine.begin() as conn:
            second = upsert_jobs(conn, "run-2", [posting()])
        assert first == (1, 0, 0)
        assert second == (0, 0, 1), "second pass must be all-unchanged"
        assert count(engine) == 1

    def test_ten_consecutive_runs_leave_one_row(self, engine):
        for n in range(10):
            with engine.begin() as conn:
                upsert_jobs(conn, f"run-{n}", [posting()])
        assert count(engine) == 1

    def test_duplicates_inside_a_single_batch_collapse(self, engine):
        # Remote OK's tag endpoints return the same posting under several tags;
        # ON CONFLICT cannot resolve a row against another row in its own
        # statement, so the batch is de-duplicated before it is sent.
        with engine.begin() as conn:
            inserted, _, _ = upsert_jobs(conn, "run-1", [posting(), posting(), posting()])
        assert inserted == 1
        assert count(engine) == 1

    def test_distinct_postings_are_kept_apart(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting("1"), posting("2"), posting("3")])
        assert count(engine) == 3

    def test_the_same_id_from_two_sources_is_two_rows(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting("42"), posting("42", source="hackernews")])
        assert count(engine) == 2


class TestChangeTracking:
    def test_an_edit_updates_in_place_and_bumps_the_revision(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting(salary_min=100000)])
        with engine.begin() as conn:
            counts = upsert_jobs(conn, "run-2", [posting(salary_min=150000)])
        assert counts == (0, 1, 0)
        assert count(engine) == 1
        assert row(engine)["salary_min"] == 150000
        assert row(engine)["revision"] == 2

    def test_an_unchanged_posting_does_not_bump_the_revision(self, engine):
        # revision counts genuine employer edits, not how often we looked.
        for n in range(5):
            with engine.begin() as conn:
                upsert_jobs(conn, f"run-{n}", [posting()])
        assert row(engine)["revision"] == 1

    def test_first_seen_at_survives_updates(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting()])
        original = row(engine)["first_seen_at"]
        with engine.begin() as conn:
            upsert_jobs(conn, "run-2", [posting(title="Staff Backend Engineer")])
        assert row(engine)["first_seen_at"] == original

    def test_last_seen_tracks_the_most_recent_run(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting()])
        with engine.begin() as conn:
            upsert_jobs(conn, "run-2", [posting()])
        assert row(engine)["last_seen_run_id"] == "run-2"

    def test_last_seen_advances_even_when_nothing_changed(self, engine):
        with engine.begin() as conn:
            upsert_jobs(conn, "run-1", [posting()])
        before = row(engine)["last_seen_at"]
        with engine.begin() as conn:
            upsert_jobs(conn, "run-2", [posting()])
        assert row(engine)["last_seen_at"] >= before


class TestCrossSourceFingerprint:
    def test_the_same_job_from_two_sources_shares_a_fingerprint(self, engine):
        # Both rows are kept -- the fingerprint groups them for reporting, it
        # never causes one to be discarded.
        with engine.begin() as conn:
            upsert_jobs(
                conn,
                "run-1",
                [
                    posting("a", source="remoteok", company="Acme, Inc.",
                            title="Senior Software Engineer (Remote)", location="Anywhere in the World"),
                    posting("b", source="weworkremotely", company="ACME Inc",
                            title="Senior Software Engineer", location="Remote"),
                ],
            )
        with engine.connect() as conn:
            fingerprints = conn.execute(select(jobs.c.content_fingerprint)).scalars().all()
        assert count(engine) == 2
        assert len(set(fingerprints)) == 1


class TestQuarantineWrites:
    def test_bad_rows_are_stored_with_their_reason_and_payload(self, engine):
        with engine.begin() as conn:
            n = insert_quarantine(
                conn, "run-1", "remoteok", "remoteok:all",
                [{"external_id": "9", "reason": "title: too short (got '')",
                  "errors": [{"field": "title"}], "raw": {"position": ""}}],
            )
        assert n == 1
        with engine.connect() as conn:
            stored = conn.execute(select(quarantine)).mappings().one()
        assert stored["reason"].startswith("title:")
        assert stored["raw"] == {"position": ""}
        assert stored["source"] == "remoteok"

    def test_an_empty_batch_writes_nothing(self, engine):
        with engine.begin() as conn:
            assert insert_quarantine(conn, "run-1", "remoteok", "t", []) == 0
