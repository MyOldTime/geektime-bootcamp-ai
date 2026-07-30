"""Serialize query results into downloadable CSV and JSON files."""

import csv
import io
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import quote

from fastapi import Response

from app.models.schemas import ExportFormat

_UNSAFE_FILE_NAME = re.compile(r"[^\w.-]+", flags=re.UNICODE)


def _json_default(value: object) -> str:
    """Convert common database values to a stable JSON representation."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _csv_value(value: object) -> object:
    """Normalize nested and database-specific values for a CSV cell."""
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _safe_file_stem(database_name: str, requested_name: str | None) -> str:
    """Build a traversal-safe, human-readable file stem."""
    if requested_name:
        requested_name = re.sub(r"\.(csv|json)$", "", requested_name, flags=re.IGNORECASE)
        candidate = requested_name
    else:
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        candidate = f"{database_name}_query_{timestamp}"

    sanitized = _UNSAFE_FILE_NAME.sub("_", candidate).strip("._")
    return sanitized[:100] or "query_export"


def _content_disposition(file_name: str) -> str:
    """Create a download header with ASCII and UTF-8 filename variants."""
    stem, extension = file_name.rsplit(".", maxsplit=1)
    ascii_stem = stem.encode("ascii", errors="ignore").decode("ascii")
    ascii_stem = _UNSAFE_FILE_NAME.sub("_", ascii_stem).strip("._")
    ascii_name = f"{ascii_stem or 'query_export'}.{extension}"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(file_name, safe='')}"
    )


def create_export_response(
    *,
    database_name: str,
    export_format: ExportFormat,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, object]],
    requested_file_name: str | None = None,
    execution_time_ms: int | None = None,
) -> Response:
    """Serialize rows and return a browser-downloadable response."""
    extension = export_format.value
    file_name = f"{_safe_file_stem(database_name, requested_file_name)}.{extension}"

    headers = {
        "Content-Disposition": _content_disposition(file_name),
        "X-Export-Format": extension,
        "X-Row-Count": str(len(rows)),
    }
    if execution_time_ms is not None:
        headers["X-Execution-Time-Ms"] = str(execution_time_ms)

    if export_format == ExportFormat.CSV:
        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_value(row.get(column)) for column in columns])
        return Response(
            content=output.getvalue().encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    content = json.dumps(
        rows,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    ).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )
