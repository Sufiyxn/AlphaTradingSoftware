import csv
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

from backend.database import JournalRepository


class LogExportService:
    KINDS = ("trades", "orders", "signals", "risk_decisions", "ai_analyses",
             "position_events", "system_events", "errors")

    def __init__(self, repository: JournalRepository):
        self.repository = repository

    def export(self, kind: str = "all", *, format: str = "json", start: date | None = None,
               end: date | None = None, symbol: str | None = None) -> str:
        selected = self.KINDS if kind in ("all", "logs") else (kind,)
        rows = []
        for category in selected:
            for record in self.repository.rows(category, start=start, end=end, symbol=symbol):
                rows.append({"record_type": category, **record})
        rows.sort(key=lambda row: str(row.get("timestamp") or row.get("entry_timestamp") or ""))
        if format.lower() == "json":
            return json.dumps(rows, default=_json_default, sort_keys=True, indent=2)
        if format.lower() != "csv":
            raise ValueError("export format must be 'json' or 'csv'")
        columns = sorted({key for row in rows for key in row}) or ["record_type"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, default=_json_default, sort_keys=True)
                             if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})
        return output.getvalue()

    def write(self, path: str | Path, kind: str = "all", *, format: str = "json",
              start: date | None = None, end: date | None = None, symbol: str | None = None) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.export(kind, format=format, start=start, end=end, symbol=symbol), encoding="utf-8")
        return destination


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"cannot export {type(value).__name__}")
