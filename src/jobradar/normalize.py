"""Text normalisation and the identity keys that make the pipeline idempotent.

Two different keys are computed for every posting, and they answer two
different questions:

``dedup_key``            -- "is this the same posting I already have?"
    sha256 of ``source | external_id``. Stable for the life of the posting,
    derived from the identifier the source itself assigns (HN comment id, WWR
    guid URL, RemoteOK numeric id). This is the primary key of the upsert, so
    re-running the pipeline over the same data can only ever update rows.

``content_fingerprint``  -- "is this the same *job* as one from another source?"
    sha256 of normalised ``company | title | location``. Deliberately lossy: it
    collapses punctuation, legal suffixes, seniority noise and whitespace so
    that "Acme, Inc." / "Senior Software Engineer (Remote)" from RemoteOK and
    "Acme Inc" / "Senior Software Engineer" from WeWorkRemotely land on the
    same value.

The first key is authoritative. The second is advisory: it groups rows for the
dashboard's "unique jobs" count but never causes a row to be discarded, because
a false positive there would silently lose real data.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Legal / structural suffixes that carry no identity information.
_COMPANY_SUFFIXES = (
    "inc",
    "inc.",
    "llc",
    "l.l.c",
    "ltd",
    "ltd.",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "company",
    "co",
    "co.",
    "gmbh",
    "bv",
    "b.v.",
    "nv",
    "n.v.",
    "plc",
    "ag",
    "sa",
    "s.a.",
    "srl",
    "s.r.l",
    "oy",
    "ab",
    "as",
    "pty",
    "pte",
    "kk",
    "kft",
    "sp z o o",
    "the",
)

# Parenthetical / bracketed noise commonly appended to titles.
_TITLE_NOISE = re.compile(
    r"\b(remote|fully[\s-]?remote|hybrid|onsite|on[\s-]?site|full[\s-]?time|part[\s-]?time|"
    r"contract|permanent|freelance|w2|c2c|visa|relocation|urgent|hiring|new|us|usa|uk|emea|"
    r"anywhere|worldwide|m/f/d|m/w/d|h/f|f/m/d|all genders)\b",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_BRACKETED = re.compile(r"[\(\[\{][^)\]\}]*[\)\]\}]")


def strip_accents(value: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )


def collapse_ws(value: str) -> str:
    return _WS.sub(" ", value).strip()


def normalize_text(value: str | None) -> str:
    """Lowercase, de-accent, strip punctuation, collapse whitespace."""
    if not value:
        return ""
    out = strip_accents(str(value)).lower()
    out = _NON_ALNUM.sub(" ", out)
    return collapse_ws(out)


def normalize_company(value: str | None) -> str:
    """Normalise a company name for fingerprinting.

    Drops bracketed asides and trailing legal suffixes: ``Acme, Inc. (YC S21)``
    and ``ACME Inc`` both become ``acme``.
    """
    if not value:
        return ""
    out = _BRACKETED.sub(" ", str(value))
    out = normalize_text(out)
    tokens = out.split()
    # Peel suffixes from the end; a company called only "The" keeps its token.
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    while len(tokens) > 1 and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def normalize_title(value: str | None) -> str:
    """Normalise a role title, dropping employment-mode and location noise."""
    if not value:
        return ""
    out = _BRACKETED.sub(" ", str(value))
    out = _TITLE_NOISE.sub(" ", out)
    out = normalize_text(out)
    return out


def normalize_location(value: str | None) -> str:
    """Normalise a location, folding the many spellings of "remote/anywhere"."""
    if not value:
        return ""
    out = normalize_text(value)
    if not out:
        return ""
    remote_aliases = {
        "anywhere",
        "anywhere in the world",
        "worldwide",
        "world wide",
        "global",
        "remote",
        "fully remote",
        "remote worldwide",
        "remote anywhere",
        "100 remote",
        "probably anywhere",
    }
    if out in remote_aliases:
        return "remote"
    return out


def sha256_of(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def make_dedup_key(source: str, external_id: str) -> str:
    """Primary identity: stable per (source, source-assigned id)."""
    return sha256_of("v1", source.strip().lower(), str(external_id).strip())


def make_content_fingerprint(
    company: str | None, title: str | None, location: str | None
) -> str:
    """Advisory cross-source identity. Never used to discard a row."""
    return sha256_of(
        "v1",
        normalize_company(company),
        normalize_title(title),
        normalize_location(location),
    )


def make_content_hash(payload: dict) -> str:
    """Hash of the mutable fields of a posting.

    Lets an upsert skip the UPDATE (and leave ``revision`` alone) when a row
    comes back byte-identical, which is the common case on a daily re-run.
    """
    items = sorted((k, "" if v is None else str(v)) for k, v in payload.items())
    return sha256_of("v1", *[f"{k}={v}" for k, v in items])
