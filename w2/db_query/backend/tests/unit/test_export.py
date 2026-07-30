"""Tests for CSV and JSON query result exports."""

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models.database import ConnectionStatus, DatabaseConnection
from app.models.metadata import DatabaseMetadata
from app.models.schemas import (
    ExportAssistantAction,
    ExportAssistantResponse,
    ExportFormat,
    QueryColumn,
    QueryResult,
)
from app.services.export import create_export_response


@pytest.fixture
def export_session():
    """Create an isolated SQLite session for export API tests."""
    engine = create_engine(
        "sqlite:///file:export_tests?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    connection = DatabaseConnection(
        name="analytics",
        url="postgresql://user:pass@localhost/analytics",
        status=ConnectionStatus.ACTIVE,
    )
    session.add(connection)
    session.commit()
    session.add(
        DatabaseMetadata(
            database_name="analytics",
            metadata_json=json.dumps(
                {
                    "tables": [
                        {
                            "name": "users",
                            "schemaName": "public",
                            "type": "table",
                            "columns": [{"name": "id", "dataType": "integer"}],
                        }
                    ],
                    "views": [],
                }
            ),
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            table_count=1,
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def export_client(export_session):
    """Create a test client wired to the isolated export database."""

    def get_export_session():
        return export_session

    app.dependency_overrides[get_session] = get_export_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_create_csv_export_response_handles_unicode_and_escaping():
    """CSV output includes a BOM, headers, Unicode, and escaped values."""
    response = create_export_response(
        database_name="analytics",
        export_format=ExportFormat.CSV,
        columns=["name", "notes", "profile"],
        rows=[
            {
                "name": "张三",
                "notes": 'contains, comma and "quotes"',
                "profile": {"active": True},
            }
        ],
        requested_file_name="用户报表",
    )

    text = response.body.decode("utf-8")
    assert text.startswith("\ufeff")
    parsed_rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert parsed_rows[0] == ["name", "notes", "profile"]
    assert parsed_rows[1][0] == "张三"
    assert parsed_rows[1][1] == 'contains, comma and "quotes"'
    assert parsed_rows[1][2] == '{"active":true}'
    assert response.headers["x-row-count"] == "1"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_create_json_export_response_serializes_database_values():
    """JSON output preserves Unicode and normalizes non-native values."""
    response = create_export_response(
        database_name="analytics",
        export_format=ExportFormat.JSON,
        columns=["day", "amount"],
        rows=[{"day": date(2026, 7, 30), "amount": Decimal("12.50")}],
    )

    payload = json.loads(response.body)
    assert payload == [{"day": "2026-07-30", "amount": "12.50"}]
    assert response.media_type == "application/json; charset=utf-8"


def test_export_current_result_endpoint(export_client):
    """A result already shown in the UI can be downloaded as CSV."""
    response = export_client.post(
        "/api/v1/dbs/analytics/export",
        json={
            "format": "csv",
            "columns": [
                {"name": "id", "dataType": "integer"},
                {"name": "name", "dataType": "text"},
            ],
            "rows": [{"id": 1, "name": "Alice"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["x-export-format"] == "csv"
    assert response.headers["x-row-count"] == "1"
    assert "id,name" in response.content.decode("utf-8")


@patch("app.api.v1.queries.execute_query_with_service", new_callable=AsyncMock)
def test_execute_and_export_endpoint(mock_execute, export_client):
    """One API call executes a query and returns a JSON download."""
    mock_execute.return_value = QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="text"),
        ],
        rows=[{"id": 1, "name": "Alice"}],
        rowCount=1,
        executionTimeMs=18,
        sql="SELECT id, name FROM users",
    )

    response = export_client.post(
        "/api/v1/dbs/analytics/query/export",
        json={
            "sql": "SELECT id, name FROM users",
            "format": "json",
            "fileName": "active_users",
        },
    )

    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "Alice"}]
    assert response.headers["x-row-count"] == "1"
    assert response.headers["x-execution-time-ms"] == "18"
    assert "active_users.json" in response.headers["content-disposition"]
    mock_execute.assert_awaited_once()


def test_export_rejects_unsupported_format(export_client):
    """Only the declared CSV and JSON formats are accepted."""
    response = export_client.post(
        "/api/v1/dbs/analytics/export",
        json={"format": "xlsx", "columns": [], "rows": []},
    )

    assert response.status_code == 422


@patch(
    "app.api.v1.queries.export_assistant_service.respond",
    new_callable=AsyncMock,
)
def test_export_assistant_endpoint(mock_respond, export_client):
    """The API forwards conversation and page context to the planner."""
    mock_respond.return_value = ExportAssistantResponse(
        message="正在导出 users 表。",
        action=ExportAssistantAction.EXECUTE_EXPORT,
        format=ExportFormat.CSV,
        sql='SELECT * FROM "public"."users" LIMIT 1000',
    )

    response = export_client.post(
        "/api/v1/dbs/analytics/export/assistant",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": "帮我导出 users 表为 cvs",
                }
            ],
            "currentSql": "SELECT * FROM",
            "hasCurrentResult": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["action"] == "execute_export"
    assert response.json()["format"] == "csv"
    assert '"public"."users"' in response.json()["sql"]
    assert mock_respond.await_args.kwargs["current_sql"] == "SELECT * FROM"
