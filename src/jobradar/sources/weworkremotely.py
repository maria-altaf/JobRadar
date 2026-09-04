"""We Work Remotely -- RSS.

The most structured of the three sources: eight category feeds, each an RSS 2.0
document with custom child elements (``region``, ``country``, ``state``,
``type``, ``expires_at``). WWR sends an ``ETag``, so re-runs within a day are
usually answered with a 304 and cost nothing to parse.

The one soft spot is the ``<title>``, which packs two fields into one string as
``"Company: Role"``. A company name containing a colon would split wrongly, so
the split is on the *first* colon only and a title with no colon is quarantined
with a stated reason rather than guessed at.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from ..config import Settings
from ..net import Fetcher, FetchResult
from .base import RawRecord, Task, clean_text, html_to_text

log = logging.getLogger(__name__)

FEED_URL = "https://weworkremotely.com/categories/{category}.rss"


def _text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _build_location(item: ET.Element) -> str | None:
    """Combine the region/country/state trio into one readable location."""
    bits = [
        _text(item, "region"),
        _text(item, "state"),
        _text(item, "country"),
    ]
    seen: list[str] = []
    for b in bits:
        if b and b not in seen:
            seen.append(b)
    return ", ".join(seen)[:300] or None


class WeWorkRemotelySource:
    name = "weworkremotely"

    async def plan(self, fetcher: Fetcher, settings: Settings) -> list[Task]:
        return [
            Task(
                task_key=f"weworkremotely:{category}",
                source=self.name,
                url=FEED_URL.format(category=category),
                conditional=True,
                meta={"category": category},
            )
            for category in settings.wwr_categories
        ]

    def parse(self, task: Task, result: FetchResult) -> list[RawRecord]:
        try:
            root = ET.fromstring(result.text)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"feed for {task.meta.get('category')} is not well-formed XML: {exc}"
            ) from exc

        items = root.findall("./channel/item")
        if not items:
            # An empty feed is legitimate for a quiet category, so this is not
            # fatal here; the run-level parser health check decides whether a
            # zero count is suspicious given what this feed usually returns.
            log.info("WWR feed %s returned no items", task.meta.get("category"))

        records: list[RawRecord] = []
        for item in items:
            guid = _text(item, "guid") or _text(item, "link")
            title_raw = _text(item, "title")
            raw = {
                "guid": guid,
                "title": title_raw,
                "region": _text(item, "region"),
                "country": _text(item, "country"),
                "state": _text(item, "state"),
                "category": _text(item, "category"),
                "type": _text(item, "type"),
                "pubDate": _text(item, "pubDate"),
                "link": _text(item, "link"),
            }

            if not guid:
                records.append(
                    RawRecord(
                        raw=raw,
                        error_code="no_stable_identity",
                        parse_error="item has neither <guid> nor <link>, so it has no stable identity",
                    )
                )
                continue

            if not title_raw:
                records.append(
                    RawRecord(
                        raw=raw, external_id=guid, error_code="missing_title",
                        parse_error="item has no <title>",
                    )
                )
                continue

            if ":" not in title_raw:
                records.append(
                    RawRecord(
                        raw=raw,
                        external_id=guid,
                        error_code="title_not_company_colon_role",
                        parse_error=(
                            "title does not use the 'Company: Role' convention "
                            f"(got {title_raw[:120]!r})"
                        ),
                    )
                )
                continue

            company, _, title = title_raw.partition(":")
            tags = [t for t in [_text(item, "category"), _text(item, "skills")] if t]

            records.append(
                RawRecord(
                    raw=raw,
                    external_id=guid,
                    payload={
                        "source": self.name,
                        "external_id": guid,
                        "url": _text(item, "link") or guid,
                        "company": clean_text(company),
                        "title": clean_text(title),
                        "location": clean_text(_build_location(item)),
                        "employment_type": _text(item, "type"),
                        "tags": tags,
                        "description_text": html_to_text(_text(item, "description")),
                        "posted_at": _text(item, "pubDate"),
                    },
                )
            )
        return records
