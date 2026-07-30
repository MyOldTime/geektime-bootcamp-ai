"""Tests for schema-aware, multi-turn export conversations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.database import DatabaseType
from app.models.schemas import (
    ExportAssistantAction,
    ExportAssistantMessage,
    ExportFormat,
)
from app.services.export_assistant import ExportAssistantService


@pytest.fixture
def metadata():
    return {
        "tables": [
            {
                "name": "wpy_content_audit",
                "schemaName": "public",
                "type": "table",
                "columns": [
                    {"name": "id", "dataType": "integer"},
                    {"name": "created_at", "dataType": "timestamp"},
                ],
            },
            {
                "name": "users",
                "schemaName": "public",
                "type": "table",
                "columns": [{"name": "id", "dataType": "integer"}],
            },
        ],
        "views": [],
    }


def user(content: str) -> ExportAssistantMessage:
    return ExportAssistantMessage(role="user", content=content)


def assistant(content: str) -> ExportAssistantMessage:
    return ExportAssistantMessage(role="assistant", content=content)


@pytest.mark.asyncio
async def test_table_export_understands_cvs_typo(metadata):
    """A named table can be exported without using the editor SQL."""
    service = ExportAssistantService()

    result = await service.respond(
        messages=[user("帮我导出wpy_content_audit的数据为cvs文件")],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql="SELECT * FROM",
        has_current_result=False,
    )

    assert result.action == ExportAssistantAction.EXECUTE_EXPORT
    assert result.format == ExportFormat.CSV
    assert result.sql == (
        'SELECT * FROM "public"."wpy_content_audit" LIMIT 1000'
    )
    assert "无需依赖编辑器" in result.message


@pytest.mark.asyncio
async def test_follow_up_format_uses_remembered_table(metadata):
    """The assistant remembers a table while asking for the format."""
    service = ExportAssistantService()
    first_messages = [user("导出 wpy_content_audit 的数据")]

    first = await service.respond(
        messages=first_messages,
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql=None,
        has_current_result=False,
    )
    assert first.action == ExportAssistantAction.CHAT
    assert "CSV 还是 JSON" in first.message

    second = await service.respond(
        messages=[
            *first_messages,
            assistant(first.message),
            user("cvs 就行"),
        ],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql=None,
        has_current_result=False,
    )
    assert second.action == ExportAssistantAction.EXECUTE_EXPORT
    assert second.format == ExportFormat.CSV
    assert "wpy_content_audit" in (second.sql or "")


@pytest.mark.asyncio
async def test_current_result_export_remains_available(metadata):
    """A format-only request exports the current result when one exists."""
    service = ExportAssistantService()

    result = await service.respond(
        messages=[user("把当前查询结果导出成 JSON")],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql="SELECT id FROM users",
        has_current_result=True,
    )

    assert result.action == ExportAssistantAction.EXPORT_CURRENT
    assert result.format == ExportFormat.JSON
    assert result.sql is None


@pytest.mark.asyncio
async def test_explicit_current_result_overrides_remembered_table(metadata):
    """An explicit current-result request is not redirected to an old table."""
    service = ExportAssistantService()

    result = await service.respond(
        messages=[
            user("导出 users 表为 CSV"),
            assistant("users 表已经导出。"),
            user("现在把当前查询结果导出为 JSON"),
        ],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql="SELECT id FROM wpy_content_audit",
        has_current_result=True,
    )

    assert result.action == ExportAssistantAction.EXPORT_CURRENT
    assert result.format == ExportFormat.JSON


@pytest.mark.asyncio
async def test_filtered_request_uses_model_for_sql(metadata):
    """Requests with filters use the model instead of exporting a whole table."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=(
                        '{"message":"正在导出最近 30 天的数据",'
                        '"action":"execute_export","format":"json",'
                        '"sql":"SELECT * FROM public.wpy_content_audit '
                        "WHERE created_at >= CURRENT_DATE - INTERVAL '30 days' "
                        'LIMIT 1000"}'
                    )
                )
            )
        ]
    )
    service = ExportAssistantService(client=client, model="test-model")

    result = await service.respond(
        messages=[user("导出 wpy_content_audit 最近30天的数据为 JSON")],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql=None,
        has_current_result=False,
    )

    assert result.action == ExportAssistantAction.EXECUTE_EXPORT
    assert result.format == ExportFormat.JSON
    assert "WHERE created_at" in (result.sql or "")
    client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_filter_survives_format_clarification(metadata):
    """A filter from the previous turn remains part of the model request."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=(
                            '{"message":"正在处理","action":"execute_export",'
                            '"format":"csv","sql":"SELECT * FROM '
                            "public.wpy_content_audit WHERE created_at >= "
                            "CURRENT_DATE - INTERVAL '30 days' LIMIT 1000\"}"
                        )
                    )
                )
            ]
        )
    )
    service = ExportAssistantService(client=client, model="test-model")

    result = await service.respond(
        messages=[
            user("导出 wpy_content_audit 最近30天的数据"),
            assistant("请选择 CSV 或 JSON。"),
            user("cvs"),
        ],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql=None,
        has_current_result=False,
    )

    assert result.action == ExportAssistantAction.EXECUTE_EXPORT
    assert result.format == ExportFormat.CSV
    assert "30 days" in (result.sql or "")
    client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_cannot_plan_a_write_query(metadata):
    """A model response containing a write statement is downgraded to chat."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=(
                            '{"message":"处理中","action":"execute_export",'
                            '"format":"csv","sql":"DELETE FROM users"}'
                        )
                    )
                )
            ]
        )
    )
    service = ExportAssistantService(client=client, model="test-model")

    result = await service.respond(
        messages=[user("删除 users 再导出，CSV")],
        metadata=metadata,
        db_type=DatabaseType.POSTGRESQL,
        current_sql=None,
        has_current_result=False,
    )

    assert result.action == ExportAssistantAction.CHAT
    assert result.sql is None
    assert "只读查询" in result.message
