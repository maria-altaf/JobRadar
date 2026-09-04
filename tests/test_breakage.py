"""End-to-end: what happens when a source changes underneath us.

The unit tests check the pieces. These drive the whole pipeline through
``execute_run`` against mocked HTTP and assert on the *run's verdict*, because
the claim being made is not "a function returns False" — it is "the pipeline
notices and refuses to call the run a success".

Each test seeds a few healthy runs first, so the parser-health check has a
trailing median to judge against, then breaks the source in one specific way.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
import respx
from sqlalchemy import func, select

from jobradar.config import Settings
from jobradar.db import jobs, runs
from jobradar.pipeline import execute_run

API = "https://remoteok.com/api"


@pytest.fixture
def solo(settings: Settings) -> Settings:
    """One source, one endpoint: the smallest thing that exercises the whole run."""
    return dataclasses.replace(
        settings, enabled_sources=("remoteok",), remoteok_tags=(), respect_robots=False
    )


def feed(n: int, start: int = 1) -> str:
    """A well-formed Remote OK response carrying ``n`` postings."""
    body = [{"legal": "API Terms of Service: ...", "last_updated": 1788462746}]
    body += [
        {
            "id": str(start + i),
            "slug": f"role-{start + i}",
            "date": "2026-09-03T03:36:21+00:00",
            "company": f"Company {start + i}",
            "position": "Backend Engineer",
            "location": "Remote",
            "tags": ["backend"],
            "description": "<p>Work.</p>",
            "url": f"https://remoteok.com/remote-jobs/role-{start + i}",
        }
        for i in range(n)
    ]
    return json.dumps(body)


async def seed_healthy(engine, solo, runs_count: int = 4, size: int = 40) -> None:
    """Establish a trailing median for the source."""
    for n in range(runs_count):
        respx.get(API).mock(return_value=httpx.Response(200, text=feed(size, start=1 + n * 1000)))
        report = await execute_run(engine, solo, trigger="seed")
        assert report.status == "succeeded", report.health


def last_run(engine) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(select(runs).order_by(runs.c.started_at.desc()).limit(1))
            .mappings()
            .one()
        )


def job_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(jobs)).scalar_one()


class TestSourceGoesQuiet:
    """The dangerous case: a successful fetch that yields nothing."""

    @respx.mock
    async def test_a_source_that_returns_no_postings_fails_the_run(self, engine, solo):
        await seed_healthy(engine, solo)

        # HTTP 200, valid JSON, parses cleanly -- and zero postings.
        respx.get(API).mock(return_value=httpx.Response(200, text=feed(0)))
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert "non_empty" in report.health["failed_checks"]

    @respx.mock
    async def test_a_severe_drop_fails_even_though_data_arrived(self, engine, solo):
        await seed_healthy(engine, solo, size=40)

        respx.get(API).mock(return_value=httpx.Response(200, text=feed(2, start=9000)))
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert "parser_health:remoteok" in report.health["failed_checks"]

    @respx.mock
    async def test_the_failure_explains_itself(self, engine, solo):
        await seed_healthy(engine, solo, size=40)
        respx.get(API).mock(return_value=httpx.Response(200, text=feed(2, start=9000)))
        report = await execute_run(engine, solo, trigger="broken")

        check = next(
            c for c in report.health["checks"] if c["name"] == "parser_health:remoteok"
        )
        assert "median of recent runs is 40" in check["detail"]
        assert "changed its markup" in check["detail"]

    @respx.mock
    async def test_a_normal_fluctuation_still_passes(self, engine, solo):
        await seed_healthy(engine, solo, size=40)
        respx.get(API).mock(return_value=httpx.Response(200, text=feed(34, start=9000)))
        report = await execute_run(engine, solo, trigger="normal")
        assert report.status == "succeeded"

    @respx.mock
    async def test_the_failed_run_is_recorded_as_failed(self, engine, solo):
        await seed_healthy(engine, solo)
        respx.get(API).mock(return_value=httpx.Response(200, text=feed(0)))
        await execute_run(engine, solo, trigger="broken")
        assert last_run(engine)["status"] == "failed"


class TestShapeChange:
    @respx.mock
    async def test_an_object_where_an_array_was_expected_fails_the_task(self, engine, solo):
        await seed_healthy(engine, solo)

        respx.get(API).mock(return_value=httpx.Response(200, text=json.dumps({"jobs": []})))
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert report.tasks_failed == 1
        assert any("shape has changed" in (e or "") for e in report.errors)

    @respx.mock
    async def test_an_html_error_page_fails_the_task(self, engine, solo):
        # A site under maintenance answering 200 with HTML is a real failure mode.
        await seed_healthy(engine, solo)
        respx.get(API).mock(
            return_value=httpx.Response(200, text="<html><body>Down for maintenance</body></html>")
        )
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert any("not valid JSON" in (e or "") for e in report.errors)

    @respx.mock
    async def test_a_shape_change_stores_nothing_rather_than_garbage(self, engine, solo):
        await seed_healthy(engine, solo, runs_count=2, size=10)
        before = job_count(engine)

        respx.get(API).mock(return_value=httpx.Response(200, text=json.dumps({"jobs": []})))
        await execute_run(engine, solo, trigger="broken")

        assert job_count(engine) == before, "a broken parse must not write partial rows"


class TestDataDegradation:
    @respx.mock
    async def test_a_flood_of_invalid_records_fails_the_run(self, engine, solo):
        await seed_healthy(engine, solo)

        # Every posting arrives with no company: parseable, but not storable.
        body = [{"legal": "..."}] + [
            {"id": str(5000 + i), "company": "", "position": "Engineer",
             "url": f"https://remoteok.com/remote-jobs/{5000 + i}"}
            for i in range(40)
        ]
        respx.get(API).mock(return_value=httpx.Response(200, text=json.dumps(body)))
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert "quarantine_rate" in report.health["failed_checks"]
        assert report.items_quarantined == 40

    @respx.mock
    async def test_the_bad_records_are_kept_with_reasons(self, engine, solo):
        from jobradar.db import quarantine

        await seed_healthy(engine, solo, runs_count=2, size=10)
        body = [{"legal": "..."}] + [
            {"id": "5000", "company": "", "position": "Engineer",
             "url": "https://remoteok.com/remote-jobs/5000"}
        ]
        respx.get(API).mock(return_value=httpx.Response(200, text=json.dumps(body)))
        await execute_run(engine, solo, trigger="broken")

        with engine.connect() as conn:
            row = conn.execute(
                select(quarantine).order_by(quarantine.c.id.desc()).limit(1)
            ).mappings().one()
        assert "company" in row["reason"]
        assert row["raw"]["id"] == "5000", "the original payload is preserved"


class TestTransportFailure:
    @respx.mock
    async def test_a_source_that_is_completely_down_fails_the_run(self, engine, solo):
        await seed_healthy(engine, solo)
        respx.get(API).mock(return_value=httpx.Response(503))
        report = await execute_run(engine, solo, trigger="broken")

        assert report.status == "failed"
        assert report.tasks_failed == 1

    @respx.mock
    async def test_a_recoverable_blip_does_not_fail_the_run(self, engine, solo):
        await seed_healthy(engine, solo, size=40)
        respx.get(API).mock(
            side_effect=[
                httpx.Response(503),
                httpx.ConnectError("dropped"),
                httpx.Response(200, text=feed(40, start=9000)),
            ]
        )
        report = await execute_run(engine, solo, trigger="flaky")

        assert report.status == "succeeded"
        assert report.retries_made == 2, "it recovered by retrying, not by luck"

    @respx.mock
    async def test_previously_stored_data_survives_a_total_outage(self, engine, solo):
        await seed_healthy(engine, solo, runs_count=2, size=10)
        before = job_count(engine)

        respx.get(API).mock(return_value=httpx.Response(503))
        await execute_run(engine, solo, trigger="broken")

        assert job_count(engine) == before, "an outage must not remove what we already had"
