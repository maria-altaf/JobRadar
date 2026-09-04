"""Parser behaviour against fixtures captured from the real sources.

Each source gets the same three questions: does it extract the right fields
from well-formed input, does it quarantine malformed input with a usable
reason, and does it fail *loudly* when the response shape changes structurally
rather than returning an empty list.

That last one matters most. A parser that returns ``[]`` when a site changes
its markup produces a green run with no data, which is the failure mode this
whole project exists to avoid.
"""

from __future__ import annotations

import json

import pytest

from jobradar.models import JobPosting, ValidationError
from jobradar.sources.hackernews import HackerNewsSource, parse_header, role_from_body
from jobradar.sources.remoteok import RemoteOKSource
from jobradar.sources.weworkremotely import WeWorkRemotelySource

from .conftest import fixture_text, make_result


def validate_all(records):
    """Split parsed records the way the pipeline does."""
    good, bad = [], []
    for rec in records:
        if rec.parse_error:
            bad.append(rec)
            continue
        try:
            good.append(JobPosting(**rec.payload))
        except ValidationError:
            bad.append(rec)
    return good, bad


# ------------------------------------------------------------ Hacker News ---


class TestHackerNewsHeader:
    def test_company_role_location(self):
        f = parse_header("Acme | Backend Engineer | Berlin, Germany | Full-time")
        assert f["company"] == "Acme"
        assert f["title"] == "Backend Engineer"
        assert "Berlin" in f["location"]
        assert f["employment_type"] == "Full-time"

    def test_a_bare_city_does_not_become_the_title(self):
        # The regression that motivated matching roles positively.
        f = parse_header("QUOBYTE | Berlin, Germany | ONSITE (Germany)", fallback_role="SRE")
        assert f["title"] == "SRE"
        assert "Berlin, Germany" in f["location"]

    def test_company_url_segment_is_not_the_title(self):
        f = parse_header("MONUMENTAL | https://www.monumental.co/ | Amsterdam | Robotics Engineer")
        assert f["title"] == "Robotics Engineer"

    def test_role_wins_over_an_employment_type_word_inside_it(self):
        f = parse_header("Acme | Contract Counsel | Remote")
        assert f["title"] == "Contract Counsel"

    def test_role_wins_over_a_place_word_inside_it(self):
        f = parse_header("Acme | Remote Infrastructure Engineer | Berlin")
        assert f["title"] == "Remote Infrastructure Engineer"

    @pytest.mark.parametrize(
        ("header", "lo", "hi", "cur"),
        [
            ("Acme | Engineer | Remote | $80-120k", 80000, 120000, "USD"),
            ("Acme | Engineer | Remote | $175k–$225k base", 175000, 225000, "USD"),
            ("Acme | Engineer | Remote | £90k-£110k", 90000, 110000, "GBP"),
        ],
    )
    def test_salary_extraction(self, header, lo, hi, cur):
        f = parse_header(header)
        assert (f["salary_min"], f["salary_max"], f["salary_currency"]) == (lo, hi, cur)

    def test_salary_range_is_ordered_even_if_written_backwards(self):
        f = parse_header("Acme | Engineer | Remote | $200k-$150k")
        assert f["salary_min"] <= f["salary_max"]

    def test_header_without_pipes_is_rejected_with_a_reason(self):
        with pytest.raises(ValueError, match="pipe convention"):
            parse_header("We are hiring great people")

    def test_header_with_no_identifiable_role_is_rejected(self):
        with pytest.raises(ValueError, match="no role"):
            parse_header("Acme | Berlin, Germany | Full-time")


class TestRoleFromBody:
    def test_recovers_a_role_stated_on_the_next_line(self):
        assert role_from_body(["Acme | Berlin | Full-time", "Backend Engineer"]) == "Backend Engineer"

    def test_strips_a_trailing_url(self):
        got = role_from_body(["Acme | Berlin", "Software Engineer: https://acme.com/jobs/1"])
        assert got == "Software Engineer"

    def test_skips_an_apply_link_line(self):
        got = role_from_body(["Acme | Berlin", "Apply here: https://acme.com/jobs", "Data Engineer"])
        assert got == "Data Engineer"

    def test_rejects_prose(self):
        assert role_from_body(["Acme | Berlin", "We are building the future of engineering."]) is None

    def test_rejects_an_overlong_line(self):
        long_line = "Senior " + "very " * 20 + "engineer"
        assert role_from_body(["Acme | Berlin", long_line]) is None

    def test_returns_none_when_there_is_nothing_to_find(self):
        assert role_from_body(["Acme | Berlin"]) is None


class TestHackerNewsParse:
    @pytest.fixture
    def records(self, hn_task):
        return HackerNewsSource().parse(hn_task, make_result(fixture_text("hn_thread.html")))

    def test_nested_replies_are_not_treated_as_postings(self, records):
        # The fixture contains one indented reply; only top-level comments are
        # job postings, replies are discussion.
        assert len(records) == 15

    def test_most_records_survive_validation(self, records):
        good, bad = validate_all(records)
        assert len(good) >= 10
        assert len(bad) <= 5

    def test_inline_links_no_longer_truncate_the_header(self, records):
        # Regression: get_text("\n") used to cut "Sudowrite | <a>..." at the
        # first inline tag, leaving the header as "Sudowrite |".
        good, _ = validate_all(records)
        titles = {p.title for p in good}
        assert not any(t.endswith("|") for t in titles)
        assert any(p.company == "QUOBYTE" for p in good)

    def test_salary_is_captured_where_present(self, records):
        good, _ = validate_all(records)
        assert any(p.salary_min == 80000 and p.salary_max == 120000 for p in good)

    def test_a_title_taken_from_the_body_is_tagged_as_such(self, records):
        good, _ = validate_all(records)
        from_body = [p for p in good if "title-from-body" in p.tags]
        assert from_body, "expected at least one body-derived title in the fixture"

    def test_every_record_has_a_reason_or_a_payload(self, records):
        for rec in records:
            assert (rec.payload is None) != (rec.parse_error is None)

    def test_missing_comment_rows_raise_rather_than_returning_empty(self, hn_task):
        # A silent [] here would produce a green run with no data.
        with pytest.raises(RuntimeError, match="markup has changed"):
            HackerNewsSource().parse(hn_task, make_result("<html><body>nothing</body></html>"))


# ------------------------------------------------------- We Work Remotely ---


class TestWeWorkRemotely:
    @pytest.fixture
    def records(self, wwr_task):
        return WeWorkRemotelySource().parse(wwr_task, make_result(fixture_text("wwr_feed.xml")))

    def test_splits_company_from_role(self, records):
        good, _ = validate_all(records)
        by_company = {p.company: p for p in good}
        assert by_company["CircleCI"].title == "Senior Software Engineer"

    def test_splits_on_the_first_colon_only(self, records):
        good, _ = validate_all(records)
        acme = next(p for p in good if p.company == "Acme")
        assert acme.title == "Systems: Staff Platform Engineer"

    def test_combines_region_state_and_country_into_a_location(self, records):
        good, _ = validate_all(records)
        p = next(p for p in good if p.company == "Collaboration.Ai")
        assert "Anywhere in the World" in p.location
        assert "Minnesota" in p.location and "United States" in p.location

    def test_uses_the_guid_url_as_the_identity(self, records):
        good, _ = validate_all(records)
        assert all(p.external_id.startswith("https://weworkremotely.com/") for p in good)

    def test_quarantines_a_title_with_no_colon(self, records):
        errors = [r.parse_error for r in records if r.parse_error]
        assert any("Company: Role" in e for e in errors)

    def test_quarantines_an_item_with_no_stable_identity(self, records):
        errors = [r.parse_error for r in records if r.parse_error]
        assert any("no stable identity" in e for e in errors)

    def test_quarantines_an_empty_title(self, records):
        errors = [r.parse_error for r in records if r.parse_error]
        assert any("no <title>" in e for e in errors)

    def test_malformed_xml_raises(self, wwr_task):
        with pytest.raises(RuntimeError, match="well-formed"):
            WeWorkRemotelySource().parse(wwr_task, make_result("<rss><channel><item>"))


# ------------------------------------------------------------- Remote OK ---


class TestRemoteOK:
    @pytest.fixture
    def records(self, remoteok_task):
        return RemoteOKSource().parse(remoteok_task, make_result(fixture_text("remoteok.json")))

    def test_skips_the_legal_notice_element_without_quarantining_it(self, records):
        # Element 0 of the real feed is a documented legal notice, not a job.
        assert len(records) == 7
        assert all("legal" not in (r.raw or {}) for r in records)

    def test_maps_position_to_title(self, records):
        good, _ = validate_all(records)
        assert any(p.title == "Senior Backend Engineer" for p in good)

    def test_keeps_a_real_salary(self, records):
        good, _ = validate_all(records)
        p = next(p for p in good if p.external_id == "1137305")
        assert (p.salary_min, p.salary_max, p.salary_currency) == (120000, 170000, "USD")

    def test_zero_salary_is_treated_as_absent(self, records):
        good, _ = validate_all(records)
        p = next(p for p in good if p.external_id == "1137306")
        assert p.salary_min is None and p.salary_currency is None

    def test_derives_a_url_from_the_slug_when_none_is_given(self, records):
        good, _ = validate_all(records)
        p = next(p for p in good if p.external_id == "1137307")
        assert p.url == "https://remoteok.com/remote-jobs/no-url-but-has-slug-1137307"

    def test_quarantines_an_entry_with_no_id(self, records):
        assert any("no 'id' field" in (r.parse_error or "") for r in records)

    def test_quarantines_an_empty_company(self, records):
        _, bad = validate_all(records)
        assert any(r.external_id == "1137309" for r in bad)

    def test_quarantines_an_inverted_salary_range(self, records):
        _, bad = validate_all(records)
        assert any(r.external_id == "1137310" for r in bad)

    def test_quarantines_a_future_timestamp(self, records):
        _, bad = validate_all(records)
        assert any(r.external_id == "1137311" for r in bad)

    def test_drops_the_bulky_description_from_quarantined_payloads(self, records):
        bad = [r for r in records if r.parse_error]
        assert all("description" not in (r.raw or {}) for r in bad)

    def test_a_json_object_instead_of_an_array_raises(self, remoteok_task):
        with pytest.raises(RuntimeError, match="shape has changed"):
            RemoteOKSource().parse(remoteok_task, make_result(json.dumps({"jobs": []})))

    def test_invalid_json_raises(self, remoteok_task):
        with pytest.raises(RuntimeError, match="not valid JSON"):
            RemoteOKSource().parse(remoteok_task, make_result("<html>maintenance</html>"))
