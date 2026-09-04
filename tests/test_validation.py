"""Schema validation and the readability of quarantine reasons.

The requirement is not merely that bad rows are rejected -- it is that the
reason attached to a rejected row tells a human what went wrong without making
them open the raw payload.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobradar.models import JobPosting, ValidationError, describe_validation_error


def base(**overrides):
    payload = {
        "source": "remoteok",
        "external_id": "1",
        "url": "https://remoteok.com/remote-jobs/1",
        "company": "Acme",
        "title": "Backend Engineer",
    }
    payload.update(overrides)
    return payload


def reason_for(**overrides) -> str:
    with pytest.raises(ValidationError) as exc:
        JobPosting(**base(**overrides))
    reason, _ = describe_validation_error(exc.value)
    return reason


class TestAccepts:
    def test_a_minimal_valid_posting(self):
        posting = JobPosting(**base())
        assert posting.company == "Acme"
        assert posting.tags == []

    def test_epoch_seconds_as_posted_at(self):
        posting = JobPosting(**base(posted_at=1788406581))
        assert posting.posted_at.year == 2026

    def test_rfc2822_dates_from_rss(self):
        posting = JobPosting(**base(posted_at="Mon, 31 Aug 2026 07:30:46 +0000"))
        assert posting.posted_at.day == 31

    def test_iso_with_z_suffix(self):
        posting = JobPosting(**base(posted_at="2026-09-04T02:33:45.000000Z"))
        assert posting.posted_at.tzinfo is not None

    def test_naive_timestamps_are_assumed_utc(self):
        posting = JobPosting(**base(posted_at="2026-09-04T02:33:45"))
        assert posting.posted_at.utcoffset() == timedelta(0)

    def test_zero_salary_means_absent_not_zero(self):
        # Remote OK sends 0 rather than null for "not stated".
        posting = JobPosting(**base(salary_min=0, salary_max=0))
        assert posting.salary_min is None and posting.salary_max is None

    def test_comma_separated_tags_are_split(self):
        assert JobPosting(**base(tags="python, backend")).tags == ["python", "backend"]

    def test_strips_surrounding_whitespace(self):
        assert JobPosting(**base(company="  Acme  ")).company == "Acme"


class TestRejects:
    def test_empty_title(self):
        assert "title" in reason_for(title="")

    def test_placeholder_company(self):
        reason = reason_for(company="N/A")
        assert "placeholder" in reason and "company" in reason

    def test_title_containing_a_newline(self):
        # Means a delimiter split failed and we captured prose.
        reason = reason_for(title="Backend Engineer\nWe are hiring")
        assert "newline" in reason

    def test_inverted_salary_range(self):
        reason = reason_for(salary_min=200000, salary_max=90000)
        assert "greater than" in reason

    def test_implausible_salary(self):
        assert "implausible" in reason_for(salary_min=99_000_000)

    def test_negative_salary(self):
        assert "negative" in reason_for(salary_min=-5)

    def test_non_http_url(self):
        assert "http" in reason_for(url="ftp://example.com/job")

    def test_timestamp_far_in_the_future(self):
        future = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        assert "future" in reason_for(posted_at=future)

    def test_timestamp_before_2005(self):
        # Catches an epoch that was parsed in the wrong unit.
        assert "2005" in reason_for(posted_at="1998-01-01T00:00:00Z")

    def test_unparseable_timestamp(self):
        assert "unparseable" in reason_for(posted_at="last tuesday")

    def test_unknown_source(self):
        assert "source" in reason_for(source="linkedin")

    def test_unexpected_field_is_not_silently_accepted(self):
        # extra="forbid": a parser that starts emitting a new key must fail
        # loudly rather than have the value quietly dropped on the floor.
        assert "surprise" in reason_for(surprise="value")


class TestReasonReadability:
    def test_names_the_field_the_message_and_the_value(self):
        reason = reason_for(title="")
        assert reason.startswith("title:")
        assert "at least 2 characters" in reason
        assert "got ''" in reason

    def test_reports_every_problem_not_just_the_first(self):
        reason = reason_for(title="", company="")
        assert "title:" in reason and "company:" in reason

    def test_structured_errors_accompany_the_sentence(self):
        with pytest.raises(ValidationError) as exc:
            JobPosting(**base(title=""))
        _, errors = describe_validation_error(exc.value)
        assert errors[0]["field"] == "title"
        assert errors[0]["input"] == "''"

    def test_long_values_are_truncated_not_dumped(self):
        reason = reason_for(title="x" * 5000)
        assert len(reason) < 1100
        assert "..." in reason
