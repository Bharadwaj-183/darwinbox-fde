from __future__ import annotations

from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio

from app.core.config import get_settings
from app.schemas.agent import TransformationStep
from app.schemas.target_schema import TargetField
from app.services.llm import LLMClient, LLMUnavailable
from app.services.semantic import SemanticMatcher


class TransformationPlanner:
    """Build the safest executable transformation plan before record execution."""

    ENUM_SYSTEM_PROMPT = """
You are the value-normalization component of a customer data migration agent.
Map every unresolved source value to exactly one allowed target enum value when the
meaning is reasonably clear. Choose only from the supplied allowed values.
If a value cannot be safely normalized even after considering context, put it in
unresolved_values. Return JSON only:
{
  "mapping": {"source value": "TARGET_VALUE"},
  "unresolved_values": ["..."],
  "reason": "short explanation"
}
""".strip()

    def __init__(self, llm: LLMClient, semantic: SemanticMatcher) -> None:
        self.llm = llm
        self.semantic = semantic
        self.settings = get_settings()

    async def plan(
        self,
        *,
        source_field: str,
        sample_values: list[Any],
        target_field: TargetField,
        proposed: list[TransformationStep],
    ) -> tuple[list[TransformationStep], dict[str, Any] | None]:
        steps = list(proposed)
        values = self._unique_values(sample_values)

        # These are known safe transformations; no LLM call is necessary.
        if target_field.data_type in {"date", "datetime"} and not self._has_operation(steps, {"parse_date", "parse_date_auto"}):
            steps.insert(0, TransformationStep(operation="parse_date_auto"))

        if target_field.data_type in {"integer", "number"} and not self._has_operation(steps, {"normalize_integer", "normalize_decimal", "extract_number"}):
            needs_numeric_cleanup = any(
                isinstance(value, str)
                and any(char.isdigit() for char in value)
                and not value.strip().replace(".", "", 1).replace("-", "", 1).isdigit()
                for value in values
            )
            if needs_numeric_cleanup:
                steps.insert(0, TransformationStep(operation="normalize_integer" if target_field.data_type == "integer" else "normalize_decimal"))

        if target_field.data_type == "boolean" and not self._has_operation(steps, {"normalize_boolean", "map_value"}):
            steps.insert(0, TransformationStep(operation="normalize_boolean"))

        if target_field.data_type == "array" and not self._has_operation(steps, {"split"}):
            if any(isinstance(value, str) and ("," in value or ";" in value or "|" in value) for value in values):
                separator = ";" if any(isinstance(value, str) and ";" in value for value in values) else ","
                steps.insert(0, TransformationStep(operation="split", params={"separator": separator}))

        if target_field.data_type == "string" and "email" in target_field.name and not self._has_operation(steps, {"normalize_email"}):
            steps.insert(0, TransformationStep(operation="normalize_email"))
        elif target_field.data_type == "string" and "phone" in target_field.name and not self._has_operation(steps, {"normalize_phone"}):
            steps.insert(0, TransformationStep(operation="normalize_phone"))

        # Unknown/custom transformations are never silently invented or executed.
        for step in steps:
            if step.proposed_new_transformation:
                return steps, self._new_transformation_review(source_field, target_field, step, values)

        if target_field.enum_values:
            return await self._complete_enum_plan(values, target_field, steps, source_field)

        if not steps:
            steps = [TransformationStep(operation="trim")]
        return steps, None

    async def _complete_enum_plan(
        self,
        values: list[Any],
        target_field: TargetField,
        steps: list[TransformationStep],
        source_field: str,
    ) -> tuple[list[TransformationStep], dict[str, Any] | None]:
        allowed = [str(value) for value in target_field.enum_values or []]
        existing_mapping: dict[str, Any] = {}
        for step in steps:
            if step.operation == "map_value":
                existing_mapping.update((step.params or {}).get("mapping") or {})

        completed: dict[str, Any] = {}
        unresolved: list[str] = []
        for value in values:
            key = str(value).strip() if value is not None else ""
            if not key:
                continue
            if key in existing_mapping and str(existing_mapping[key]) in allowed:
                completed[key] = existing_mapping[key]
                continue

            exact = next((candidate for candidate in allowed if candidate.casefold() == key.casefold()), None)
            if exact is not None:
                completed[key] = exact
                continue

            heuristic = self._generic_enum_heuristic(key, allowed)
            if heuristic is not None:
                completed[key] = heuristic
                continue

            ranked = self._lexical_rank(key, allowed)
            if ranked:
                # Even a close lexical match is usable as autonomous fallback if
                # the stronger LLM reasoning path is unavailable. Avoid loading the
                # embedding model for every enum value.
                top_value, top_score = ranked[0]
                if top_score >= self.settings.auto_fallback_threshold:
                    completed[key] = top_value
                    continue
            unresolved.append(key)

        if unresolved and self.llm.settings.openrouter_api_key:
            try:
                result = await self.llm.structured_json(
                    purpose="enum_normalization",
                    system=self.ENUM_SYSTEM_PROMPT,
                    user={
                        "source_field": source_field,
                        "source_values": unresolved,
                        "target_field": target_field.model_dump(mode="json"),
                        "allowed_values": allowed,
                    },
                )
                llm_mapping = result.get("mapping") or {}
                for source_value in unresolved:
                    candidate = llm_mapping.get(source_value)
                    if candidate is not None and str(candidate) in allowed:
                        completed[source_value] = candidate
                unresolved = [value for value in unresolved if value not in completed]
            except LLMUnavailable:
                # Re-run semantic fallback. This intentionally prefers progress over
                # a human review when a target enum candidate is still plausible.
                for source_value in list(unresolved):
                    ranked = self._lexical_rank(source_value, allowed)
                    if ranked and ranked[0][1] >= self.settings.auto_fallback_threshold:
                        completed[source_value] = ranked[0][0]
                        unresolved.remove(source_value)

        non_enum_steps = [step for step in steps if step.operation != "map_value"]
        if completed:
            non_enum_steps.append(TransformationStep(operation="map_value", params={"mapping": completed}))

        if unresolved:
            return non_enum_steps, {
                "reason_code": "unmapped_value",
                "title": f"Value normalization needs consultant review for {source_field}",
                "context": {
                    "source_field": source_field,
                    "target_field": target_field.name,
                    "allowed_values": allowed,
                    "unresolved_values": unresolved,
                    "error": "unmapped_value",
                    "reason": "The source value could not be safely normalized even after semantic and AI-assisted attempts.",
                },
                "suggested_resolution": {
                    "transformations": [
                        {
                            "operation": "map_value",
                            "params": {"mapping": {value: "" for value in unresolved}},
                        }
                    ]
                },
            }

        return non_enum_steps or [TransformationStep(operation="trim")], None

    @staticmethod
    def _new_transformation_review(source_field: str, target_field: TargetField, step: TransformationStep, values: list[Any]) -> dict[str, Any]:
        return {
            "reason_code": "new_transformation",
            "title": f"New transformation proposed for {source_field}",
            "context": {
                "source_field": source_field,
                "target_field": target_field.name,
                "record_value": values[0] if values else None,
                "proposed_new_transformation": step.proposed_new_transformation,
                "reason": "This transformation is outside the supported library and requires consultant approval before execution.",
            },
            "suggested_resolution": {
                "proposed_new_transformation": step.proposed_new_transformation,
                "approved_by_consultant": True,
            },
        }

    @staticmethod
    def _lexical_rank(value: str, allowed: list[str]) -> list[tuple[str, float]]:
        query = " ".join(str(value).replace("_", " ").replace("-", " ").split()).casefold()
        ranked = []
        for candidate in allowed:
            normalized = " ".join(str(candidate).replace("_", " ").replace("-", " ").split()).casefold()
            score = max(ratio(query, normalized), token_set_ratio(query, normalized)) / 100.0
            ranked.append((candidate, score))
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    @staticmethod
    def _generic_enum_heuristic(value: str, allowed: list[str]) -> str | None:
        normalized = value.strip().casefold()
        compact = " ".join(normalized.replace("-", " ").replace("_", " ").split())
        lowered_allowed = {item.casefold(): item for item in allowed}
        active = lowered_allowed.get("active")
        inactive = lowered_allowed.get("inactive")
        if active and any(token in compact.split() for token in {"active", "working", "employed", "current", "currently"}):
            return active
        if inactive and any(token in compact.split() for token in {"inactive", "left", "terminated", "former", "exited", "resigned", "retired"}):
            return inactive
        return None

    @staticmethod
    def _has_operation(steps: list[TransformationStep], operations: set[str]) -> bool:
        return any(step.operation in operations for step in steps)

    @staticmethod
    def _unique_values(values: list[Any]) -> list[Any]:
        seen: set[str] = set()
        result: list[Any] = []
        for value in values:
            if value in (None, ""):
                continue
            key = str(value)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result
