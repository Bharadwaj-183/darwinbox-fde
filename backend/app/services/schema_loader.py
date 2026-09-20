from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.schemas.target_schema import TargetSchema


SUPPORTED_TYPES = "string, integer, number, boolean, date, datetime, array, object"


class TargetSchemaLoader:
    def load_text(self, filename: str, content: str) -> TargetSchema:
        suffix = Path(filename).suffix.lower()
        try:
            if suffix == ".json":
                payload: Any = json.loads(content)
            elif suffix in {".yaml", ".yml"}:
                payload = yaml.safe_load(content)
            else:
                raise ValueError("Target schema must be JSON or YAML.")

            return TargetSchema.model_validate(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Target schema is not valid JSON. Check the file near line {exc.lineno}, column {exc.colno}."
            ) from exc
        except yaml.YAMLError as exc:
            raise ValueError("Target schema is not valid YAML. Check indentation and field names.") from exc
        except ValidationError as exc:
            raise ValueError(self._friendly_validation_error(exc)) from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Target schema could not be loaded. Check its structure and field definitions.") from exc

    @staticmethod
    def _friendly_validation_error(exc: ValidationError) -> str:
        errors = exc.errors()
        type_errors = [item for item in errors if item.get("loc", ()) and item["loc"][-1] == "data_type"]

        for item in type_errors:
            value = item.get("input")
            if value == "email":
                return (
                    "Target schema is invalid: email fields must use data_type 'string'. "
                    "Use the field name/description to identify the email and keep data_type as 'string'."
                )
            if value == "enum":
                return (
                    "Target schema is invalid: enum fields must use data_type 'string' "
                    "and define their allowed values with 'enum_values'."
                )

        # Keep unexpected validation failures concise instead of exposing Pydantic internals.
        first = errors[0] if errors else None
        if first:
            location = ".".join(str(part) for part in first.get("loc", ())) or "schema"
            return f"Target schema is invalid near '{location}'. Check the required schema structure and field definitions."
        return "Target schema is invalid. Check the required schema structure and field definitions."
