from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.target_schema import EntitySchema, TargetField


class ValidationEngine:
    def validate_record(self, record: dict[str, Any], entity: EntitySchema) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for field in entity.fields:
            value = record.get(field.name)
            if field.required and value in (None, ""):
                errors.append({"field": field.name, "code": "required", "message": "Required field is missing"})
                continue
            if value in (None, ""):
                continue
            if field.enum_values and str(value) not in set(field.enum_values):
                errors.append({"field": field.name, "code": "enum", "message": f"Value must be one of {field.enum_values}"})
                continue
            if not self._type_ok(value, field):
                errors.append({"field": field.name, "code": "type", "message": f"Value does not match type {field.data_type}"})
        return errors

    @staticmethod
    def _type_ok(value: Any, field: TargetField) -> bool:
        kind = field.data_type
        if kind == "string": return isinstance(value, str)
        if kind == "integer": return isinstance(value, int) and not isinstance(value, bool)
        if kind == "number": return isinstance(value, (int, float)) and not isinstance(value, bool)
        if kind == "boolean": return isinstance(value, bool)
        if kind == "array": return isinstance(value, list)
        if kind == "object": return isinstance(value, dict)
        if kind == "date":
            try: datetime.strptime(str(value), "%Y-%m-%d")
            except ValueError: return False
            return True
        if kind == "datetime":
            try: datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError: return False
            return True
        return True
