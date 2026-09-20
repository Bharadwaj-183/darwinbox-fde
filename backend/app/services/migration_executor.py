from __future__ import annotations

from typing import Any
import logging

from sqlalchemy.orm import Session

from app.core.constants import EscalationReason
from app.models.migration import AuditEvent, Escalation, MigrationRecord, MigrationRun
from app.services.event_bus import event_bus
from app.services.target_client import TargetClient

logger = logging.getLogger(__name__)


class MigrationExecutor:
    def __init__(self, db: Session) -> None:
        from app.core.config import get_settings
        self.db = db
        self.target = TargetClient(get_settings().target_api_url)

    def _refresh_stats(self, migration: MigrationRun) -> None:
        records = self.db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration.id).all()
        migration.stats = {
            **(migration.stats or {}),
            "records": len(records),
            "ready_to_push": sum(1 for r in records if r.status == "READY_TO_PUSH"),
            "pushed": sum(1 for r in records if r.status == "PUSHED"),
            "push_failed": sum(1 for r in records if r.status == "PUSH_FAILED"),
            "escalations": self.db.query(Escalation).filter(Escalation.migration_id == migration.id, Escalation.status == "OPEN").count(),
        }

    async def push(self, migration: MigrationRun) -> dict[str, int]:
        records = self.db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration.id, MigrationRecord.status == "READY_TO_PUSH").all()
        migration.status = "PUSHING"
        migration.stats = {**(migration.stats or {}), "progress": 90, "status_message": "Pushing changes to target", "action_required": False, "next_action": "" , "stage": "PUSHING"}
        self.db.commit()
        await event_bus.publish(migration.id, {"type": "agent_status", "migration_id": migration.id, "message": "Pushing changes to target", "status": "PUSHING", "progress": 90})
        success = failed = 0
        for record in records:
            current = self.db.get(MigrationRun, migration.id)
            if current is None or current.status == "STOPPED":
                migration.status = "STOPPED"
                migration.stats = {**(migration.stats or {}), "status_message": "Migration stopped by consultant", "action_required": False, "next_action": "Start a new migration when ready.", "stage": "STOPPED"}
                self.db.commit()
                await event_bus.publish(migration.id, {"type": "agent_status", "migration_id": migration.id, "message": "Migration stopped by consultant.", "status": "STOPPED", "progress": migration.stats.get("progress", 90)})
                return {"success": success, "failed": failed}
            try:
                response = await self.target.push(record.entity_name, record.canonical_data or {}, idempotency_key=f"{migration.id}:{record.id}")
                record.status = "PUSHED"
                record.target_record_key = response.get("record_key")
                record.target_response = response
                success += 1
                self.db.add(AuditEvent(migration_id=migration.id, record_id=record.id, actor="agent", action="TARGET_PUSHED", reason="Target API accepted the record", after_data=response))
                await event_bus.publish(migration.id, {"type": "record_pushed", "migration_id": migration.id, "message": f"Pushed {record.entity_name} record {record.id}", "record_id": record.id})
            except Exception as exc:
                record.status = "PUSH_FAILED"
                failed += 1
                logger.error("Target push failed for migration=%s record=%s: %s", migration.id, record.id, exc)
                self.db.add(Escalation(
                    migration_id=migration.id,
                    record_id=record.id,
                    entity_name=record.entity_name,
                    reason_code=EscalationReason.TARGET_API_FAILURE.value,
                    title=f"Target API failed for {record.entity_name}",
                    context={
                        "error": str(exc),
                        "canonical_data": record.canonical_data,
                        "record_id": record.id,
                    },
                ))
                self.db.add(AuditEvent(migration_id=migration.id, record_id=record.id, actor="agent", action="TARGET_PUSH_FAILED", reason=str(exc)))
                await event_bus.publish(migration.id, {"type": "escalation_created", "migration_id": migration.id, "message": f"Target push failed for record {record.id}: {exc}", "reason_code": EscalationReason.TARGET_API_FAILURE.value, "record_id": record.id})
            self.db.commit()

        current = self.db.get(MigrationRun, migration.id)
        if current is not None and current.status == "STOPPED":
            migration.status = "STOPPED"
            migration.stats = {**(migration.stats or {}), "status_message": "Migration stopped by consultant", "action_required": False, "next_action": "Start a new migration when ready.", "stage": "STOPPED"}
            self.db.commit()
            await event_bus.publish(migration.id, {"type": "agent_status", "migration_id": migration.id, "message": "Migration stopped by consultant.", "status": "STOPPED", "progress": migration.stats.get("progress", 90)})
            return {"success": success, "failed": failed}
        migration.status = "COMPLETED" if failed == 0 else "PARTIAL_FAILURE"
        self._refresh_stats(migration)
        stats = dict(migration.stats or {})
        if failed:
            stats.update({
                "progress": 90,
                "status_message": f"{failed} record(s) failed — please check Retry",
                "action_required": True,
                "next_action": "Retry the failed target record(s) or roll back the migration.",
                "stage": "PARTIAL_FAILURE",
            })
        else:
            stats.update({
                "progress": 100,
                "status_message": "Migration completed successfully",
                "action_required": False,
                "next_action": "View the final source and target data.",
                "stage": "COMPLETED",
            })
        migration.stats = stats
        self.db.add(AuditEvent(migration_id=migration.id, actor="agent", action="MIGRATION_PUSH_COMPLETE", reason=migration.status))
        self.db.commit()
        message = stats["status_message"]
        await event_bus.publish(migration.id, {"type": "agent_status", "migration_id": migration.id, "message": message, "status": migration.status, "progress": stats["progress"], "action_required": bool(stats["action_required"])})
        return {"success": success, "failed": failed}

    async def retry_record(self, migration: MigrationRun, record: MigrationRecord) -> dict[str, Any]:
        response = await self.target.push(record.entity_name, record.canonical_data or {}, idempotency_key=f"{migration.id}:{record.id}")
        record.status = "PUSHED"
        record.target_record_key = response.get("record_key")
        record.target_response = response
        self.db.query(Escalation).filter(Escalation.migration_id == migration.id, Escalation.record_id == record.id, Escalation.status == "OPEN").update({"status": "RESOLVED", "resolution": {"action": "retry_succeeded"}}, synchronize_session=False)
        self.db.add(AuditEvent(migration_id=migration.id, record_id=record.id, actor="consultant", action="TARGET_RETRY_SUCCEEDED", reason="Manual retry succeeded", after_data=response))
        self._refresh_stats(migration)
        remaining_failures = int((migration.stats or {}).get("push_failed", 0))
        if remaining_failures == 0:
            migration.status = "COMPLETED"
            migration.stats = {**(migration.stats or {}), "progress": 100, "status_message": "Migration completed after retry", "action_required": False, "next_action": "View the final source and target data.", "stage": "COMPLETED"}
            message = "Migration completed after retry"
        else:
            migration.status = "PARTIAL_FAILURE"
            migration.stats = {**(migration.stats or {}), "progress": 90, "status_message": f"{remaining_failures} record(s) failed — please check Retry", "action_required": True, "next_action": "Retry the remaining failed target record(s) or roll back.", "stage": "PARTIAL_FAILURE"}
            message = f"{remaining_failures} record(s) still need retry"
        self.db.commit()
        await event_bus.publish(migration.id, {"type": "record_pushed", "migration_id": migration.id, "message": "Target record retry succeeded.", "record_id": record.id, "retry": True})
        await event_bus.publish(migration.id, {"type": "agent_status", "migration_id": migration.id, "message": message, "status": migration.status, "progress": migration.stats["progress"], "action_required": bool(migration.stats["action_required"])})
        return response

    async def rollback(self, migration: MigrationRun) -> dict[str, int]:
        pushed = self.db.query(MigrationRecord).filter(MigrationRecord.migration_id == migration.id, MigrationRecord.status == "PUSHED").all()
        deleted = failed = 0
        for record in pushed:
            if not record.target_record_key:
                continue
            try:
                await self.target.delete(record.entity_name, record.target_record_key)
                record.status = "ROLLED_BACK"
                deleted += 1
            except Exception as exc:
                failed += 1
                logger.error("Rollback failed for migration=%s record=%s: %s", migration.id, record.id, exc)
                self.db.add(AuditEvent(migration_id=migration.id, record_id=record.id, actor="consultant", action="ROLLBACK_FAILED", reason=str(exc)))
            self.db.commit()
        if failed == 0:
            self.db.query(Escalation).filter(
                Escalation.migration_id == migration.id,
                Escalation.reason_code == EscalationReason.TARGET_API_FAILURE.value,
                Escalation.status == "OPEN",
            ).update(
                {"status": "RESOLVED", "resolution": {"action": "rolled_back"}},
                synchronize_session=False,
            )
        migration.status = "ROLLED_BACK" if failed == 0 else "ROLLBACK_PARTIAL_FAILURE"
        self._refresh_stats(migration)
        migration.stats = {**(migration.stats or {}), "progress": 100 if failed == 0 else 92, "status_message": "Migration rolled back successfully" if failed == 0 else "Rollback partially failed — review Audit Trail", "action_required": failed > 0, "next_action": "Review Audit Trail and remaining target records." if failed else "Start a new migration when ready.", "stage": migration.status}
        self.db.add(AuditEvent(
            migration_id=migration.id,
            actor="consultant",
            action="ROLLBACK_COMPLETE",
            reason=f"{migration.status}: {deleted} target record(s) removed, {failed} failed.",
        ))
        self.db.commit()
        await event_bus.publish(
            migration.id,
            {
                "type": "agent_status",
                "migration_id": migration.id,
                "message": migration.stats.get("status_message", "Rollback complete."),
                "status": migration.status,
                "progress": migration.stats.get("progress", 100),
                "action_required": bool(migration.stats.get("action_required")),
            },
        )
        return {"deleted": deleted, "failed": failed}
