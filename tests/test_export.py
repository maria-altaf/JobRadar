"""The static site build.

Netlify cannot run Python, so the dashboard is exported rather than served.
The risk that introduces is a page whose health indicator was true when it was
built and is a lie by the time anyone reads it — so these tests care most about
the timestamps being carried into the page in a form the browser can re-derive.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
from datetime import timedelta

import pytest
from sqlalchemy import update

import jobradar.render
from jobradar.db import runs, utcnow
from jobradar.export import export_site
from jobradar.models import JobPosting
from jobradar.storage import insert_quarantine, upsert_jobs

from .test_health import seed_run


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


@pytest.fixture
def populated(engine):
    """A database with a successful run and some postings behind it."""
    seed_run(engine, "remoteok", 3)
    with engine.begin() as conn:
        upsert_jobs(conn, "run-1", [posting(str(i)) for i in range(1, 6)])
        insert_quarantine(
            conn, "run-1", "remoteok", "remoteok:all",
            [{"external_id": "9", "reason": "title: too short (got '')",
              "code": "title:string_too_short", "errors": [{"field": "title"}],
              "raw": {"position": ""}}],
        )
    return engine


@pytest.fixture
def site(populated, settings, tmp_path):
    out = tmp_path / "dist"
    summary = export_site(populated, settings, out)
    return out, summary


class TestNoWebFrameworkNeeded:
    """The exporter must render without a web framework installed.

    The static build installs only ``requirements.txt``, which has no FastAPI in
    it — the dashboard's dependencies are not the scraper's. Importing the
    FastAPI app to reach ``render_page`` therefore killed the Netlify build with
    ``ModuleNotFoundError: No module named 'fastapi'``, which is why rendering
    lives in ``jobradar.render`` and imports nothing from the web layer.
    """

    def test_export_imports_cleanly_with_fastapi_unavailable(self):
        # A subprocess with fastapi blocked at import time, which is the
        # honest simulation of the build environment.
        code = textwrap.dedent(
            """
            import sys

            class BlockFastAPI:
                def find_spec(self, name, path=None, target=None):
                    if name == "fastapi" or name.startswith("fastapi."):
                        raise ImportError("fastapi is not installed in the static build")
                    return None

            sys.meta_path.insert(0, BlockFastAPI())

            import jobradar.render      # noqa: F401
            import jobradar.export      # noqa: F401
            import jobradar.cli         # noqa: F401
            from jobradar.render import render_page  # noqa: F401
            print("IMPORTS-OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert "IMPORTS-OK" in result.stdout, (
            "the exporter still pulls in a web framework:\n" + result.stderr[-1500:]
        )

    def test_render_module_does_not_import_the_web_layer(self):
        source = pathlib.Path(jobradar.render.__file__).read_text(encoding="utf-8")
        code_lines = [
            ln for ln in source.splitlines()
            if ln.startswith(("import ", "from ")) and "fastapi" in ln
        ]
        assert not code_lines, f"render.py must not import fastapi: {code_lines}"

    def test_the_live_app_and_the_export_render_the_same_page(self, populated, settings, tmp_path):
        # Two deployment paths, one renderer. If these ever diverge, the static
        # site silently stops being the thing that was tested.
        from jobradar import queries
        from jobradar.render import render_page

        with populated.connect() as conn:
            snap = queries.snapshot(conn, settings.stale_after_hours)
            jobs_ = queries.recent_jobs(conn, limit=30)
        direct = render_page(snap, jobs_)

        export_site(populated, settings, tmp_path / "dist")
        exported = (tmp_path / "dist" / "index.html").read_text(encoding="utf-8")

        # Timestamps differ by milliseconds between the two calls; compare the
        # structure that matters instead of demanding byte equality.
        for marker in ("<title>", "health-banner", "New postings per day", "Backend Engineer"):
            assert marker in direct and marker in exported


class TestWhatGetsWritten:
    def test_the_expected_files_exist(self, site):
        out, _ = site
        for name in ("index.html", "api/health.json", "api/stats.json",
                     "api/jobs.json", "_redirects", "_headers"):
            assert (out / name).exists(), f"{name} was not written"

    def test_the_summary_reports_what_it_did(self, site):
        _, summary = site
        assert summary["jobs"] == 5
        assert summary["bytes"] > 0
        assert "index.html" in summary["files"]

    def test_it_creates_the_output_directory(self, populated, settings, tmp_path):
        out = tmp_path / "nested" / "deeper" / "dist"
        export_site(populated, settings, out)
        assert (out / "index.html").exists()

    def test_it_can_be_run_twice_over_the_same_directory(self, populated, settings, tmp_path):
        out = tmp_path / "dist"
        export_site(populated, settings, out)
        export_site(populated, settings, out)
        assert (out / "index.html").exists()


class TestThePage:
    def test_it_is_a_complete_standalone_document(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "</html>" in html
        # No CDN, no build step: everything the page needs is inlined, so it
        # renders from a file:// URL and cannot be broken by a third party.
        assert "<style>" in html and "<script>" in html
        assert "http-equiv" not in html
        for external in ("cdn.", "unpkg", "jsdelivr", "googleapis"):
            assert external not in html, f"unexpected external dependency: {external}"

    def test_it_shows_the_postings(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert "Backend Engineer" in html
        assert "Acme" in html

    def test_it_shows_quarantine_reasons(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert "title: too short" in html

    def test_it_credits_remote_ok(self, site):
        # Required by Remote OK's API terms, and the link must not be nofollow.
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert "remoteok.com" in html
        assert "nofollow" not in html


class TestFreshness:
    """The static build's one real hazard: a health indicator frozen at green."""

    def test_the_page_carries_a_machine_readable_last_success(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert 'data-last-success="' in html
        assert 'data-stale-hours="' in html

    def test_the_page_recomputes_the_age_in_the_browser(self, site):
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert 'data-role="age"' in html
        assert "function freshen()" in html
        assert "setInterval(freshen" in html

    def test_it_rechecks_when_the_tab_is_looked_at_again(self, site):
        # A dashboard gets left open overnight; waiting up to a minute after
        # returning to the tab to learn the pipeline died is too long.
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert "addEventListener('focus', freshen)" in html
        assert "visibilitychange" in html

    def test_the_page_can_turn_itself_red(self, site):
        # Without this the page would keep claiming green forever after the
        # pipeline stopped, which is the worst possible failure for a health
        # indicator: silent, and confidently wrong.
        out, _ = site
        html = (out / "index.html").read_text(encoding="utf-8")
        assert "banner-critical" in html
        assert "staleHours" in html

    def test_health_json_carries_its_own_generation_time(self, site):
        out, _ = site
        data = json.loads((out / "api/health.json").read_text(encoding="utf-8"))
        assert data["generated_at"], "a consumer must be able to judge staleness itself"
        assert data["stale_after_hours"] == 30

    def test_a_stale_database_exports_a_critical_state(self, populated, settings, tmp_path):
        with populated.begin() as conn:
            conn.execute(update(runs).values(finished_at=utcnow() - timedelta(days=5)))
        summary = export_site(populated, settings, tmp_path / "dist")
        assert summary["health"] == "critical"

        data = json.loads((tmp_path / "dist/api/health.json").read_text(encoding="utf-8"))
        assert data["state"] == "critical"
        assert "Stale" in data["detail"]


class TestTheJson:
    def test_jobs_json_is_valid_and_populated(self, site):
        out, _ = site
        data = json.loads((out / "api/jobs.json").read_text(encoding="utf-8"))
        assert data["count"] == 5
        assert len(data["jobs"]) == 5

    def test_descriptions_are_excerpted_not_shipped_whole(self, populated, settings, tmp_path):
        # 8 KB descriptions turned a 500-posting export into a 2 MB file.
        with populated.begin() as conn:
            upsert_jobs(conn, "run-2", [posting("big", description_text="x" * 8000)])
        export_site(populated, settings, tmp_path / "dist")
        data = json.loads((tmp_path / "dist/api/jobs.json").read_text(encoding="utf-8"))

        job = next(j for j in data["jobs"] if j["external_id"] == "big")
        assert "description_text" not in job
        assert len(job["description_excerpt"]) <= 280
        assert job["url"], "the excerpt is only acceptable because the full text is a click away"

    def test_stats_json_is_valid(self, site):
        out, _ = site
        data = json.loads((out / "api/stats.json").read_text(encoding="utf-8"))
        assert data["totals"]["jobs"] == 5
        assert "health" in data and "daily" in data

    def test_timestamps_are_iso_strings(self, site):
        out, _ = site
        data = json.loads((out / "api/health.json").read_text(encoding="utf-8"))
        assert "T" in data["last_success_at"]


class TestNetlifyConfig:
    def test_redirects_map_the_live_servers_api_paths(self, site):
        # The same URLs must work whether it is served live or statically.
        out, _ = site
        redirects = (out / "_redirects").read_text(encoding="utf-8")
        for path in ("/api/health", "/api/stats", "/api/jobs"):
            assert path in redirects

    def test_headers_allow_cross_origin_reads_of_the_api(self, site):
        out, _ = site
        headers = (out / "_headers").read_text(encoding="utf-8")
        assert "Access-Control-Allow-Origin: *" in headers

    def test_caching_requires_revalidation(self, site):
        # Rebuilt after every run, so a visitor must not be shown yesterday's
        # numbers from a cache.
        out, _ = site
        headers = (out / "_headers").read_text(encoding="utf-8")
        assert "must-revalidate" in headers
