from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable

import pandas as pd


class TransformationError(ValueError):
    pass


def trim(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def collapse_whitespace(value: Any) -> Any:
    return re.sub(r"\s+", " ", value.strip()) if isinstance(value, str) else value


def lowercase(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def uppercase(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value


def normalize_name(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return " ".join(value.strip().split())


def normalize_email(value: Any) -> Any:
    return lowercase(trim(value)) if isinstance(value, str) else value


def normalize_phone(value: Any) -> Any:
    if value is None:
        return value
    return re.sub(r"\D", "", str(value))


def normalize_integer(value: Any) -> int | None:
    if value in (None, ""):
        return value
    try:
        number = extract_number(value)
        return int(number) if number is not None else None
    except Exception as exc:
        raise TransformationError(f"Unable to normalize integer {value!r}: {exc}") from exc


def normalize_decimal(value: Any, decimals: int = 2) -> float | None:
    if value in (None, ""):
        return value
    try:
        number = extract_number(value)
        return round(float(number), int(decimals)) if number is not None else None
    except Exception as exc:
        raise TransformationError(f"Unable to normalize number {value!r}: {exc}") from exc


def parse_date(value: Any, input_format: str, output_format: str = "%Y-%m-%d") -> str:
    if value in (None, ""):
        return value
    try:
        return datetime.strptime(str(value).strip(), input_format).strftime(output_format)
    except ValueError as exc:
        raise TransformationError(str(exc)) from exc


def parse_date_auto(value: Any, output_format: str = "%Y-%m-%d") -> str:
    if value in (None, ""):
        return value
    text_value = str(value).strip()
    dayfirst = not bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", text_value))
    try:
        parsed = pd.to_datetime(text_value, errors="raise", dayfirst=dayfirst)
        return parsed.strftime(output_format)
    except Exception as exc:
        raise TransformationError(f"Unable to parse date {value!r}: {exc}") from exc


def parse_datetime_auto(value: Any, output_format: str = "%Y-%m-%dT%H:%M:%S") -> str:
    if value in (None, ""):
        return value
    try:
        parsed = pd.to_datetime(str(value).strip(), errors="raise")
        return parsed.to_pydatetime().strftime(output_format)
    except Exception as exc:
        raise TransformationError(f"Unable to parse datetime {value!r}: {exc}") from exc


def map_value(value: Any, mapping: dict[str, Any]) -> Any:
    key = str(value).strip() if value is not None else value
    if key in mapping:
        return mapping[key]
    normalized = {str(k).strip().casefold(): v for k, v in mapping.items()}
    if str(key).strip().casefold() in normalized:
        return normalized[str(key).strip().casefold()]
    raise TransformationError(f"No mapping configured for value: {value!r}")


def extract_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return value
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        raise TransformationError(f"No numeric value found in {value!r}")
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def split_value(value: Any, separator: str = ",") -> list[str]:
    if value in (None, ""):
        return []
    return [part.strip() for part in str(value).split(separator) if part.strip()]


def join_value(value: Any, separator: str = ", ") -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, (list, tuple)):
        raise TransformationError("join expects an array/list value")
    return separator.join(str(item).strip() for item in value if str(item).strip())


def normalize_boolean(value: Any, true_values: list[str] | None = None, false_values: list[str] | None = None) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    true_set = {item.casefold() for item in (true_values or ["true", "yes", "y", "1", "active", "enabled", "working"])}
    false_set = {item.casefold() for item in (false_values or ["false", "no", "n", "0", "inactive", "disabled", "left"])}
    if normalized in true_set:
        return True
    if normalized in false_set:
        return False
    raise TransformationError(f"Unable to normalize boolean value {value!r}")


TRANSFORMATIONS: dict[str, Callable[..., Any]] = {
    "trim": trim,
    "collapse_whitespace": collapse_whitespace,
    "lowercase": lowercase,
    "uppercase": uppercase,
    "normalize_name": normalize_name,
    "normalize_email": normalize_email,
    "normalize_phone": normalize_phone,
    "normalize_integer": normalize_integer,
    "normalize_decimal": normalize_decimal,
    "parse_date": parse_date,
    "parse_date_auto": parse_date_auto,
    "parse_datetime_auto": parse_datetime_auto,
    "map_value": map_value,
    "extract_number": extract_number,
    "split": split_value,
    "join": join_value,
    "normalize_boolean": normalize_boolean,
}


def apply_transformation(value: Any, operation: str, params: dict[str, Any] | None = None) -> Any:
    if operation not in TRANSFORMATIONS:
        raise TransformationError(f"Unsupported transformation: {operation}")

    normalized_params = dict(params or {})
    if operation in {"parse_date", "parse_date_auto", "parse_datetime_auto"}:
        if "outputFormat" in normalized_params and "output_format" not in normalized_params:
            normalized_params["output_format"] = normalized_params.pop("outputFormat")
        if operation == "parse_date" and "input_format" not in normalized_params:
            for alias in ("format", "date_format", "inputFormat"):
                if alias in normalized_params:
                    normalized_params["input_format"] = normalized_params.pop(alias)
                    break
    if operation == "normalize_decimal" and "precision" in normalized_params and "decimals" not in normalized_params:
        normalized_params["decimals"] = normalized_params.pop("precision")

    try:
        return TRANSFORMATIONS[operation](value, **normalized_params)
    except TypeError as exc:
        raise TransformationError(f"Invalid parameters for transformation '{operation}': {exc}") from exc
