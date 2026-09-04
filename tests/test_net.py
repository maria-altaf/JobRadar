"""Rate limiting, retry/backoff and robots compliance."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC

import httpx
import pytest
import respx

from jobradar.net import (
    Fetcher,
    FetchError,
    RateLimiter,
    backoff_delay,
    parse_retry_after,
)

URL = "https://example.test/data"


class TestBackoff:
    def test_never_exceeds_the_exponential_for_the_attempt(self):
        for attempt in range(1, 8):
            ceiling = min(30.0, 1.0 * 2 ** (attempt - 1))
            for _ in range(200):
                assert 0.0 <= backoff_delay(attempt, 1.0, 30.0) <= ceiling

    def test_is_capped(self):
        assert all(backoff_delay(20, 1.0, 5.0) <= 5.0 for _ in range(200))

    def test_grows_with_the_attempt_number(self):
        early = max(backoff_delay(1, 1.0, 60.0) for _ in range(300))
        late = max(backoff_delay(5, 1.0, 60.0) for _ in range(300))
        assert late > early

    def test_is_jittered_rather_than_fixed(self):
        # Undithered backoff makes concurrent tasks retry in lockstep and
        # re-trigger the same 429, so the spread is the point.
        values = {backoff_delay(4, 1.0, 60.0) for _ in range(200)}
        assert len(values) > 100


class TestRetryAfter:
    def test_parses_seconds(self):
        assert parse_retry_after("120") == 120.0

    def test_parses_an_http_date(self):
        from datetime import datetime, timedelta
        from email.utils import format_datetime

        when = datetime.now(UTC) + timedelta(seconds=60)
        assert 30 <= parse_retry_after(format_datetime(when)) <= 90

    def test_a_date_in_the_past_is_zero_not_negative(self):
        assert parse_retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "soon", "not-a-date"])
    def test_unusable_values_yield_none(self, value):
        assert parse_retry_after(value) is None


class TestRateLimiter:
    async def test_spaces_consecutive_requests_to_one_host(self):
        limiter = RateLimiter()
        limiter.gate("slow.test").interval = 0.15

        async def hit():
            async with limiter.slot("slow.test"):
                pass

        started = time.monotonic()
        await asyncio.gather(*(hit() for _ in range(4)))
        # Four requests at 0.15s spacing means at least three gaps.
        assert time.monotonic() - started >= 0.45

    async def test_different_hosts_do_not_block_each_other(self):
        limiter = RateLimiter()
        limiter.gate("slow.test").interval = 0.3
        limiter.gate("fast.test").interval = 0.0

        async def hit(host):
            async with limiter.slot(host):
                pass

        started = time.monotonic()
        await asyncio.gather(hit("slow.test"), *(hit("fast.test") for _ in range(5)))
        # The fast host must not have been serialised behind the slow one.
        assert time.monotonic() - started < 0.3

    def test_tighten_adopts_a_stricter_interval_only(self):
        limiter = RateLimiter()
        limiter.gate("h.test").interval = 5.0
        limiter.tighten("h.test", 30.0)
        assert limiter.gate("h.test").interval == 30.0
        limiter.tighten("h.test", 1.0)
        assert limiter.gate("h.test").interval == 30.0


class TestFetcherRetries:
    @respx.mock
    async def test_retries_a_500_then_succeeds(self, settings):
        route = respx.get(URL).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(200, text="ok"),
            ]
        )
        async with Fetcher(settings) as f:
            result = await f.get(URL)
        assert result.text == "ok"
        assert route.call_count == 3
        assert f.retries_made == 2

    @respx.mock
    async def test_retries_a_429(self, settings):
        respx.get(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, text="ok")]
        )
        async with Fetcher(settings) as f:
            assert (await f.get(URL)).text == "ok"

    @respx.mock
    async def test_retries_a_transport_error(self, settings):
        respx.get(URL).mock(
            side_effect=[httpx.ConnectError("dropped"), httpx.Response(200, text="ok")]
        )
        async with Fetcher(settings) as f:
            assert (await f.get(URL)).text == "ok"

    @respx.mock
    async def test_retries_a_timeout(self, settings):
        respx.get(URL).mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, text="ok")]
        )
        async with Fetcher(settings) as f:
            assert (await f.get(URL)).text == "ok"

    @respx.mock
    async def test_gives_up_after_max_attempts(self, settings):
        route = respx.get(URL).mock(return_value=httpx.Response(503))
        async with Fetcher(settings) as f:
            with pytest.raises(FetchError) as exc:
                await f.get(URL)
        assert route.call_count == settings.max_attempts
        assert not exc.value.permanent  # worth retrying on the next run

    @respx.mock
    async def test_does_not_retry_a_404(self, settings):
        route = respx.get(URL).mock(return_value=httpx.Response(404))
        async with Fetcher(settings) as f:
            with pytest.raises(FetchError) as exc:
                await f.get(URL)
        assert route.call_count == 1, "a 404 will not fix itself"
        assert exc.value.permanent

    @respx.mock
    async def test_does_not_retry_a_403(self, settings):
        route = respx.get(URL).mock(return_value=httpx.Response(403))
        async with Fetcher(settings) as f:
            with pytest.raises(FetchError):
                await f.get(URL)
        assert route.call_count == 1

    @respx.mock
    async def test_counts_requests(self, settings):
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        async with Fetcher(settings) as f:
            await f.get(URL)
            await f.get(URL)
        assert f.requests_made == 2


class TestConditionalRequests:
    @respx.mock
    async def test_304_is_reported_as_not_modified(self, settings):
        respx.get(URL).mock(return_value=httpx.Response(304))
        async with Fetcher(settings) as f:
            result = await f.get(URL, etag='W/"abc"')
        assert result.not_modified and result.status == 304

    @respx.mock
    async def test_sends_the_conditional_headers(self, settings):
        route = respx.get(URL).mock(return_value=httpx.Response(200, text="x"))
        async with Fetcher(settings) as f:
            await f.get(URL, etag='W/"abc"', last_modified="Mon, 01 Sep 2026 00:00:00 GMT")
        request = route.calls[0].request
        assert request.headers["if-none-match"] == 'W/"abc"'
        assert request.headers["if-modified-since"] == "Mon, 01 Sep 2026 00:00:00 GMT"

    @respx.mock
    async def test_exposes_the_etag_for_storage(self, settings):
        respx.get(URL).mock(return_value=httpx.Response(200, text="x", headers={"ETag": 'W/"z"'}))
        async with Fetcher(settings) as f:
            assert (await f.get(URL)).etag == 'W/"z"'


class TestRobots:
    @respx.mock
    async def test_refuses_a_disallowed_path(self, settings):
        polite = dataclasses.replace(settings, respect_robots=True)
        respx.get("https://example.test/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
        )
        async with Fetcher(polite) as f:
            with pytest.raises(FetchError, match="robots.txt disallows"):
                await f.get("https://example.test/private/data")

    @respx.mock
    async def test_adopts_a_published_crawl_delay(self, settings):
        # Hacker News publishes Crawl-delay: 30; honouring it is the whole
        # reason per-host rate limiting exists here.
        polite = dataclasses.replace(settings, respect_robots=True)
        respx.get("https://example.test/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 30\nAllow: /")
        )
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        async with Fetcher(polite) as f:
            await f.get(URL)
            assert f.limiter.gate("example.test").interval == 30.0

    @respx.mock
    async def test_an_unavailable_robots_file_does_not_stop_the_run(self, settings):
        polite = dataclasses.replace(settings, respect_robots=True)
        respx.get("https://example.test/robots.txt").mock(return_value=httpx.Response(500))
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        async with Fetcher(polite) as f:
            assert (await f.get(URL)).text == "ok"

    @respx.mock
    async def test_robots_is_fetched_once_per_host(self, settings):
        polite = dataclasses.replace(settings, respect_robots=True)
        robots = respx.get("https://example.test/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
        )
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        async with Fetcher(polite) as f:
            await f.get(URL)
            await f.get(URL)
            await f.get(URL)
        assert robots.call_count == 1
