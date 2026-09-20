from __future__ import annotations

from typing import Iterable

from rapidfuzz.fuzz import ratio, token_set_ratio

from app.core.config import get_settings


class SemanticMatcher:
    """Semantic matcher with lexical-first resolution and lazy BGE use for ambiguous cases."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._model = None
        self._model_load_error: str | None = None
        self._embedding_cache: dict[str, list[float]] = {}

    def _load_model(self):
        if self._model is not None or self._model_load_error is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.settings.semantic_model)
        except Exception as exc:
            self._model_load_error = str(exc)
        return self._model

    def similarity(self, left: str, right: str) -> float:
        left_n = self._normalize(left)
        right_n = self._normalize(right)
        if left_n == right_n:
            return 1.0

        lexical = max(ratio(left_n, right_n), token_set_ratio(left_n, right_n)) / 100.0
        # Avoid loading/downloading the embedding model when lexical evidence is
        # already decisive. This keeps common alias matches fast and predictable.
        if lexical >= 0.92:
            return lexical

        model = self._load_model()
        if model is not None:
            try:
                left_vector = self._embed_one(left)
                right_vector = self._embed_one(right)
                return max(lexical, self._cosine(left_vector, right_vector))
            except Exception:
                pass
        return lexical

    def best_match(self, query: str, candidates: list[str], *, aliases: dict[str, list[str]] | None = None) -> list[tuple[str, float]]:
        aliases = aliases or {}
        query_n = self._normalize(query)
        lexical_scored: list[tuple[str, float]] = []
        exact_alias_found = False

        for candidate in candidates:
            normalized_candidate = self._normalize(candidate)
            direct = max(ratio(query_n, normalized_candidate), token_set_ratio(query_n, normalized_candidate)) / 100.0
            alias_scores = [
                max(
                    ratio(query_n, self._normalize(alias)),
                    token_set_ratio(query_n, self._normalize(alias)),
                ) / 100.0
                for alias in aliases.get(candidate, [])
            ]
            score = max([direct, *alias_scores] if alias_scores else [direct])
            if query_n == normalized_candidate or any(query_n == self._normalize(alias) for alias in aliases.get(candidate, [])):
                score = 1.0
                exact_alias_found = True
            lexical_scored.append((candidate, score))

        lexical_scored.sort(key=lambda item: item[1], reverse=True)
        if not lexical_scored:
            return []

        # Exact name/alias matches require no BGE inference.
        if exact_alias_found:
            return lexical_scored

        top_score = lexical_scored[0][1]
        second_score = lexical_scored[1][1] if len(lexical_scored) > 1 else 0.0
        if top_score >= self.settings.auto_mapping_threshold and top_score - second_score >= self.settings.auto_mapping_margin:
            return lexical_scored

        # Only now pay the cost of embedding-based similarity for ambiguous fields.
        model = self._load_model()
        if model is None:
            return lexical_scored

        query_vector = self._embed_one(query)
        semantic_scored: list[tuple[str, float]] = []
        for candidate, lexical_score in lexical_scored:
            candidate_vectors = [self._embed_one(candidate)] + [self._embed_one(alias) for alias in aliases.get(candidate, [])]
            semantic_score = max(self._cosine(query_vector, vector) for vector in candidate_vectors)
            semantic_scored.append((candidate, max(lexical_score, semantic_score)))
        return sorted(semantic_scored, key=lambda item: item[1], reverse=True)

    def rank(self, query: str, candidates: Iterable[str]) -> list[tuple[str, float]]:
        return self.best_match(query, list(candidates))

    def _embed_one(self, text: str) -> list[float]:
        key = str(text)
        cached = self._embedding_cache.get(key)
        if cached is not None:
            return cached
        model = self._model
        if model is None:
            raise RuntimeError("semantic model is not loaded")
        vector = list(next(iter(model.embed([text]))))
        self._embedding_cache[key] = vector
        return vector

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(str(text).replace("_", " ").replace("-", " ").split()).casefold()

    @staticmethod
    def _cosine(left, right) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = sum(a * a for a in left) ** 0.5
        right_norm = sum(b * b for b in right) ** 0.5
        if not left_norm or not right_norm:
            return 0.0
        return float(dot / (left_norm * right_norm))
