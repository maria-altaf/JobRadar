#!/usr/bin/env python
"""Capture dashboard screenshots for the README.

Run after `jobradar export --out dist`. The exported page inlines all its CSS
and JS, so it renders correctly straight from a file:// URL with no server.

Both colour schemes are captured because both were designed and validated --
the categorical palette was checked for contrast and colour-vision deficiency
against the light *and* dark chart surfaces, and a README that only ever shows
one of them hides half of that.

    python scripts/screenshot.py
"""

from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
DIST = REPO / "dist" / "index.html"
OUT = REPO / "docs"

WIDTH = 1400
HERO_HEIGHT = 1000

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright is not installed:  pip install playwright && playwright install chromium")
    sys.exit(1)


def main() -> int:
    if not DIST.exists():
        print(f"{DIST} does not exist — run `jobradar export --out dist` first")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    url = DIST.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for scheme in ("light", "dark"):
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": HERO_HEIGHT},
                    color_scheme=scheme,
                    device_scale_factor=2,  # legible when GitHub scales it down
                )
                page = context.new_page()
                page.goto(url)
                # The page recomputes its own freshness on load; let that settle
                # so the banner in the shot is the one a visitor would see.
                page.wait_for_timeout(900)

                suffix = "" if scheme == "light" else "-dark"

                hero = OUT / f"dashboard{suffix}.png"
                page.screenshot(path=str(hero))
                print(f"  {hero.relative_to(REPO)}  ({hero.stat().st_size / 1024:.0f} KB)")

                full = OUT / f"dashboard-full{suffix}.png"
                page.screenshot(path=str(full), full_page=True)
                print(f"  {full.relative_to(REPO)}  ({full.stat().st_size / 1024:.0f} KB)")

                context.close()
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
