from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db import get_db
from app.models.migration import AuditEvent, Escalation, MigrationFile, MigrationRecord, MigrationRun
from app.schemas.api import AuditEventResponse, EscalationResponse, HealthResponse, MigrationCreatedResponse, MigrationSummaryResponse
from app.services.agent_runtime import start_agent, stop_agent
from app.services.event_bus import event_bus
from app.services.file_profiler import FileProfileError, FileProfiler
from app.services.migration_executor import MigrationExecutor
from app.services.schema_loader import TargetSchemaLoader

router = APIRouter()
settings = get_settings()
profiler = FileProfiler()
schema_loader = TargetSchemaLoader()


def _has_meaningful_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value) and all(bool(str(key).strip()) and _has_meaningful_value(item) for key, item in value.items())
    if isinstance(value, list):
        return bool(value) and all(_has_meaningful_value(item) for item in value)
    return True


def _validate_resolution_payload(resolution: object, item: Escalation | None = None) -> None:
    if not isinstance(resolution, dict) or not resolution or not _has_meaningful_value(resolution):
        raise HTTPException(status_code=400, detail="Resolution must contain at least one non-empty value.")
    if "target_field" in resolution and (not isinstance(resolution["target_field"], str) or not resolution["target_field"].strip()):
        raise HTTPException(status_code=400, detail="target_field must be a non-empty string.")
    if "transformations" in resolution:
        transformations = resolution["transformations"]
        if not isinstance(transformations, list) or not transformations:
            raise HTTPException(status_code=400, detail="transformations must be a non-empty list.")
        for step in transformations:
            if not isinstance(step, dict) or not str(step.get("operation", "")).strip():
                raise HTTPException(status_code=400, detail="Each transformation must specify a non-empty operation.")
    if "mappings" in resolution:
        mappings = resolution["mappings"]
        if not isinstance(mappings, list) or not mappings:
            raise HTTPException(status_code=400, detail="mappings must be a non-empty list.")
        for mapping in mappings:
            if not isinstance(mapping, dict) or not str(mapping.get("source_field", "")).strip() or not str(mapping.get("target_field", "")).strip():
                raise HTTPException(status_code=400, detail="Each mapping must include non-empty source_field and target_field values.")
            for step in mapping.get("transformations") or []:
                if not isinstance(step, dict) or not str(step.get("operation", "")).strip():
                    raise HTTPException(status_code=400, detail="Each mapping transformation must specify a non-empty operation.")

    if item is None:
        return

    context = item.context or {}
    if item.reason_code == "ambiguous_mapping":
        allowed = {str(candidate.get("target_field")) for candidate in context.get("candidates") or [] if candidate.get("target_field")}
        target = resolution.get("target_field") or context.get("target_field")
        source = resolution.get("source_field")
        available_sources = {str(value) for value in context.get("available_source_fields") or []}
        if target and allowed and str(target) not in allowed:
            raise HTTPException(status_code=400, detail=f"Choose one of the agent's candidate target fields: {sorted(allowed)}")
        if available_sources and (not source or str(source) not in available_sources):
            raise HTTPException(status_code=400, detail=f"Choose one of the available source fields: {sorted(available_sources)}")
    elif item.reason_code == "llm_unavailable":
        mappings = resolution.get("mappings") or []
        if not mappings and not resolution.get("target_field"):
            raise HTTPException(status_code=400, detail="AI reasoning is unavailable. Provide at least one concrete source-to-target mapping.")
    elif item.reason_code == "invalid_record":
        corrections = resolution.get("corrections")
        if not isinstance(corrections, dict) or not corrections:
            raise HTTPException(status_code=400, detail="Provide at least one concrete correction for this record.")
        allowed_fields = {str(field) for field in context.get("missing_fields") or []}
        allowed_fields.update(
            str(error.get("field")) for error in context.get("errors") or []
            if isinstance(error, dict) and error.get("field")
        )
        if allowed_fields and any(str(field) not in allowed_fields for field in corrections):
            raise HTTPException(status_code=400, detail=f"Correct only these fields for this record: {sorted(allowed_fields)}")
        if any(value is None or (isinstance(value, str) and not value.strip()) for value in corrections.values()):
            raise HTTPException(status_code=400, detail="Every correction value must be non-empty.")
    elif item.reason_code == "unmapped_value":
        allowed_values = {str(value) for value in context.get("allowed_values") or []}
        unresolved = {str(value) for value in context.get("unresolved_values") or []}
        mapping_steps = [step for step in resolution.get("transformations") or [] if step.get("operation") == "map_value"]
        mapping = (mapping_steps[-1].get("params") or {}).get("mapping") if mapping_steps else None
        if not isinstance(mapping, dict) or any(str(mapping.get(value, "")).strip() not in allowed_values for value in unresolved):
            raise HTTPException(status_code=400, detail=f"Provide a map_value transformation for every unresolved value using only: {sorted(allowed_values)}")


@router.post("/migrations", response_model=MigrationCreatedResponse, status_code=201)
async def create_migration(
    files: Annotated[list[UploadFile], File(...)],
    target_schema: Annotated[str, Form(...)],
    target_schema_filename: Annotated[str, Form()] = "target_schema.json",
    db: Session = Depends(get_db),
) -> MigrationCreatedResponse:
    if not files:
        raise HTTPException(status_code=400, detail="At least one source file is required")
    try:
        schema = schema_loader.load_text(target_schema_filename, target_schema)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    migration = MigrationRun(
        status="CREATED",
        target_schema=schema.model_dump(mode="json"),
        stats={"files": 0, "records": 0, "escalations": 0, "ready_to_push": 0, "pushed": 0, "push_failed": 0, "progress": 0, "status_message": "Ready to start", "action_required": True, "next_action": "Start the migration.", "stage": "CREATED"},
        artifacts={},
    )
    db.add(migration)
    db.flush()

    upload_dir = Path(settings.uploads_dir) / migration.id
    upload_dir.mkdir(parents=True, exist_ok=True)
    created_files: list[str] = []
    total_rows = 0

    for upload in files:
        if not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            db.rollback()
            raise HTTPException(status_code=400, detail=f"Unsupported source file: {upload.filename}")

        destination = upload_dir / f"{uuid4().hex}_{Path(upload.filename).name}"
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)

        try:
            profile = profiler.profile(str(destination))
        except FileProfileError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        db.add(MigrationFile(
            migration_id=migration.id,
            filename=upload.filename,
            content_type=upload.content_type or "application/octet-stream",
            storage_path=str(destination),
            row_count=profile["row_count"],
            columns=profile["columns"],
        ))
        created_files.append(upload.filename)
        total_rows += profile["row_count"]

    migration.stats = {**(migration.stats or {}), "files": len(created_files), "records": total_rows}
    db.add(AuditEvent(
        migration_id=migration.id,
        actor="system",
        action="MIGRATION_CREATED",
        reason="Source files uploaded and user-provided target schema validated",
    ))
    db.commit()

    await event_bus.publish(migration.id, {
        "type": "migration_created",
        "migration_id": migration.id,
        "message": f"Migration created with {len(created_files)} source file(s).",
    })
    return MigrationCreatedResponse(
        migration_id=migration.id,
        status=migration.status,
        files=created_files,
        entities=[entity.entity_name for entity in schema.entities],
    )


@router.post("/migrations/{migration_id}/start", status_code=202)
async def start_migration(migration_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    if migration.status not in {"CREATED", "READY", "PAUSED", "CONSULTANT_REVIEW", "WAITING_HUMAN"}:
        raise HTTPException(status_code=409, detail=f"Migration cannot start from status {migration.status}")
    migration.status = "QUEUED"
    db.add(AuditEvent(migration_id=migration_id, actor="consultant", action="MIGRATION_STARTED", reason="Consultant requested agent execution"))
    db.commit()
    await event_bus.publish(migration_id, {"type": "agent_status", "migration_id": migration_id, "message": "Migration queued for the agent."})
    start_agent(migration_id)
    return {"migration_id": migration_id, "status": migration.status}


@router.post("/migrations/{migration_id}/stop", status_code=200)
async def stop_migration(migration_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    if migration.status in {"COMPLETED", "ROLLED_BACK", "STOPPED", "FAILED"}:
        return {"migration_id": migration_id, "status": migration.status, "stopped": False, "message": "Migration is already stopped."}

    migration.status = "STOPPED"
    stats = dict(migration.stats or {})
    stats.update({
        "status_message": "Migration stopped by consultant",
        "action_required": False,
        "next_action": "Start a new migration when ready.",
    })
    migration.stats = stats
    db.add(AuditEvent(
        migration_id=migration_id,
        actor="consultant",
        action="MIGRATION_STOPPED",
        reason="Consultant force-stopped the migration.",
    ))
    db.commit()
    cancelled = await stop_agent(migration_id)
    await event_bus.publish(migration_id, {
        "type": "agent_status",
        "migration_id": migration_id,
        "message": "Migration stopped by consultant.",
        "status": "STOPPED",
    })
    return {"migration_id": migration_id, "status": "STOPPED", "stopped": True, "agent_cancelled": cancelled}


@router.get("/migrations/{migration_id}", response_model=MigrationSummaryResponse)
def get_migration(migration_id: str, db: Session = Depends(get_db)) -> MigrationSummaryResponse:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    return MigrationSummaryResponse(
        migration_id=migration.id,
        status=migration.status,
        stats=migration.stats or {},
        files=[{"filename": item.filename, "row_count": item.row_count, "columns": item.columns} for item in migration.files],
    )


@router.get("/migrations/{migration_id}/events")
async def migration_events(migration_id: str):
    async def event_generator():
        async for event in event_bus.stream(migration_id):
            if event.get("type") == "keepalive":
                # SSE comment: keeps the connection alive without creating a UI event.
                yield ": keepalive\n\n"
                continue
            yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/migrations/{migration_id}/escalations", response_model=list[EscalationResponse])
def list_escalations(migration_id: str, db: Session = Depends(get_db)) -> list[EscalationResponse]:
    items = db.query(Escalation).filter(Escalation.migration_id == migration_id).order_by(Escalation.created_at.asc()).all()
    return [EscalationResponse(
        id=item.id,
        reason_code=item.reason_code,
        status=item.status,
        title=item.title,
        entity_name=item.entity_name,
        context=item.context or {},
        suggested_resolution=item.suggested_resolution,
        resolution=item.resolution,
    ) for item in items]


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationResponse)
async def resolve_escalation(
    escalation_id: str,
    resolution: dict,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> EscalationResponse:
    item = db.get(Escalation, escalation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    if item.status != "OPEN":
        raise HTTPException(status_code=409, detail="Escalation is already resolved")
    _validate_resolution_payload(resolution, item)

    item.status = "RESOLVED"
    item.resolution = resolution
    from datetime import datetime, timezone
    item.resolved_at = datetime.now(timezone.utc)
    migration = db.get(MigrationRun, item.migration_id)
    if migration is not None:
        artifacts = dict(migration.artifacts or {})
        decisions = dict(artifacts.get("human_resolutions") or {})
        decisions[item.id] = resolution
        migration.artifacts = {**artifacts, "human_resolutions": decisions}
    db.add(AuditEvent(
        migration_id=item.migration_id,
        record_id=item.record_id,
        actor="consultant",
        action="ESCALATION_RESOLVED",
        reason=item.reason_code,
        after_data=resolution,
    ))
    db.commit()

    remaining = db.query(Escalation).filter(Escalation.migration_id == item.migration_id, Escalation.status == "OPEN").count()
    if migration is not None:
        migration.stats = {
            **(migration.stats or {}),
            "escalations": remaining,
            "progress": max(int((migration.stats or {}).get("progress", 40)), 55) if remaining == 0 else max(int((migration.stats or {}).get("progress", 40)), 40),
            "status_message": "Review resolved — continuing migration" if remaining == 0 else "Review needed — please check Consultant Review",
            "action_required": remaining > 0,
            "next_action": "The agent is resuming automatically." if remaining == 0 else "Resolve the remaining consultant review item(s).",
            "stage": "QUEUED" if remaining == 0 else "CONSULTANT_REVIEW",
        }
        migration.status = "QUEUED" if remaining == 0 else "CONSULTANT_REVIEW"
        db.commit()
        if remaining == 0 and item.reason_code != "target_api_failure":
            start_agent(migration.id)

    return EscalationResponse(
        id=item.id,
        reason_code=item.reason_code,
        status=item.status,
        title=item.title,
        entity_name=item.entity_name,
        context=item.context or {},
        suggested_resolution=item.suggested_resolution,
        resolution=item.resolution,
    )


@router.get("/migrations/{migration_id}/records")
def migration_records(migration_id: str, db: Session = Depends(get_db)) -> list[dict]:
    records = db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration_id).order_by(MigrationRecord.id.asc()).all()
    return [{
        "id": record.id,
        "entity_name": record.entity_name,
        "source_file": record.source_file,
        "source_row_number": record.source_row_number,
        "raw_data": record.raw_data,
        "canonical_data": record.canonical_data,
        "status": record.status,
        "target_record_key": record.target_record_key,
        "target_response": record.target_response,
    } for record in records]


@router.get("/migrations/{migration_id}/preview")
def migration_preview(migration_id: str, db: Session = Depends(get_db)) -> dict:
    records = db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration_id).order_by(MigrationRecord.id.asc()).all()
    return {
        "migration_id": migration_id,
        "status": db.get(MigrationRun, migration_id).status if db.get(MigrationRun, migration_id) else "UNKNOWN",
        "records": [{"id": record.id, "entity_name": record.entity_name, "status": record.status, "canonical_data": record.canonical_data} for record in records],
    }


@router.get("/migrations/{migration_id}/results")
def migration_results(migration_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")

    records = db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration_id).order_by(MigrationRecord.source_file, MigrationRecord.source_row_number).all()
    source_tables: dict[str, list[dict[str, object]]] = {}
    for record in records:
        source_tables.setdefault(record.source_file, []).append(record.raw_data or {})

    target_tables: dict[str, list[dict[str, object]]] = {}
    for record in records:
        if record.status != "PUSHED" or not record.canonical_data:
            continue
        target_tables.setdefault(record.entity_name, []).append(record.canonical_data)

    target_base = settings.target_api_public_url.rstrip("/")
    target_urls = {entity: f"{target_base}/entities/{entity}" for entity in sorted(target_tables)}
    return {
        "migration_id": migration_id,
        "status": migration.status,
        "source_tables": source_tables,
        "target_tables": target_tables,
        "target_urls": target_urls,
    }


@router.post("/migrations/{migration_id}/push", status_code=202)
async def push_migration(migration_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    open_count = db.query(Escalation).filter(Escalation.migration_id == migration_id, Escalation.status == "OPEN").count()
    if open_count:
        raise HTTPException(status_code=409, detail="Resolve all open escalations before pushing to target")
    if migration.status not in {"READY_TO_PUSH", "PARTIAL_FAILURE"}:
        raise HTTPException(status_code=409, detail=f"Migration cannot be pushed from status {migration.status}")
    result = await MigrationExecutor(db).push(migration)
    return {"migration_id": migration_id, **result, "status": migration.status}


@router.post("/migrations/{migration_id}/rollback")
async def rollback_migration(migration_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    migration = db.get(MigrationRun, migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    result = await MigrationExecutor(db).rollback(migration)
    return {"migration_id": migration_id, **result, "status": migration.status}


@router.post("/records/{record_id}/retry")
async def retry_record(record_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    record = db.get(MigrationRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    migration = db.get(MigrationRun, record.migration_id)
    if migration is None:
        raise HTTPException(status_code=404, detail="Migration not found")
    if migration.status == "STOPPED":
        raise HTTPException(status_code=409, detail="Migration is stopped; start a new migration before retrying target records.")
    try:
        response = await MigrationExecutor(db).retry_record(migration, record)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    migration.status = "COMPLETED" if not db.query(Escalation).filter(Escalation.migration_id == migration.id, Escalation.status == "OPEN").count() else "PARTIAL_FAILURE"
    db.commit()
    return {"record_id": record_id, "status": record.status, "response": response}


@router.get("/migrations/{migration_id}/audit", response_model=list[AuditEventResponse])
def migration_audit(migration_id: str, db: Session = Depends(get_db)) -> list[AuditEventResponse]:
    events = db.query(AuditEvent).filter(AuditEvent.migration_id == migration_id).order_by(AuditEvent.created_at.asc()).all()
    return [AuditEventResponse(
        id=item.id, record_id=item.record_id, actor=item.actor, action=item.action, reason=item.reason,
        before_data=item.before_data, after_data=item.after_data, created_at=item.created_at.isoformat(),
    ) for item in events]


@router.get("/health/details", response_model=HealthResponse)
def health_details(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
        return HealthResponse(
            status="ok",
            database="ok",
            llm_configured=bool(settings.openrouter_api_key),
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            semantic_model=settings.semantic_model,
            target_api_url=settings.target_api_url,
            target_api_public_url=settings.target_api_public_url,
        )
    except Exception:
        return HealthResponse(
            status="degraded",
            database="unavailable",
            llm_configured=bool(settings.openrouter_api_key),
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            semantic_model=settings.semantic_model,
            target_api_url=settings.target_api_url,
            target_api_public_url=settings.target_api_public_url,
        )
