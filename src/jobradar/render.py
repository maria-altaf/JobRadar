"""HTML rendering for the dashboard.

Separated from the web app on purpose: turning a database snapshot into a page
is a pure function of that snapshot, and it has no business importing a web
framework to do it.

That is not tidiness for its own sake. The static build installs only the
scraper's dependencies, so a ``from fastapi import ...`` at the top of this
module made ``jobradar export`` fail on Netlify with ModuleNotFoundError --
a page renderer that cannot render without a web server is the bug, not the
build. Both the live app and the static exporter now import from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import charts, queries
from .charts import esc


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ------------------------------------------------------------------ HTML ----


def _ago(value: datetime | None) -> str:
    if not isinstance(value, datetime):
        return "never"
    seconds = (datetime.now(UTC) - value).total_seconds()
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _fmt(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


def _salary(row: dict[str, Any]) -> str:
    lo, hi, cur = row.get("salary_min"), row.get("salary_max"), row.get("salary_currency") or ""
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(cur, "")
    if lo and hi:
        return f"{symbol}{lo // 1000}k–{symbol}{hi // 1000}k"
    if lo:
        return f"{symbol}{lo // 1000}k+"
    return "—"


def stat_tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="tile-note">{esc(note)}</div>' if note else ""
    return (
        f'<div class="tile"><div class="tile-label">{esc(label)}</div>'
        f'<div class="tile-value">{esc(value)}</div>{note_html}</div>'
    )


def table_view(headers: list[str], rows: list[list[str]], caption: str) -> str:
    """The table companion every chart ships.

    Required here rather than optional: one of the three series colours sits
    below 3:1 against the light surface, and the relief rule for that is direct
    labels plus a table view.
    """
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return (
        f'<details class="table-view"><summary>{esc(caption)}</summary>'
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div></details>"
    )


def render_page(snap: dict[str, Any], recent: list[dict[str, Any]]) -> str:
    health = snap["health"]
    totals = snap["totals"]
    daily = snap["daily"]
    runs = snap["runs"]

    # ---- health banner: colour is never the only channel, so icon + text too.
    #
    # The age and the state are also recomputed in the browser from the
    # timestamps carried in the data- attributes. On the statically exported
    # build the HTML is only as fresh as the last deploy, so a server-rendered
    # "last success 2 h ago" would still claim 2 h a week later -- a health
    # indicator that goes stale is worse than none at all, because it reports
    # green precisely when things have stopped.
    icon = {"good": "●", "warning": "▲", "critical": "✕"}[health["state"]]
    last_run = health.get("last_run") or {}
    banner = f"""
    <section class="banner banner-{health['state']}" role="status" id="health-banner"
             data-last-success="{esc(_iso(health['last_success_at']) or '')}"
             data-stale-hours="{health['stale_after_hours']}"
             data-state="{health['state']}">
      <div class="banner-main">
        <span class="banner-icon" data-role="icon">{icon}</span>
        <div>
          <div class="banner-title" data-role="title">{esc(health['label'])}</div>
          <div class="banner-sub">
            Last successful run <span data-role="age">{esc(_ago(health['last_success_at']))}</span>
            &middot; {health['consecutive_days']} consecutive day(s) green
            &middot; considered stale after {health['stale_after_hours']}h
          </div>
        </div>
      </div>
      <div class="banner-side">
        <div class="banner-side-label">Most recent run</div>
        <div class="banner-side-value">{esc(last_run.get('status', 'none'))}</div>
        <div class="banner-side-sub"
             data-role="age" data-at="{esc(_iso(last_run.get('started_at')) or '')}"
             >{esc(_ago(last_run.get('started_at')))}</div>
      </div>
    </section>
    """

    tiles = "".join(
        [
            stat_tile("Postings stored", _fmt(totals["jobs"]), f"+{_fmt(totals['added_24h'])} in 24h"),
            stat_tile("Distinct jobs", _fmt(totals["distinct_groups"]),
                      "after fingerprint grouping"),
            stat_tile("Companies", _fmt(totals["companies"])),
            stat_tile("Duplicates collapsed", _fmt(totals["duplicates_collapsed"]),
                      f"{_fmt(totals['cross_source_groups'])} span 2+ sources"),
            stat_tile("With salary", _fmt(totals["with_salary"])),
            stat_tile("Quarantined last run", _fmt(totals["quarantined_last_run"]),
                      f"{_fmt(totals['quarantined'])} all-time, kept with reasons"),
        ]
    )

    # ---- daily chart + its table companion
    daily_chart = charts.stacked_days(daily["days"], daily["sources"], daily["series"])
    daily_rows = [
        [esc(day)] + [str(daily["series"][s["key"]][i]) for s in daily["sources"]]
        + [f"<strong>{sum(daily['series'][s['key']][i] for s in daily['sources'])}</strong>"]
        for i, day in enumerate(daily["days"])
    ][::-1]
    daily_table = table_view(
        ["Day"] + [s["label"] for s in daily["sources"]] + ["Total"],
        daily_rows,
        "View as table",
    )

    # ---- runs chart + table
    runs_chart = charts.run_bars(runs)
    runs_rows = [
        [
            esc(r["started_at"].strftime("%d %b %H:%M") if r.get("started_at") else "—"),
            f'<span class="pill pill-{esc(r["status"])}">{esc(r["status"])}</span>',
            f"{(r.get('duration_seconds') or 0):.0f}s",
            f"{r.get('tasks_done', 0)}/{r.get('tasks_total', 0)}",
            _fmt(r.get("items_inserted")),
            _fmt(r.get("items_updated")),
            _fmt(r.get("items_quarantined")),
            _fmt(r.get("requests_made")),
            _fmt(r.get("retries_made")),
            "yes" if (r.get("resumed_count") or 0) else "—",
        ]
        for r in reversed(runs)
    ]
    runs_table = table_view(
        ["Started", "Status", "Duration", "Tasks", "New", "Updated",
         "Quarantined", "Requests", "Retries", "Resumed"],
        runs_rows,
        "View run history as table",
    )

    companies_chart = charts.hbars(
        snap["top_companies"], "company", "count", empty="No companies yet"
    )
    quarantine_chart = charts.hbars(
        snap["quarantine_breakdown"], "code", "count", sub_key="label",
        empty="Nothing quarantined — good news",
    )

    # ---- dedup evidence
    dup_rows = ""
    for group in snap["duplicates"]:
        links = " ".join(
            f'<a class="src-link" href="{esc(r["url"])}" target="_blank" rel="noopener">'
            f'{esc(r["label"])}</a>'
            for r in group["rows"]
        )
        dup_rows += (
            f'<tr><td><strong>{esc(group["company"])}</strong></td>'
            f'<td>{esc(group["title"])}</td>'
            f'<td>{len(group["rows"])} × {links}</td>'
            f'<td class="nowrap muted">{esc(group["kind"])}</td>'
            f'<td class="mono">{esc(group["fingerprint"])}</td></tr>'
        )
    dup_section = (
        f'<div class="table-wrap"><table><thead><tr><th>Company</th><th>Role</th>'
        f"<th>Rows</th><th>Kind</th><th>Fingerprint</th></tr></thead>"
        f"<tbody>{dup_rows}</tbody></table></div>"
        if dup_rows
        else '<p class="muted">No duplicate groups yet.</p>'
    )

    # ---- quarantine sample
    q_rows = "".join(
        f'<tr><td>{esc(queries.SOURCE_LABELS.get(q["source"], q["source"]))}</td>'
        f'<td class="reason">{esc(q["reason"])}</td>'
        f'<td class="mono nowrap">{esc(_ago(q["created_at"]))}</td></tr>'
        for q in snap["recent_quarantine"]
    )
    q_section = (
        f'<div class="table-wrap"><table><thead><tr><th>Source</th><th>Why it was held back</th>'
        f"<th>When</th></tr></thead><tbody>{q_rows}</tbody></table></div>"
        if q_rows
        else '<p class="muted">Nothing has been quarantined.</p>'
    )

    # ---- recent postings
    job_rows = "".join(
        f'<tr><td><a href="{esc(j["url"])}" target="_blank" rel="noopener">'
        f'<strong>{esc(j["title"])}</strong></a></td>'
        f'<td>{esc(j["company"])}</td>'
        f'<td>{esc(j.get("location") or "—")}</td>'
        f'<td class="nowrap">{esc(_salary(j))}</td>'
        f'<td><span class="src src-{esc(j["source"])}">'
        f'{esc(queries.SOURCE_LABELS.get(j["source"], j["source"]))}</span></td>'
        f'<td class="mono nowrap">{esc(_ago(j.get("first_seen_at")))}</td></tr>'
        for j in recent
    )

    generated = snap["generated_at"].strftime("%d %b %Y %H:%M UTC")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobradar — remote job market pipeline</title>
<meta name="description" content="An unattended pipeline scraping remote job postings from Hacker News, We Work Remotely and Remote OK.">
<style>{CSS}</style>
</head>
<body>
<div class="viz-root">

<header class="head">
  <div>
    <h1>jobradar</h1>
    <p class="sub">Remote job postings from three sources, collected unattended every day.</p>
  </div>
  <button id="theme" class="theme-btn" type="button" aria-label="Toggle colour theme">◐</button>
</header>

{banner}

<section class="tiles">{tiles}</section>

<section class="card">
  <div class="card-head">
    <h2>New postings per day</h2>
    <p class="card-sub">Counted from when the pipeline first saw a posting. A gap means no run that day.</p>
  </div>
  {daily_chart}
  {daily_table}
</section>

<section class="card">
  <div class="card-head">
    <h2>Run history</h2>
    <p class="card-sub">Bar height is run duration; colour and icon are the outcome.</p>
  </div>
  {runs_chart}
  {runs_table}
</section>

<div class="grid-2">
  <section class="card">
    <div class="card-head">
      <h2>Most active companies</h2>
      <p class="card-sub">By number of postings stored.</p>
    </div>
    {companies_chart}
  </section>

  <section class="card">
    <div class="card-head">
      <h2>Why rows get quarantined</h2>
      <p class="card-sub">Grouped by failure mode. A shift in this mix is how a broken parser announces itself.</p>
    </div>
    {quarantine_chart}
  </section>
</div>

<section class="card">
  <div class="card-head">
    <h2>The same job, found twice</h2>
    <p class="card-sub">Matched by a normalised company + role + location fingerprint —
      either the same role on two sources, or one company reposting into consecutive
      Hacker News threads. Every row is kept; the fingerprint groups them, it never deletes one.</p>
  </div>
  {dup_section}
</section>

<section class="card">
  <div class="card-head">
    <h2>Quarantine</h2>
    <p class="card-sub">Rows that failed validation, kept with the reason attached rather than dropped.</p>
  </div>
  {q_section}
</section>

<section class="card">
  <div class="card-head">
    <h2>Latest postings</h2>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Role</th><th>Company</th><th>Location</th><th>Salary</th><th>Source</th><th>Seen</th></tr></thead>
      <tbody>{job_rows}</tbody>
    </table>
  </div>
</section>

<footer class="foot">
  <p>
    Sources:
    <a href="https://news.ycombinator.com/item?id=49522897" target="_blank" rel="noopener">Hacker News “Who is hiring?”</a>,
    <a href="https://weworkremotely.com" target="_blank" rel="noopener">We Work Remotely</a>, and
    <a href="https://remoteok.com" target="_blank" rel="noopener">Remote OK</a>.
  </p>
  <p class="muted">
    Job data from <a href="https://remoteok.com" target="_blank" rel="noopener">Remote OK</a>,
    used under its API terms. Postings link back to their original listing.
    Page generated {esc(generated)}.
  </p>
  <p class="muted">
    <a href="/api/health">/api/health</a> &middot;
    <a href="/api/stats">/api/stats</a> &middot;
    <a href="/api/jobs">/api/jobs</a>
  </p>
</footer>

</div>
<div id="tip" class="tip" role="tooltip" hidden></div>
<script>{JS}</script>
</body>
</html>"""


CSS = """
*,*::before,*::after{box-sizing:border-box}
.viz-root{
  color-scheme:light;
  --surface-1:#fcfcfb; --page:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a;
  --status-good:#0ca30c; --status-warning:#fab219;
  --status-serious:#ec835a; --status-critical:#d03b3b;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])) .viz-root{
    color-scheme:dark;
    --surface-1:#1a1a19; --page:#0d0d0d;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
  }
}
:root[data-theme="dark"] .viz-root{
  color-scheme:dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70;
}
html,body{margin:0;padding:0}
body{background:var(--page);}
.viz-root{
  background:var(--page); color:var(--text-primary);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  max-width:1180px; margin:0 auto; padding:28px 20px 60px;
}
h1{font-size:26px;margin:0;letter-spacing:-0.01em}
h2{font-size:15px;margin:0;font-weight:600}
.sub{margin:4px 0 0;color:var(--text-secondary)}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:20px}
.theme-btn{background:var(--surface-1);border:1px solid var(--border);color:var(--text-secondary);
  border-radius:8px;width:34px;height:34px;font-size:15px;cursor:pointer;flex:none}

/* health banner: icon + text carry the state, colour only reinforces it */
.banner{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;
  background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
  padding:16px 18px;margin-bottom:18px;border-left-width:4px;border-left-style:solid}
.banner-good{border-left-color:var(--status-good)}
.banner-warning{border-left-color:var(--status-warning)}
.banner-critical{border-left-color:var(--status-critical)}
.banner-main{display:flex;gap:14px;align-items:center}
.banner-icon{font-size:20px;line-height:1}
.banner-good .banner-icon{color:var(--status-good)}
.banner-warning .banner-icon{color:var(--status-warning)}
.banner-critical .banner-icon{color:var(--status-critical)}
.banner-title{font-weight:600;font-size:15px}
.banner-sub{color:var(--text-secondary);font-size:12.5px;margin-top:2px}
.banner-side{text-align:right}
.banner-side-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.banner-side-value{font-weight:600;margin-top:2px}
.banner-side-sub{color:var(--text-secondary);font-size:12px}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin-bottom:18px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile-label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.tile-value{font-size:26px;font-weight:600;margin-top:4px;letter-spacing:-0.02em}
.tile-note{color:var(--text-secondary);font-size:12px;margin-top:2px}

.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;
  padding:18px 18px 14px;margin-bottom:16px}
.card-head{margin-bottom:12px}
.card-sub{margin:3px 0 0;color:var(--text-secondary);font-size:12.5px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.grid-2{grid-template-columns:1fr}}

.chart{width:100%;height:auto;display:block;overflow:visible}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .axis{stroke:var(--axis);stroke-width:1}
.chart .tick{fill:var(--muted);font-size:10.5px;text-anchor:middle;font-variant-numeric:tabular-nums}
.chart .tick-y{text-anchor:end}
.chart .datalabel{fill:var(--text-secondary);font-size:10.5px;text-anchor:middle;font-weight:600}
.chart .mark{transition:opacity .12s}
.chart:hover .mark{opacity:.55}
.chart .mark:hover{opacity:1}
.chart-empty{display:flex;align-items:center;justify-content:center;color:var(--muted);
  border:1px dashed var(--grid);border-radius:8px}

.legend{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:10px;font-size:12px;color:var(--text-secondary)}
.legend-item{display:inline-flex;align-items:center;gap:6px}
.swatch{width:10px;height:10px;border-radius:3px;display:inline-block}
.status-icon{font-size:12px}

.hbars{display:flex;flex-direction:column;gap:7px}
.hbar-row{display:grid;grid-template-columns:minmax(90px,1.35fr) 3fr auto;gap:10px;align-items:center}
.hbar-label{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hbar-sub{color:var(--muted);margin-left:6px;font-size:11px}
.hbar-track{background:var(--grid);border-radius:4px;height:10px;overflow:hidden}
.hbar-fill{background:var(--series-1);height:100%;border-radius:4px}
.hbar-value{font-variant-numeric:tabular-nums;color:var(--text-secondary);font-size:12.5px;min-width:34px;text-align:right}

.table-view{margin-top:12px;border-top:1px solid var(--border);padding-top:8px}
.table-view summary{cursor:pointer;color:var(--text-secondary);font-size:12.5px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--text-secondary)}
.nowrap{white-space:nowrap}
.reason{color:var(--text-secondary);max-width:640px}
a{color:var(--series-1);text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:var(--muted);font-size:12px}

.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border)}
.pill-succeeded{color:var(--status-good)}
.pill-partial{color:var(--status-warning)}
.pill-failed{color:var(--status-critical)}
.pill-running{color:var(--status-serious)}
.src{font-size:11px;color:var(--text-secondary);white-space:nowrap}
.src-link{font-size:11.5px;margin-right:8px}

.foot{margin-top:28px;padding-top:16px;border-top:1px solid var(--border);color:var(--text-secondary);font-size:12.5px}
.foot p{margin:6px 0}

.tip{position:fixed;z-index:50;background:var(--text-primary);color:var(--surface-1);
  padding:6px 9px;border-radius:6px;font-size:11.5px;pointer-events:none;max-width:280px;
  box-shadow:0 4px 14px rgba(0,0,0,.18)}
"""

JS = """
(function(){
  var root=document.documentElement, btn=document.getElementById('theme');
  try{ var saved=localStorage.getItem('jobradar-theme'); if(saved) root.dataset.theme=saved; }catch(e){}
  btn && btn.addEventListener('click',function(){
    var next = root.dataset.theme==='dark' ? 'light' : 'dark';
    root.dataset.theme=next;
    try{ localStorage.setItem('jobradar-theme',next); }catch(e){}
  });

  // --- keep the health indicator honest on a statically exported page ------
  // The HTML is rendered when the pipeline last ran. Ages baked in at render
  // time would still read "2 h ago" days later, so they are recomputed here
  // from the ISO timestamps, and the state is re-derived against the same
  // staleness threshold the server uses. A frozen green banner is the one
  // failure this whole page exists to make impossible.
  function ago(ms){
    var s = ms/1000;
    if (s < 90) return Math.max(0, Math.round(s)) + 's ago';
    if (s < 5400) return Math.round(s/60) + 'm ago';
    if (s < 172800) return Math.round(s/3600) + 'h ago';
    return Math.round(s/86400) + 'd ago';
  }
  function freshen(){
    var b = document.getElementById('health-banner');
    if (!b) return;
    var now = Date.now();

    b.querySelectorAll('[data-role="age"]').forEach(function(el){
      var iso = el.dataset.at || b.dataset.lastSuccess;
      if (iso) { var t = Date.parse(iso); if (!isNaN(t)) el.textContent = ago(now - t); }
    });

    var iso = b.dataset.lastSuccess;
    if (!iso) return;
    var t = Date.parse(iso);
    if (isNaN(t)) return;
    var hours = (now - t) / 3600000;
    var limit = parseFloat(b.dataset.staleHours || '30');
    var icon = b.querySelector('[data-role="icon"]');
    var title = b.querySelector('[data-role="title"]');

    if (hours > limit) {
      // It has gone stale since this page was built. Say so.
      if (b.dataset.state !== 'critical') {
        b.classList.remove('banner-good', 'banner-warning');
        b.classList.add('banner-critical');
        b.dataset.state = 'critical';
        if (icon) icon.textContent = '✕';
      }
      if (title) title.textContent = 'Stale — no successful run in ' + ago(now - t).replace(' ago','');
    } else if (b.dataset.state === 'good' && title) {
      // Still healthy, but the headline embeds an age that was rendered at
      // build time. Left alone it would keep reporting "41 min ago" for up to
      // the whole staleness window -- green and wrong, which is the failure
      // this recomputation exists to prevent.
      title.textContent = 'Healthy — last success ' + ago(now - t);
    }
  }
  freshen();
  setInterval(freshen, 60000);
  // A dashboard gets left open on a second monitor overnight. Re-check when
  // the tab is looked at again rather than making the reader wait up to a
  // minute to find out the pipeline died while they were away.
  window.addEventListener('focus', freshen);
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden) freshen();
  });

  // Hover layer for the SVG marks. <title> children remain as the fallback.
  var tip=document.getElementById('tip');
  function show(e){
    var t=e.target.getAttribute && e.target.getAttribute('data-tip');
    if(!t){ return; }
    tip.textContent=t; tip.hidden=false;
    var x=e.clientX+12, y=e.clientY+12;
    if(x+tip.offsetWidth>window.innerWidth-8){ x=e.clientX-tip.offsetWidth-12; }
    if(y+tip.offsetHeight>window.innerHeight-8){ y=e.clientY-tip.offsetHeight-12; }
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }
  document.addEventListener('mousemove',function(e){
    if(e.target.closest && e.target.closest('.chart') && e.target.getAttribute('data-tip')){ show(e); }
    else { tip.hidden=true; }
  });
})();
"""
