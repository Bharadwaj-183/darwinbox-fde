from __future__ import annotations

from typing import Any

from rapidfuzz.fuzz import ratio, token_set_ratio

from app.core.config import get_settings
from app.services.llm import LLMClient, LLMUnavailable
from app.services.semantic import SemanticMatcher
from app.schemas.target_schema import EntitySchema, TargetSchema


class EntityDetector:
    """Identify the target entity represented by a source file, deterministically first."""

    def __init__(self, llm: LLMClient, semantic: SemanticMatcher) -> None:
        self.llm = llm
        self.semantic = semantic
        self.settings = get_settings()

    async def detect(
        self,
        *,
        filename: str,
        columns: list[str],
        sample_values: dict[str, list[Any]],
        target_schema: TargetSchema,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if len(target_schema.entities) == 1:
            return target_schema.entities[0].entity_name, []

        candidates = self._rank_entities(filename, columns, target_schema.entities)
        top = candidates[0] if candidates else None
        second = candidates[1]["score"] if len(candidates) > 1 else 0.0

        if top and (top["score"] >= self.settings.auto_mapping_threshold or top["score"] - second >= self.settings.auto_mapping_margin):
            return top["entity"], []

        # BGE is only used when lightweight lexical/entity-profile evidence is genuinely ambiguous.
        query = f"{filename} | {' | '.join(columns)}"
        entity_descriptions = {
            entity.entity_name: self._entity_description(entity)
            for entity in target_schema.entities
        }
        ranked = self.semantic.rank(query, entity_descriptions.values())
        reverse = {text: name for name, text in entity_descriptions.items()}
        semantic_candidates = [
            {"entity": reverse[text], "score": round(score, 4)}
            for text, score in ranked[:5]
            if text in reverse
        ]
        if semantic_candidates:
            top_semantic = semantic_candidates[0]
            second_semantic = semantic_candidates[1]["score"] if len(semantic_candidates) > 1 else 0.0
            if top_semantic["score"] >= self.settings.auto_fallback_threshold and top_semantic["score"] - second_semantic >= 0.06:
                return top_semantic["entity"], []
        else:
            top_semantic = None

        candidates_for_llm = semantic_candidates or candidates
        if not self.llm.settings.openrouter_api_key:
            fallback = candidates_for_llm[0] if candidates_for_llm else None
            if fallback and fallback["score"] >= self.settings.auto_fallback_threshold:
                return fallback["entity"], []
            return None, [self._review(filename, candidates_for_llm, None)]

        try:
            result = await self.llm.structured_json(
                purpose="entity_detection",
                system=(
                    "You identify which target entity a messy source file represents. "
                    "Choose only from the supplied entities. Return JSON only with "
                    "entity_name and confidence. Do not request human review if a reasonable "
                    "best candidate exists."
                ),
                user={
                    "source_file": filename,
                    "columns": columns,
                    "sample_values": sample_values,
                    "target_entities": [entity.model_dump(mode="json") for entity in target_schema.entities],
                    "semantic_candidates": candidates_for_llm,
                },
            )
            entity_name = result.get("entity_name")
            allowed = {entity.entity_name for entity in target_schema.entities}
            if entity_name in allowed:
                return entity_name, []
        except LLMUnavailable as exc:
            fallback = candidates_for_llm[0] if candidates_for_llm else None
            if fallback and fallback["score"] >= self.settings.auto_fallback_threshold:
                return fallback["entity"], []
            return None, [self._review(filename, candidates_for_llm, str(exc))]

        fallback = candidates_for_llm[0] if candidates_for_llm else None
        if fallback and fallback["score"] >= self.settings.auto_fallback_threshold:
            return fallback["entity"], []
        return None, [self._review(filename, candidates_for_llm, "AI returned no valid entity choice")]

    @staticmethod
    def _entity_description(entity: EntitySchema) -> str:
        return " | ".join([
            entity.entity_name,
            entity.description,
            *[field.name for field in entity.fields],
            *[field.description for field in entity.fields if field.description],
            *[alias for field in entity.fields for alias in field.aliases],
        ])

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(str(value).replace("_", " ").replace("-", " ").split()).casefold()

    def _rank_entities(self, filename: str, columns: list[str], entities: list[EntitySchema]) -> list[dict[str, Any]]:
        filename_tokens = self._normalize(filename)
        scored: list[dict[str, Any]] = []
        for entity in entities:
            field_scores: list[float] = []
            for column in columns:
                query = self._normalize(column)
                scores = []
                for field in entity.fields:
                    target_texts = [field.name, *field.aliases]
                    scores.append(max(
                        max(ratio(query, self._normalize(text)), token_set_ratio(query, self._normalize(text))) / 100.0
                        for text in target_texts
                    ))
                field_scores.append(max(scores) if scores else 0.0)
            coverage = sum(score >= 0.72 for score in field_scores) / max(1, len(field_scores))
            filename_bonus = 0.06 if self._normalize(entity.entity_name) in filename_tokens else 0.0
            score = min(1.0, 0.55 * (sum(field_scores) / max(1, len(field_scores))) + 0.39 * coverage + filename_bonus)
            scored.append({"entity": entity.entity_name, "score": round(score, 4)})
        return sorted(scored, key=lambda item: item["score"], reverse=True)

    @staticmethod
    def _review(filename: str, candidates: list[dict[str, Any]], error: str | None) -> dict[str, Any]:
        return {
            "reason_code": "llm_unavailable" if error else "ambiguous_mapping",
            "title": f"Choose the target entity for {filename}",
            "context": {
                "filename": filename,
                "candidates": candidates,
                "error": error,
                "reason": "No target entity was strong enough for autonomous resolution.",
            },
            "suggested_resolution": {"entity_name": candidates[0]["entity"] if candidates else ""},
        }
