from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import EscalationReason
from app.db import SessionLocal
from app.models.migration import AuditEvent, Escalation, MigrationFile, MigrationRecord, MigrationRun
from app.schemas.agent import TransformationStep
from app.schemas.target_schema import EntitySchema, TargetSchema
from app.services.entity_detector import EntityDetector
from app.services.entity_resolution import EntityResolutionEngine
from app.services.event_bus import event_bus
from app.services.file_profiler import FileProfiler
from app.services.llm import LLMClient
from app.services.mapping import MappingEngine
from app.services.merge import MergeEngine
from app.services.semantic import SemanticMatcher
from app.services.source_loader import SourceLoader
from app.services.transformation_engine import TransformationEngine
from app.services.transformation_planner import TransformationPlanner
from app.services.validation import ValidationEngine


@dataclass
class MigrationState:
    migration_id: str
    status: str = "CREATED"
    current_step: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)


class MigrationStopped(Exception):
    """Raised when a consultant force-stops a running agent."""


class MigrationAgent:
    """A stateful agent: it observes migration state, reasons through the LLM, calls tools, and pauses for human decisions."""

    def __init__(self, db: Session, migration: MigrationRun) -> None:
        self.db = db
        self.migration = migration
        self.semantic = SemanticMatcher()
        self.llm = LLMClient(db)
        self.profiler = FileProfiler()
        self.loader = SourceLoader()
        self.detector = EntityDetector(self.llm, self.semantic)
        self.mapper = MappingEngine(self.llm, self.semantic)
        self.transformer = TransformationEngine()
        self.transformation_planner = TransformationPlanner(self.llm, self.semantic)
        self.resolver = EntityResolutionEngine()
        self.merger = MergeEngine()
        self.validator = ValidationEngine()

    async def run(self) -> MigrationState:
        self._assert_not_stopped()
        self._reset_open_work_for_resume()
        state = MigrationState(migration_id=self.migration.id, status="RUNNING")
        target_schema = TargetSchema.model_validate(self.migration.target_schema)

        await self._set_status(state, "PROFILING", "Agent is profiling source files and target schema.")
        payloads: list[dict[str, Any]] = []
        for migration_file in self.migration.files:
            self._assert_not_stopped()
            profile = self.profiler.profile(migration_file.storage_path)
            records = self.loader.load(migration_file.storage_path)
            entity_name, entity_escalations = await self.detector.detect(
                filename=migration_file.filename,
                columns=profile["columns"],
                sample_values=profile["sample_values"],
                target_schema=target_schema,
            )
            payloads.append({"file": migration_file, "profile": profile, "records": records, "entity_name": entity_name})
            for item in entity_escalations:
                await self._create_escalation(item, migration_file=migration_file, entity=entity_name)

        if self._has_open_escalations():
            await self._finalize_waiting(state)
            return state

        await self._set_status(state, "MAPPING", "Resolving clear field matches automatically; AI will review only ambiguous mappings.")
        grouped: dict[str, list[tuple[str, dict[str, Any], MigrationRecord]]] = {}
        field_plans: dict[tuple[str, str], tuple[list[TransformationStep], dict[str, Any] | None]] = {}
        mapping_reviews: dict[str, list[dict[str, Any]]] = {}
        mapped_targets_by_entity: dict[str, set[str]] = {}

        for payload in payloads:
            self._assert_not_stopped()
            entity_name = payload["entity_name"]
            if not entity_name:
                continue
            entity = self._entity(target_schema, entity_name)
            proposals, mapping_escalations = await self.mapper.map_fields(
                columns=payload["profile"]["columns"],
                sample_values=payload["profile"]["sample_values"],
                inferred_types=payload["profile"]["inferred_types"],
                entity=entity,
                human_overrides=self._mapping_overrides(entity_name),
                transformation_overrides=self._transformation_overrides(entity_name),
            )
            mapping_reviews.setdefault(entity_name, []).extend([{**item, "source_file": payload["file"].filename} for item in mapping_escalations])
            mapped_targets_by_entity.setdefault(entity_name, set()).update(item.target_field for item in proposals if item.target_field)
            proposal_by_source = {item.source_field: item for item in proposals}
            for record in payload["records"]:
                self._assert_not_stopped()
                raw = {k: v for k, v in record.items() if not k.startswith("__")}
                canonical: dict[str, Any] = {}
                transformation_errors: list[dict[str, Any]] = []
                for source_field, value in raw.items():
                    proposal = proposal_by_source.get(source_field)
                    if not proposal or not proposal.target_field:
                        continue
                    plan_key = (entity_name, source_field)
                    if plan_key not in field_plans:
                        planned_steps, planner_issue = await self.transformation_planner.plan(
                            source_field=source_field,
                            sample_values=payload["profile"]["sample_values"].get(source_field, []),
                            target_field=self._target_field(entity, proposal.target_field),
                            proposed=proposal.transformations,
                        )
                        field_plans[plan_key] = (planned_steps, planner_issue)
                        if planner_issue:
                            mapping_reviews.setdefault(entity_name, []).append({**planner_issue, "source_file": payload["file"].filename})
                    planned_steps, _planner_issue = field_plans[plan_key]
                    steps = [step.model_dump(mode="json") for step in planned_steps]
                    current, applied, error = self.transformer.execute(value, steps)
                    if error:
                        reason_code = (
                            EscalationReason.NEW_TRANSFORMATION.value if error == "new_transformation"
                            else EscalationReason.UNSUPPORTED_OPERATION.value if error == "unsupported_operation"
                            else EscalationReason.UNMAPPED_VALUE.value if error == "unmapped_value"
                            else EscalationReason.INVALID_RECORD.value
                        )
                        transformation_errors.append({
                            "reason_code": reason_code,
                            "title": f"Transformation requires approval for {source_field}",
                            "context": {
                                "source_field": source_field,
                                "target_field": proposal.target_field,
                                "error": error,
                                "applied": applied,
                                "proposed_transformations": steps,
                                "record_value": value,
                                "record_key": self._record_key(entity, canonical or raw),
                            },
                            "suggested_resolution": {"transformations": steps},
                        })
                    else:
                        canonical[proposal.target_field] = current

                record_key = self._record_key(entity, canonical or raw)
                migration_record = MigrationRecord(
                    migration_id=self.migration.id,
                    entity_name=entity_name,
                    source_file=payload["file"].filename,
                    source_row_number=int(record.get("__source_row_number", 0)),
                    raw_data=raw,
                    canonical_data=canonical,
                    status="CONSULTANT_REVIEW" if transformation_errors else "TRANSFORMED",
                    target_record_key=record_key,
                )
                self.db.add(migration_record)
                self.db.flush()
                for item in transformation_errors:
                    await self._create_escalation(item, record_id=migration_record.id, entity=entity_name)
                grouped.setdefault(entity_name, []).append((payload["file"].filename, canonical, migration_record))

        # Only escalate ambiguous field mappings that are necessary to cover the target schema.
        entity_names = {payload["entity_name"] for payload in payloads if payload.get("entity_name")}
        for entity_name in entity_names:
            entity = self._entity(target_schema, entity_name)
            covered = mapped_targets_by_entity.get(entity_name, set())
            required = {field.name for field in entity.fields if field.required}
            missing_required = required - covered

            # Required target fields with no plausible source mapping are the
            # exceptional cases where automation genuinely cannot proceed.
            # Give the consultant the actual source columns so the UI can offer a
            # simple "Use <source> → <target>" action instead of requiring JSON.
            available_source_fields = sorted({
                column
                for payload in payloads
                if payload.get("entity_name") == entity_name
                for column in payload.get("profile", {}).get("columns", [])
            })
            for missing_field in sorted(missing_required):
                await self._create_escalation({
                    "reason_code": EscalationReason.AMBIGUOUS_MAPPING.value,
                    "title": f"Required target field {missing_field} has no safe source mapping",
                    "context": {
                        "target_field": missing_field,
                        "candidates": [],
                        "available_source_fields": available_source_fields,
                        "reason": "No uploaded source field was sufficiently plausible for autonomous mapping.",
                        "resolution_note": f"Choose the source column that should populate {missing_field}. The agent will apply the choice and continue.",
                    },
                    "suggested_resolution": {"target_field": missing_field},
                }, entity=entity_name)

            for item in mapping_reviews.get(entity_name, []):
                context = item.get("context") or {}
                candidates = [candidate.get("target_field") for candidate in context.get("candidates", []) if candidate.get("target_field")]
                explicit = context.get("target_field")
                possible_targets = set(candidates) | ({explicit} if explicit else set())
                if possible_targets & missing_required or not candidates:
                    await self._create_escalation(item, entity=entity_name)
                else:
                    self.db.add(AuditEvent(
                        migration_id=self.migration.id,
                        actor="agent",
                        action="AMBIGUITY_SUPPRESSED",
                        reason=f"Redundant ambiguous field {context.get('source_field')} because required target coverage exists elsewhere.",
                        after_data={"source_field": context.get("source_field"), "candidates": candidates},
                    ))
        self.db.commit()

        if self._has_open_escalations():
            await self._finalize_waiting(state)
            return state

        await self._set_status(state, "RECONCILING", "Agent is resolving duplicate representations across source files.")
        self._assert_not_stopped()
        await self._set_status(state, "VALIDATING", "Agent is validating the transformed target data.")
        for entity_name, triples in grouped.items():
            self._assert_not_stopped()
            entity = self._entity(target_schema, entity_name)
            resolution_groups, match_escalations = self.resolver.resolve([item[1] for item in triples], entity)
            for item in match_escalations:
                await self._create_escalation(item, entity=entity_name)

            for group in resolution_groups:
                members = [triples[index] for index in group["record_indexes"]]
                merged, merge_escalations = self.merger.merge_group([(source, canonical) for source, canonical, _ in members], entity)
                # A consultant correction belongs to this canonical record/group, not to
                # the current source row. Apply previously approved conflict corrections
                # BEFORE deciding whether the merge still needs review. The old flow
                # escalated first and applied corrections afterward, which caused the same
                # conflict to reappear forever on every agent resume.
                record_key = self._record_key(entity, merged) or members[0][2].target_record_key or members[0][2].id
                source_files = sorted({source for source, _, _ in members})
                corrections = self._field_corrections(entity_name, record_key)
                merged, unresolved_merge_escalations = self._apply_merge_corrections(
                    merged, merge_escalations, corrections
                )
                for field, value in corrections.items():
                    if field in merged and value not in (None, ""):
                        self.db.add(AuditEvent(
                            migration_id=self.migration.id,
                            actor="agent",
                            action="CONFLICT_RESOLUTION_APPLIED",
                            reason=f"Applied consultant decision for {field}",
                            after_data={"field": field, "value": value, "record_key": record_key},
                        ))

                for item in unresolved_merge_escalations:
                    context = dict(item.get("context") or {})
                    context.update({
                        "record_key": record_key,
                        "source_files": source_files,
                        "record": merged,
                        "record_summary": self._record_summary(entity, merged),
                    })
                    await self._create_escalation({**item, "context": context}, record_id=members[0][2].id, entity=entity_name)
                if unresolved_merge_escalations:
                    for _, _, record in members:
                        record.status = "CONSULTANT_REVIEW"
                    continue
                errors = self.validator.validate_record(merged, entity)
                if errors:
                    for _, _, record in members:
                        record.status = "CONSULTANT_REVIEW"
                    missing_fields = [
                        error.get("field") for error in errors
                        if isinstance(error, dict) and error.get("code") in {"required", "missing"} and error.get("field")
                    ]
                    title = (
                        f"Missing required field for {record_key}"
                        if missing_fields else f"Validation failed for {record_key}"
                    )
                    reason = (
                        f"The target requires {', '.join(str(field).replace('_', ' ') for field in missing_fields)}, "
                        "but no safe value was found in the uploaded data."
                        if missing_fields else
                        "The canonical record failed target-schema validation and needs a consultant correction."
                    )
                    await self._create_escalation({
                        "reason_code": EscalationReason.INVALID_RECORD.value,
                        "title": title,
                        "context": {
                            "errors": errors,
                            "record": merged,
                            "record_summary": self._record_summary(entity, merged),
                            "record_key": record_key,
                            "source_files": source_files,
                            "member_count": len(members),
                            "missing_fields": missing_fields,
                            "reason": reason,
                            "resolution_note": "Correct the value for this specific record. The agent will revalidate it and continue automatically.",
                        },
                        "suggested_resolution": {"corrections": {str(field): "" for field in missing_fields}},
                    }, record_id=members[0][2].id, entity=entity_name)
                    continue
                primary = members[0][2]
                primary.canonical_data = merged
                primary.status = "READY_TO_PUSH"
                primary.resolution_group = group["group_id"]
                for _, _, record in members[1:]:
                    record.canonical_data = merged
                    record.status = "RECONCILED"
                    record.resolution_group = group["group_id"]

        self.db.commit()
        await self._finalize_state(state)
        return state

    def _reset_open_work_for_resume(self) -> None:
        if self.migration.status not in {"QUEUED", "PAUSED", "CONSULTANT_REVIEW"}:
            return
        self.db.query(MigrationRecord).filter(MigrationRecord.migration_id == self.migration.id).delete(synchronize_session=False)
        self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "OPEN").update({"status": "SUPERSEDED"}, synchronize_session=False)
        self.db.commit()

    async def _set_status(self, state: MigrationState, status: str, message: str) -> None:
        self._assert_not_stopped()
        progress_by_status = {
            "QUEUED": 5,
            "PROFILING": 15,
            "MAPPING": 35,
            "RECONCILING": 58,
            "VALIDATING": 72,
            "READY_TO_PUSH": 82,
            "PUSHING": 90,
        }
        state.status = status
        state.current_step = status
        self.migration.status = status
        stats = dict(self.migration.stats or {})
        next_actions = {
            "PROFILING": "Reading files, sample values, and target schema.",
            "MAPPING": "Clear matches are automatic; AI checks only ambiguous mappings.",
            "RECONCILING": "Combining duplicate representations into canonical records.",
            "VALIDATING": "Checking types, required fields, formats, and enums.",
            "PUSHING": "Writing validated records to the target system.",
        }
        stats.update({
            "progress": progress_by_status.get(status, int(stats.get("progress", 0))),
            "status_message": message,
            "action_required": False,
            "next_action": next_actions.get(status, ""),
            "stage": status,
        })
        self.migration.stats = stats
        self.db.add(AuditEvent(migration_id=self.migration.id, actor="agent", action="AGENT_STATUS", reason=status))
        self.db.commit()
        await event_bus.publish(self.migration.id, {
            "type": "agent_status",
            "migration_id": self.migration.id,
            "message": message,
            "status": status,
            "progress": stats["progress"],
        })

    async def _create_escalation(self, item: dict[str, Any], *, record_id: str | None = None, migration_file: MigrationFile | None = None, entity: str | None = None) -> None:
        reason_code = item.get("reason_code", EscalationReason.INVALID_RECORD.value)
        context = dict(item.get("context") or {})
        if migration_file is not None:
            context.setdefault("source_file", migration_file.filename)
        if item.get("source_file"):
            context.setdefault("source_file", str(item.get("source_file")))
            context.setdefault("source_files", [str(item.get("source_file"))])
        record = self.db.get(MigrationRecord, record_id) if record_id else None
        if record is not None:
            context.setdefault("source_file", record.source_file)
            context.setdefault("source_files", [record.source_file])
            context.setdefault("record_key", record.target_record_key)
            record_data = record.canonical_data or record.raw_data or {}
            context.setdefault("record", record_data)
            try:
                target_entity = self._entity(TargetSchema.model_validate(self.migration.target_schema), entity) if entity else None
                if target_entity is not None:
                    context.setdefault("record_summary", self._record_summary(target_entity, record_data))
            except Exception:
                context.setdefault("record_summary", self._generic_record_summary(record_data))
        elif context.get("record") is None and item.get("record") is not None:
            context["record"] = item.get("record")
        if context.get("source_files") and not context.get("source_file"):
            context["source_file"] = context["source_files"][0]
        # Field-level mapping/transformation issues should be represented once per
        # migration, not once per affected row. One consultant decision can then
        # be replayed across every affected record on agent resume.
        field_level_reasons = {
            EscalationReason.AMBIGUOUS_MAPPING.value,
            EscalationReason.LLM_UNAVAILABLE.value,
            EscalationReason.NEW_TRANSFORMATION.value,
            EscalationReason.UNSUPPORTED_OPERATION.value,
            EscalationReason.UNMAPPED_VALUE.value,
        }
        signature = self._escalation_signature(reason_code, entity, context, record_id)
        if reason_code in field_level_reasons or reason_code == EscalationReason.INVALID_RECORD.value:
            for existing in self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "OPEN").all():
                existing_context = existing.context or {}
                if self._escalation_signature(existing.reason_code, existing.entity_name, existing_context, existing.record_id) == signature:
                    return

        escalation = Escalation(
            migration_id=self.migration.id,
            record_id=record_id,
            entity_name=entity,
            reason_code=reason_code,
            title=item.get("title", "Consultant review required"),
            context=context,
            suggested_resolution=item.get("suggested_resolution"),
        )
        self.db.add(escalation)
        self.db.flush()
        await event_bus.publish(self.migration.id, {
            "type": "escalation_created", "migration_id": self.migration.id,
            "message": escalation.title, "reason_code": escalation.reason_code, "escalation_id": escalation.id,
        })

    @staticmethod
    def _escalation_signature(reason_code: str, entity: str | None, context: dict[str, Any], record_id: str | None) -> str:
        if reason_code in {
            EscalationReason.AMBIGUOUS_MAPPING.value,
            EscalationReason.LLM_UNAVAILABLE.value,
            EscalationReason.NEW_TRANSFORMATION.value,
            EscalationReason.UNSUPPORTED_OPERATION.value,
            EscalationReason.UNMAPPED_VALUE.value,
        }:
            parts = [reason_code, str(entity), str(context.get("source_field")), str(context.get("target_field")), str(context.get("error"))]
        elif reason_code == EscalationReason.INVALID_RECORD.value:
            errors = context.get("errors") or []
            parts = [reason_code, str(entity), str(sorted((error.get("field"), error.get("code")) for error in errors if isinstance(error, dict)))]
        else:
            parts = [reason_code, str(entity), str(record_id), str(context.get("field")), str(context.get("source_field"))]
        return "|".join(parts)

    async def _finalize_waiting(self, state: MigrationState) -> None:
        self.migration.status = "CONSULTANT_REVIEW"
        stats = self._stats()
        stats.update({
            "progress": max(40, min(78, int(stats.get("progress", 40)))),
            "status_message": "Review needed — check Consultant Review",
            "action_required": True,
            "next_action": "Resolve the open review item(s) to continue.",
            "stage": "CONSULTANT_REVIEW",
        })
        self.migration.stats = stats
        self.db.add(AuditEvent(migration_id=self.migration.id, actor="agent", action="AGENT_CONSULTANT_REVIEW", reason="Open escalation(s) require consultant intervention"))
        self.db.commit()
        await event_bus.publish(self.migration.id, {
            "type": "agent_status",
            "migration_id": self.migration.id,
            "message": "Review needed — please check Consultant Review.",
            "status": "CONSULTANT_REVIEW",
            "progress": stats["progress"],
            "action_required": True,
        })

    async def _finalize_state(self, state: MigrationState) -> None:
        self._assert_not_stopped()
        escalation_count = self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "OPEN").count()
        self.migration.stats = self._stats()
        if escalation_count:
            self.migration.status = "CONSULTANT_REVIEW"
            message = "Review needed — check Consultant Review"
            progress = max(40, min(78, int((self.migration.stats or {}).get("progress", 40))))
            action_required = True
            next_action = "Resolve the open consultant review item(s)."
        else:
            self.migration.status = "READY_TO_PUSH"
            message = "Ready to push changes"
            progress = 82
            action_required = True
            next_action = "Review the target preview, then push the changes."
        stats = dict(self.migration.stats or {})
        stats.update({"progress": progress, "status_message": message, "action_required": action_required, "next_action": next_action, "stage": self.migration.status})
        self.migration.stats = stats
        self.db.add(AuditEvent(migration_id=self.migration.id, actor="agent", action="AGENT_RUN_COMPLETED", reason=self.migration.status))
        self.db.commit()
        await event_bus.publish(self.migration.id, {
            "type": "agent_status", "migration_id": self.migration.id, "message": message,
            "status": self.migration.status, "progress": progress, "action_required": action_required,
        })

    def _stats(self) -> dict[str, int]:
        records = self.db.query(MigrationRecord).filter(MigrationRecord.migration_id == self.migration.id).all()
        return {
            "files": len(self.migration.files),
            "records": len(records),
            "escalations": sum(1 for x in self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id).all() if x.status == "OPEN"),
            "ready_to_push": sum(1 for x in records if x.status == "READY_TO_PUSH"),
            "pushed": sum(1 for x in records if x.status == "PUSHED"),
            "push_failed": sum(1 for x in records if x.status == "PUSH_FAILED"),
        }

    def _mapping_overrides(self, entity_name: str) -> dict[str, dict[str, Any]]:
        overrides: dict[str, dict[str, Any]] = {}
        items = self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "RESOLVED").all()
        for item in items:
            context = item.context or {}
            if item.entity_name not in {None, entity_name}:
                continue
            resolution = item.resolution or {}
            source_field = context.get("source_field") or resolution.get("source_field")
            if source_field and resolution.get("target_field"):
                overrides[str(source_field)] = resolution
            for mapping in resolution.get("mappings") or []:
                if isinstance(mapping, dict) and mapping.get("source_field") and mapping.get("target_field"):
                    overrides[str(mapping["source_field"])] = mapping
        return overrides

    def _transformation_overrides(self, entity_name: str) -> dict[str, list[dict[str, Any]]]:
        overrides: dict[str, list[dict[str, Any]]] = {}
        items = self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "RESOLVED").all()
        for item in items:
            context = item.context or {}
            if item.entity_name not in {None, entity_name}:
                continue
            resolution = item.resolution or {}
            source_field = context.get("source_field")
            transformations = resolution.get("transformations")
            if source_field and transformations:
                overrides[source_field] = transformations
            for mapping in resolution.get("mappings") or []:
                if isinstance(mapping, dict) and mapping.get("source_field") and mapping.get("transformations"):
                    overrides[str(mapping["source_field"])] = mapping["transformations"]
        return overrides

    @staticmethod
    def _apply_merge_corrections(
        merged: dict[str, Any],
        merge_escalations: list[dict[str, Any]],
        corrections: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Apply resolved conflict corrections before deciding which conflicts remain open."""
        resolved = dict(merged)
        for field, value in corrections.items():
            if field in resolved and value not in (None, ""):
                resolved[field] = value

        unresolved: list[dict[str, Any]] = []
        for item in merge_escalations:
            field = (item.get("context") or {}).get("field")
            if field and field in corrections:
                continue
            unresolved.append(item)
        return resolved, unresolved

    def _field_corrections(self, entity_name: str, record_key: str | None) -> dict[str, Any]:
        corrections: dict[str, Any] = {}
        if not record_key:
            return corrections
        items = self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "RESOLVED").all()
        for item in items:
            if item.entity_name not in {None, entity_name}:
                continue
            if item.reason_code not in {EscalationReason.INVALID_RECORD.value, EscalationReason.CONFLICTING_VALUES.value}:
                continue
            context = item.context or {}
            if str(context.get("record_key") or "") != str(record_key):
                continue
            corrections.update({str(k): v for k, v in ((item.resolution or {}).get("corrections") or {}).items() if v not in (None, "")})
        return corrections

    @staticmethod
    def _record_key(entity: EntitySchema, data: dict[str, Any]) -> str | None:
        for field in entity.fields:
            if field.unique and data.get(field.name) not in (None, ""):
                return str(data[field.name]).strip()
        for fallback in ("employee_id", "id", "record_id"):
            if data.get(fallback) not in (None, ""):
                return str(data[fallback]).strip()
        return None

    @staticmethod
    def _generic_record_summary(data: dict[str, Any]) -> dict[str, Any]:
        preferred = ("employee_id", "id", "record_id", "first_name", "last_name", "email", "name")
        summary: dict[str, Any] = {}
        for key in preferred:
            if key in data and data.get(key) not in (None, ""):
                summary[key] = data[key]
        return summary

    @classmethod
    def _record_summary(cls, entity: EntitySchema, data: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        names = [field.name for field in entity.fields if field.unique]
        names.extend(name for name in ("first_name", "last_name", "email", "name") if name in {field.name for field in entity.fields})
        for name in names:
            if name in data and data.get(name) not in (None, ""):
                summary[name] = data[name]
        return summary or cls._generic_record_summary(data)

    @staticmethod
    def _target_field(entity: EntitySchema, name: str):
        for field in entity.fields:
            if field.name == name:
                return field
        raise KeyError(name)

    @staticmethod
    def _entity(schema: TargetSchema, name: str) -> EntitySchema:
        for entity in schema.entities:
            if entity.entity_name == name:
                return entity
        raise KeyError(name)

    def _has_open_escalations(self) -> bool:
        return self.db.query(Escalation).filter(Escalation.migration_id == self.migration.id, Escalation.status == "OPEN").count() > 0

    def _assert_not_stopped(self) -> None:
        current = self.db.get(MigrationRun, self.migration.id)
        if current is None or current.status == "STOPPED":
            raise MigrationStopped()


async def run_migration_agent(migration_id: str) -> None:
    db = SessionLocal()
    try:
        migration = db.get(MigrationRun, migration_id)
        if migration is None:
            return
        agent = MigrationAgent(db, migration)
        try:
            await agent.run()
        except MigrationStopped:
            # The stop endpoint already persisted the STOPPED state. Do not overwrite it.
            db.expire_all()
            current = db.get(MigrationRun, migration_id)
            if current is not None and current.status != "STOPPED":
                current.status = "STOPPED"
                current.stats = {**(current.stats or {}), "status_message": "Migration stopped", "action_required": False}
                db.commit()
        except asyncio.CancelledError:
            db.expire_all()
            current = db.get(MigrationRun, migration_id)
            if current is not None and current.status != "STOPPED":
                current.status = "STOPPED"
                current.stats = {**(current.stats or {}), "status_message": "Migration stopped", "action_required": False}
                db.commit()
            raise
        except Exception as exc:
            migration.status = "FAILED"
            migration.stats = {**(migration.stats or {}), "progress": 100, "status_message": "Migration failed — review Agent Activity", "action_required": True, "next_action": "Review Agent Activity and Audit Trail.", "error": str(exc)}
            db.add(AuditEvent(migration_id=migration_id, actor="agent", action="AGENT_FAILED", reason=str(exc)))
            db.commit()
            await event_bus.publish(migration_id, {"type": "agent_status", "migration_id": migration_id, "message": f"Migration failed — review Agent Activity: {exc}", "status": "FAILED", "progress": 100, "action_required": True})
    finally:
        db.close()
