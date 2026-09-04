"""Source registry."""

from __future__ import annotations

from ..config import Settings
from .base import RawRecord, Source, Task, html_to_text
from .hackernews import HackerNewsSource
from .remoteok import RemoteOKSource
from .weworkremotely import WeWorkRemotelySource

_REGISTRY = {
    "hackernews": HackerNewsSource,
    "weworkremotely": WeWorkRemotelySource,
    "remoteok": RemoteOKSource,
}


def get_sources(settings: Settings) -> list[Source]:
    """Instantiate the sources enabled for this run, in registry order."""
    return [
        _REGISTRY[name]()
        for name in settings.enabled_sources
        if name in _REGISTRY
    ]


__all__ = [
    "get_sources",
    "Source",
    "Task",
    "RawRecord",
    "html_to_text",
    "HackerNewsSource",
    "WeWorkRemotelySource",
    "RemoteOKSource",
]
