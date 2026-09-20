from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.constants import EscalationReason
from app.schemas.agent import FieldMappingProposal, TransformationStep
from app.schemas.target_schema import EntitySchema
from app.services.llm import LLMClient, LLMUnavailable
from app.services.semantic import SemanticMatcher


class MappingEngine:
    SUPPORTED_OPERATIONS = {
        "trim", "collapse_whitespace", "lowercase", "uppercase", "normalize_name", "normalize_email", "normalize_phone",
        "normalize_integer", "normalize_decimal", "parse_date", "parse_date_auto", "parse_datetime_auto", "map_value", "extract_number",
        "split", "join", "normalize_boolean",
    }

    SYSTEM_PROMPT = """
You are the semantic planning component of a customer data migration agent.
Only analyze source fields supplied in this request because their semantic match is
not strong enough for deterministic auto-resolution.
Given source columns, representative values, inferred types, semantic candidates,
and a user-provided target schema:
1. Choose the best target field only from the supplied candidates.
2. Inspect representative values, not just column names.
3. For date/datetime targets, include parse_date or parse_date_auto as needed.
4. For enum targets, include map_value covering representative source values.
5. Prefer supported transformation operations.
6. If no candidate is defensible, return target_field=null and needs_human=true.
7. Never invent a target field outside the supplied candidates.
8. Do not request human review merely because the top two candidates are close if
   one candidate is still a reasonable best fit; choose the best supported option.
Return JSON only:
{
  "field_mappings": [
    {
      "source_field": "...",
      "target_field": "...",
      "confidence": 0.0,
      "reason": "...",
      "transformations": [{"operation": "...", "params": {}}],
      "needs_human": false,
      "alternatives": [["target_field", 0.0]]
    }
  ],
  "escalations": []
}
""".strip()

    def __init__(self, llm: LLMClient, semantic: SemanticMatcher) -> None:
        self.llm = llm
        self.semantic = semantic
        self.settings = get_settings()

    async def map_fields(
        self,
        *,
        columns: list[str],
        sample_values: dict[str, list[Any]],
        inferred_types: dict[str, str],
        entity: EntitySchema,
        human_overrides: dict[str, dict[str, Any]] | None = None,
        transformation_overrides: dict[str, list[dict[str, Any]]] | None = None,
    ) -> tuple[list[FieldMappingProposal], list[dict[str, Any]]]:
        human_overrides = human_overrides or {}
        transformation_overrides = transformation_overrides or {}

        target_names = [field.name for field in entity.fields]
        aliases = {field.name: list(field.aliases) for field in entity.fields}
        target_fields = {field.name: field for field in entity.fields}
        candidates: dict[str, list[dict[str, Any]]] = {}
        for source_field in columns:
            ranked = self.semantic.best_match(source_field, target_names, aliases=aliases)
            adjusted: list[dict[str, Any]] = []
            for target, score in ranked[:5]:
                adjusted.append({
                    "target_field": target,
                    "score": round(self._adjust_score(source_field, target_fields[target], float(score), entity), 4),
                    "raw_score": round(float(score), 4),
                    "match": self._match_reason(source_field, target, aliases[target]),
                })
            adjusted.sort(key=lambda item: item["score"], reverse=True)
            candidates[source_field] = adjusted

        proposals: list[FieldMappingProposal] = []
        escalations: list[dict[str, Any]] = []
        llm_candidates: list[str] = []

        for column in columns:
            override = human_overrides.get(column)
            if override and override.get("target_field"):
                target_name = str(override["target_field"])
                proposals.append(
                    FieldMappingProposal(
                        source_field=column,
                        target_field=target_name,
                        confidence=1.0,
                        reason="Consultant-approved mapping from prior review.",
                        transformations=[
                            TransformationStep.model_validate(item)
                            for item in (override.get("transformations") or transformation_overrides.get(column, []))
                        ],
                    )
                )
                continue

            choices = candidates.get(column, [])
            if not choices:
                continue
            top = choices[0]
            second_score = float(choices[1]["score"]) if len(choices) > 1 else 0.0
            margin = float(top["score"]) - second_score
            source_type = inferred_types.get(column, "string")

            # Strong aliases/exact names and clearly separated semantic matches are
            # resolved without an LLM.
            if self._is_confident_auto(top, margin, source_type, target_fields[top["target_field"]]):
                proposals.append(self._proposal_from_candidate(
                    column, top, choices, target_fields[top["target_field"]],
                    transformation_overrides.get(column),
                    reason="Resolved automatically from field-name/alias/type evidence.",
                ))
                continue

            # Ambiguous/uncertain mappings are sent to the LLM in a single batch.
            # We deliberately keep the floor permissive: if there is a plausible
            # candidate, let the model reason first; if the model is unavailable,
            # we will still fall back to that best candidate instead of creating a
            # needless consultant task.
            if self._llm_candidate_allowed(top, margin, source_type, target_fields[top["target_field"]]):
                llm_candidates.append(column)
                continue

            # Very weak/incompatible candidates are treated as extra source fields.
            continue

        if llm_candidates and self.llm_available():
            try:
                result = await self.llm.structured_json(
                    system=self.SYSTEM_PROMPT,
                    purpose="field_mapping",
                    user={
                        "entity": entity.model_dump(mode="json"),
                        "source": {
                            "columns": llm_candidates,
                            "inferred_types": {key: inferred_types.get(key, "string") for key in llm_candidates},
                            "sample_values": {key: sample_values.get(key, [])[:8] for key in llm_candidates},
                        },
                        "semantic_candidates": {key: candidates[key] for key in llm_candidates},
                        "supported_transformations": sorted(self.SUPPORTED_OPERATIONS),
                    },
                )
                llm_mappings = {
                    str(item.get("source_field")): item
                    for item in (result.get("field_mappings") or [])
                    if item.get("source_field") in llm_candidates
                }
                for source_field in llm_candidates:
                    item = llm_mappings.get(source_field)
                    choices = candidates[source_field]
                    top = choices[0]
                    source_type = inferred_types.get(source_field, "string")
                    target_name = item.get("target_field") if item else None
                    candidate_names = {choice["target_field"] for choice in choices}
                    # The model is allowed to refine the ambiguous choice, but not
                    # invent a target field. If it declines and the semantic top match
                    # is plausible, use the top candidate rather than blocking.
                    if target_name not in candidate_names:
                        target_name = None
                    if target_name is None or (item and item.get("needs_human")):
                        # Even if the model requests human review, keep the pipeline
                        # autonomous whenever a semantically plausible candidate is
                        # still available. This is the requested fallback behavior.
                        if self._safe_fallback_candidate(top, source_type, target_fields[top["target_field"]]):
                            target_name = top["target_field"]
                        else:
                            target_name = None
                    if target_name is None:
                        escalations.append(self._mapping_review(source_field, choices, sample_values.get(source_field, [])))
                        continue
                    target_field = target_fields[target_name]
                    if item and item.get("target_field") == target_name and not item.get("needs_human"):
                        proposal = FieldMappingProposal.model_validate({
                            "source_field": source_field,
                            "target_field": target_name,
                            "confidence": float(item.get("confidence") or top["score"]),
                            "reason": item.get("reason") or "AI-assisted resolution of an ambiguous semantic match.",
                            "transformations": item.get("transformations") or [],
                            "needs_human": False,
                            "alternatives": item.get("alternatives") or [(c["target_field"], c["score"]) for c in choices[1:3]],
                        })
                        proposals.append(proposal)
                    else:
                        proposals.append(self._proposal_from_candidate(
                            source_field, top if target_name == top["target_field"] else next(c for c in choices if c["target_field"] == target_name),
                            choices, target_field, transformation_overrides.get(source_field),
                            reason="AI was consulted for an ambiguous mapping and selected the best supported target.",
                        ))
            except LLMUnavailable as exc:
                # The user explicitly prefers autonomous progress. When a plausible
                # candidate exists, fall back to the best semantic candidate rather
                # than creating a human task just because the model timed out.
                for source_field in llm_candidates:
                    choices = candidates[source_field]
                    top = choices[0]
                    if self._safe_fallback_candidate(top, source_type, target_fields[top["target_field"]]):
                        target_field = target_fields[top["target_field"]]
                        proposals.append(self._proposal_from_candidate(
                            source_field, top, choices, target_field,
                            transformation_overrides.get(source_field),
                            reason=f"AI reasoning unavailable after retry window; automatically used the best semantic match. ({exc})",
                        ))
                    else:
                        escalations.append(self._mapping_review(source_field, choices, sample_values.get(source_field, []), llm_error=str(exc)))
        elif llm_candidates:
            # No key configured: still prefer autonomous progress when a plausible
            # candidate exists.
            for source_field in llm_candidates:
                choices = candidates[source_field]
                top = choices[0]
                if self._safe_fallback_candidate(top, inferred_types.get(source_field, "string"), target_fields[top["target_field"]]):
                    proposals.append(self._proposal_from_candidate(
                        source_field, top, choices, target_fields[top["target_field"]],
                        transformation_overrides.get(source_field),
                        reason="AI reasoning unavailable; automatically used the best semantic match.",
                    ))
                else:
                    escalations.append(self._mapping_review(source_field, choices, sample_values.get(source_field, [])))

        return proposals, escalations

    def llm_available(self) -> bool:
        return bool(self.llm.settings.openrouter_api_key)

    @staticmethod
    def _llm_candidate_allowed(top: dict[str, Any], margin: float, source_type: str, target_field) -> bool:
        score = float(top.get("score", 0.0))
        # Let the LLM arbitrate close calls and otherwise-uncertain but still
        # plausible mappings. A hard domain/type mismatch is excluded.
        if score < 0.40:
            return False
        if not MappingEngine._types_compatible(source_type, target_field.data_type):
            return False
        return margin < 0.15 or score < 0.86

    @staticmethod
    def _safe_fallback_candidate(top: dict[str, Any], source_type: str, target_field) -> bool:
        score = float(top.get("score", 0.0))
        return score >= 0.40 and MappingEngine._types_compatible(source_type, target_field.data_type)

    def _adjust_score(self, source_field: str, target_field, score: float, entity: EntitySchema) -> float:
        """Apply lightweight domain/type guards on top of semantic similarity.

        Similar wording alone can produce dangerous matches such as `Employee Name`
        -> `employee_id`. These guards keep obvious mismatches out of autonomous
        fallback while still allowing the LLM to arbitrate genuinely ambiguous fields.
        """
        source = self._tokens(source_field)
        target = self._tokens(target_field.name, target_field.description, *target_field.aliases)
        adjusted = score

        groups = {
            "identifier": {"id", "identifier", "number", "no", "code", "key"},
            "name": {"name", "given", "first", "last", "surname", "family", "fname"},
            "email": {"email", "mail"},
            "phone": {"phone", "mobile", "telephone", "tel", "contact"},
            "date": {"date", "dob", "birth", "birthday", "joining", "doj", "hire", "start", "joined"},
            "status": {"status", "state", "active", "inactive"},
            "type": {"type", "kind", "category"},
            "organization": {"department", "dept", "team", "org", "organisation", "division", "unit", "business"},
        }

        source_groups = {name for name, words in groups.items() if source & words}
        target_groups = {name for name, words in groups.items() if target & words}

        strong_domains = {"identifier", "name", "email", "phone", "date", "status", "type", "organization"}
        source_strong = source_groups & strong_domains
        target_strong = target_groups & strong_domains
        if source_strong and target_strong and not (source_strong & target_strong):
            # Keep the candidate visible for LLM diagnostics, but make it too weak
            # for autonomous fallback when its semantic domain clearly disagrees.
            adjusted *= 0.15

        # Compound/full-name fields should not silently overwrite a first/last-name
        # field when the target schema contains multiple name components.
        if "name" in source_groups and ("first_name" in target_field.name or "last_name" in target_field.name) and {
            "first", "last", "given", "surname", "family", "fname"
        }.isdisjoint(source):
            name_targets = [
                field.name for field in entity.fields
                if any(token in self._tokens(field.name, field.description, *field.aliases) for token in {"first", "last", "given", "surname", "family"})
            ]
            if len(name_targets) >= 2:
                adjusted *= 0.55

        # Strong domain mismatches are penalized. This is intentionally a guard, not
        # a hard-coded customer mapping.
        incompatible_pairs = [
            ("name", "identifier"),
            ("identifier", "name"),
            ("email", "phone"),
            ("phone", "email"),
            ("date", "identifier"),
            ("date", "status"),
            ("type", "status"),
        ]
        if any(source_group == left and target_group == right for left, right in incompatible_pairs for source_group in source_groups for target_group in target_groups):
            adjusted *= 0.42

        # Strong aligned domains get a modest boost so obvious semantics beat noisy
        # lexical overlap (e.g. Employee Name vs Employee ID).
        aligned_pairs = {
            ("identifier", "identifier"),
            ("name", "name"),
            ("email", "email"),
            ("phone", "phone"),
            ("date", "date"),
            ("organization", "organization"),
            ("status", "status"),
        }
        if any((source_group, target_group) in aligned_pairs for source_group in source_groups for target_group in target_groups):
            adjusted = min(1.0, adjusted + 0.08)

        return max(0.0, min(1.0, adjusted))

    @staticmethod
    def _tokens(*values: Any) -> set[str]:
        tokens: set[str] = set()
        for value in values:
            normalized = str(value).replace("_", " ").replace("-", " ").replace("/", " ").casefold()
            tokens.update(token for token in normalized.split() if token)
        return tokens

    def _is_confident_auto(self, top: dict[str, Any], margin: float, source_type: str, target_field) -> bool:
        if top["match"] in {"exact field-name match", "exact alias match"}:
            return True
        return (
            float(top["score"]) >= self.settings.auto_mapping_threshold
            and margin >= self.settings.auto_mapping_margin
            and self._types_compatible(source_type, target_field.data_type)
        )

    @staticmethod
    def _types_compatible(source_type: str, target_type: str) -> bool:
        if source_type in {"unknown", "string"}:
            return True
        if source_type == target_type:
            return True
        if source_type in {"integer", "number"} and target_type in {"integer", "number"}:
            return True
        if source_type == "date" and target_type in {"date", "datetime"}:
            return True
        return False

    def _proposal_from_candidate(
        self,
        source_field: str,
        candidate: dict[str, Any],
        choices: list[dict[str, Any]],
        target_field,
        override_transformations: list[dict[str, Any]] | None,
        *,
        reason: str,
    ) -> FieldMappingProposal:
        transformations = override_transformations or self._infer_basic_transformations(source_field, target_field)
        return FieldMappingProposal(
            source_field=source_field,
            target_field=candidate["target_field"],
            confidence=float(candidate["score"]),
            reason=reason,
            transformations=[TransformationStep.model_validate(item) for item in transformations],
            alternatives=[(item["target_field"], item["score"]) for item in choices[1:3]],
        )

    @staticmethod
    def _mapping_review(source_field: str, choices: list[dict[str, Any]], sample_values: list[Any], llm_error: str | None = None) -> dict[str, Any]:
        return {
            "reason_code": EscalationReason.AMBIGUOUS_MAPPING.value if not llm_error else EscalationReason.LLM_UNAVAILABLE.value,
            "title": f"No safe automatic mapping for {source_field}",
            "context": {
                "source_field": source_field,
                "candidates": choices[:3],
                "sample_values": sample_values[:6],
                "llm_error": llm_error,
                "reason": "No candidate is strong enough for safe autonomous fallback.",
            },
            "suggested_resolution": {"target_field": choices[0]["target_field"] if choices else ""},
        }

    @staticmethod
    def _infer_basic_transformations(source_field: str, target_field):
        ops: list[TransformationStep] = []
        if target_field.enum_values:
            ops.append(TransformationStep(operation="map_value", params={"mapping": {}}))
        elif target_field.data_type in {"date", "datetime"}:
            ops.append(TransformationStep(operation="parse_date_auto"))
        elif target_field.data_type == "string" and "email" in target_field.name:
            ops.append(TransformationStep(operation="normalize_email"))
        elif target_field.data_type == "string" and "phone" in target_field.name:
            ops.append(TransformationStep(operation="normalize_phone"))
        else:
            ops.append(TransformationStep(operation="trim"))
        return ops

    @staticmethod
    def _match_reason(source_field: str, target_field: str, aliases: list[str]) -> str:
        normalized_source = source_field.replace("_", " ").replace("-", " ").strip().casefold()
        if normalized_source == target_field.replace("_", " ").casefold():
            return "exact field-name match"
        if any(normalized_source == alias.replace("_", " ").replace("-", " ").strip().casefold() for alias in aliases):
            return "exact alias match"
        return "semantic similarity"
