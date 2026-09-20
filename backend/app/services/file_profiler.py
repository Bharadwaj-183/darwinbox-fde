from pathlib import Path
from typing import Any

import pandas as pd


class FileProfileError(ValueError):
    pass


class FileProfiler:
    """Lightweight source profiling; no customer-specific mappings are hardcoded."""

    def profile(self, path: str) -> dict[str, Any]:
        file_path = Path(path)
        suffix = file_path.suffix.lower()
        try:
            if suffix == ".csv":
                frame = pd.read_csv(file_path, nrows=100)
                row_count = int(sum(1 for _ in file_path.open("r", encoding="utf-8", errors="ignore"))) - 1
            elif suffix in {".xlsx", ".xls"}:
                frame = pd.read_excel(file_path, nrows=100)
                row_count = int(pd.read_excel(file_path).shape[0])
            else:
                raise FileProfileError(f"Unsupported file type: {suffix or 'unknown'}")
        except Exception as exc:
            raise FileProfileError(f"Unable to profile {file_path.name}: {exc}") from exc

        samples = {
            column: [self._json_safe(value) for value in frame[column].dropna().head(5).tolist()]
            for column in frame.columns
        }
        inferred_types = {column: str(frame[column].dtype) for column in frame.columns}
        return {
            "filename": file_path.name,
            "row_count": max(row_count, 0),
            "columns": [str(column) for column in frame.columns],
            "inferred_types": inferred_types,
            "sample_values": samples,
        }

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
