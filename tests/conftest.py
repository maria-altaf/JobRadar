"""Shared test fixtures.

Tests run against SQLite so CI needs no database service, but every statement
under test is dialect-neutral or goes through the helpers in ``db.py`` that
branch on dialect. The production path on Postgres is exercised separately by
the scheduled run itself.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from jobradar.config import Settings
from jobradar.db import ensure_schema, make_engine
from jobradar.net import FetchResult
from jobradar.sources.base import Task

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        user_agent="jobradar-test/1.0 (tests)",
        max_concurrency=4,
        http_timeout=5.0,
        max_attempts=3,
        backoff_base=0.01,
        backoff_cap=0.05,
        respect_robots=False,
        hn_threads=2,
        wwr_categories=("remote-programming-jobs",),
        remoteok_tags=("dev",),
        max_quarantine_rate=0.25,
        parser_health_ratio=0.2,
        stale_after_hours=30,
        task_max_attempts=3,
        enabled_sources=("hackernews", "weworkremotely", "remoteok"),
    )


@pytest.fixture
def engine(settings):
    eng = make_engine(settings.database_url)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


def make_result(text: str, status: int = 200, **headers) -> FetchResult:
    return FetchResult(url="https://example.test/", status=status, text=text, headers=headers)


@pytest.fixture
def hn_task() -> Task:
    return Task(
        task_key="hackernews:thread:49522897",
        source="hackernews",
        url="https://news.ycombinator.com/item",
        params={"id": "49522897"},
        conditional=False,
        meta={"thread_id": "49522897"},
    )


@pytest.fixture
def wwr_task() -> Task:
    return Task(
        task_key="weworkremotely:remote-programming-jobs",
        source="weworkremotely",
        url="https://weworkremotely.com/categories/remote-programming-jobs.rss",
        meta={"category": "remote-programming-jobs"},
    )


@pytest.fixture
def remoteok_task() -> Task:
    return Task(
        task_key="remoteok:all",
        source="remoteok",
        url="https://remoteok.com/api",
        meta={"tag": None},
    )
