# PermitPilot

**Turn a city's permit ordinance into a checklist a small contractor can actually follow, track every application through review, and never miss a permit deadline.**

[![CI](https://github.com/yashchhabra57/permitpilot/actions/workflows/ci.yml/badge.svg)](https://github.com/yashchhabra57/permitpilot/actions/workflows/ci.yml)

## The problem

A two-person deck or roofing crew pulls dozens of permits a year, often across several towns. Each town's rules live in a long municipal code: which documents to attach, what the fee is, how deep footings go, which inspections are required, and when the permit quietly expires. Missing one of these means a rejected application, a failed inspection, or an expired permit and a re-application fee. Most small contractors track all of this in their heads or on paper.

PermitPilot:

1. **Reads the ordinance** and extracts every requirement (documents, fees, inspections, dimensions, deadlines), each with a **verbatim quote and section citation**.
2. **Builds a checklist** for one job (for example "deck at 12 Pine St") from the requirements that apply to that permit type.
3. **Tracks the application** through a state machine (draft → submitted → in review → corrections → approved → issued → inspection → closed). Transitions are guarded by the checklist: you can't submit until the required documents are ticked, and the permit can't be issued until fees are paid.
4. **Schedules reminders** (corrections due, permit about to expire, inspection tomorrow) on a durable job queue with retries, so deadlines are caught even weeks later.
5. **Answers questions** such as "how deep do deck footings go?" with the matching ordinance sentence and its section number.

The repo ships with a fictional sample ordinance (`app/seed/lakeport_residential.txt`) modeled on common small-city residential permit rules. It is not the law of any real place.

## Architecture

```mermaid
flowchart LR
    UI["Dashboard<br/>(vanilla JS, static)"] -->|JSON over HTTP| API["FastAPI routes<br/>app/api.py"]
    API --> SVC["Services<br/>ingest · checklist · search"]
    API --> WF["Workflow state machine<br/>guards + effects"]
    SVC --> CH["Chunker<br/>§ sections"]
    SVC --> EX{"Extractor"}
    EX -->|no key / failure| RULES["Rule extractor<br/>(deterministic)"]
    EX -->|key set| LLM["LLM extractor<br/>structured outputs"]
    LLM --> GROUND["Grounding check<br/>quote must be verbatim"]
    SVC --> BM25["BM25 index<br/>(from scratch)"]
    WF -->|same transaction| JOBS[("jobs table<br/>= the queue")]
    SVC --> DB[("SQLite / PostgreSQL<br/>SQLAlchemy 2")]
    WF --> DB
    WORKER["Worker process<br/>python -m app.worker"] -->|compare-and-set claim| JOBS
    WORKER --> OUTBOX[("notifications<br/>outbox")]
```

### Application lifecycle

```mermaid
stateDiagram-v2
    [*] --> draft
    draft --> submitted: submit [documents done]
    submitted --> in_review: start_review
    in_review --> corrections_requested: request_corrections (note)
    corrections_requested --> submitted: submit
    in_review --> approved: approve
    in_review --> rejected: reject (note)
    approved --> issued: issue [fees paid]
    issued --> inspection_scheduled: schedule_inspection (future date)
    inspection_scheduled --> issued: fail_inspection (note)
    inspection_scheduled --> closed: pass_inspection [inspections done]
    draft --> withdrawn: withdraw
    issued --> withdrawn: withdraw
    closed --> [*]
    rejected --> [*]
    withdrawn --> [*]
```

`withdraw` is allowed from every non-terminal state (only two arrows are drawn to keep the diagram readable).

## Tech stack

| Layer | Choice |
|---|---|
| API | Python 3.12, FastAPI, Pydantic v2 |
| Data | SQLAlchemy 2 (typed ORM), SQLite for dev, PostgreSQL 16 in Docker and CI |
| Search | BM25 implemented from scratch (`app/retrieval.py`) |
| AI | Optional LLM extraction with structured outputs (`messages.parse` + Pydantic schema), grounding check, rule-based fallback |
| Jobs | Database-backed queue with leases, exponential backoff, dead-lettering, dedupe keys |
| Frontend | Static HTML/CSS/vanilla JS served by FastAPI (light and dark mode) |
| Quality | pytest (90+ tests, ~94% coverage), ruff, mypy `--strict` |
| Ops | GitHub Actions (lint, SQLite matrix on 3.12/3.13, PostgreSQL service job, Docker smoke test), Dockerfile, docker-compose (api + worker + postgres) |

## Run it

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev,llm]"
.venv\Scripts\uvicorn app.main:create_app --factory --reload
```

macOS / Linux:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,llm]"
.venv/bin/uvicorn app.main:create_app --factory --reload
```

Open <http://localhost:8000> for the dashboard, or <http://localhost:8000/docs> for the interactive API docs. The sample ordinance is loaded on first start.

Run the reminder worker in a second terminal (`python -m app.worker`), or press **Run worker** in the dashboard. In demo mode you can pick a future "as of" date to see reminders fire.

With Docker (API + worker + PostgreSQL):

```bash
docker compose up --build
```

Configuration is read from environment variables prefixed `PERMITPILOT_`; see [`.env.example`](.env.example). Nothing is required. Without `PERMITPILOT_ANTHROPIC_API_KEY` the app uses the rule extractor.

### Tests and checks

```bash
pytest --cov=app            # unit + API tests on in-memory SQLite
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/permitpilot_test pytest   # same suite on Postgres
ruff check . && ruff format --check . && mypy app
python scripts/smoke.py     # starts a real server and walks one permit from draft to closed
```

## API

| Method | Path | What it does |
|---|---|---|
| GET | `/api/ordinances` | List ordinances with permit types, section and requirement counts |
| POST | `/api/ordinances` | Ingest ordinance text: chunk it, extract requirements (`{jurisdiction, title, text}`) |
| GET | `/api/ordinances/{id}/requirements?permit_type=deck` | Requirements with quotes and sections |
| GET | `/api/search?q=...&ordinance_id=&k=5` | BM25 search returning cited excerpts |
| POST | `/api/applications` | Open an application; builds its checklist |
| GET | `/api/applications?state=` | List applications |
| GET | `/api/applications/{id}` | Full detail: checklist, timeline, available actions with blockers, scheduled jobs |
| PATCH | `/api/applications/{id}/checklist/{item_id}` | Tick or untick an item (`{done}`) |
| POST | `/api/applications/{id}/transitions` | `{action, expected_version, note?, inspection_at?}` |
| GET | `/api/jobs?status=`, `/api/jobs/stats` | Inspect the queue |
| POST | `/api/jobs/run-due?as_of=` | Run the worker once in-process (`as_of` only in demo mode) |
| GET | `/api/notifications?application_id=` | Reminder outbox |

Errors share one shape: `{"code": "guard_failed", "message": "cannot submit yet", "details": ["[§ 14-201] A deck permit application must include framing plans ..."]}`. Status codes: 404 not found, 409 `invalid_transition` / `stale_version`, 422 validation, `guard_failed`, `note_required`.

## Engineering notes

**Every requirement is grounded in the source text.** The LLM extractor asks for structured output (a Pydantic schema the SDK validates), then rejects any item whose `quote` does not appear verbatim (ignoring case and whitespace) in the section it cites, or whose section doesn't exist. A paraphrased or invented rule never reaches a contractor's checklist. If the API errors, refuses, or returns nothing grounded, ingest falls back to the rule extractor and reports why (`fallback_reason`). Tests cover all of this with a fake client, so CI never calls a real API.

**The rule extractor is deliberately boring.** Legal drafting is formulaic ("shall", "must", "is required", "The permit fee for X is $N"), so sentence rules catch most obligations. The tests pin down real bugs found while building it: `"proof"` contains `"roof"` (whole-word matching fixed it), "Building Inspection Division" is an office, not an inspection, and "submit revised documents within 30 days" is a deadline, not a document to attach before the first submission.

**Why the queue lives in the database.** Reminders are low-volume and must survive restarts. Keeping jobs in the same database as applications means a job is created in the *same transaction* as the transition that caused it: a permit is never "issued" without its expiry reminders. A Redis queue would need an outbox to get the same guarantee.

**Claiming without `SELECT ... FOR UPDATE`.** A worker claims a job with a compare-and-set `UPDATE jobs SET status='running', locked_until=... WHERE id=? AND <still claimable>`. The database applies it atomically, so when two workers race exactly one gets `rowcount == 1`. The same code works on SQLite and PostgreSQL. A claim is a *lease*: if a worker dies, its lease expires and another worker picks the job up. That means at-least-once delivery, so the reminder handler is idempotent (it checks the outbox for its job id before writing).

**Failures are isolated per job.** Each job runs in its own session. If a handler raises, its writes are rolled back, the job is rescheduled with exponential backoff (`base * 2^(attempt-1)`, capped), and after `max_attempts` it is dead-lettered with the last error kept. `PermanentJobError` (bad payload, unknown kind) dead-letters immediately. If a worker finishes after losing its lease, its writes are rolled back too.

**Reminders re-check state when they fire.** A reminder scheduled 150 days ahead may no longer apply. The handler skips it if the application has moved on, and transitions cancel reminders that became moot (resubmitting cancels "corrections due"; booking an inspection cancels "start work"). Dedupe keys include the application version, so a second corrections round gets its own reminder.

**Two layers of concurrency control.** Clients send `expected_version` with each transition (a stale browser tab gets a 409). Underneath, SQLAlchemy's `version_id_col` adds `WHERE version = ?` to every UPDATE, catching the race the explicit check can't see (two requests that read the same version at the same moment).

**Why BM25, not embeddings.** Ordinances use exact legal vocabulary (setback, footing, guard), so keyword ranking works well, needs no model, API key, or vector database, and its results are explainable. The stemmer only strips plurals on purpose: stemming "footing" to "foot" made retrieval worse, and a test keeps it from coming back.

**Naive UTC everywhere.** SQLite has no time zones, so every timestamp is stored as naive UTC and converted at the edges. This keeps behavior identical across SQLite and PostgreSQL, which CI checks by running the same suite on both.

## Limitations (honest)

- **Sample data only.** The bundled ordinance is fictional. Real codes have tables, cross-references ("as required by § 14-103"), amendments, and PDFs. The chunker handles numbered sections only, and there is no PDF/OCR input yet.
- **Rule extractor heuristics.** It classifies one sentence at a time. It can mis-scope a sentence that names a different permit type, and it doesn't paraphrase into short checklist lines (the LLM extractor does).
- **The LLM path was not run against the real API** during development (no key available). It is covered by tests with a mocked client, and every failure mode falls back to rules.
- **No authentication or multi-tenancy.** Every user sees every application. A real deployment needs accounts, per-company data isolation, and roles (contractor vs. reviewer).
- **Notifications go to an outbox table**, not to email or SMS. A delivery adapter (SES, Twilio) would read from `notifications`.
- **Jurisdiction timing rules are constants** in `app/workflow.py` (30-day corrections window, 180/365-day expiry). They should be extracted per ordinance.
- **Schema is created with `create_all`.** A production system should use Alembic migrations.
- **Ingest is synchronous.** A long ordinance with LLM extraction blocks the request. It should be a job on the same queue.

## Roadmap

1. PDF ingest (text layer + OCR) and cross-reference resolution.
2. Per-jurisdiction timing rules extracted from the ordinance instead of constants.
3. Email/SMS delivery adapter reading the outbox, with delivery receipts.
4. Accounts and company workspaces, with Postgres row-level security.
5. Ingest as a background job with progress updates over WebSocket.
6. An evaluation set of real ordinances with labeled requirements to compare the rule and LLM extractors.

## License

MIT
