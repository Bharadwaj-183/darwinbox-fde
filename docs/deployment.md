# Deployment Guide

## Recommended topology

- React frontend: static hosting or the included Nginx container.
- FastAPI backend: containerized service.
- PostgreSQL 16: managed PostgreSQL in production.
- Target API: mock service for the assessment; replace with the customer's real API connector in production.
- OpenRouter: hosted inference for the configured open-weight model/router.

## Docker Compose

From the repository root:

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY in .env
# Optional production values: VITE_API_BASE, FRONTEND_ORIGIN, TARGET_API_PUBLIC_URL

docker compose up --build
```

Services:

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- Backend docs: `http://localhost:8000/docs`
- Target API: `http://localhost:8001`
- Target API docs: `http://localhost:8001/docs`

The deployed Docker stack uses PostgreSQL. Local non-Docker development may use SQLite by setting `DATABASE_URL` in `backend/.env`.

## Required production environment variables

```text
DATABASE_URL
OPENROUTER_API_KEY
LLM_MODEL
TARGET_API_URL
TARGET_API_PUBLIC_URL
FRONTEND_ORIGIN
VITE_API_BASE
```

`TARGET_API_URL` is the backend-to-target internal URL. `TARGET_API_PUBLIC_URL` is the browser-facing URL shown on the Results page.
