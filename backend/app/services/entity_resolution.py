from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import uuid4

from rapidfuzz.fuzz import ratio

from app.core.constants import EscalationReason
from app.schemas.target_schema import EntitySchema


class EntityResolutionEngine:
    """Resolve multiple source representations into entity groups using stable keys first, fuzzy evidence second."""

    def resolve(self, records: list[dict[str, Any]], entity: EntitySchema) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        unique_fields = [field.name for field in entity.fields if field.unique]
        groups: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []
        assigned: set[int] = set()

        # Build connected components for records sharing the same unique value.
        parent = list(range(len(records)))

        def find(value: int) -> int:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: int, right: int) -> None:
            root_l, root_r = find(left), find(right)
            if root_l != root_r:
                parent[root_r] = root_l

        indexes: dict[str, dict[str, list[int]]] = {field: defaultdict(list) for field in unique_fields}
        for index, record in enumerate(records):
            for field in unique_fields:
                value = self._norm(record.get(field))
                if value:
                    indexes[field][value].append(index)

        for field, buckets in indexes.items():
            for key, members in buckets.items():
                if len(members) > 1:
                    for member in members[1:]:
                        union(members[0], member)

        components: dict[int, list[int]] = defaultdict(list)
        for index in range(len(records)):
            components[find(index)].append(index)

        for members in components.values():
            if len(members) <= 1:
                continue
            # A component formed by at least one shared unique key is a valid
            # identity group. If another unique field differs (for example
            # E1024 vs 1024 while email is identical), let the merge policy
            # resolve the field conflict instead of blocking the entire entity.
            key_values: dict[str, set[str]] = {field: set() for field in unique_fields}
            for member in members:
                for field in unique_fields:
                    value = self._norm(records[member].get(field))
                    if value:
                        key_values[field].add(value)
            groups.append(self._group(members, "exact", [field for field, values in key_values.items() if len(values) == 1]))
            assigned.update(members)

        remaining = [i for i in range(len(records)) if i not in assigned]
        while remaining:
            index = remaining.pop(0)
            best_index: int | None = None
            best_score = 0.0
            second_score = 0.0
            for candidate in remaining:
                score = self._record_similarity(records[index], records[candidate])
                if score > best_score:
                    second_score = best_score
                    best_score = score
                    best_index = candidate
                elif score > second_score:
                    second_score = score
            if best_index is not None and best_score >= 0.90 and best_score - second_score >= 0.08:
                remaining.remove(best_index)
                assigned.update({index, best_index})
                groups.append(self._group([index, best_index], "fuzzy", []))
            else:
                groups.append(self._group([index], "single", []))
                assigned.add(index)
                if best_index is not None and best_score >= 0.72:
                    escalations.append(self._escalation(records[index], {
                        "match_type": "ambiguous_fuzzy",
                        "best_candidate_index": best_index,
                        "score": round(best_score, 4),
                        "second_score": round(second_score, 4),
                    }))
        return groups, escalations

    @staticmethod
    def _group(indexes: list[int], method: str, matched_fields: list[str]) -> dict[str, Any]:
        return {"group_id": str(uuid4()), "record_indexes": indexes, "method": method, "matched_fields": matched_fields}

    @staticmethod
    def _record_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
        scores: list[float] = []
        for field in ("first_name", "last_name", "email", "phone", "date_of_birth"):
            lval, rval = left.get(field), right.get(field)
            if lval in (None, "") or rval in (None, ""):
                continue
            if field in {"email", "phone", "date_of_birth"}:
                scores.append(1.0 if EntityResolutionEngine._norm(lval) == EntityResolutionEngine._norm(rval) else 0.0)
            else:
                scores.append(ratio(str(lval), str(rval)) / 100.0)
        return sum(scores) / len(scores) if scores else 0.0

    @staticmethod
    def _norm(value: Any) -> str:
        return " ".join(str(value).strip().casefold().split()) if value not in (None, "") else ""

    @staticmethod
    def _escalation(record: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {
            "reason_code": EscalationReason.AMBIGUOUS_MATCH.value,
            "title": "Record match requires review",
            "record": record,
            "context": context,
        }
