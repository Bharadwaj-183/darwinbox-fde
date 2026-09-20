# Darwinbox FDE — Migration Copilot

A generic, human-supervised client data migration agent built for the Darwinbox Forward Deployed Engineer assessment.

## Product contract

The application is designed around a simple customer-facing workflow:

1. The user uploads **multiple source files** (`CSV`, `XLSX`, etc.) containing inconsistent column names, formats, duplicates, missing values, and conflicting records.
2. The user uploads a **target JSON/YAML schema** describing the exact structure required for the final data.
3. The agent discovers source-to-target mappings, cleans and reconciles the data, validates it, asks the consultant only when necessary, and pushes the final dataset to a mock target API.

The Employee files shipped with the project are only **demo data**. The migration engine is designed to be entity-agnostic and can work with multiple entities when the supplied target schema defines them.

## High-level flow

```text
Source files + target schema
        |
        v
     Profiling
        |
        v
  Entity identification
        |
        v
 Source -> target mapping
        |
   +----+------------------+
   |                       |
   v                       v
Safe/clear mapping      Ambiguous mapping
AUTO                    LLM reasoning
                           |
                    +------+------+
                    |             |
                 safe answer   no safe fallback
                    |             |
                    v             v
                   AUTO      Consultant Review
                    |
                    v
        Transformation planning
                    |
        +-----------+------------+
        |                        |
   known operation         new/unknown operation
     Python executes       Consultant approval
        |
        v
 Cross-file reconciliation
        |
        v
 Target-schema validation
        |
        v
 READY TO PUSH
        |
        v
 Mock Target API
   |             |
success        failure
   |             |
   |          manual retry
   |             |
   +------+------+
          v
      Results + audit trail
```

### Autonomy principle

**AI proposes; deterministic software executes; policy controls safety.**

The agent does not hard-code customer-specific source-to-target mappings. It discovers mappings at runtime from the uploaded source data and the user-provided target schema.

## AI and semantic matching

### Semantic similarity

`BAAI/bge-small-en-v1.5` is used to identify likely source-field → target-field candidates.

The similarity score is treated as **evidence, not a probability**. The policy layer also considers datatype compatibility, target constraints, sample values, aliases, candidate separation, and prior consultant decisions.

### LLM

The hosted LLM is accessed through OpenRouter using the configured model (`openrouter/free` in the deployed setup). The LLM is intentionally **not called for every field**.

- Clear/high-confidence mappings → automatic resolution.
- Ambiguous mappings or semantic value transformations → LLM reasoning.
- If the LLM is unavailable and a safe automatic candidate exists → use the automatic fallback.
- If no safe fallback exists → send the case to Consultant Review.
- Unknown/new transformations → require consultant approval before execution.

The LLM client uses bounded retries/timeouts and caching for repeat requests.

## Transformation engine

The application contains a controlled Python transformation library for common operations, including:

- trimming and whitespace normalization
- casing/name normalization
- email and phone normalization
- integer/decimal normalization
- date and datetime parsing
- value mapping
- splitting/joining
- boolean normalization
- number extraction

Known transformations are executed deterministically by Python. A transformation that is not supported by the library is not executed automatically; it becomes a consultant-approval case.

## Human-in-the-loop

Consultant Review is designed for non-technical users. Reviews identify:

- the **record** involved;
- the **source file**;
- the affected **field/value**;
- why the agent stopped;
- the recommended action, when available.

Normal review cases should be resolved with clear UI actions rather than requiring users to write JSON.

Typical review types include:

- ambiguous field mapping;
- conflicting values across source files;
- missing required target data;
- unknown transformations;
- LLM unavailable when no safe automatic fallback exists.

## Cross-file reconciliation

Multiple representations of the same entity are reconciled into one canonical record using a combination of stable identifiers and other evidence such as email, phone, name/date-of-birth, and fuzzy similarity.

Source conflicts can be resolved through configured source precedence or sent to Consultant Review when a safe automatic choice does not exist.

## Demo / test data

The easiest way to test the application is with the provided Employee demo files:

### Source files

Upload these as the source dataset:

```text
payroll.xlsx
crm.csv
legacy_hr.csv
```

### Target schema

Upload this as the target schema:

```text
employee_target_schema.json
```

These files intentionally demonstrate the type of messy customer data the agent is expected to handle: inconsistent field names, mixed formats, duplicate representations, and source-level differences.

### Expected demo behavior

The demo data includes a **simulated target-system failure** for the `E-FAIL` record. Its first push is intentionally rejected by the mock target API; the record can then be retried and should succeed.

Depending on the exact source files and schema used, the migration can also create **a small number of Consultant Review items**. These are intentional and demonstrate the human-in-the-loop boundary rather than being application errors.

When a migration completes, the **Migration Completed / Results page** shows the final source and target data, audit information, and a **clickable Target API URL at the bottom of the page**. Opening that URL shows the actual records stored by the mock target API.

## Migration lifecycle

The UI keeps the consultant informed with a progress bar, loader, and human-readable status messages such as:

- Reading source files
- Resolving field mappings
- Merging duplicate records
- Waiting for Consultant Review
- Review needed — check Consultant Review
- Preparing target data
- Pushing changes
- Retry needed — check failed records
- Migration completed successfully

A running migration can be force-stopped. Restarting the backend does not intentionally resurrect an old in-progress migration.

## Results

After completion, the Results page provides:

- source data tables grouped by uploaded file;
- canonical target data tables grouped by entity;
- migration statistics;
- audit history;
- clickable Target API URLs showing the actual target records.

## Local development

Use **Python 3.12** for the backend dependency set.

### Backend

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

Open:

```text
http://localhost:5173
```

Backend health details:

```text
http://localhost:8000/api/health/details
```

## Environment variables

At minimum, the backend needs the OpenRouter key for AI-powered reasoning:

```env
OPENROUTER_API_KEY=your_key_here
LLM_MODEL=openrouter/free
```

Do not commit the real API key to GitHub.

## Database

The deployed setup uses **PostgreSQL**. Local development can use SQLite when configured through `DATABASE_URL`.

Stored data includes migration runs, uploaded-file metadata, canonical records, consultant decisions, LLM cache entries, and audit events.

## Deployment

The application is designed for a multi-service deployment:

```text
Frontend  -> React/Vite static site
Backend   -> FastAPI
Target API -> FastAPI mock destination
Database  -> PostgreSQL
LLM       -> OpenRouter
```

A `render.yaml` Blueprint is included for Render deployment.

## Testing

From the backend directory:

```powershell
pytest -q
```

The test suite covers mapping policy, transformations, reconciliation, conflict handling, LLM fallback behavior, migration lifecycle, and target API retry scenarios.

## Submission artifacts

- `docs/architecture.md` — system architecture and autonomy boundary.
- `docs/assessment-writeup.md` — one-page approach write-up.
- `docs/demo-script.md` — suggested evaluator demo flow.
- `docs/deployment.md` — deployment and environment configuration.
