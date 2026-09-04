"""Vercel serverless entry point for the dashboard.

Vercel's Python runtime serves an ASGI app exported as ``app`` from a module
under ``api/``. The package itself lives in ``src/``, which is not on the path
inside the build, so it is added here rather than by installing the project --
Vercel installs from ``requirements.txt`` and does not run a project build.
"""

from __future__ import annotations

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from jobradar.dashboard import app  # noqa: E402

__all__ = ["app"]
