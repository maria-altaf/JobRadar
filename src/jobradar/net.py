"""HTTP layer: per-host rate limiting, robots.txt compliance, retries.

Three separate concerns are stacked here, outermost first:

1. **robots.txt** -- fetched once per host per process and consulted before
   every request. A site's published ``Crawl-delay`` *overrides* our configured
   interval when it is stricter, so politeness is driven by what the operator
   actually asked for rather than by a constant we guessed. Hacker News asks for
   30 seconds; that single directive dominates the wall-clock of a run.

2. **Rate limiting** -- a per-host gate spacing the *start* of consecutive
   requests, plus a per-host concurrency cap, plus a global cap. Hosts are
   independent, so the slow one (HN) does not stall the fast ones.

3. **Retries** -- exponential backoff with full jitter on transport errors and
   the retryable status codes, honouring ``Retry-After`` when the server sends
   it. Non-retryable 4xx failures raise immediately instead of burning the
   budget on a request that will never succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC
from urllib.parse import urlparse

import httpx

from .config import Settings, policy_for

log = logging.getLogger(__name__)

#: Statuses worth trying again. 429 is rate limiting, 5xx are transient server
#: faults, 408/425 are the server telling us to re-send.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Robots is advisory infrastructure, not data. If it cannot be fetched we fail
#: open (allow) but keep the configured interval -- refusing to run because a
#: robots.txt 500'd would be a self-inflicted outage.
ROBOTS_TIMEOUT = 10.0


class FetchError(Exception):
    """A request that could not be completed.

    ``permanent`` distinguishes "this will never work" (404, 403, a parse-level
    4xx) from "we ran out of attempts" -- the pipeline retries the latter on the
    next run and gives up loudly on the former.
    """

    def __init__(self, message: str, *, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    #: True when the server answered 304 and there is nothing new to parse.
    not_modified: bool = False
    attempts: int = 1
    elapsed: float = 0.0

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass
class _HostGate:
    interval: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sem: asyncio.Semaphore | None = None
    next_allowed: float = 0.0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_loaded: bool = False


class RateLimiter:
    """Spaces request starts per host and caps concurrency per host."""

    def __init__(self) -> None:
        self._gates: dict[str, _HostGate] = {}

    def gate(self, host: str) -> _HostGate:
        gate = self._gates.get(host)
        if gate is None:
            pol = policy_for(host)
            gate = _HostGate(interval=pol.min_interval)
            gate.sem = asyncio.Semaphore(pol.max_concurrency)
            self._gates[host] = gate
        return gate

    def tighten(self, host: str, interval: float) -> None:
        """Adopt a stricter interval (e.g. a robots.txt ``Crawl-delay``)."""
        gate = self.gate(host)
        if interval > gate.interval:
            log.info(
                "rate limit for %s tightened %.1fs -> %.1fs (robots.txt Crawl-delay)",
                host,
                gate.interval,
                interval,
            )
            gate.interval = interval

    @contextlib.asynccontextmanager
    async def slot(self, host: str):
        gate = self.gate(host)
        assert gate.sem is not None
        await gate.sem.acquire()
        try:
            # Holding the lock across the sleep is what enforces the spacing:
            # only one coroutine at a time may claim the next departure slot.
            async with gate.lock:
                now = time.monotonic()
                wait = gate.next_allowed - now
                if wait > 0:
                    log.debug("rate limit: waiting %.1fs for %s", wait, host)
                    await asyncio.sleep(wait)
                gate.next_allowed = max(gate.next_allowed, time.monotonic()) + gate.interval
            yield
        finally:
            gate.sem.release()


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter.

    ``random.uniform(0, exp)`` rather than ``exp`` on purpose: when several
    tasks are throttled by the same host at the same moment, undithered backoff
    makes them retry in lockstep and re-trigger the same 429.
    """
    exponential = min(cap, base * (2 ** max(0, attempt - 1)))
    return random.uniform(0.0, exponential)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class Fetcher:
    """An httpx client wrapped in politeness and persistence."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.limiter = RateLimiter()
        self._global_sem = asyncio.Semaphore(settings.max_concurrency)
        self._client: httpx.AsyncClient | None = None
        self.requests_made = 0
        self.retries_made = 0

    async def __aenter__(self) -> Fetcher:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=httpx.Timeout(self.settings.http_timeout),
            follow_redirects=True,
            # A daily run makes ~16 requests; a small pool is plenty.
            limits=httpx.Limits(max_connections=self.settings.max_concurrency),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- robots --

    async def _ensure_robots(self, host: str, scheme: str) -> None:
        gate = self.limiter.gate(host)
        if gate.robots_loaded:
            return
        gate.robots_loaded = True  # only ever attempt once per process
        if not self.settings.respect_robots:
            return
        assert self._client is not None
        parser = urllib.robotparser.RobotFileParser()
        url = f"{scheme}://{host}/robots.txt"
        try:
            resp = await self._client.get(url, timeout=ROBOTS_TIMEOUT)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                gate.robots = parser
                delay = parser.crawl_delay(_ua_token(self.settings.user_agent))
                if delay:
                    self.limiter.tighten(host, float(delay))
            else:
                log.warning("robots.txt for %s returned %s; proceeding", host, resp.status_code)
        except Exception as exc:  # noqa: BLE001 - robots must never break a run
            log.warning("robots.txt for %s unavailable (%s); proceeding", host, exc)

    def _robots_allows(self, host: str, url: str) -> bool:
        gate = self.limiter.gate(host)
        if not self.settings.respect_robots or gate.robots is None:
            return True
        return gate.robots.can_fetch(_ua_token(self.settings.user_agent), url)

    # --------------------------------------------------------------- fetch --

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """GET with robots compliance, rate limiting and retry.

        Passing ``etag``/``last_modified`` turns the call into a conditional
        request; a 304 comes back as ``not_modified`` with no body, which the
        pipeline treats as "nothing changed" rather than "no data".
        """
        assert self._client is not None, "Fetcher must be used as an async context manager"
        parsed = urlparse(url)
        host = parsed.netloc
        await self._ensure_robots(host, parsed.scheme or "https")

        if not self._robots_allows(host, url):
            raise FetchError(f"robots.txt disallows {url}", permanent=True)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        started = time.monotonic()
        last_exc: Exception | None = None

        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                async with self._global_sem, self.limiter.slot(host):
                    self.requests_made += 1
                    resp = await self._client.get(url, params=params, headers=headers)

                if resp.status_code == 304:
                    return FetchResult(
                        url=str(resp.url),
                        status=304,
                        text="",
                        headers=dict(resp.headers),
                        not_modified=True,
                        attempts=attempt,
                        elapsed=time.monotonic() - started,
                    )

                if resp.status_code in RETRYABLE_STATUSES:
                    if attempt == self.settings.max_attempts:
                        raise FetchError(
                            f"{resp.status_code} after {attempt} attempts",
                            status=resp.status_code,
                        )
                    delay = backoff_delay(
                        attempt, self.settings.backoff_base, self.settings.backoff_cap
                    )
                    # A server that tells us how long to wait outranks our guess.
                    ra = parse_retry_after(resp.headers.get("retry-after"))
                    if ra is not None:
                        delay = max(delay, min(ra, self.settings.backoff_cap))
                    self.retries_made += 1
                    log.warning(
                        "%s %s -> retry %d/%d in %.1fs",
                        resp.status_code,
                        url,
                        attempt,
                        self.settings.max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code >= 400:
                    # 403/404/410 etc: retrying cannot help.
                    raise FetchError(
                        f"{resp.status_code} {resp.reason_phrase} for {url}",
                        status=resp.status_code,
                        permanent=True,
                    )

                return FetchResult(
                    url=str(resp.url),
                    status=resp.status_code,
                    text=resp.text,
                    headers=dict(resp.headers),
                    attempts=attempt,
                    elapsed=time.monotonic() - started,
                )

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == self.settings.max_attempts:
                    break
                delay = backoff_delay(
                    attempt, self.settings.backoff_base, self.settings.backoff_cap
                )
                self.retries_made += 1
                log.warning(
                    "%s on %s -> retry %d/%d in %.1fs",
                    type(exc).__name__,
                    url,
                    attempt,
                    self.settings.max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

        raise FetchError(
            f"{type(last_exc).__name__ if last_exc else 'exhausted'} after "
            f"{self.settings.max_attempts} attempts for {url}: {last_exc}"
        )


def _ua_token(user_agent: str) -> str:
    """The bare product token robots.txt matching should use."""
    return user_agent.split("/", 1)[0].strip() or "*"


__all__ = [
    "Fetcher",
    "FetchResult",
    "FetchError",
    "RateLimiter",
    "backoff_delay",
    "parse_retry_after",
    "RETRYABLE_STATUSES",
]
