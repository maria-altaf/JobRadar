"""Ingest schema, and the machinery that turns a validation failure into a
sentence a human can act on.

Every posting a parser produces is validated against :class:`JobPosting` before
it can reach the ``jobs`` table. Anything that fails is written to
``quarantine`` together with the original payload and a readable reason -- it is
never silently discarded, because a row quietly vanishing is precisely the
failure mode that lets a broken parser go unnoticed for weeks.

The validators are deliberately strict about things that indicate a *parser*
problem rather than merely untidy data. A title of 900 characters, for example,
almost always means an HN comment was swallowed whole because the ``|``
delimiter convention changed -- that should surface as a loud quarantine entry,
not as a plausible-looking row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SourceName = Literal["hackernews", "weworkremotely", "remoteok"]

#: Values that appear where a company name should be but carry no information.
_PLACEHOLDERS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "confidential",
    "?",
}

_MIN_POSTED = datetime(2005, 1, 1, tzinfo=UTC)
_MAX_SALARY = 10_000_000


class JobPosting(BaseModel):
    """A validated posting, ready to be upserted."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source: SourceName
    external_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=2000)
    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=2, max_length=400)
    location: str | None = Field(default=None, max_length=300)
    employment_type: str | None = Field(default=None, max_length=60)
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = Field(default=None, max_length=8)
    tags: list[str] = Field(default_factory=list)
    description_text: str | None = None
    posted_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, v: str) -> str:
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("company")
    @classmethod
    def _company_is_meaningful(cls, v: str) -> str:
        if v.strip().lower() in _PLACEHOLDERS:
            raise ValueError(f"placeholder company name {v.strip()!r} carries no identity")
        return v

    @field_validator("title")
    @classmethod
    def _title_is_a_title(cls, v: str) -> str:
        # A newline in a title means the delimiter split failed and we captured
        # a block of prose. Fail loudly rather than storing a garbage title.
        if "\n" in v:
            raise ValueError("contains a newline, which means the field was not split correctly")
        if v.strip().lower() in _PLACEHOLDERS:
            raise ValueError(f"placeholder title {v.strip()!r} carries no identity")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, (list, tuple)):
            return [str(t).strip() for t in v if str(t).strip()][:40]
        raise ValueError(f"expected a list of strings, got {type(v).__name__}")

    @field_validator("salary_min", "salary_max", mode="before")
    @classmethod
    def _coerce_salary(cls, v: Any) -> int | None:
        if v in (None, "", 0, "0"):
            return None
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"not a number: {v!r}") from None
        if n < 0:
            raise ValueError(f"negative salary {n}")
        if n > _MAX_SALARY:
            raise ValueError(f"implausible salary {n} (above {_MAX_SALARY})")
        return n

    @field_validator("posted_at", mode="before")
    @classmethod
    def _coerce_posted_at(cls, v: Any) -> datetime | None:
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=UTC)
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00")
            try:
                v = datetime.fromisoformat(s)
            except ValueError:
                from email.utils import parsedate_to_datetime

                try:
                    v = parsedate_to_datetime(s)
                except (TypeError, ValueError):
                    raise ValueError(f"unparseable timestamp {v!r}") from None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        raise ValueError(f"unparseable timestamp {v!r}")

    @field_validator("posted_at")
    @classmethod
    def _posted_at_is_sane(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        v = v.astimezone(UTC)
        # Clock skew between us and a source is normal; two days is not.
        if v > datetime.now(UTC) + timedelta(days=2):
            raise ValueError(f"posted more than 2 days in the future ({v.isoformat()})")
        if v < _MIN_POSTED:
            raise ValueError(f"posted before 2005 ({v.isoformat()}), likely a bad epoch")
        return v

    @model_validator(mode="after")
    def _salary_range_is_ordered(self) -> JobPosting:
        lo, hi = self.salary_min, self.salary_max
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"salary_min ({lo}) is greater than salary_max ({hi})")
        return self


def describe_validation_error(exc: ValidationError) -> tuple[str, list[dict[str, Any]]]:
    """Turn a pydantic error into a one-line reason plus structured detail.

    The one-liner is what shows on the dashboard's quarantine table, so it has
    to name the field, say what was wrong, and show the offending value:

        ``title: String should have at least 2 characters (got '')``
    """
    errors = exc.errors(include_url=False)
    parts: list[str] = []
    structured: list[dict[str, Any]] = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        msg = err.get("msg", "invalid")
        # Pydantic prefixes custom ValueErrors; drop the noise.
        msg = msg.removeprefix("Value error, ")
        raw = err.get("input", None)
        shown = repr(raw)
        if len(shown) > 120:
            shown = shown[:117] + "...'"
        parts.append(f"{loc}: {msg} (got {shown})")
        structured.append(
            {"field": loc, "message": msg, "type": err.get("type"), "input": shown}
        )
    reason = "; ".join(parts)
    if len(reason) > 1000:
        reason = reason[:997] + "..."
    return reason, structured


__all__ = ["JobPosting", "describe_validation_error", "ValidationError", "SourceName"]
