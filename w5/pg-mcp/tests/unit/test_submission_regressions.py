"""Regression cases for the resubmission, beyond the original happy-path tests."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from pg_mcp.config.settings import (
    DatabaseConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.errors import DatabaseError
from pg_mcp.models.query import QueryRequest, QueryResponse
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_tokens_used, record_tokens_used, request_context
from pg_mcp.services import result_validator, sql_generator
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_validator import SQLValidator


@pytest.mark.parametrize("name", ["", "  "])
def test_empty_database_identity_is_rejected(name):
    with pytest.raises(ValidationError):
        DatabaseConfig(name=name)


def test_explicit_database_list_requires_names_instead_of_environment_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_NAME", "legacy")
    with pytest.raises(ValidationError, match="name"):
        Settings(openai=OpenAIConfig(api_key="test"), databases=[{"host": "other-host"}])


def test_invalid_pool_bounds_are_rejected_before_startup():
    with pytest.raises(ValidationError, match="pool"):
        DatabaseConfig(min_pool_size=5, max_pool_size=2)


@pytest.mark.parametrize("executors", [MagicMock(), {"a": MagicMock()}])
def test_multidatabase_runtime_rejects_incomplete_executor_mapping(executors):
    with pytest.raises(ValueError, match="executor"):
        QueryOrchestrator(
            MagicMock(),
            MagicMock(),
            executors,
            MagicMock(),
            MagicMock(),
            {"a": MagicMock(), "b": MagicMock()},
            ResilienceConfig(),
            ValidationConfig(),
        )


@pytest.mark.parametrize("wrong_pool", [False, True])
def test_multidatabase_runtime_rejects_cross_wired_executor(wrong_pool):
    pools = {"a": MagicMock(), "b": MagicMock()}
    executors = {
        name: SQLExecutor(pool, SecurityConfig(), DatabaseConfig(name=name))
        for name, pool in pools.items()
    }
    if wrong_pool:
        executors["b"].pool = pools["a"]
    else:
        executors["b"].db_config = DatabaseConfig(name="a")
    with pytest.raises(ValueError, match="wrong executor"):
        QueryOrchestrator(
            MagicMock(),
            MagicMock(),
            executors,
            MagicMock(),
            MagicMock(),
            pools,
            ResilienceConfig(),
            ValidationConfig(),
        )


def test_json_policy_names_are_trimmed_instead_of_silently_disabling_protection():
    config = SecurityConfig(blocked_columns='[" users.ssn "]')
    assert config.blocked_columns == ["users.ssn"]
    assert not SQLValidator(config).validate("SELECT ssn FROM users")[0]


@pytest.mark.parametrize("value", ['[" "]', "[1]", '{"name":"ssn"}'])
def test_malformed_policy_lists_fail_at_configuration_time(value):
    with pytest.raises(ValidationError):
        SecurityConfig(blocked_columns=value)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.id FROM users u JOIN other_table o USING (ssn)",
        "SELECT u.id FROM other_table o JOIN public.users u USING (ssn)",
        "SELECT u.id FROM users u NATURAL JOIN other_table o",
        "WITH x AS (SELECT u.id FROM users u JOIN other_table o USING (ssn)) SELECT id FROM x",
        "EXPLAIN ANALYZE SELECT u.id FROM users u JOIN other_table o USING (ssn)",
    ],
)
def test_implicit_join_columns_cannot_bypass_column_policy(sql):
    validator = SQLValidator(
        SecurityConfig(blocked_columns=["public.users.ssn"], allow_explain=True)
    )
    valid, reason = validator.validate(sql)
    assert not valid, sql
    assert reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.id FROM users u JOIN other_table o USING (id)",
        "SELECT a.id FROM allowed a NATURAL JOIN other_table o",
        "SELECT a.id FROM allowed a JOIN other_table o USING (ssn)",
    ],
)
def test_unrestricted_join_columns_still_work(sql):
    SQLValidator(SecurityConfig(blocked_columns=["public.users.ssn"])).validate_or_raise(sql)


def test_response_model_and_all_serializers_agree_on_zero_before_llm_calls():
    response = QueryResponse(success=True)
    assert response.tokens_used == 0
    assert response.model_dump()["tokens_used"] == 0
    assert json.loads(response.model_dump_json())["tokens_used"] == 0
    assert response.to_dict()["tokens_used"] == 0


async def test_usage_survives_child_tasks_and_restores_outer_context():
    async def child():
        record_tokens_used(7)

    async with request_context("outer"):
        record_tokens_used(3)
        async with request_context("inner"):
            await asyncio.gather(child(), child())
            assert get_tokens_used() == 14
        assert get_tokens_used() == 3
    assert get_tokens_used() == 0


@pytest.fixture
async def sdk_runtime(monkeypatch):
    """Exercise actual SDK response parsing; only the HTTP transport is replaced."""
    state = {"responses": [], "clients": [], "requests": []}

    def respond(request):
        state["requests"].append(json.loads(request.content))
        return httpx.Response(200, json=state["responses"].pop(0))

    def make_client(**kwargs):
        client = AsyncOpenAI(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )
        state["clients"].append(client)
        return client

    monkeypatch.setattr(sql_generator, "AsyncOpenAI", make_client)
    monkeypatch.setattr(result_validator, "AsyncOpenAI", make_client)
    config = OpenAIConfig(api_key="test", base_url="https://llm.example/v1")
    validation = ValidationConfig()
    executor = AsyncMock()
    executor.execute.return_value = ([{"id": 1}], 1)
    pool = MagicMock()
    pool.get_size.return_value = 1
    pool.get_idle_size.return_value = 1
    runtime = QueryOrchestrator(
        sql_generator.SQLGenerator(config),
        SQLValidator(SecurityConfig(blocked_tables=["secrets"], blocked_columns=["users.ssn"])),
        {"test": executor},
        result_validator.ResultValidator(config, validation),
        MagicMock(get=MagicMock(return_value=DatabaseSchema(database_name="test"))),
        {"test": pool},
        ResilienceConfig(max_retries=1, retry_delay=0.1),
        validation,
        metrics=MetricsCollector(),
    )
    yield runtime, state
    for client in state["clients"]:
        await client.close()


def completion(content, usage):
    return {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                },
            }
        ],
        "usage": usage,
    }


async def test_actual_sdk_usage_includes_sql_correction_and_result_validation(sdk_runtime):
    runtime, state = sdk_runtime
    state["responses"] = [
        completion(
            "SELECT * FROM secrets",
            {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        ),
        completion(
            "SELECT id FROM users",
            {"prompt_tokens": 13, "completion_tokens": 4, "total_tokens": 17},
        ),
        completion(
            '{"confidence": 94, "explanation": "matches"}',
            {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        ),
    ]
    response = await runtime.execute_query(QueryRequest(question="list users"))
    assert response.success
    assert response.tokens_used == 53
    assert response.token_usage_complete is True
    runtime.sql_executors["test"].execute.assert_awaited_once_with("SELECT id FROM users;")
    assert (
        runtime.metrics.registry.get_sample_value(
            "pg_mcp_llm_tokens_used_total", {"operation": "generate"}
        )
        == 30
    )
    assert (
        runtime.metrics.registry.get_sample_value(
            "pg_mcp_llm_tokens_used_total", {"operation": "validate"}
        )
        == 23
    )


@pytest.mark.parametrize("return_type", ["sql", "result"])
async def test_token_count_is_nonzero_without_result_validation(sdk_runtime, return_type):
    runtime, state = sdk_runtime
    runtime.validation_config.enabled = False
    state["responses"] = [
        completion("SELECT 1", {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7})
    ]
    response = await runtime.execute_query(QueryRequest(question="count", return_type=return_type))
    assert response.success and response.tokens_used == 7
    assert len(state["requests"]) == 1


async def test_failed_generation_response_still_accounts_for_consumed_tokens(sdk_runtime):
    runtime, state = sdk_runtime
    state["responses"] = [
        completion(None, {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9})
    ]
    response = await runtime.execute_query(QueryRequest(question="count"))
    assert not response.success and response.tokens_used == 9
    runtime.sql_executors["test"].execute.assert_not_awaited()


@pytest.mark.parametrize("total", [None, 0])
async def test_compatible_provider_usage_falls_back_to_prompt_plus_completion(sdk_runtime, total):
    runtime, state = sdk_runtime
    state["responses"] = [
        completion("SELECT 1", {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": total})
    ]
    response = await runtime.execute_query(QueryRequest(question="count", return_type="sql"))
    assert response.success and response.tokens_used == 18
    assert response.token_usage_complete is True


async def test_missing_provider_usage_is_explicit_not_fabricated(sdk_runtime):
    runtime, state = sdk_runtime
    state["responses"] = [completion("SELECT 1", None)]
    response = await runtime.execute_query(QueryRequest(question="count", return_type="sql"))
    assert response.success and response.tokens_used == 0
    assert response.token_usage_complete is False
    assert "usage" in response.warning.lower()


async def test_partial_usage_keeps_known_subtotal_and_validation_warning(sdk_runtime):
    runtime, state = sdk_runtime
    state["responses"] = [
        completion("SELECT 1", {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}),
        completion('{"confidence": 40, "explanation": "unclear"}', None),
    ]
    response = await runtime.execute_query(QueryRequest(question="count"))
    assert response.success and response.tokens_used == 7
    assert response.token_usage_complete is False
    assert "confidence" in response.warning and "usage" in response.warning


async def test_database_failure_does_not_erase_generation_tokens(sdk_runtime):
    runtime, state = sdk_runtime
    state["responses"] = [
        completion("SELECT 1", {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}),
    ]
    runtime.sql_executors["test"].execute.side_effect = DatabaseError("permission denied")
    response = await runtime.execute_query(QueryRequest(question="count"))
    assert not response.success
    assert response.error.code == "database_error"
    assert response.tokens_used == 7
    assert len(state["requests"]) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN",
        "EXPLAIN VERBOSE",
        "EXPLAIN VACUUM users",
        "EXPLAIN ANALYZE /* SELECT decoy */ DELETE FROM users",
        "EXPLAIN (ANALYZE FALSE) WITH x AS (SELECT ssn FROM users) SELECT ssn FROM x",
        'SELECT u.id FROM users u JOIN other_table o USING ("ssn")',
    ],
)
def test_security_rejects_explain_edge_cases_and_quoted_join_keys(sql):
    valid, error = SQLValidator(
        SecurityConfig(blocked_columns=["users.ssn"], allow_explain=True)
    ).validate(sql)
    assert not valid and error


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT u.ssn FROM other_table AS "U", users AS u',
        'SELECT "U".ssn FROM other_table AS u, users AS "U"',
        'SELECT row_to_json(u) FROM users u, other_table "U"',
        "SELECT U.ssn FROM users u",
        "SELECT U.ssn /* sqlglot.meta case_sensitive */ FROM users u",
        'SELECT "U".id FROM users "U" WHERE EXISTS '
        "(SELECT 1 FROM other_table u WHERE \"U\".ssn = 'x')",
    ],
)
def test_alias_case_and_scope_shadowing_cannot_bypass_sensitive_columns(sql):
    assert not SQLValidator(SecurityConfig(blocked_columns=["users.ssn"])).validate(sql)[0]


def test_case_distinct_alias_can_still_read_an_unrestricted_table():
    SQLValidator(SecurityConfig(blocked_columns=["users.ssn"])).validate_or_raise(
        'SELECT "U".ssn FROM other_table "U", users u'
    )
