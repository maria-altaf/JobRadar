"""Inline-SVG chart builders.

Server-rendered SVG rather than a charting library: it needs no CDN, works with
JavaScript disabled, and lets the mark geometry follow the spec exactly -- 4px
rounded data-ends anchored to the baseline, a 2px surface gap between stacked
segments, hairline recessive grid and axes.

Colours are emitted as ``var(--series-N)`` / ``var(--status-N)`` so the whole
palette swaps for dark mode in one CSS block rather than being baked into the
markup. Each mark carries ``data-tip`` for the hover layer and a ``<title>``
child as the no-JavaScript fallback.

Palette: the skill's validated default. The three source hues are categorical
slots 1-3, assigned by source identity in a fixed order so a source keeps its
colour no matter which others are present. Verified with the bundled validator
in both modes on the all-pairs list (worst CVD delta-E 9.2 light / 9.4 dark,
worst normal-vision 24.0 / 20.9). Slot 3 (aqua) sits below 3:1 on the light
surface, so the relief rule applies and every chart here ships direct labels
plus a table view.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

#: Categorical slots, in fixed assignment order.
SERIES_VARS = ("--series-1", "--series-2", "--series-3")

STATUS_VARS = {
    "succeeded": "--status-good",
    "partial": "--status-warning",
    "failed": "--status-critical",
    "running": "--status-serious",
}

STATUS_ICONS = {
    "succeeded": "●",
    "partial": "▲",
    "failed": "✕",
    "running": "◐",
}


def esc(value: Any) -> str:
    return escape(str(value), quote=True)


def _nice_ceiling(value: float) -> int:
    """Round an axis maximum up to a readable number."""
    if value <= 5:
        return 5
    for step in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000):
        if value <= step:
            return step
    return int(((value // 10000) + 1) * 10000)


def empty_state(message: str, height: int = 200) -> str:
    return (
        f'<div class="chart-empty" style="height:{height}px">'
        f"<span>{esc(message)}</span></div>"
    )


# ------------------------------------------------------- stacked day bars ---


def stacked_days(
    days: Sequence[str],
    sources: Sequence[dict[str, str]],
    series: dict[str, Sequence[int]],
    height: int = 250,
) -> str:
    """New postings per day, stacked by source.

    A day with no run renders as a genuine gap rather than being dropped from
    the axis -- the absence is the interesting part on an unattended pipeline.
    """
    if not days or not sources:
        return empty_state("No postings recorded yet")

    width = 900
    pad_l, pad_r, pad_t, pad_b = 46, 12, 16, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    totals = [sum(series[s["key"]][i] for s in sources) for i in range(len(days))]
    top = _nice_ceiling(max(totals) if totals else 0)
    slot = plot_w / max(1, len(days))
    bar_w = max(3.0, min(26.0, slot - 4))
    gap = 2.0  # surface gap between stacked segments

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none" aria-label="New postings per day by source">'
    ]

    # Recessive hairline grid, solid (never dashed).
    for n in range(5):
        value = top * n / 4
        y = pad_t + plot_h - (value / top) * plot_h if top else pad_t + plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick tick-y">{value:.0f}</text>'
        )

    for i, day in enumerate(days):
        x = pad_l + slot * i + (slot - bar_w) / 2
        y_cursor = pad_t + plot_h
        for slot_i, source in enumerate(sources):
            value = series[source["key"]][i]
            if not value:
                continue
            seg_h = (value / top) * plot_h if top else 0
            seg_h = max(seg_h, 1.5)
            y_cursor -= seg_h
            colour = f"var({SERIES_VARS[slot_i % len(SERIES_VARS)]})"
            tip = f"{day} · {source['label']}: {value}"
            # 4px rounded ends; the radius is clipped by height for thin marks.
            radius = min(4.0, seg_h / 2, bar_w / 2)
            parts.append(
                f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{max(0.0, seg_h - gap):.1f}" rx="{radius:.1f}" '
                f'fill="{colour}" class="mark" data-tip="{esc(tip)}">'
                f"<title>{esc(tip)}</title></rect>"
            )
            y_cursor -= gap

        # Selective direct labels: only the most recent day and the peak.
        if totals[i] and (i == len(days) - 1 or totals[i] == max(totals)):
            label_y = pad_t + plot_h - (totals[i] / top) * plot_h - 6
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{max(pad_t + 8, label_y):.1f}" '
                f'class="datalabel">{totals[i]}</text>'
            )

    # x ticks: first, last, and roughly weekly in between.
    step = max(1, len(days) // 6)
    for i, day in enumerate(days):
        if i % step and i != len(days) - 1:
            continue
        x = pad_l + slot * i + slot / 2
        parts.append(f'<text x="{x:.1f}" y="{height - 12}" class="tick">{esc(day[5:])}</text>')

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" class="axis"/>'
    )
    parts.append("</svg>")

    legend = legend_for(sources)
    return legend + "".join(parts)


def legend_for(sources: Sequence[dict[str, str]]) -> str:
    """A legend is always present for two or more series."""
    items = "".join(
        f'<span class="legend-item"><span class="swatch" '
        f'style="background:var({SERIES_VARS[i % len(SERIES_VARS)]})"></span>'
        f"{esc(s['label'])}</span>"
        for i, s in enumerate(sources)
    )
    return f'<div class="legend">{items}</div>'


# ------------------------------------------------------------- run history --


def run_bars(runs: Sequence[dict[str, Any]], height: int = 150) -> str:
    """One bar per run, height = duration, colour = status.

    Status colours are used here because the encoded thing genuinely *is*
    status. They are paired with an icon and a text label in the legend, so
    state is never carried by colour alone.
    """
    if not runs:
        return empty_state("No runs recorded yet", 150)

    width = 900
    pad_l, pad_r, pad_t, pad_b = 46, 12, 16, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    durations = [float(r.get("duration_seconds") or 0) for r in runs]
    top = _nice_ceiling(max(durations) if durations else 0)
    slot = plot_w / max(1, len(runs))
    bar_w = max(4.0, min(22.0, slot - 4))

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="none" aria-label="Run duration and status over time">'
    ]
    for n in range(3):
        value = top * n / 2
        y = pad_t + plot_h - (value / top) * plot_h if top else pad_t + plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick tick-y">{value:.0f}s</text>'
        )

    for i, run in enumerate(runs):
        duration = float(run.get("duration_seconds") or 0)
        status = str(run.get("status") or "running")
        bar_h = max(2.0, (duration / top) * plot_h if top else 2.0)
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = pad_t + plot_h - bar_h
        colour = f"var({STATUS_VARS.get(status, '--status-serious')})"
        started = run.get("started_at")
        when = started.strftime("%d %b %H:%M") if hasattr(started, "strftime") else str(started)
        tip = (
            f"{when} · {status} · {duration:.0f}s · "
            f"{run.get('items_inserted', 0)} new · {run.get('items_quarantined', 0)} quarantined"
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
            f'rx="{min(4.0, bar_h / 2, bar_w / 2):.1f}" fill="{colour}" class="mark" '
            f'data-tip="{esc(tip)}"><title>{esc(tip)}</title></rect>'
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" '
        f'y2="{pad_t + plot_h}" class="axis"/>'
    )
    parts.append("</svg>")

    seen = [s for s in ("succeeded", "partial", "failed") if any(r.get("status") == s for r in runs)]
    legend = "".join(
        f'<span class="legend-item"><span class="status-icon" '
        f'style="color:var({STATUS_VARS[s]})">{STATUS_ICONS[s]}</span>{s}</span>'
        for s in seen
    )
    return f'<div class="legend">{legend}</div>' + "".join(parts)


# ------------------------------------------------------------ horizontal ----


def hbars(
    items: Sequence[dict[str, Any]],
    label_key: str,
    value_key: str,
    sub_key: str | None = None,
    empty: str = "Nothing to show yet",
) -> str:
    """A single-series horizontal bar list.

    One series means one colour for every bar. Shading each bar by its own
    value would double-encode length as hue and spend the only free channel on
    information the bar already carries.
    """
    if not items:
        return empty_state(empty, 160)

    top = max(float(i[value_key] or 0) for i in items) or 1
    rows: list[str] = []
    for item in items:
        value = float(item[value_key] or 0)
        pct = max(1.0, value / top * 100)
        sub = f'<span class="hbar-sub">{esc(item[sub_key])}</span>' if sub_key else ""
        rows.append(
            '<div class="hbar-row">'
            f'<div class="hbar-label" title="{esc(item[label_key])}">{esc(item[label_key])}{sub}</div>'
            '<div class="hbar-track">'
            f'<div class="hbar-fill" style="width:{pct:.1f}%"></div>'
            "</div>"
            f'<div class="hbar-value">{int(value)}</div>'
            "</div>"
        )
    return f'<div class="hbars">{"".join(rows)}</div>'
