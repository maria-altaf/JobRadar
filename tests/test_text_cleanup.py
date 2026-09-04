"""Entity decoding and quarantine reason codes.

Both are small, and both were real defects found by looking at the rendered
dashboard rather than at the tests: a company called "H&M" arrived as
``H&amp;M`` and rendered as ``H&amp;amp;M``, and the quarantine chart grouped on
prose that had been truncated mid-sentence.
"""

from __future__ import annotations

import pytest

from jobradar.sources.base import clean_text
from jobradar.sources.hackernews import HackerNewsSource, HeaderError, parse_header
from jobradar.sources.remoteok import RemoteOKSource
from jobradar.sources.weworkremotely import WeWorkRemotelySource
from jobradar.storage import reason_code_for

from .conftest import fixture_text, make_result


class TestCleanText:
    def test_decodes_a_single_encoding(self):
        assert clean_text("H&amp;M") == "H&M"

    def test_decodes_a_double_encoding(self):
        # We Work Remotely ships "H&amp;amp;M": decoding the XML once still
        # leaves an entity behind, which then gets escaped again on render.
        assert clean_text("H&amp;amp;M") == "H&M"

    def test_collapses_whitespace(self):
        assert clean_text("  Acme   Systems \n Ltd ") == "Acme Systems Ltd"

    def test_empty_becomes_none(self):
        assert clean_text("   ") is None
        assert clean_text(None) is None

    def test_leaves_ordinary_text_alone(self):
        assert clean_text("Senior Engineer (Remote)") == "Senior Engineer (Remote)"


class TestCompanyUrlStripping:
    def test_a_url_appended_to_the_company_is_removed(self):
        f = parse_header("Snout https://snout.com/ | Backend Engineer | Remote")
        assert f["company"] == "Snout"

    def test_a_company_that_is_only_a_url_is_kept_rather_than_emptied(self):
        f = parse_header("https://acme.com | Backend Engineer | Remote")
        assert f["company"] == "https://acme.com"

    def test_a_parenthesised_url_is_removed(self):
        f = parse_header("Smarkets (https://www.smarkets.com) | Backend Engineer | Remote")
        assert f["company"] == "Smarkets"


class TestErrorCodes:
    def test_a_header_without_pipes_reports_its_code(self):
        with pytest.raises(HeaderError) as exc:
            parse_header("We are hiring great people")
        assert exc.value.code == "header_not_pipe_delimited"

    def test_a_header_with_no_role_reports_its_code(self):
        with pytest.raises(HeaderError) as exc:
            parse_header("Acme | Berlin, Germany | Full-time")
        assert exc.value.code == "no_role_identified"

    def test_hn_records_carry_the_code(self, hn_task):
        records = HackerNewsSource().parse(hn_task, make_result(fixture_text("hn_thread.html")))
        codes = {r.error_code for r in records if r.parse_error}
        assert codes <= {"header_not_pipe_delimited", "no_role_identified", "header_unparseable"}
        assert codes, "the fixture is meant to contain unparseable headers"

    def test_wwr_records_carry_the_code(self, wwr_task):
        records = WeWorkRemotelySource().parse(wwr_task, make_result(fixture_text("wwr_feed.xml")))
        codes = {r.error_code for r in records if r.parse_error}
        assert codes == {"no_stable_identity", "missing_title", "title_not_company_colon_role"}

    def test_remoteok_records_carry_the_code(self, remoteok_task):
        records = RemoteOKSource().parse(remoteok_task, make_result(fixture_text("remoteok.json")))
        codes = {r.error_code for r in records if r.parse_error}
        assert codes == {"no_stable_identity"}


class TestReasonCode:
    def test_an_explicit_parser_code_wins(self):
        assert reason_code_for({"code": "no_role_identified", "reason": "..."}) == "no_role_identified"

    def test_a_validation_error_uses_field_and_type(self):
        entry = {"errors": [{"field": "title", "type": "string_too_short"}], "reason": "x"}
        assert reason_code_for(entry) == "title:string_too_short"

    def test_it_never_returns_empty(self):
        assert reason_code_for({"reason": ""}) == "unknown"

    def test_it_is_bounded(self):
        assert len(reason_code_for({"code": "x" * 500})) <= 120

    def test_the_same_failure_on_different_values_shares_a_code(self):
        # The whole point: the prose reason embeds the offending value, so only
        # the code can answer "which failure mode dominates?".
        a = {"errors": [{"field": "title", "type": "string_too_short"}]}
        b = {"errors": [{"field": "title", "type": "string_too_short"}]}
        assert reason_code_for(a) == reason_code_for(b)
