from __future__ import annotations

from typing import Any

from app.services.mapping import MappingEngine
from app.tools.transformations import TransformationError, apply_transformation


class TransformationEngine:
    def execute(self, value: Any, transformations: list[dict[str, Any]]) -> tuple[Any, list[str], str | None]:
        current = value
        applied: list[str] = []
        for step in transformations:
            operation = step.get("operation", "")
            if step.get("proposed_new_transformation"):
                return current, applied, "new_transformation"
            if operation not in MappingEngine.SUPPORTED_OPERATIONS:
                return current, applied, "unsupported_operation"
            try:
                current = apply_transformation(current, operation, step.get("params") or {})
                applied.append(operation)
            except TransformationError as exc:
                message = str(exc)
                if message.startswith("No mapping configured for value:"):
                    return current, applied, "unmapped_value"
                return current, applied, message
        return current, applied, None
