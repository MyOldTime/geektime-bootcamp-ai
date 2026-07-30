"""Schema-aware, multi-turn conversational planning for result exports."""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.models.database import DatabaseType
from app.models.schemas import (
    ExportAssistantAction,
    ExportAssistantMessage,
    ExportAssistantResponse,
    ExportFormat,
)
from app.services.nl2sql import nl2sql_service

logger = logging.getLogger(__name__)

_EXPORT_WORDS = re.compile(
    r"导出|下载|保存|文件|\bexport\b|\bdownload\b|\bsave\b",
    flags=re.IGNORECASE,
)
_FILTER_WORDS = re.compile(
    r"最近|过去|今天|昨天|本周|本月|只要|筛选|过滤|条件|状态|日期|时间|"
    r"大于|小于|等于|包含|排序|前\s*\d+|后\s*\d+|"
    r"\bwhere\b|\bfilter\b|\blast\b|\brecent\b|\btoday\b|\byesterday\b|"
    r"\border\s+by\b",
    flags=re.IGNORECASE,
)
_HELP_WORDS = re.compile(
    r"帮助|怎么用|如何使用|你能做什么|\bhelp\b|what can you do",
    flags=re.IGNORECASE,
)
_WRITE_WORDS = re.compile(
    r"删除|修改|更新|插入|清空|建表|删表|"
    r"\bdelete\b|\bupdate\b|\binsert\b|\bdrop\b|\btruncate\b|\balter\b",
    flags=re.IGNORECASE,
)
_CURRENT_RESULT_WORDS = re.compile(
    r"当前结果|查询结果|刚才的结果|上次结果|这个结果|"
    r"\bcurrent\s+result\b|\bquery\s+result\b|\bprevious\s+result\b",
    flags=re.IGNORECASE,
)
_GREETING_WORDS = re.compile(
    r"^(你好|您好|嗨|哈喽|hello|hi|hey)[!！,.，。\s]*$",
    flags=re.IGNORECASE,
)


def _extract_format(value: str) -> ExportFormat | None:
    """Recognize formats, including common CSV misspellings and aliases."""
    has_json = bool(
        re.search(r"(?<![a-z0-9])json(?![a-z0-9])", value, flags=re.IGNORECASE)
    )
    has_csv = bool(
        re.search(
            r"(?<![a-z0-9])(?:csv|cvs)(?![a-z0-9])",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(r"excel|表格|逗号分隔", value, flags=re.IGNORECASE)
    )
    if has_json == has_csv:
        return None
    return ExportFormat.JSON if has_json else ExportFormat.CSV


def _latest_format(messages: Sequence[ExportAssistantMessage]) -> ExportFormat | None:
    for message in reversed(messages):
        if message.role == "user":
            export_format = _extract_format(message.content)
            if export_format:
                return export_format
    return None


def _relations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    relations = [
        relation
        for group in ("tables", "views")
        for relation in metadata.get(group, [])
        if relation.get("name")
    ]
    return sorted(
        relations,
        key=lambda relation: len(str(relation.get("name", ""))),
        reverse=True,
    )


def _mentioned_relations(
    value: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    mentioned: list[dict[str, Any]] = []
    for relation in _relations(metadata):
        name = str(relation["name"])
        if name.isascii():
            pattern = rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])"
        else:
            pattern = re.escape(name)
        if re.search(pattern, value, flags=re.IGNORECASE):
            mentioned.append(relation)
    return mentioned


def _latest_relation(
    messages: Sequence[ExportAssistantMessage],
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        mentioned = _mentioned_relations(message.content, metadata)
        if len(mentioned) == 1:
            return mentioned[0]
        if len(mentioned) > 1:
            return None
    return None


def _relation_context_has_filters(
    messages: Sequence[ExportAssistantMessage],
    metadata: dict[str, Any],
    relation: dict[str, Any],
) -> bool:
    """Check filters introduced since the active relation was last named."""
    relation_index = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "user":
            continue
        if relation in _mentioned_relations(message.content, metadata):
            relation_index = index
            break
    return any(
        message.role == "user" and _FILTER_WORDS.search(message.content)
        for message in messages[relation_index:]
    )


def _qualified_relation(
    relation: dict[str, Any],
    db_type: DatabaseType,
) -> str:
    quote = "`" if db_type == DatabaseType.MYSQL else '"'
    schema_name = str(relation.get("schemaName") or "public")
    table_name = str(relation["name"])
    escaped_schema = schema_name.replace(quote, quote * 2)
    escaped_table = table_name.replace(quote, quote * 2)
    return f"{quote}{escaped_schema}{quote}.{quote}{escaped_table}{quote}"


def _rule_based_plan(
    *,
    messages: Sequence[ExportAssistantMessage],
    metadata: dict[str, Any],
    db_type: DatabaseType,
    has_current_result: bool,
) -> ExportAssistantResponse | None:
    """Handle common export conversations without an external model call."""
    last_user_message = next(
        (message.content for message in reversed(messages) if message.role == "user"),
        "",
    )
    if not last_user_message:
        return ExportAssistantResponse(message="请告诉我想导出哪些数据。")

    if _GREETING_WORDS.match(last_user_message.strip()):
        return ExportAssistantResponse(
            message=(
                "你好！我可以按表名或查询条件导出数据。"
                "例如：“把 users 表导出成 CSV”，也可以分两次告诉我表名和格式。"
            )
        )
    if _HELP_WORDS.search(last_user_message):
        return ExportAssistantResponse(
            message=(
                "我支持多轮导出：告诉我表名、筛选条件和 CSV/JSON 格式即可。"
                "如果信息不完整，我会继续询问；已有查询结果也可以直接导出。"
            )
        )
    if _WRITE_WORDS.search(last_user_message):
        return ExportAssistantResponse(
            message=(
                "为了数据安全，我只能执行只读查询并导出数据，"
                "不能执行删除或修改操作。"
            )
        )

    current_mentions = _mentioned_relations(last_user_message, metadata)
    if len(current_mentions) > 1:
        names = "、".join(str(relation["name"]) for relation in current_mentions)
        return ExportAssistantResponse(
            message=f"我识别到多个表：{names}。请确认这次要导出哪一个。"
        )

    relation = _latest_relation(messages, metadata)
    export_format = _latest_format(messages)
    current_format = _extract_format(last_user_message)
    mentions_export = bool(_EXPORT_WORDS.search(last_user_message)) or bool(
        current_format
    )

    if _CURRENT_RESULT_WORDS.search(last_user_message):
        if not has_current_result:
            return ExportAssistantResponse(
                message="当前没有可导出的查询结果，请告诉我要查询的表或数据条件。"
            )
        if not export_format:
            return ExportAssistantResponse(
                message="可以，请选择将当前查询结果导出为 CSV 还是 JSON。"
            )
        return ExportAssistantResponse(
            message=f"好的，正在导出当前查询结果为 {export_format.value.upper()}。",
            action=ExportAssistantAction.EXPORT_CURRENT,
            format=export_format,
        )

    if current_mentions and not current_format and mentions_export:
        return ExportAssistantResponse(
            message=(
                f"已找到表 {current_mentions[0]['name']}。"
                "请选择导出为 CSV 还是 JSON，我会记住这个表。"
            )
        )

    if relation and not export_format and mentions_export:
        return ExportAssistantResponse(
            message=(
                f"已找到表 {relation['name']}。请选择导出为 CSV 还是 JSON，"
                "我会记住这个表。"
            )
        )

    continues_export = bool(current_mentions) or bool(current_format) or mentions_export
    if relation and export_format and continues_export:
        if _relation_context_has_filters(messages, metadata, relation):
            return None
        qualified_name = _qualified_relation(relation, db_type)
        sql = f"SELECT * FROM {qualified_name} LIMIT 1000"
        display_name = f"{relation.get('schemaName', 'public')}.{relation['name']}"
        return ExportAssistantResponse(
            message=(
                f"已找到 {display_name}，正在直接导出 "
                f"{export_format.value.upper()}，无需依赖编辑器中的 SQL。"
            ),
            action=ExportAssistantAction.EXECUTE_EXPORT,
            format=export_format,
            sql=sql,
        )

    if has_current_result and export_format and mentions_export:
        return ExportAssistantResponse(
            message=f"好的，正在导出当前查询结果为 {export_format.value.upper()}。",
            action=ExportAssistantAction.EXPORT_CURRENT,
            format=export_format,
        )

    if mentions_export and not export_format:
        return ExportAssistantResponse(message="可以，请选择 CSV 或 JSON 格式。")

    return None


def _schema_summary(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "schema": relation.get("schemaName", "public"),
            "name": relation["name"],
            "type": relation.get("type", "table"),
            "columns": [
                column.get("name")
                for column in relation.get("columns", [])
                if column.get("name")
            ],
        }
        for relation in _relations(metadata)[:200]
    ]


def _clean_json_response(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Assistant response did not contain a JSON object")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Assistant response must be a JSON object")
    return parsed


def _response_from_model(
    payload: dict[str, Any],
    *,
    has_current_result: bool,
) -> ExportAssistantResponse:
    action_value = str(payload.get("action", "chat")).lower()
    try:
        action = ExportAssistantAction(action_value)
    except ValueError:
        action = ExportAssistantAction.CHAT

    raw_format = str(payload.get("format") or "")
    export_format = _extract_format(raw_format)
    sql = str(payload.get("sql") or "").strip() or None
    message = str(payload.get("message") or "").strip()

    if action == ExportAssistantAction.EXPORT_CURRENT and not has_current_result:
        return ExportAssistantResponse(
            message="当前没有可导出的查询结果。请告诉我要查询的表或数据条件。"
        )
    if action == ExportAssistantAction.EXECUTE_EXPORT:
        if not export_format or not sql:
            return ExportAssistantResponse(
                message=message or "请补充导出格式以及要查询的数据。"
            )
        if not re.match(r"^\s*(?:select|with)\b", sql, flags=re.IGNORECASE):
            return ExportAssistantResponse(
                message="为了数据安全，我只能生成只读查询。请换一种方式描述导出需求。"
            )
    if action == ExportAssistantAction.EXPORT_CURRENT and not export_format:
        return ExportAssistantResponse(message="请选择 CSV 或 JSON 格式。")

    return ExportAssistantResponse(
        message=message or "好的，我来处理这次导出。",
        action=action,
        format=export_format,
        sql=sql if action == ExportAssistantAction.EXECUTE_EXPORT else None,
    )


class ExportAssistantService:
    """Plan conversational export actions using rules with an LLM fallback."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        model: str | None = None,
    ) -> None:
        self.client = client or nl2sql_service.client
        self.model = model or nl2sql_service.model

    async def respond(
        self,
        *,
        messages: Sequence[ExportAssistantMessage],
        metadata: dict[str, Any],
        db_type: DatabaseType,
        current_sql: str | None,
        has_current_result: bool,
    ) -> ExportAssistantResponse:
        """Reply conversationally and optionally return a safe export plan."""
        rule_plan = _rule_based_plan(
            messages=messages,
            metadata=metadata,
            db_type=db_type,
            has_current_result=has_current_result,
        )
        if rule_plan:
            return rule_plan

        system_message = f"""You are a conversational data export assistant.
Reply in the user's language and use the full conversation for follow-up references.
The database type is {db_type.value}.
Available schema:
{json.dumps(_schema_summary(metadata), ensure_ascii=False)}

Page context:
- A current query result exists: {has_current_result}
- SQL currently in the editor: {current_sql or "(empty)"}

Your task is to help export data as CSV or JSON.
- Treat "cvs" as a typo for CSV.
- Ask a concise follow-up when table/data scope or format is missing.
- If the user explicitly names a table or describes filters, generate a read-only
  SELECT query with LIMIT 1000 and use action "execute_export".
- Use "export_current" only when the user refers to the current/previous result.
- Never generate INSERT, UPDATE, DELETE, DDL, or multiple statements.
- For normal conversation or clarification, use action "chat".

Return only one JSON object:
{{"message":"...", "action":"chat|export_current|execute_export",
  "format":"csv|json|null", "sql":"SELECT ...|null"}}"""

        model_messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system_message}
        ]
        for message in messages[-20:]:
            if message.role == "user":
                model_messages.append(
                    {"role": "user", "content": message.content}
                )
            else:
                model_messages.append(
                    {"role": "assistant", "content": message.content}
                )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=model_messages,
                temperature=0.1,
                max_tokens=800,
            )
            content = response.choices[0].message.content or ""
            return _response_from_model(
                _clean_json_response(content),
                has_current_result=has_current_result,
            )
        except Exception as exc:
            logger.warning("Export assistant model fallback failed: %s", exc)
            return ExportAssistantResponse(
                message=(
                    "我还不能确定要导出的数据。请提供准确的表名、筛选条件，"
                    "以及 CSV 或 JSON 格式。"
                )
            )


export_assistant_service = ExportAssistantService()
