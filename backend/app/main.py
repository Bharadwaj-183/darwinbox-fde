from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import get_settings
from app.db import SessionLocal, init_db
from app.models.migration import AuditEvent, MigrationRun

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()

    # A process restart means in-memory agent tasks are gone. Never silently
    # revive them from the database; mark them stopped so the user can choose
    # what to do next.
    db = SessionLocal()
    try:
        interrupted = db.query(MigrationRun).filter(
            MigrationRun.status.notin_({
                "COMPLETED", "ROLLED_BACK", "STOPPED", "FAILED",
            })
        ).all()
        for migration in interrupted:
            stats = dict(migration.stats or {})
            stats.update({
                "progress": min(int(stats.get("progress", 0)), 80),
                "status_message": "Migration stopped after backend restart",
                "action_required": True,
                "next_action": "Start a new migration or review this stopped run.",
            })
            migration.stats = stats
            migration.status = "STOPPED"
            db.add(AuditEvent(
                migration_id=migration.id,
                actor="system",
                action="MIGRATION_STOPPED",
                reason="Backend restarted before migration completed; previous agent task cannot be resumed safely.",
            ))
        db.commit()
    finally:
        db.close()

    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
