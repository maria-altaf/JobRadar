"""Runtime configuration, loaded from the environment.

Everything tunable lives here so that a run can be reshaped from CI without a
code change. Defaults are the values used by the scheduled production run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

#: Sent on every outbound request. Identifies the project and gives site
#: operators a way to reach a human -- the minimum courtesy for a scraper that
#: runs unattended every day.
DEFAULT_USER_AGENT = (
    "jobradar/1.0 (personal job-market data pipeline; "
    "contact: mariaaltaf792@gmail.com)"
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def normalize_database_url(url: str) -> str:
    """Force the psycopg3 driver and strip options SQLAlchemy cannot pass through.

    Neon hands out URLs like ``postgresql://u:p@host/db?sslmode=require&channel_binding=require``.
    SQLAlchemy needs the explicit ``+psycopg`` driver to avoid defaulting to psycopg2,
    and ``channel_binding`` is a libpq-only parameter that psycopg3 rejects as a
    connect argument.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class HostPolicy:
    """Politeness policy for a single host.

    ``min_interval`` is seconds between the *start* of consecutive requests to
    this host. Where a site publishes a ``Crawl-delay`` we use it verbatim; the
    values below are the ones observed in each site's robots.txt.
    """

    min_interval: float
    max_concurrency: int


#: Per-host politeness. HN publishes ``Crawl-delay: 30``, which dominates the
#: wall-clock of a run; the other hosts are fetched concurrently around it.
HOST_POLICIES: dict[str, HostPolicy] = {
    "news.ycombinator.com": HostPolicy(min_interval=30.0, max_concurrency=1),
    "hn.algolia.com": HostPolicy(min_interval=1.0, max_concurrency=2),
    "weworkremotely.com": HostPolicy(min_interval=1.5, max_concurrency=3),
    "remoteok.com": HostPolicy(min_interval=1.0, max_concurrency=2),
}

DEFAULT_HOST_POLICY = HostPolicy(min_interval=2.0, max_concurrency=2)


@dataclass(frozen=True)
class Settings:
    database_url: str
    user_agent: str

    # concurrency / networking
    max_concurrency: int
    http_timeout: float
    max_attempts: int
    backoff_base: float
    backoff_cap: float
    respect_robots: bool

    # source scope
    hn_threads: int
    wwr_categories: tuple[str, ...]
    remoteok_tags: tuple[str, ...]

    # run health thresholds -- these decide whether a run is allowed to pass
    max_quarantine_rate: float
    parser_health_ratio: float
    stale_after_hours: int

    # a task that has failed this many times across the run is given up on
    task_max_attempts: int

    enabled_sources: tuple[str, ...] = field(default=())

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


DEFAULT_WWR_CATEGORIES = (
    "remote-programming-jobs",
    "remote-devops-sysadmin-jobs",
    "remote-design-jobs",
    "remote-customer-support-jobs",
    "remote-product-jobs",
    "remote-sales-and-marketing-jobs",
    "remote-management-and-finance-jobs",
    "all-other-remote-jobs",
)

DEFAULT_REMOTEOK_TAGS = ("dev", "engineer", "design", "marketing")

ALL_SOURCES = ("hackernews", "weworkremotely", "remoteok")


def load_settings() -> Settings:
    raw_db = os.getenv("DATABASE_URL", "sqlite:///data/jobradar.db")

    wwr = os.getenv("JOBRADAR_WWR_CATEGORIES")
    tags = os.getenv("JOBRADAR_REMOTEOK_TAGS")
    sources = os.getenv("JOBRADAR_SOURCES")

    return Settings(
        database_url=normalize_database_url(raw_db),
        user_agent=os.getenv("JOBRADAR_USER_AGENT", DEFAULT_USER_AGENT),
        max_concurrency=_env_int("JOBRADAR_MAX_CONCURRENCY", 8),
        http_timeout=_env_float("JOBRADAR_HTTP_TIMEOUT", 30.0),
        max_attempts=_env_int("JOBRADAR_MAX_ATTEMPTS", 5),
        backoff_base=_env_float("JOBRADAR_BACKOFF_BASE", 1.0),
        backoff_cap=_env_float("JOBRADAR_BACKOFF_CAP", 60.0),
        respect_robots=_env_bool("JOBRADAR_RESPECT_ROBOTS", True),
        hn_threads=_env_int("JOBRADAR_HN_THREADS", 2),
        wwr_categories=tuple(c.strip() for c in wwr.split(",") if c.strip())
        if wwr
        else DEFAULT_WWR_CATEGORIES,
        remoteok_tags=tuple(t.strip() for t in tags.split(",") if t.strip())
        if tags
        else DEFAULT_REMOTEOK_TAGS,
        max_quarantine_rate=_env_float("JOBRADAR_MAX_QUARANTINE_RATE", 0.25),
        parser_health_ratio=_env_float("JOBRADAR_PARSER_HEALTH_RATIO", 0.2),
        stale_after_hours=_env_int("JOBRADAR_STALE_AFTER_HOURS", 30),
        task_max_attempts=_env_int("JOBRADAR_TASK_MAX_ATTEMPTS", 3),
        enabled_sources=tuple(s.strip() for s in sources.split(",") if s.strip())
        if sources
        else ALL_SOURCES,
    )


def policy_for(host: str) -> HostPolicy:
    return HOST_POLICIES.get(host, DEFAULT_HOST_POLICY)
