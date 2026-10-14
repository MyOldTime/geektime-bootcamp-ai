"""Behavioral coverage for the connected request pipeline."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from pg_mcp.config.settings import (
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.errors import DatabaseError, LLMError, LLMTimeoutError, SecurityViolationError
from pg_mcp.models.query import ErrorDetail, QueryRequest, QueryResponse, QueryResult
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, get_tokens_used, record_tokens_used
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_validator import SQLValidator


@pytest.fixture
def runtime():
    pools = {name: MagicMock() for name in ("a", "b")}
    cache = MagicMock()
    cache.get.side_effect = lambda name: DatabaseSchema(database_name=name)
    executors = {
        name: AsyncMock(execute=AsyncMock(return_value=([{"database": name}], 1))) for name in pools
    }
    generator = AsyncMock()
    generator.generate.return_value = "SELECT 1"
    result_validator = AsyncMock()
    result_validator.validate.return_value = MagicMock(confidence=90)
    return QueryOrchestrator(
        generator,
        SQLValidator(SecurityConfig()),
        executors,
        result_validator,
        cache,
        pools,
        ResilienceConfig(max_retries=2, retry_delay=0.1, db_retry_delay=0.1),
        ValidationConfig(),
        metrics=MetricsCollector(),
    )


async def test_routing_uses_same_database_for_schema_and_execution(runtime):
    for name in ("a", "b"):
        response = await runtime.execute_query(QueryRequest(question="count", database=name))
        assert response.success
        assert response.data.rows == [{"database": name}]
        assert runtime.sql_generator.generate.call_args.kwargs["schema"].database_name == name
        assert (
            runtime.metrics.registry.get_sample_value(
                "pg_mcp_query_requests_total",
                {"database": name, "status": "success"},
            )
            == 1
        )
    assert all(executor.execute.await_count == 1 for executor in runtime.sql_executors.values())


@pytest.mark.parametrize("database", [None, "missing"])
async def test_invalid_selection_never_calls_llm_or_database(runtime, database):
    response = await runtime.execute_query(QueryRequest(question="count", database=database))
    assert not response.success
    runtime.sql_generator.generate.assert_not_awaited()
    assert all(executor.execute.await_count == 0 for executor in runtime.sql_executors.values())


async def test_missing_executor_fails_closed(runtime):
    runtime.sql_executors.pop("b")
    response = await runtime.execute_query(QueryRequest(question="count", database="b"))
    assert not response.success
    runtime.sql_executors["a"].execute.assert_not_awaited()


async def test_backoff_transient_errors_and_token_usage(runtime, monkeypatch):
    attempts = 0

    async def generate(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise LLMTimeoutError("temporary outage")
        record_tokens_used(12)
        return "SELECT 1"

    async def validate(**kwargs):
        record_tokens_used(8)
        return MagicMock(confidence=90)

    runtime.sql_generator.generate.side_effect = generate
    runtime.result_validator.validate.side_effect = validate
    sleep = AsyncMock()
    monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", sleep)
    response = await runtime.execute_query(QueryRequest(question="count", database="a"))
    assert response.success and response.tokens_used == 20
    assert [call.args[0] for call in sleep.call_args_list] == [0.1, 0.2]
    assert runtime.circuit_breaker.failure_count == 0
    assert (
        runtime.metrics.registry.get_sample_value(
            "pg_mcp_llm_calls_total",
            {"operation": "generate"},
        )
        == 3
    )
    assert (
        runtime.metrics.registry.get_sample_value(
            "pg_mcp_llm_tokens_used_total",
            {"operation": "validate"},
        )
        == 8
    )
    assert get_tokens_used() == 0


async def test_non_transient_failure_does_not_retry(runtime):
    runtime.sql_generator.generate.side_effect = LLMError("invalid API key")
    response = await runtime.execute_query(QueryRequest(question="count", database="a"))
    assert not response.success
    assert runtime.sql_generator.generate.await_count == 1
    assert runtime.circuit_breaker.failure_count == 1


async def test_circuit_opens_during_retry_and_blocks_next_request(runtime, monkeypatch):
    runtime.circuit_breaker._failure_threshold = 1
    runtime.sql_generator.generate.side_effect = LLMTimeoutError("outage")
    monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", AsyncMock())
    for _ in range(2):
        response = await runtime.execute_query(QueryRequest(question="count", database="a"))
        assert not response.success
    assert runtime.sql_generator.generate.await_count == 1


async def test_database_retries_only_transient_failures(runtime, monkeypatch):
    runtime.sql_executors["b"].execute.side_effect = [
        DatabaseError("connection lost", details={"retryable": True}),
        ([{"database": "b"}], 1),
    ]
    sleep = AsyncMock()
    monkeypatch.setattr("pg_mcp.services.orchestrator.asyncio.sleep", sleep)
    response = await runtime.execute_query(QueryRequest(question="count", database="b"))
    assert response.success
    assert runtime.sql_executors["b"].execute.await_count == 2
    runtime.sql_executors["a"].execute.assert_not_awaited()
    sleep.assert_awaited_once_with(0.1)


async def test_traces_and_tokens_are_isolated_across_concurrent_requests(runtime):
    seen = []

    async def generate(**kwargs):
        request_id = get_request_id()
        record_tokens_used(5 if kwargs["schema"].database_name == "a" else 11)
        await asyncio.sleep(0)
        assert request_id == get_request_id()
        seen.append(request_id)
        return "SELECT 1"

    runtime.sql_generator.generate.side_effect = generate
    responses = await asyncio.gather(
        *(
            runtime.execute_query(QueryRequest(question="count", database=name))
            for name in ("a", "b")
        )
    )
    assert [response.tokens_used for response in responses] == [5, 11]
    assert {response.request_id for response in responses} == set(seen)
    assert len(set(seen)) == 2
    assert get_request_id() is None


async def test_queue_limit_cancellation_and_recovery(runtime):
    from pg_mcp.resilience.rate_limiter import MultiRateLimiter

    runtime.resilience_config.max_concurrent_queries = 1
    runtime.resilience_config.request_queue_size = 0
    runtime.rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return "SELECT 1"

    runtime.sql_generator.generate.side_effect = generate
    task = asyncio.create_task(runtime.execute_query(QueryRequest(question="count", database="a")))
    await entered.wait()
    rejected = await runtime.execute_query(QueryRequest(question="count", database="b"))
    assert rejected.error.code == "rate_limit_exceeded"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.rate_limiter.query_limiter.active_count == 0
    assert runtime.rate_limiter.llm_limiter.active_count == 0
    assert runtime._pending_requests == 0
    release.set()
    response = await runtime.execute_query(QueryRequest(question="count", database="b"))
    assert response.success


async def test_question_limit_and_failed_validation_are_visible(runtime):
    runtime.validation_config.max_question_length = 1
    response = await runtime.execute_query(QueryRequest(question="too long", database="a"))
    assert response.error.code == "question_too_long"
    runtime.sql_generator.generate.assert_not_awaited()
    runtime.result_validator.validate.side_effect = LLMError("invalid response")
    response = await runtime.execute_query(QueryRequest(question="x", database="a"))
    assert response.success and response.confidence == 0
    assert response.warning


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM public.secrets",
        'SELECT * FROM public."Secrets" s',
        "WITH s AS (SELECT * FROM secrets) SELECT * FROM s",
        "EXPLAIN (ANALYZE FALSE, FORMAT JSON) SELECT * FROM secrets",
        "SELECT ssn FROM users",
        "SELECT u.ssn FROM public.users AS u",
        "SELECT id FROM users u WHERE u.ssn = '123'",
        "SELECT * FROM users",
        "SELECT u.* FROM users u",
        "SELECT row_to_json(u) FROM users u",
        "SELECT row_to_json(u.*) FROM users u",
        "WITH x AS (SELECT ssn AS name FROM users) SELECT name FROM x",
        "SELECT id FROM users u WHERE EXISTS (SELECT 1 WHERE u.ssn = '123')",
        "SELECT id FROM users WHERE EXISTS (SELECT 1 WHERE ssn = '123')",
        "SELECT renamed FROM users AS u(a, b, renamed)",
        "WITH x AS (DELETE FROM users RETURNING id) SELECT id FROM x",
        "EXPLAIN ANALYZE SELECT pg_catalog.pg_sleep(1)",
        "EXPLAIN SELECT query_to_xml('SELECT * FROM secrets', true, true, '')",
        "SELECT id INTO new_users FROM users",
        "SELECT id FROM users FOR UPDATE",
    ],
)
def test_security_cannot_be_bypassed(sql):
    validator = SQLValidator(
        SecurityConfig(
            blocked_tables=["public.secrets"],
            blocked_columns=["public.users.ssn"],
            allow_explain=True,
        )
    )
    with pytest.raises(SecurityViolationError):
        validator.validate_or_raise(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT u.id FROM public.users u",
        "SELECT COUNT(*) FROM users",
        "WITH x AS (SELECT id FROM users) SELECT id FROM x",
        "SELECT id FROM users UNION SELECT id FROM users",
        "EXPLAIN (ANALYZE FALSE, FORMAT JSON) SELECT id FROM users",
        "SELECT ssn FROM other_table",
    ],
)
def test_safe_queries_remain_available(sql):
    SQLValidator(
        SecurityConfig(blocked_columns=["users.ssn"], allow_explain=True)
    ).validate_or_raise(sql)


def test_dotenv_database_list_and_environment_override(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-test\nOPENAI_BASE_URL=https://gateway.example/v1\n"
        'DATABASES=[{"name":"a"},{"name":"b","host":"second"}]\n'
        "SECURITY_BLOCKED_TABLES=secrets,private.users\n"
        'SECURITY_BLOCKED_COLUMNS=["users.ssn"]\n'
        "SECURITY_ALLOW_WRITE_OPERATIONS=false\nVALIDATION_MIN_CONFIDENCE_SCORE=81\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_MODEL", "custom-model")
    settings = Settings()
    assert [database.name for database in settings.configured_databases] == ["a", "b"]
    assert settings.configured_databases[1].host == "second"
    assert settings.security.blocked_tables == ["secrets", "private.users"]
    assert settings.security.blocked_columns == ["users.ssn"]
    assert settings.openai.base_url == "https://gateway.example/v1"
    assert settings.openai.model == "custom-model"
    assert settings.validation.confidence_threshold == 81


def test_duplicate_databases_fail_configuration():
    with pytest.raises(ValidationError, match="unique"):
        Settings(openai=OpenAIConfig(api_key="sk-test"), databases=[{"name": "a"}, {"name": "a"}])


def test_response_contract_and_consistency():
    response = QueryResponse(success=True, data=QueryResult(rows=[{"value": 1}]))
    payload = response.to_dict()
    assert payload["tokens_used"] == 0
    assert payload["data"]["row_count"] == 1
    assert json.loads(json.dumps(payload)) == payload
    with pytest.raises(ValidationError):
        QueryResponse(success=False)
    with pytest.raises(ValidationError):
        QueryResponse(success=True, error=ErrorDetail(code="error", message="bad"))


def test_metrics_collectors_and_resets_do_not_duplicate_registration():
    first, second = MetricsCollector(), MetricsCollector()
    first.increment_llm_call("generate")
    assert (
        second.registry.get_sample_value("pg_mcp_llm_calls_total", {"operation": "generate"})
        is None
    )
    first.reset_all_metrics()
    assert (
        first.registry.get_sample_value("pg_mcp_llm_calls_total", {"operation": "generate"}) is None
    )


async def test_queue_timeout_and_llm_concurrency(runtime):
    from pg_mcp.resilience.rate_limiter import MultiRateLimiter

    runtime.resilience_config.queue_timeout = 0.01
    runtime.rate_limiter = MultiRateLimiter(query_limit=2, llm_limit=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return "SELECT 1"

    runtime.sql_generator.generate.side_effect = generate
    running = asyncio.create_task(runtime.execute_query(QueryRequest(question="x", database="a")))
    await entered.wait()
    response = await runtime.execute_query(QueryRequest(question="x", database="b"))
    assert response.error.code == "rate_limit_exceeded"
    assert runtime.sql_generator.generate.await_count == 1
    assert runtime.rate_limiter.llm_limiter.active_count == 1
    release.set()
    assert (await running).success
    assert runtime.rate_limiter.llm_limiter.active_count == 0


async def test_partial_startup_cleans_pools(monkeypatch):
    from pg_mcp import server
    from pg_mcp.config.settings import DatabaseConfig

    settings = Settings(
        openai=OpenAIConfig(api_key="sk-test"),
        databases=[DatabaseConfig(name="a"), DatabaseConfig(name="b")],
        resilience=ResilienceConfig(db_retry_attempts=0),
    )
    first = MagicMock(close=AsyncMock())
    monkeypatch.setattr(server, "Settings", lambda: settings)
    monkeypatch.setattr(server, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(server, "create_pool", AsyncMock(side_effect=[first, OSError("offline")]))
    with pytest.raises(OSError):
        async with server.lifespan(server.mcp):
            pytest.fail("Startup should have failed")
    first.close.assert_awaited_once()
    assert server._pools is None and server._orchestrator is None


async def test_metrics_http_server_stops_and_port_can_be_reused():
    import httpx

    collector = MetricsCollector()
    collector.start_metrics_server(0)
    port = collector._server.server_port
    try:
        collector.increment_query_request("success", "a")
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/metrics")
        assert response.status_code == 200
        assert 'pg_mcp_query_requests_total{database="a",status="success"} 1.0' in response.text
    finally:
        await asyncio.to_thread(collector.stop_metrics_server)
    collector.start_metrics_server(port)
    await asyncio.to_thread(collector.stop_metrics_server)


async def test_cache_capacity_is_enforced(monkeypatch):
    from pg_mcp.cache.schema_cache import SchemaCache
    from pg_mcp.config.settings import CacheConfig

    cache = SchemaCache(CacheConfig(max_size=1))
    introspector = MagicMock()
    introspector.introspect = AsyncMock(return_value=DatabaseSchema(database_name="a"))
    monkeypatch.setattr("pg_mcp.cache.schema_cache.SchemaIntrospector", lambda *args: introspector)
    await cache.load("a", MagicMock())
    await cache.load("b", MagicMock())
    assert cache.get("a") is None and cache.get("b") is not None


@pytest.mark.parametrize(
    "text",
    [
        "EXPLAIN ANALYZE SELECT id FROM users;",
        "SELECT 'value;with;semicolons';",
        "SELECT 1; DELETE FROM users;",
    ],
)
def test_sql_extraction_preserves_security_relevant_content(text):
    from pg_mcp.services.sql_generator import SQLGenerator

    generator = object.__new__(SQLGenerator)
    assert generator._extract_sql(text) == text


async def test_structured_logs_include_trace_and_mask_credentials(capsys):
    import logging

    from pg_mcp.observability.logging import configure_logging
    from pg_mcp.observability.tracing import request_context

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging(log_format="json")
        async with request_context("trace-for-logging"):
            logging.getLogger("pg_mcp.services.test").info(
                "operation completed",
                extra={"password": "secret-value", "api_key": "sk-private"},
            )
        captured = capsys.readouterr()
        assert captured.out == ""
        record = json.loads(captured.err)
        assert record["request_id"] == "trace-for-logging"
        assert "secret-value" not in captured.err and "sk-private" not in captured.err
    finally:
        root.handlers = handlers
        root.setLevel(level)


async def test_server_input_failures_keep_response_and_metrics_contract(monkeypatch):
    from pg_mcp import server

    collector = MetricsCollector()
    monkeypatch.setattr(server, "_metrics", collector)
    monkeypatch.setattr(server, "_orchestrator", MagicMock())
    for question, return_type in [("x", "bad"), ("", "sql")]:
        response = await server.query(question, return_type=return_type)
        assert not response["success"] and response["tokens_used"] == 0
        assert response["request_id"]
    assert (
        collector.registry.get_sample_value(
            "pg_mcp_query_requests_total",
            {"status": "invalid_request", "database": "__unknown__"},
        )
        == 2
    )


def test_environment_example_can_be_loaded(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    contents = example.read_text(encoding="utf-8").replace(
        "# OPENAI_API_KEY=sk-your-openai-api-key-here",
        "OPENAI_API_KEY=sk-test",
    )
    (tmp_path / ".env").write_text(contents, encoding="utf-8")
    settings = Settings()
    assert settings.security.allow_write_operations is False
    assert settings.openai.max_tokens == 2000
