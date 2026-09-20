from __future__ import annotations

from typing import Any

from app.core.constants import EscalationReason
from app.schemas.target_schema import EntitySchema


class MergeEngine:
    def merge_group(
        self,
        records: list[tuple[str, dict[str, Any]]],
        entity: EntitySchema,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        precedence = {name: index for index, name in enumerate(entity.source_precedence)}
        ordered = sorted(records, key=lambda item: precedence.get(item[0], len(precedence)))
        merged: dict[str, Any] = {}
        escalations: list[dict[str, Any]] = []

        for field in entity.fields:
            values = [(source, record.get(field.name)) for source, record in ordered if record.get(field.name) not in (None, "")]
            if not values:
                merged[field.name] = None
                continue
            winner_source, winner_value = values[0]
            conflicts = [item for item in values[1:] if self._norm(item[1]) != self._norm(winner_value)]
            if conflicts:
                if entity.source_precedence and winner_source in precedence and all(source in precedence for source, _ in values):
                    merged[field.name] = winner_value
                    # Configured source precedence is an explicit customer rule, so it
                    # resolves the conflict autonomously. The event/audit layer can still
                    # record that a conflict existed.
                else:
                    merged[field.name] = winner_value
                    escalations.append({
                        "reason_code": EscalationReason.CONFLICTING_VALUES.value,
                        "title": f"Conflicting values for {field.name}",
                        "context": {"field": field.name, "values": [{"source": s, "value": v} for s, v in values]},
                    })
            else:
                merged[field.name] = winner_value
        return merged, escalations

    @staticmethod
    def _norm(value: Any) -> str:
        return str(value).strip().casefold()
