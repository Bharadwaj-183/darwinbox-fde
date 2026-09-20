from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


class SourceLoader:
    def load(self, path: str) -> list[dict[str, Any]]:
        suffix = Path(path).suffix.lower()
        if suffix == ".csv":
            frame = pd.read_csv(path)
        elif suffix == ".xlsx":
            frame = pd.read_excel(path)
        else:
            raise ValueError(f"Unsupported source file type: {suffix}")

        frame = frame.where(pd.notnull(frame), None)
        records: list[dict[str, Any]] = []
        for index, row in frame.iterrows():
            record = {str(k): self._json_safe(v) for k, v in row.to_dict().items()}
            record["__source_row_number"] = int(index) + 2
            records.append(record)
        return records

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
