# Darwinbox FDE — Migration Copilot

A generic, human-supervised client data migration agent for the Darwinbox Forward Deployed Engineer assessment.

## Product contract

The consultant provides:

1. **Multiple source files** (`CSV`, `XLSX`) containing inconsistent fields, formats, duplicates, and missing values.
2. **A target JSON/YAML schema** that defines the exact output structure, field types, required/optional fields, enum values, relationships, and optional source precedence.

The Employee files in `data/sample/` are only demo data. The engine is entity-agnostic and supports multiple target entities.

## What the agent does

```text
Source files + target schema
        |
        v
      Profile
        |
        v
   Identify entities
        |
        v
  Rank field mappings
        |
        +--> strong / safe -> AUTO
        |
        +--> ambiguous -> LLM -> AUTO fallback if safe
        |
        +--> no safe fallback -> CONSULTANT REVIEW
        |
        v
  Transformation plan
        |
        +--> known operation -> Python executes
        |
        +--> new transformation -> HUMAN APPROVAL
        |
        v
 Entity reconciliation + merge policy
        |
        v
 Target-schema validation
        |
        v
 READY_TO_PUSH
        |
        v
 Target API -> retry / rollback
        |
        v
 Results + audit trail
```

### Autonomy principle

**AI proposes; deterministic software executes; policy controls safety.**

The agent does not hard-code customer-specific source-to-target mappings. It discovers mappings at runtime from the uploaded data and the user-provided target schema.

### LLM usage

BGE-small is used for semantic candidate matching. The hosted LLM is used only when semantic evidence is ambiguous or a transformation/value normalization needs judgment. Common mappings and transformations do not wait for the LLM.

The LLM client:

- caches repeat prompts;
- retries transient failures with bounded timeouts;
- logs request start, HTTP status, served model, usage and success/failure;
- falls back to a safe semantic choice when one exists;
- sends only genuinely unresolved cases to consultant review with `llm_unavailable`.

## Transformation library

The controlled Python transformation library includes:

- `trim`
- `collapse_whitespace`
- `lowercase`
- `uppercase`
- `normalize_name`
- `normalize_email`
- `normalize_phone`
- `normalize_integer`
- `normalize_decimal`
- `parse_date`
- `parse_date_auto`
- `parse_datetime_auto`
- `map_value`
- `extract_number`
- `split`
- `join`
- `normalize_boolean`

The LLM may choose/compose only supported operations. An unsupported/new transformation is never executed automatically; it becomes a consultant approval item.

## Human-in-the-loop policy

- strong deterministic mapping -> automatic;
- clear semantic mapping -> automatic;
- ambiguous mapping -> LLM first, then safe automatic fallback if possible;
- no safe mapping -> consultant review;
- known transformation -> automatic;
- unknown transformation -> consultant approval;
- source conflict -> use configured precedence if present, otherwise review;
- strong identity match -> automatic reconciliation;
- ambiguous identity match -> review;
- target API failure -> record-level retry or rollback.

The application intentionally minimizes human review while avoiding irreversible guesses.

## Data model

PostgreSQL is the standard database in the deployed Docker stack. Local development can use SQLite by setting `DATABASE_URL` in `backend/.env`.

Stored objects include:

- migration runs
- uploaded files and profiling metadata
- canonical records
- consultant escalations and decisions
- LLM cache
- audit events

## Local development

### Backend

Use **Python 3.12** for the pinned dependency set.

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### Mock target API

```powershell
cd target_api
python -m pip install -r requirements.txt
uvicorn main:app --port 8001
```

### Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

A browser refresh does not resurrect an old active migration. A backend restart stops all non-final migrations. Closing/leaving the page while a migration is active shows a browser confirmation; leaving force-stops the migration.

## Docker / deployed-style environment

From the repository root:

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY
# Optional values: VITE_API_BASE, FRONTEND_ORIGIN, TARGET_API_PUBLIC_URL

docker compose up --build
```

Services:

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- Backend docs: `http://localhost:8000/docs`
- Target API: `http://localhost:8001`
- Target API docs: `http://localhost:8001/docs`

The target API stores data in a JSON-backed persistent store in the Docker volume, so pushed records survive a target-container restart during the assessment demo.

## Results page

After successful completion the UI shows:

- original source tables grouped by uploaded file;
- canonical target tables grouped by entity;
- clickable public target API URLs;
- the audit trail;
- final migration statistics.

## Demo data

Use:

```text
data/sample/legacy_hr.csv
data/sample/payroll.xlsx
data/sample/crm.csv
data/sample/employee_target_schema.json
```

The demo includes:

- inconsistent source column names;
- mixed date formats;
- duplicate employee representations;
- one deliberately ambiguous mapping scenario;
- an intentionally one-time target API failure for `E-FAIL` so retry/rollback can be demonstrated.

For multi-entity testing, the target schema format supports multiple entities with independent fields, aliases, uniqueness rules, relationships, and source precedence.

## Tests

```powershell
cd backend
pytest -q
```

The suite covers transformations, semantic/mapping policy, entity resolution, merge precedence, multi-entity schemas, LLM fallback behavior, and target API failure/retry behavior.

## Submission artifacts

- `docs/architecture.md` — architecture and autonomy boundary.
- `docs/assessment-writeup.md` — one-page approach write-up.
- `docs/demo-script.md` — suggested evaluator demo path.
- `docs/deployment.md` — deployed-style setup and environment variables.
