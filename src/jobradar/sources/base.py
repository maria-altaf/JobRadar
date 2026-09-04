"""Source protocol shared by the three adapters.

A source does two things:

``plan``   decide the units of work for this run. Planning may itself require a
           network call (Hacker News has to discover the current month's thread
           id before it can fetch it), so it is async and gets the fetcher.

``parse``  turn one fetched response into records. A parser never raises for a
           single bad record and never silently skips one: it returns a
           :class:`RawRecord` carrying either a payload to validate or a
           ``parse_error`` explaining, in words, why this record could not be
           read. Both paths are accounted for downstream.

Planning is separated from fetching because the task list is what gets written
to the resume ledger before any work starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from typing import Any, Protocol, runtime_checkable

from bs4 import BeautifulSoup

from ..config import Settings
from ..net import Fetcher, FetchResult

#: Descriptions are stored for search/context, not for display in full.
MAX_DESCRIPTION_CHARS = 8000


@dataclass(frozen=True)
class Task:
    """One fetchable unit of work.

    ``task_key`` must be deterministic for a given logical unit -- it is the
    resume ledger's identity, so the same task on a re-run has to produce the
    same key or a restarted run would redo finished work.
    """

    task_key: str
    source: str
    url: str
    params: dict[str, Any] | None = None
    #: Use stored ETag / Last-Modified to make this a conditional request.
    conditional: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawRecord:
    """One record extracted from a response.

    Exactly one of ``payload`` (to be schema-validated) or ``parse_error``
    (straight to quarantine with this reason) is meaningful.

    ``error_code`` is the short, stable slug the dashboard groups on. The prose
    reason embeds the offending value and so is unique per row; the code is what
    makes "which failure mode dominates, and has that mix shifted?" answerable.
    """

    raw: dict[str, Any]
    payload: dict[str, Any] | None = None
    parse_error: str | None = None
    error_code: str | None = None
    external_id: str | None = None


@runtime_checkable
class Source(Protocol):
    name: str

    async def plan(self, fetcher: Fetcher, settings: Settings) -> list[Task]: ...

    def parse(self, task: Task, result: FetchResult) -> list[RawRecord]: ...


def html_to_text(html: str | None, limit: int = MAX_DESCRIPTION_CHARS) -> str | None:
    """Flatten a fragment of HTML to readable plain text."""
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit]


def clean_text(value: str | None) -> str | None:
    """Decode HTML entities and collapse whitespace in a short field.

    Needed because sources double-encode: We Work Remotely's RSS carries
    ``H&amp;amp;M`` in the XML, so decoding the document once still leaves a
    literal ``&amp;`` in the value. Storing that unnoticed means the dashboard
    escapes it a second time and renders "H&amp;amp;M" to the reader.

    Applied only to short identity fields (company, title, location); long
    descriptions already pass through :func:`html_to_text`, which unescapes.
    """
    if value is None:
        return None
    # Twice, because a single pass leaves the inner entity of a double encoding.
    text = unescape(unescape(str(value)))
    text = " ".join(text.split())
    return text or None
