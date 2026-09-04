"""Hacker News "Ask HN: Who is hiring?" -- HTML scraping.

This is the fragile source, and deliberately so: it is the one that exercises
what happens when a site changes its markup or its users change a convention.

Two steps:

1. **Discover** the current thread id through the Algolia HN search API. The
   thread is posted monthly by the ``whoishiring`` account, so its id changes
   every month and cannot be hardcoded.
2. **Scrape** ``news.ycombinator.com/item?id=...`` and read the top-level
   comments. HN serves the whole thread in one response (verified at ~390
   comments), so there is no pagination to walk.

Posts follow a *convention*, not a schema::

    Company | Role | Location | Full Time | Visa | Relocation

Nothing enforces it. Comments that do not carry a ``|`` delimiter -- meta
discussion, replies that slipped to top level, people ignoring the format -- are
quarantined with a stated reason rather than dropped. That makes the quarantine
*rate* the health signal: a steady low percentage is normal human noise, while a
sudden jump to nearly everything means HN changed its markup and the selector
stopped matching.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString

from ..config import Settings
from ..net import Fetcher, FetchResult
from .base import RawRecord, Task, clean_text, html_to_text

log = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item"


class HeaderError(ValueError):
    """A header that does not yield a posting, with a groupable code."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code

# Words that positively identify a segment as a *role*. Matching roles
# affirmatively beats identifying them by elimination: the pipe convention
# fixes no field order, so "QUOBYTE | Berlin, Germany | ONSITE" and
# "QUOBYTE | Backend Engineer | ONSITE" are indistinguishable to a rule that
# only knows what a role is *not*.
_ROLE_WORDS = (
    "engineer", "engineering", "developer", "programmer", "designer", "architect",
    "scientist", "analyst", "researcher", "manager", "director", "administrator",
    "consultant", "specialist", "coordinator", "technician", "strategist",
    "lead", "head of", "principal", "staff", "senior", "junior", "intern",
    "founder", "president", "officer", "chief", "cto", "ceo", "cfo", "coo", "vp",
    "devops", "sre", "mlops", "qa", "tester", "dba", "sysadmin", "admin",
    "backend", "back-end", "back end", "frontend", "front-end", "front end",
    "fullstack", "full-stack", "full stack", "webmaster",
    "writer", "editor", "marketer", "recruiter", "accountant", "controller",
    "counsel", "lawyer", "attorney", "paralegal", "nurse", "teacher", "tutor",
    "salesperson", "copywriter", "producer", "animator", "illustrator",
    "generalist", "apprentice", "associate", "assistant", "advocate", "evangelist",
)

# Employment mode.
_TYPE_WORDS = (
    "full-time", "fulltime", "full time", "part-time", "parttime", "part time",
    "contract", "contractor", "internship", "intern", "permanent", "freelance",
    "temporary", "w2", "c2c",
)

# Explicit work-arrangement words.
_PLACE_WORDS = (
    "remote", "onsite", "on-site", "on site", "hybrid", "anywhere", "worldwide",
    "in-office", "in office", "distributed", "wfh", "time zone", "timezone",
)

# Place tokens with no arrangement keyword: bare cities, countries and regions.
# Not exhaustive by design -- it only has to cover what actually shows up in
# this thread, and anything it misses falls through to the existing behaviour.
_PLACE_TOKENS = frozenset(
    ["usa", "us", "u.s.", "u.s.a", "america", "americas", "canada", "mexico", "brazil", "argentina", "chile", "colombia", "uk", "u.k", "england", "scotland", "wales", "ireland", "london", "manchester", "cambridge", "oxford", "bristol", "germany", "berlin", "munich", "münchen", "hamburg", "cologne", "frankfurt", "stuttgart", "france", "paris", "lyon", "spain", "madrid", "barcelona", "valencia", "portugal", "lisbon", "lisboa", "porto", "italy", "rome", "milan", "netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven", "holland", "belgium", "brussels", "antwerp", "switzerland", "zurich", "zürich", "geneva", "basel", "lausanne", "austria", "vienna", "poland", "warsaw", "krakow", "kraków", "wroclaw", "czechia", "prague", "hungary", "budapest", "romania", "bucharest", "bulgaria", "sofia", "greece", "athens", "turkey", "istanbul", "ukraine", "kyiv", "sweden", "stockholm", "norway", "oslo", "denmark", "copenhagen", "finland", "helsinki", "iceland", "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius", "serbia", "belgrade", "croatia", "zagreb", "israel", "telaviv", '"tel', 'aviv"', "jerusalem", "uae", "dubai", '"abu', 'dhabi"', "qatar", "doha", "saudi", "riyadh", "india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "chennai", "noida", "gurgaon", "pakistan", "karachi", "lahore", "islamabad", "bangladesh", "dhaka", "srilanka", "colombo", "china", "beijing", "shanghai", "shenzhen", "hongkong", '"hong', 'kong"', "taiwan", "taipei", "japan", "tokyo", "osaka", "kyoto", "korea", "seoul", "singapore", "malaysia", '"kuala', 'lumpur"', "indonesia", "jakarta", "thailand", "bangkok", "vietnam", "hanoi", '"ho', "chi", 'minh"', "philippines", "manila", "cebu", "australia", "sydney", "melbourne", "brisbane", "perth", '"new', 'zealand"', "auckland", "wellington", "nigeria", "lagos", "kenya", "nairobi", "ghana", "accra", "egypt", "cairo", '"south', 'africa"', '"cape', 'town"', "johannesburg", '"new', 'york"', "nyc", "brooklyn", "manhattan", "boston", "cambridge", "philadelphia", '"washington"', "dc", "atlanta", "miami", "orlando", "tampa", "charlotte", "raleigh", "durham", "nashville", "austin", "dallas", "houston", '"san', 'antonio"', "denver", "boulder", "phoenix", '"salt', 'lake"', "chicago", "detroit", "minneapolis", '"st', 'louis"', "kansas", "seattle", "portland", '"san', 'francisco"', "sf", '"bay', 'area"', '"silicon', 'valley"', '"palo', 'alto"', '"mountain', 'view"', '"san', 'jose"', "oakland", "berkeley", '"los', 'angeles"', "la", '"san', 'diego"', "sacramento", "vegas", "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo", "emea", "apac", "latam", "americas", "europe", "asia", "africa", '"north', 'america"', '"south', 'america"', '"middle', 'east"', "nordics", "benelux", "dach", "global", "worldwide", "anywhere", '"eu"', '"e.u"']
)


def _looks_like_place(part: str) -> bool:
    """True when a segment reads as a location rather than a role."""
    low = part.lower()
    if any(w in low for w in _PLACE_WORDS):
        return True
    # Tokenise on punctuation so "Berlin, Germany" and "(Zürich)" both split.
    tokens = {t for t in re.split(r"[^\w.À-ɏ]+", low) if t}
    if tokens & _PLACE_TOKENS:
        return True
    # "City, Country" with no role word anywhere in it.
    return bool(re.fullmatch(r"[^,]{2,28},\s*[^,]{2,28}", part.strip()))

# Short parts that are neither role nor place and should never become a title.
_NOISE_WORDS = (
    "visa",
    "no visa",
    "relocation",
    "no relocation",
    "h1b",
    "sponsorship",
    "equity",
    "green card",
    # "Hiring" on its own is an announcement, not a role. A genuine role such
    # as "Hiring Manager" is caught by the role check, which runs first.
    "hiring",
    "we are hiring",
    "now hiring",
)

# Posters very often put the company URL in its own segment, right after the
# company name. It is not the role, so it must not be picked up as the title.
_URL_RE = re.compile(r"^\s*(?:https?://|www\.)\S+\s*$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^\s*[\w.+-]+@[\w-]+\.[\w.]+\s*$")


def comment_lines(body) -> list[str]:
    """Split an HN comment into logical lines.

    ``get_text("\\n")`` inserts a break at *every* element boundary, which
    shreds a header the moment it contains an inline ``<a>`` -- and posters
    routinely put their company URL inline. That truncates
    ``"Sudowrite | https://... | Senior Engineer"`` down to ``"Sudowrite |"``,
    and the posting then looks unparseable when it is perfectly well formed.

    Breaking only on genuine block boundaries (``<p>`` and ``<br>``) and joining
    the rest with no separator preserves the original spacing, because HN's own
    text nodes already carry the spaces around inline tags.
    """
    work = copy.copy(body)
    for br in work.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for para in work.find_all("p"):
        para.insert_before(NavigableString("\n"))
    text = work.get_text("")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]

_SALARY_RE = re.compile(
    r"(?P<cur>[$£€])\s?(?P<lo>\d{2,3}(?:[,.]\d{3})?)\s?(?P<lok>k?)"
    r"(?:\s?[-–—to]+\s?(?P<cur2>[$£€])?\s?(?P<hi>\d{2,3}(?:[,.]\d{3})?)\s?(?P<hik>k?))?",
    re.IGNORECASE,
)

_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}


def _is_short(part: str, limit: int = 30) -> bool:
    return len(part) <= limit


def _matches(part: str, words: tuple[str, ...]) -> bool:
    low = part.lower()
    return any(w in low for w in words)


# Prefix-anchored: matches "engineering" and "engineers" from the stem
# "engineer", while refusing to match a stem sitting inside an unrelated word.
_ROLE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _ROLE_WORDS) + r")", re.IGNORECASE
)
_ANY_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# A full stop followed by a capital means we are looking at prose, not a title.
_SENTENCE_RE = re.compile(r"[.!?]\s+[A-Z]")
_PROSE_START = (
    "we ", "we'", "our ", "the ", "i ", "i'", "you ", "this ", "that ", "it ",
    "what ", "why ", "how ", "if ", "about ", "apply", "join ", "come ",
    "looking for", "interested", "please ", "there ", "here ", "more ",
)


def _clean_role_candidate(text: str) -> str | None:
    """Return ``text`` as a usable role title, or None if it is not one."""
    stripped = text.strip(" -*•·—\t|:,")
    if not (3 <= len(stripped) <= 120):
        return None
    if len(stripped.split()) > 8:
        return None
    if stripped.lower().startswith(_PROSE_START):
        return None
    if _SENTENCE_RE.search(stripped):
        return None
    if not _ROLE_RE.search(stripped):
        return None
    return stripped


def role_from_body(lines: list[str]) -> str | None:
    """Recover a role from the body of a post whose header omits one.

    A large minority of posts use the header purely for company, location and
    employment type -- ``QUOBYTE | Berlin, Germany | Full-time | ONSITE`` -- and
    name the actual role on the next line. Reading only the header would
    quarantine those as unparseable even though the posting is perfectly good.

    Only the first few lines are considered, URLs are stripped, and the text is
    split on colons so that ``Software Engineer: https://...`` yields the role
    rather than the link. Anything that still reads as prose is rejected: a
    wrong title is worse than a quarantined row with a stated reason.
    """
    for line in lines[1:8]:
        without_urls = _ANY_URL_RE.sub(" ", line)
        segments = [s for s in re.split(r"\s*:\s*", without_urls) if s.strip()]
        for candidate in [*segments, without_urls]:
            cleaned = _clean_role_candidate(candidate)
            if cleaned:
                return cleaned
    return None


def _parse_salary(part: str) -> tuple[int | None, int | None, str | None]:
    """Best-effort salary extraction. Returns (min, max, currency).

    Forgiving on purpose -- a header that does not mention money, or mentions it
    in a shape we do not recognise, yields ``(None, None, None)`` rather than a
    validation failure. Money is enrichment here, not identity.
    """
    m = _SALARY_RE.search(part)
    if not m:
        return None, None, None

    def to_int(num: str | None, k: str | None) -> int | None:
        if not num:
            return None
        try:
            n = float(num.replace(",", "").replace(".", ""))
        except ValueError:
            return None
        if k or n < 1000:
            n *= 1000
        return int(n)

    lo = to_int(m.group("lo"), m.group("lok"))
    hi = to_int(m.group("hi"), m.group("hik"))
    cur = _CURRENCY.get(m.group("cur") or m.group("cur2") or "")
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi, cur


def parse_header(header: str, fallback_role: str | None = None) -> dict[str, Any]:
    """Split a Who-is-hiring header line into structured fields.

    ``company`` is always the first segment. Of the rest, the first segment that
    is not obviously an employment mode, a place, money or visa boilerplate
    becomes the title; place-ish segments become the location. Segments are only
    classified as mode/place when they are *short*, so a genuine role called
    "Contract Counsel" is not mistaken for an employment type.
    """
    parts = [p.strip() for p in header.split("|")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        raise HeaderError(
            "header line does not use the 'Company | Role | ...' pipe convention "
            f"(got {header[:120]!r})",
            code="header_not_pipe_delimited",
        )

    # Posters often append their site to the company segment rather than giving
    # it its own: "Snout https://snout.com/ | Backend Engineer | Remote".
    company = _ANY_URL_RE.sub(" ", parts[0]).strip(" -–—([{,")
    if not company:
        company = parts[0]
    role_title: str | None = None
    fallback_title: str | None = None
    location_bits: list[str] = []
    employment_type: str | None = None
    salary_min = salary_max = None
    currency: str | None = None

    for part in parts[1:]:
        # A bare URL or email is contact detail, never the role.
        if _URL_RE.match(part) or _EMAIL_RE.match(part):
            continue

        if salary_min is None and _SALARY_RE.search(part):
            lo, hi, cur = _parse_salary(part)
            if lo or hi:
                salary_min, salary_max, currency = lo, hi, cur
                # A segment that is only money is neither a title nor a place.
                if len(part) <= 40:
                    continue

        has_role = bool(_ROLE_RE.search(part))

        # Roles win outright: "Contract Counsel" is a job, not an arrangement,
        # and "Berlin Site Reliability Engineer" is a job, not a city.
        if has_role:
            if role_title is None:
                role_title = part
            continue

        if _matches(part, _TYPE_WORDS):
            employment_type = employment_type or part
            continue
        if _looks_like_place(part):
            location_bits.append(part)
            continue
        if _is_short(part) and _matches(part, _NOISE_WORDS):
            continue

        # Unclassified: a possible role we have no vocabulary for.
        if fallback_title is None:
            fallback_title = part

    title = role_title or fallback_title or fallback_role
    if not title:
        raise HeaderError(
            "no role could be identified in the header or the first lines of the "
            f"post; every segment was a location, an employment type or contact "
            f"detail (got {header[:120]!r})",
            code="no_role_identified",
        )
    # Provenance: a title lifted from the body is a weaker claim than one read
    # from the header, and the dashboard should be able to tell them apart.
    title_source = (
        "header" if (role_title or fallback_title) else "body"
    )

    return {
        "company": company,
        "title": title,
        "location": ", ".join(location_bits)[:300] or None,
        "employment_type": employment_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": currency,
        "title_source": title_source,
    }


class HackerNewsSource:
    name = "hackernews"

    async def plan(self, fetcher: Fetcher, settings: Settings) -> list[Task]:
        """Find the most recent "Who is hiring?" threads via Algolia."""
        result = await fetcher.get(
            ALGOLIA_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": 12},
        )
        import json

        data = json.loads(result.text)
        tasks: list[Task] = []
        for hit in data.get("hits", []):
            title = (hit.get("title") or "").lower()
            # The same account also posts "Who wants to be hired?" -- skip it.
            if "who is hiring" not in title:
                continue
            thread_id = str(hit.get("objectID") or "").strip()
            if not thread_id:
                continue
            tasks.append(
                Task(
                    task_key=f"hackernews:thread:{thread_id}",
                    source=self.name,
                    url=ITEM_URL,
                    params={"id": thread_id},
                    # HN sends no ETag; a conditional request would be wasted.
                    conditional=False,
                    meta={"thread_id": thread_id, "thread_title": hit.get("title")},
                )
            )
            if len(tasks) >= settings.hn_threads:
                break
        if not tasks:
            raise RuntimeError(
                "Algolia returned no 'Who is hiring' threads -- the search API or "
                "the whoishiring account's posting convention may have changed"
            )
        return tasks

    def parse(self, task: Task, result: FetchResult) -> list[RawRecord]:
        soup = BeautifulSoup(result.text, "html.parser")
        rows = soup.select("tr.athing.comtr")
        if not rows:
            # No comment rows at all means the page structure changed, which is
            # a different (and much worse) problem than a few unparseable posts.
            raise RuntimeError(
                "no 'tr.athing.comtr' rows found on the HN thread page -- the "
                "page markup has changed and the selector needs updating"
            )

        records: list[RawRecord] = []
        for row in rows:
            ind = row.select_one("td.ind")
            indent = (ind.get("indent") if ind else None) or "0"
            if str(indent) != "0":
                continue  # replies are discussion, not postings

            comment_id = str(row.get("id") or "").strip()
            body = row.select_one("div.commtext")
            if body is None or not comment_id:
                continue  # deleted/flagged comment placeholder

            # The header is the first logical line of the comment.
            lines = comment_lines(body)
            header = lines[0] if lines else ""

            age = row.select_one("span.age")
            posted_at = None
            if age is not None and age.get("title"):
                # e.g. "2026-09-04T02:33:45.000000Z" (newer pages append an epoch)
                posted_at = str(age.get("title")).split()[0]

            raw = {
                "comment_id": comment_id,
                "header": header,
                "thread_id": task.meta.get("thread_id"),
                "posted_at": posted_at,
                "text_preview": "\n".join(lines)[:500],
            }

            try:
                fields = parse_header(header, fallback_role=role_from_body(lines))
            except ValueError as exc:
                records.append(
                    RawRecord(
                        raw=raw,
                        parse_error=str(exc),
                        error_code=getattr(exc, "code", "header_unparseable"),
                        external_id=comment_id,
                    )
                )
                continue

            # ``title_source`` is provenance, not a posting field; carry it as a
            # tag so it survives into the store and stays visible.
            tags = ["hn-who-is-hiring"]
            if fields.pop("title_source", "header") == "body":
                tags.append("title-from-body")

            records.append(
                RawRecord(
                    raw=raw,
                    external_id=comment_id,
                    payload={
                        "source": self.name,
                        "external_id": comment_id,
                        "url": f"https://news.ycombinator.com/item?id={comment_id}",
                        "tags": tags,
                        "description_text": html_to_text(str(body)),
                        "posted_at": posted_at,
                        **fields,
                        "company": clean_text(fields["company"]),
                        "title": clean_text(fields["title"]),
                        "location": clean_text(fields["location"]),
                    },
                )
            )
        return records
