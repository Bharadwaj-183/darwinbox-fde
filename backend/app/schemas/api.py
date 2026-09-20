from typing import Any
from pydantic import BaseModel, Field


class MigrationCreatedResponse(BaseModel):
    migration_id: str
    status: str
    files: list[str]
    entities: list[str]


class MigrationSummaryResponse(BaseModel):
    migration_id: str
    status: str
    stats: dict[str, Any] = Field(default_factory=dict)
    files: list[dict[str, Any]] = Field(default_factory=list)


class EscalationResponse(BaseModel):
    id: str
    reason_code: str
    status: str
    title: str
    entity_name: str | None
    context: dict[str, Any]
    suggested_resolution: dict[str, Any] | None
    resolution: dict[str, Any] | None


class AuditEventResponse(BaseModel):
    id: str
    record_id: str | None
    actor: str
    action: str
    reason: str
    before_data: dict[str, Any] | None
    after_data: dict[str, Any] | None
    created_at: str


class HealthResponse(BaseModel):
    status: str
    database: str
    llm_configured: bool = False
    llm_provider: str = ""
    llm_model: str = ""
    semantic_model: str = ""
    target_api_url: str = ""
    target_api_public_url: str = ""
