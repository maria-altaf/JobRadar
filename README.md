# jobradar

A remote-job data pipeline that runs itself. It scrapes three sources on a
schedule, validates and stores what it finds, and serves a dashboard over the
result.

The point of the project is not the scraping. It is everything around it: what
happens when a source changes its HTML, when the network drops halfway through,
and when the whole thing runs twice by accident.

<!-- BADGES -->
<!-- LIVE-URL -->

---

## What it does

| | |
|---|---|
| **Sources** | Hacker News "Ask HN: Who is hiring?" (HTML), We Work Remotely (RSS), Remote OK (JSON) |
| **Schedule** | GitHub Actions cron, 06:15 UTC daily |
| **Storage** | Postgres in production, SQLite for local development and CI |
| **Dashboard** | Server-rendered FastAPI page + JSON API |
| **Alerting** | A failed run exits non-zero → the Actions job fails → GitHub emails, and a tracking issue is opened and auto-closed on recovery |

Three sources rather than one, on purpose. They fail in different ways: HN is
free-form HTML held together by a *convention* that nothing enforces, WWR is
well-structured RSS that packs two fields into one string, and Remote OK is a
clean JSON API whose tag endpoints return the same posting several times over.
Each exercises a different part of the design.

---

## Deduplication strategy

Duplicates arrive from four directions, and the design answers each one
separately. This is the part of the project worth reading closely.

### The two keys

Every posting gets two hashes, and they answer different questions.

**`dedup_key` — "is this the same posting I already have?"**

```
sha256("v1" | source | external_id)
```

Derived from the identifier the *source itself* assigns: the HN comment id, the
WWR `<guid>` URL, the Remote OK numeric id. It is the `UNIQUE` column that every
write targets:

```sql
INSERT INTO jobs (...) VALUES (...)
ON CONFLICT (dedup_key) DO UPDATE SET ...
```

There is **no code path that inserts a second copy of a posting**. Not "we check
first" — the database constraint makes it impossible. That is what makes the
pipeline safe to re-run, safe to resume, and safe to run twice by accident.

**`content_fingerprint` — "is this the same *job* as one from another source?"**

```
sha256("v1" | normalised(company) | normalised(title) | normalised(location))
```

Deliberately lossy. Normalisation strips legal suffixes (`Acme, Inc.` → `acme`),
bracketed asides (`Acme (YC S21)` → `acme`), employment-mode and location noise
from titles (`Senior Software Engineer (Remote)` → `senior software engineer`),
and folds the many spellings of remote (`Anywhere in the World`, `Worldwide`,
`REMOTE` → `remote`). So these two rows land on the same fingerprint:

| Source | Company | Title | Location |
|---|---|---|---|
| Remote OK | `Acme, Inc.` | `Senior Software Engineer (Remote)` | `Anywhere in the World` |
| We Work Remotely | `ACME Inc` | `Senior Software Engineer` | `Remote` |

**The fingerprint never deletes anything.** It groups rows for counting and for
the dashboard's "distinct jobs" figure; both source rows are kept, with their
own URLs, because a false positive here would silently destroy real data. The
authoritative key is `dedup_key`; the fingerprint is advisory. That asymmetry is
the whole safety argument: the strict key decides what exists, the fuzzy key
only decides what gets counted together.

### The four cases

**1. The same posting inside one response.** Remote OK's tag endpoints overlap
heavily — one posting routinely appears under `dev`, `engineer` *and* the
unfiltered feed. A single response can also repeat a row.

Handled by collapsing the batch on `dedup_key` before it is sent, because
`ON CONFLICT` cannot resolve a row against another row in its own statement.
Measured on a real run: **1,199 records fetched → 906 unique rows stored.**

**2. Running the pipeline twice.** The second run's upsert finds every
`dedup_key` already present and updates in place.

```
run 1:  1199 records seen → 906 new,   0 unchanged
run 2:  1199 records seen →   0 new, 906 unchanged     ← table still 906 rows
```

**3. Resuming a killed run.** The task that was in flight when the process died
is re-processed from the start on restart. Harmless, because the writes are
idempotent. Resume and idempotency are *one mechanism here, not two* — the
resume story only works because re-doing work is free.

**4. The same job posted twice over time.** A company reposting into
consecutive months' HN threads gets a different comment id each time, so
`dedup_key` correctly treats them as two postings — but they are plainly one
job. The fingerprint catches these. On a two-thread run, **32 such groups** were
identified.

### What is deliberately *not* deduplicated

Two different roles at the same company. Two locations for the same role. A
reposting with a changed salary — that updates the existing row and increments
`revision`, so the edit is visible rather than hidden.

`revision` counts genuine employer edits, not how often we looked: an upsert
whose `content_hash` is unchanged leaves `revision` alone.

---

## Surviving failure

### When the source changes its HTML

This is the failure that matters, because it is *silent*. A parser that returns
`[]` when a selector stops matching produces a green run, an empty table, and
nobody finds out for a week.

Three defences:

1. **Structural failures raise, they never return empty.** If `tr.athing.comtr`
   matches nothing on an HN thread page, the parser raises rather than returning
   zero records. Same for XML that will not parse and for a Remote OK response
   that is an object where an array was expected. There are tests asserting
   exactly this.

2. **Per-record failures are quarantined, never dropped** (below).

3. **A parser-health check compares each source against its own history.** Every
   source's valid-record count is measured against the *median* of its recent
   successful runs. A source that fetches fine (HTTP 200, no exception) but
   yields under 20% of its usual volume fails the run:

   > `parser_health:hackernews` — produced 3 valid records; median of recent
   > runs is 199. A large drop with a successful fetch usually means the source
   > changed its markup or response shape.

   The median rather than the mean, so one bad day does not move the threshold
   enough to mask the next real breakage. Sources with a trailing median under
   10 records are exempt — for them a drop is noise, not news.

### When the network drops halfway

- **Exponential backoff with full jitter**, `random.uniform(0, min(cap, base · 2ⁿ))`.
  Jittered rather than fixed on purpose: when several tasks are throttled by the
  same host at once, undithered backoff makes them retry in lockstep and
  re-trigger the same 429.
- **`Retry-After` is honoured** when the server sends it — a server that tells us
  how long to wait outranks our guess.
- **Retryable vs permanent are distinguished.** 429 and 5xx are retried; 403 and
  404 fail immediately rather than burning the attempt budget on a request that
  will never succeed.
- **Two layers.** The HTTP client retries within a request; the task is then
  re-queued up to `task_max_attempts` times within the run.
- **A partly-finished run is still useful.** One dead source does not fail the
  others; the run is marked `partial` and the health checks judge whether the
  data is sound.

### When you kill it mid-run

The task list is written to the `run_tasks` ledger **before the first network
call**. Recovery then reads the ledger instead of guessing.

Each task's postings, its quarantine rows, its fetch state and its `done` marker
are committed in **one transaction**. There is no state in which a task is
marked finished but its rows are missing — which is precisely the state that
would make a restart skip work that never happened.

On startup the pipeline looks for a run still marked `running`, adopts its id,
resets tasks the dead process left `running` back to `pending`, and processes
only what is unfinished. See [the kill test](#proof-it-resumes) below.

### When it runs twice by accident

A database-level lock with a TTL. The second process exits cleanly with a note
rather than an error — an overlapping schedule is not something to alert on. The
TTL means a process killed while holding the lock does not wedge the pipeline
forever; the next run reclaims the lease.

And underneath all of it, the upsert: even if the lock failed entirely,
concurrent runs still could not duplicate a row.

---

## Schema validation and quarantine

Every record is validated against a Pydantic model before it can reach the
`jobs` table. Anything that fails is written to `quarantine` **with the original
payload and a reason a human can act on** — never silently dropped.

```
title: String should have at least 2 characters (got '')
salary_min (200000) is greater than salary_max (90000)
posted more than 2 days in the future (2199-01-01T00:00:00+00:00)
header line does not use the 'Company | Role | ...' pipe convention
  (got 'SwingVision is the AI tennis, pickleball, and padel app that...')
```

The validators are strict about things that indicate a *parser* problem rather
than merely untidy data: a title containing a newline means a delimiter split
failed and captured prose; `extra="forbid"` means a parser that starts emitting
a new key fails loudly instead of having the value dropped on the floor.

Each row also gets a **`reason_code`** (`title:string_too_short`,
`no_role_identified`) — the prose reason embeds the offending value and so is
unique per row, and only the code can answer "which failure mode dominates, and
has that mix shifted?"

**The quarantine rate is a health signal, not a nuisance.** HN sits around 12%,
because a stable minority of posters ignore the pipe convention. A steady low
percentage is normal human noise; a jump to nearly everything means the markup
changed. The run fails above 25%.

---

## Politeness

This scraper runs unattended every day against sites owned by other people.

- **robots.txt is fetched and obeyed** per host, and a published `Crawl-delay`
  *overrides* the configured interval when it is stricter. Hacker News asks for
  30 seconds, and that single directive dominates the wall-clock of a run — the
  other hosts are crawled concurrently around it.
- **Per-host rate limiting** spaces request starts and caps concurrency per
  host, so the slow host never stalls the fast ones.
- **Conditional requests.** Stored `ETag`/`Last-Modified` mean an unchanged WWR
  feed comes back `304` and costs nothing to parse.
- **A real User-Agent** naming the project and a contact address.
- **A whole run is ~16 HTTP requests.**
- **Remote OK's API terms** require a followed link back and a credit. The
  dashboard does both; outbound links carry `rel="noopener"` only, deliberately
  not `nofollow`.

---

## The dashboard

Server-rendered: no charting CDN, no client-side data fetching, no build step.
Charts are inline SVG generated in Python.

- A **health indicator** showing the last successful run, its age, and the
  consecutive-green-day streak. State is carried by an icon and a text label,
  not by colour alone.
- New postings per day, stacked by source. A day with no run renders as a
  genuine gap — the absence is the interesting part.
- Run history: bar height is duration, colour and icon are the outcome.
- Quarantine, grouped by failure mode, with the raw reasons underneath.
- Duplicate groups, showing the fingerprint doing its job.

Colours come from a validated categorical palette (checked with a CVD/contrast
validator in both light and dark mode). One of the three series sits below 3:1
on the light surface, so every chart ships direct labels and a table view.

### JSON API

| Endpoint | |
|---|---|
| `GET /api/health` | Health. **503 when stale**, so an uptime monitor can poll it. |
| `GET /api/stats` | The full dashboard snapshot. |
| `GET /api/jobs?limit=&source=&q=` | Postings, filterable. |

---

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,serve]"
cp .env.example .env          # defaults to a local SQLite file
```

```bash
jobradar run                  # scrape (resumes an unfinished run if there is one)
jobradar run --no-resume      # force a fresh run
jobradar run --sources remoteok
jobradar status               # health and recent runs
jobradar serve                # dashboard at http://127.0.0.1:8000
pytest -q                     # 192 tests, no database service needed
```

Everything tunable lives in environment variables — see `.env.example`.

---

## Deployment

| Piece | Where | Notes |
|---|---|---|
| Scraper | GitHub Actions cron | `DATABASE_URL` as a repository secret |
| Database | Supabase Postgres | Use the **Session pooler** URI: direct connections are IPv6-only and Actions runners are IPv4-only |
| Dashboard | See `vercel.json` / `api/index.py` | Any ASGI host works |

---

## How it is put together

```
src/jobradar/
  config.py       settings; per-host politeness policy
  models.py       the ingest schema, and readable quarantine reasons
  normalize.py    the two identity keys
  net.py          rate limiting, robots, retry/backoff
  db.py           schema; the dialect-portable bits; the run lock
  storage.py      the upsert, the quarantine, the ledger
  pipeline.py     planning, resuming, executing, judging
  queries.py      read-side aggregates
  charts.py       inline-SVG chart builders
  dashboard.py    the FastAPI app
  sources/        one adapter per source
```

A source does two things: `plan()` decides the units of work (async, because HN
must discover the current month's thread id before it can fetch it), and
`parse()` turns one response into records. Planning is separate from fetching
because the plan is what gets written to the resume ledger before any work
starts.

---

## Incidents

<!-- INCIDENTS -->

---

## Proof it resumes

Reproduce it yourself:

```bash
python scripts/resume_proof.py
```

The script starts a real run, waits until some tasks have committed and others
are genuinely in flight, then kills the process **hard** — `SIGKILL` /
`TerminateProcess`, no cleanup, no signal handler, the way a machine dying
would. Then it restarts and checks the result.

```
1. Start a real run, then kill it mid-flight
   started pid 10912; waiting for 4 tasks to finish...
   KILLED pid 10912 (no cleanup, no signal handler)

2. What the killed process left behind
   run          072cb322-8959-4ce1-8f4d-21fab95963b3
   tasks        {'done': 4, 'pending': 3, 'running': 8}  (total 15)
   rows in jobs 257
   -> 4 task(s) finished and committed
   -> 11 task(s) left unfinished
   -> the run is still marked 'running', so it is resumable

3. Wait for the dead process's lock lease to lapse
   the killed process still holds the run lock; its 10s lease
   must expire before anything may touch this run  . lapsed

4. Restart -- it should adopt the same run, not start over
   WARNING  resuming run 072cb322-... (8 task(s) reclaimed from a previous process)
   INFO     run 072cb322-...: 11 open task(s) (resumed)

5. Final state
   tasks        {'done': 15}  (total 15)
   rows in jobs 907

6. Checks
   [PASS] resumed the SAME run rather than starting a new one
   [PASS] every task reached 'done' ({'done': 15})
   [PASS] the resumed half added rows (257 -> 907)
   [PASS] no duplicates: 907 rows, 907 distinct dedup_keys
   [PASS] only one run row exists, not two (1)

PASS -- the pipeline resumed and did not duplicate.
```

Three details worth pulling out:

**Eight tasks were `running` when it died, not one.** Workers claim a task and
then wait on the rate limiter, so a kill strands every in-flight claim at once.
`reclaim_orphaned_tasks` returns all of them to `pending`, because no live
worker owns them any more.

**The 257 rows written before the kill survived.** They were committed
alongside their tasks' `done` markers, so the restart correctly skipped those
four tasks instead of redoing them.

**No duplicates, despite eight tasks being re-processed from scratch.** That is
the `dedup_key` upsert doing its job, and it is why resume did not need any
"where was I in this response" bookkeeping.
