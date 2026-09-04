"""Remote OK -- JSON API.

Attribution
-----------
Remote OK's API terms require that anything built on this data link back to the
posting on Remote OK with a *followed* link and name Remote OK as the source.
The dashboard does both: postings link to their ``remoteok.com`` URL with
``rel="noopener"`` only (deliberately **not** ``nofollow``), and the footer
credits Remote OK. Changing that would breach the terms we accepted by using
the endpoint.

Shape
-----
``GET /api`` returns a JSON array whose **first element is a legal notice, not a
job** -- it has ``legal`` and ``last_updated`` keys and no ``id``. Anything
without an ``id`` is skipped as non-job metadata rather than quarantined.

The tag endpoints (``/api?tag=dev`` and friends) overlap heavily with each other
and with the unfiltered feed -- the same posting routinely appears under three
tags. Nothing here tries to avoid that: the overlap is absorbed by the
``dedup_key`` upsert, which is exactly the property that makes running the
pipeline twice harmless.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import Settings
from ..net import Fetcher, FetchResult
from .base import RawRecord, Task, clean_text, html_to_text

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


class RemoteOKSource:
    name = "remoteok"

    async def plan(self, fetcher: Fetcher, settings: Settings) -> list[Task]:
        tasks = [
            Task(
                task_key="remoteok:all",
                source=self.name,
                url=API_URL,
                conditional=True,
                meta={"tag": None},
            )
        ]
        tasks.extend(
            Task(
                task_key=f"remoteok:tag:{tag}",
                source=self.name,
                url=API_URL,
                params={"tag": tag},
                conditional=True,
                meta={"tag": tag},
            )
            for tag in settings.remoteok_tags
        )
        return tasks

    def parse(self, task: Task, result: FetchResult) -> list[RawRecord]:
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"response was not valid JSON (tag={task.meta.get('tag')}): {exc}"
            ) from exc

        if not isinstance(data, list):
            raise RuntimeError(
                f"expected a JSON array, got {type(data).__name__} -- the API shape has changed"
            )

        records: list[RawRecord] = []
        for entry in data:
            if not isinstance(entry, dict):
                records.append(
                    RawRecord(
                        raw={"value": repr(entry)[:300]},
                        error_code="element_not_an_object",
                        parse_error=f"array element is {type(entry).__name__}, not an object",
                    )
                )
                continue

            # The documented legal-notice element, and any future metadata
            # element, has no id. Not an error -- just not a job.
            if "id" not in entry and "legal" in entry:
                continue

            external_id = str(entry.get("id") or "").strip()
            if not external_id:
                records.append(
                    RawRecord(
                        raw=_trim(entry),
                        error_code="no_stable_identity",
                        parse_error="object has no 'id' field, so it has no stable identity",
                    )
                )
                continue

            url = entry.get("url") or entry.get("apply_url")
            if not url and entry.get("slug"):
                url = f"https://remoteok.com/remote-jobs/{entry['slug']}"

            records.append(
                RawRecord(
                    raw=_trim(entry),
                    external_id=external_id,
                    payload={
                        "source": self.name,
                        "external_id": external_id,
                        "url": url or "",
                        "company": clean_text(entry.get("company")) or "",
                        "title": clean_text(entry.get("position")) or "",
                        "location": clean_text(entry.get("location")),
                        "employment_type": None,
                        "salary_min": entry.get("salary_min"),
                        "salary_max": entry.get("salary_max"),
                        # Remote OK quotes salaries in USD.
                        "salary_currency": "USD"
                        if (entry.get("salary_min") or entry.get("salary_max"))
                        else None,
                        "tags": entry.get("tags") or [],
                        "description_text": html_to_text(entry.get("description")),
                        "posted_at": entry.get("date") or entry.get("epoch"),
                    },
                )
            )
        return records


def _trim(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky description before storing a raw payload in quarantine."""
    out = {k: v for k, v in entry.items() if k not in {"description"}}
    desc = entry.get("description")
    if isinstance(desc, str):
        out["description_excerpt"] = desc[:300]
    return out
