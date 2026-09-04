"""Judging a finished run.

Completing is not succeeding. The check that earns its keep is parser health:
a source can return HTTP 200 and parse without raising while yielding almost
nothing, which is what "the site changed its HTML" looks like from the inside.
Without this the run goes green and the dashboard quietly flatlines.
"""

from __future__ import annotations

import dataclasses
import uuid

from jobradar.db import run_tasks, runs, utcnow
from jobradar.pipeline import TaskOutcome, evaluate_health, source_baselines


def seed_run(engine, source: str, items: int, status: str = "succeeded") -> str:
    """Record a historical run that stored ``items`` rows for ``source``."""
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            runs.insert(),
            [{"id": run_id, "started_at": utcnow(), "finished_at": utcnow(),
              "status": status, "trigger": "test", "tasks_total": 1, "tasks_done": 1,
              "tasks_failed": 0, "items_seen": items, "items_inserted": items,
              "items_updated": 0, "items_unchanged": 0, "items_quarantined": 0,
              "requests_made": 1, "retries_made": 0, "resumed_count": 0}],
        )
        conn.execute(
            run_tasks.insert(),
            [{"run_id": run_id, "task_key": f"{source}:all", "source": source,
              "url": "https://example.test", "state": "done", "attempts": 1,
              "items_seen": items, "items_valid": items, "items_quarantined": 0,
              "not_modified": False}],
        )
    return run_id


def ok(source="remoteok", valid=100, seen=100, quarantined=0, not_modified=False):
    return TaskOutcome(
        task_key=f"{source}:all", source=source, ok=True, not_modified=not_modified,
        items_seen=seen, items_valid=valid, items_quarantined=quarantined,
    )


class TestBaselines:
    def test_uses_the_median_of_recent_successful_runs(self, engine):
        for n in (100, 110, 90, 105, 95):
            seed_run(engine, "remoteok", n)
        assert source_baselines(engine)["remoteok"] == 100

    def test_one_anomalous_run_does_not_move_the_threshold_much(self, engine):
        # A mean would be dragged down by the bad day and mask the next break.
        for n in (100, 100, 100, 100, 0):
            seed_run(engine, "remoteok", n)
        assert source_baselines(engine)["remoteok"] == 100

    def test_failed_runs_are_excluded_from_the_baseline(self, engine):
        for n in (100, 100, 100):
            seed_run(engine, "remoteok", n)
        for _ in range(3):
            seed_run(engine, "remoteok", 0, status="failed")
        assert source_baselines(engine)["remoteok"] == 100

    def test_no_history_yields_no_baseline(self, engine):
        assert source_baselines(engine) == {}


class TestQuarantineRate:
    def test_passes_below_the_threshold(self, engine, settings):
        health = evaluate_health(engine, settings, "r", [ok(seen=100, valid=90, quarantined=10)])
        assert health["status"] == "succeeded"

    def test_fails_above_the_threshold(self, engine, settings):
        health = evaluate_health(engine, settings, "r", [ok(seen=100, valid=60, quarantined=40)])
        assert health["status"] == "failed"
        assert "quarantine_rate" in health["failed_checks"]

    def test_the_detail_names_the_actual_counts(self, engine, settings):
        health = evaluate_health(engine, settings, "r", [ok(seen=100, valid=60, quarantined=40)])
        check = next(c for c in health["checks"] if c["name"] == "quarantine_rate")
        assert "40 of 100" in check["detail"]


class TestEmptyRun:
    def test_a_run_that_stores_nothing_fails(self, engine, settings):
        health = evaluate_health(engine, settings, "r", [ok(valid=0, seen=0)])
        assert health["status"] == "failed"
        assert "non_empty" in health["failed_checks"]

    def test_an_all_304_run_is_not_treated_as_empty(self, engine, settings):
        # Nothing changed at the source is a success, not a data loss.
        health = evaluate_health(
            engine, settings, "r", [ok(valid=0, seen=0, not_modified=True)]
        )
        assert health["status"] == "succeeded"


class TestParserHealth:
    def test_a_collapse_against_an_established_baseline_fails(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "remoteok", 100)
        health = evaluate_health(engine, settings, "r", [ok(valid=3, seen=3)])
        assert health["status"] == "failed"
        assert "parser_health:remoteok" in health["failed_checks"]

    def test_the_message_explains_what_a_collapse_usually_means(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "remoteok", 100)
        health = evaluate_health(engine, settings, "r", [ok(valid=3, seen=3)])
        check = next(c for c in health["checks"] if c["name"] == "parser_health:remoteok")
        assert "changed its markup" in check["detail"]
        assert "median of recent runs is 100" in check["detail"]

    def test_normal_variation_passes(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "remoteok", 100)
        health = evaluate_health(engine, settings, "r", [ok(valid=85, seen=85)])
        assert health["status"] == "succeeded"

    def test_a_small_baseline_is_not_used_as_a_threshold(self, engine, settings):
        # A source that normally returns 4 rows returning 1 is noise, not news.
        for _ in range(5):
            seed_run(engine, "hackernews", 4)
        health = evaluate_health(engine, settings, "r", [ok(source="hackernews", valid=1, seen=1)])
        assert "parser_health:hackernews" not in health["failed_checks"]

    def test_a_304_source_is_exempt(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "weworkremotely", 100)
        health = evaluate_health(
            engine, settings, "r",
            [ok(source="weworkremotely", valid=0, seen=0, not_modified=True)],
        )
        assert "parser_health:weworkremotely" not in health["failed_checks"]

    def test_each_source_is_judged_against_its_own_history(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "remoteok", 100)
            seed_run(engine, "weworkremotely", 200)
        health = evaluate_health(
            engine, settings, "r",
            [ok(source="remoteok", valid=100), ok(source="weworkremotely", valid=5)],
        )
        assert health["failed_checks"] == ["parser_health:weworkremotely"]

    def test_the_ratio_is_configurable(self, engine, settings):
        for _ in range(5):
            seed_run(engine, "remoteok", 100)
        strict = dataclasses.replace(settings, parser_health_ratio=0.9)
        health = evaluate_health(engine, strict, "r", [ok(valid=85, seen=85)])
        assert "parser_health:remoteok" in health["failed_checks"]


class TestPartialRuns:
    def test_a_failed_task_with_sound_data_is_partial_not_failed(self, engine, settings):
        outcomes = [
            ok(valid=100),
            TaskOutcome("remoteok:tag:dev", "remoteok", ok=False, error="503 after 5 attempts"),
        ]
        health = evaluate_health(engine, settings, "r", outcomes)
        assert health["status"] == "partial"
        assert health["failed_tasks"][0]["error"].startswith("503")

    def test_a_clean_run_succeeds(self, engine, settings):
        assert evaluate_health(engine, settings, "r", [ok()])["status"] == "succeeded"

    def test_per_source_counts_are_reported(self, engine, settings):
        health = evaluate_health(
            engine, settings, "r",
            [ok(source="remoteok", valid=10), ok(source="hackernews", valid=20)],
        )
        assert health["per_source"] == {"remoteok": 10, "hackernews": 20}
