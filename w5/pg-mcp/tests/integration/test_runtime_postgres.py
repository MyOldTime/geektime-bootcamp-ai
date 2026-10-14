"""Real PostgreSQL routing and authorization with deterministic LLM responses.

Set PG_MCP_TEST_PORT to an isolated test PostgreSQL instance using the test-only
password below. Databases and the role are unique to each test and removed afterward.
"""

import asyncio
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import asyncpg
import httpx
import pytest
from openai import AsyncOpenAI

from pg_mcp import server
from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.models.errors import DatabaseError
from pg_mcp.services import result_validator, sql_generator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PG_MCP_TEST_PORT"), reason="Requires isolated test PostgreSQL"
    ),
]


@pytest.fixture
async def postgres_runtime(monkeypatch, tmp_path, request):
    port = int(os.environ["PG_MCP_TEST_PORT"])
    monkeypatch.chdir(tmp_path)
    for key in os.environ:
        if key == "DATABASES" or key.startswith(
            (
                "DATABASE_",
                "OPENAI_",
                "SECURITY_",
                "VALIDATION_",
                "CACHE_",
                "RESILIENCE_",
                "OBSERVABILITY_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    suffix = uuid.uuid4().hex[:12]
    names = [f"pg_mcp_a_{suffix}", f"pg_mcp_b_{suffix}"]
    role = f"mcp_reader_{suffix}"
    credentials = {
        "host": "127.0.0.1",
        "port": port,
        "user": "postgres",
        "password": "pg-mcp-test-only",
    }
    admin = await asyncpg.connect(**credentials, database="postgres")
    configs = []
    try:
        await admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
        for index, name in enumerate(names):
            await admin.execute(f'CREATE DATABASE "{name}"')
            conn = await asyncpg.connect(**credentials, database=name)
            try:
                await conn.execute(
                    "CREATE TABLE users (id integer PRIMARY KEY, name text, ssn text);"  # noqa: S608
                    "CREATE TABLE secrets (value text);"
                    "CREATE TABLE other_table (id integer, ssn text);"
                    f"INSERT INTO users VALUES ({index + 1}, 'database-{index}', 'private');"
                    f"INSERT INTO other_table VALUES ({index + 1}, 'private');"
                    f'GRANT SELECT (id, name) ON users TO "{role}";'
                    f'GRANT SELECT ON other_table TO "{role}";'
                )
            finally:
                await conn.close()
            configs.append(
                DatabaseConfig(**credentials, name=name, min_pool_size=1, max_pool_size=2)
            )
        # Boot through the real .env -> Settings -> lifespan path, not a prebuilt
        # Settings mock. In particular, DATABASES and SECURITY_* must reach runtime.
        environment = {
            "DATABASES": json.dumps([config.model_dump() for config in configs]),
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://llm.example/v1",
            "SECURITY_BLOCKED_TABLES": '["secrets"]',
            "SECURITY_BLOCKED_COLUMNS": '["users.ssn"]',
            "SECURITY_ALLOW_EXPLAIN": str(getattr(request, "param", True)).lower(),
            "SECURITY_READONLY_ROLE": role,
            "RESILIENCE_MAX_RETRIES": "0",
            "OBSERVABILITY_METRICS_ENABLED": "false",
        }
        (tmp_path / ".env").write_text(
            "\n".join(f"{key}={value}" for key, value in environment.items()), encoding="utf-8"
        )
        state = {"sql": "SELECT id, name FROM users", "requests": [], "clients": []}

        def respond(request):
            state["requests"].append(request)
            body = json.loads(request.content)
            validation = "response_format" in body
            content = (
                json.dumps({"confidence": 93, "explanation": "matches"})
                if validation
                else state["sql"]
            )
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": content},
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                },
            )

        def make_client(**kwargs):
            client = AsyncOpenAI(
                **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
            )
            state["clients"].append(client)
            return client

        monkeypatch.setattr(server, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(sql_generator, "AsyncOpenAI", make_client)
        monkeypatch.setattr(result_validator, "AsyncOpenAI", make_client)
        async with server.lifespan(server.mcp):
            yield names, state
        assert all(client.is_closed() for client in state["clients"])
        assert server._orchestrator is None and server._pools is None
    finally:
        for name in names:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def test_real_database_routing_and_session_reset(postgres_runtime):
    names, state = postgres_runtime
    for index, name in enumerate(names):
        response = await server.query("List users", database=name)
        assert response["success"] is True
        assert response["data"]["rows"] == [{"id": index + 1, "name": f"database-{index}"}]
        assert response["tokens_used"] == 10
        assert response["confidence"] == 93
        assert response["request_id"]
        async with server._pools[name].acquire() as conn:
            assert await conn.fetchval("SELECT current_user") == "postgres"
            assert await conn.fetchval("SHOW transaction_read_only") == "off"
    assert len(state["requests"]) == 4
    assert all(
        str(request.url) == "https://llm.example/v1/chat/completions"
        for request in state["requests"]
    )
    for name in (None, "missing"):
        response = await server.query("List users", database=name)
        assert response["success"] is False
    assert len(state["requests"]) == 4


async def test_real_security_and_explain(postgres_runtime):
    names, state = postgres_runtime
    for sql in (
        "SELECT * FROM secrets",
        "SELECT u.ssn FROM users u",
        "SELECT * FROM users",
        "WITH u AS (DELETE FROM users RETURNING id) SELECT id FROM u",
        "SELECT u.id FROM users u JOIN other_table o USING (ssn)",
        "SELECT u.id FROM users u NATURAL JOIN other_table o",
        "SELECT id FROM users WHERE EXISTS (SELECT 1 WHERE ssn = 'private')",
        "EXPLAIN ANALYZE SELECT * FROM secrets",
        "EXPLAIN ANALYZE DELETE FROM users",
        'SELECT u.ssn FROM other_table AS "U", users AS u',
        'SELECT row_to_json(u) FROM users u, other_table "U"',
    ):
        state["sql"] = sql
        for return_type in ("sql", "result"):
            response = await server.query("Read data", database=names[0], return_type=return_type)
            assert response["success"] is False
            assert response["error"]["code"] == "security_violation"
            assert response["tokens_used"] == 5
    # Database role permissions still protect data when the executor is called directly.
    executor = server._orchestrator.sql_executors[names[0]]
    with pytest.raises(DatabaseError):
        await executor.execute("SELECT ssn FROM users")
    rows, _ = await executor.execute("EXPLAIN (ANALYZE FALSE) SELECT id FROM users")
    assert rows and "QUERY PLAN" in rows[0]
    state["sql"] = "EXPLAIN (ANALYZE FALSE) SELECT id FROM users"
    response = await server.query("Explain query plan", database=names[0])
    assert response["success"] is True
    assert "QUERY PLAN" in response["data"]["rows"][0]
    async with server._pools[names[0]].acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM users") == 1


@pytest.mark.parametrize("postgres_runtime", [False, True], indirect=True)
async def test_explain_policy_is_loaded_from_environment(postgres_runtime):
    names, state = postgres_runtime
    state["sql"] = "EXPLAIN (ANALYZE FALSE, FORMAT JSON) SELECT id FROM users"
    response = await server.query("Explain query", database=names[1])
    assert response["success"] is server._settings.security.allow_explain
    if response["success"]:
        assert "QUERY PLAN" in response["data"]["rows"][0]
        assert response["tokens_used"] == 10
    else:
        assert response["error"]["code"] == "security_violation"
        assert response["tokens_used"] == 5


async def test_concurrent_real_routes_and_usage_are_isolated(postgres_runtime):
    names, state = postgres_runtime
    responses = await asyncio.gather(*(server.query("List users", database=name) for name in names))
    for index, response in enumerate(responses):
        assert response["success"]
        assert response["data"]["rows"] == [{"id": index + 1, "name": f"database-{index}"}]
        assert response["tokens_used"] == 10
        assert response["token_usage_complete"] is True
    assert len({response["request_id"] for response in responses}) == 2
    prompts = [
        json.loads(request.content)["messages"][1]["content"] for request in state["requests"]
    ]
    for name in names:
        assert any(f"Database: {name}" in prompt for prompt in prompts)


@pytest.fixture
async def local_llm_endpoint():
    """A loopback-only fake provider reachable by the stdio subprocess."""
    state = {"sql": "SELECT id, name FROM users", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            content = (
                '{"confidence":93,"explanation":"matches"}'
                if "response_format" in body
                else state["sql"]
            )
            payload = json.dumps(
                {
                    "id": "test",
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
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{endpoint.server_port}/v1", state
    finally:
        await asyncio.to_thread(endpoint.shutdown)
        endpoint.server_close()
        await asyncio.to_thread(thread.join)


async def test_real_stdio_entrypoint(postgres_runtime, local_llm_endpoint, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    names, _ = postgres_runtime
    base_url, state = local_llm_endpoint
    configs = server._settings.configured_databases
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pg_mcp"],
        cwd=str(tmp_path),
        env={
            "DATABASES": json.dumps([config.model_dump() for config in configs]),
            "SECURITY_BLOCKED_TABLES": '["secrets"]',
            "SECURITY_BLOCKED_COLUMNS": '["users.ssn"]',
            "SECURITY_READONLY_ROLE": server._settings.security.readonly_role,
            "RESILIENCE_MAX_RETRIES": "0",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": base_url,
            "OBSERVABILITY_METRICS_ENABLED": "false",
        },
    )
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert any(tool.name == "query" for tool in tools.tools)
        result = await session.call_tool("query", {"question": "count", "database": "missing"})
        payload = json.loads(result.content[0].text)
        assert payload["success"] is False
        assert payload["error"]["code"] == "database_error"
        assert payload["tokens_used"] == 0 and payload["request_id"]
        assert not state["requests"]
        for index, name in enumerate(names):
            result = await session.call_tool("query", {"question": "List users", "database": name})
            payload = json.loads(result.content[0].text)
            assert payload["success"]
            assert payload["data"]["rows"] == [{"id": index + 1, "name": f"database-{index}"}]
            assert payload["tokens_used"] == 10
            assert payload["token_usage_complete"] is True
        state["sql"] = "SELECT ssn FROM users"
        result = await session.call_tool("query", {"question": "Read data", "database": names[0]})
        payload = json.loads(result.content[0].text)
        assert not payload["success"]
        assert payload["error"]["code"] == "security_violation"
        assert payload["tokens_used"] == 5
