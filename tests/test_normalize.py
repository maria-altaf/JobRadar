"""The identity keys that idempotency and dedup rest on."""

from __future__ import annotations

import pytest

from jobradar.normalize import (
    make_content_fingerprint,
    make_content_hash,
    make_dedup_key,
    normalize_company,
    normalize_location,
    normalize_title,
)


class TestDedupKey:
    def test_is_stable_across_calls(self):
        assert make_dedup_key("remoteok", "1137305") == make_dedup_key("remoteok", "1137305")

    def test_ignores_surrounding_whitespace_and_case_of_source(self):
        assert make_dedup_key("RemoteOK", " 1137305 ") == make_dedup_key("remoteok", "1137305")

    def test_same_id_from_different_sources_is_a_different_row(self):
        # Two sources numbering their postings from 1 must not collide.
        assert make_dedup_key("remoteok", "42") != make_dedup_key("hackernews", "42")


class TestCompanyNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Acme, Inc.", "acme"),
            ("ACME Inc", "acme"),
            ("Acme Corporation", "acme"),
            ("Acme GmbH", "acme"),
            ("Acme (YC S21)", "acme"),
            ("  Acme   Inc.  ", "acme"),
            ("The Acme Company", "acme"),
            ("Café Systems", "cafe systems"),
        ],
    )
    def test_collapses_legal_and_formatting_noise(self, raw, expected):
        assert normalize_company(raw) == expected

    def test_keeps_a_single_token_that_is_also_a_suffix(self):
        # Stripping every suffix token would erase the name entirely.
        assert normalize_company("The") == "the"
        assert normalize_company("Co") == "co"

    def test_distinct_companies_stay_distinct(self):
        assert normalize_company("Acme") != normalize_company("Acme Labs")


class TestTitleNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Senior Software Engineer (Remote)", "senior software engineer"),
            ("Senior Software Engineer - Remote", "senior software engineer"),
            ("Senior Software Engineer", "senior software engineer"),
            ("SENIOR SOFTWARE ENGINEER, Full-Time", "senior software engineer"),
        ],
    )
    def test_strips_employment_and_location_noise(self, raw, expected):
        assert normalize_title(raw) == expected


class TestLocationNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["Anywhere in the World", "Worldwide", "Remote", "REMOTE", "Anywhere", "Global"],
    )
    def test_folds_the_many_spellings_of_remote(self, raw):
        assert normalize_location(raw) == "remote"

    def test_keeps_a_real_place(self):
        assert normalize_location("Berlin, Germany") == "berlin germany"


class TestContentFingerprint:
    def test_matches_the_same_job_described_by_two_sources(self):
        # This is the cross-source dedup claim, stated as a test.
        a = make_content_fingerprint("Acme, Inc.", "Senior Software Engineer (Remote)", "Anywhere in the World")
        b = make_content_fingerprint("ACME Inc", "Senior Software Engineer", "Remote")
        assert a == b

    def test_separates_different_roles_at_the_same_company(self):
        a = make_content_fingerprint("Acme", "Backend Engineer", "Remote")
        b = make_content_fingerprint("Acme", "Frontend Engineer", "Remote")
        assert a != b

    def test_separates_the_same_role_at_different_companies(self):
        a = make_content_fingerprint("Acme", "Backend Engineer", "Remote")
        b = make_content_fingerprint("Globex", "Backend Engineer", "Remote")
        assert a != b


class TestContentHash:
    def test_is_order_independent(self):
        assert make_content_hash({"a": 1, "b": 2}) == make_content_hash({"b": 2, "a": 1})

    def test_changes_when_a_value_changes(self):
        assert make_content_hash({"salary_max": 100}) != make_content_hash({"salary_max": 200})

    def test_treats_none_and_empty_string_alike(self):
        # Sources flip between the two for absent fields; that must not read as
        # an edit and inflate the revision counter.
        assert make_content_hash({"location": None}) == make_content_hash({"location": ""})
