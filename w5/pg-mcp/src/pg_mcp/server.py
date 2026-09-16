"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool. It includes complete lifespan management for
initializing and cleaning up all components.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool, PostgresConnectionError
from mcp.server.fastmcp import FastMCP

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pool
from pg_mcp.models.query import ErrorDetail, QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Create a routed runtime and release every resource on failure or shutdown."""
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter
    clients = []
    _pools = {}
    try:
        _settings = Settings()
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )
        for config in _settings.configured_databases:
            for attempt in range(_settings.resilience.db_retry_attempts + 1):
                try:
                    _pools[config.name] = await create_pool(config)
                    break
                except (OSError, PostgresConnectionError):
                    if attempt == _settings.resilience.db_retry_attempts:
                        raise
                    await asyncio.sleep(
                        min(
                            _settings.resilience.db_retry_delay
                            * _settings.resilience.backoff_factor**attempt,
                            60,
                        )
                    )
        _schema_cache = SchemaCache(_settings.cache)
        for name, pool in _pools.items():
            async with asyncio.timeout(_settings.cache.startup_timeout_seconds):
                await _schema_cache.load(name, pool)
        if _settings.cache.auto_refresh:
            await _schema_cache.start_auto_refresh(
                _settings.cache.refresh_interval_minutes,
                _pools,
            )
        _metrics = MetricsCollector() if _settings.observability.metrics_enabled else None
        sql_generator = SQLGenerator(_settings.openai)
        clients.append(sql_generator.client)
        result_validator = ResultValidator(_settings.openai, _settings.validation)
        clients.append(result_validator.client)
        executors = {
            config.name: SQLExecutor(
                _pools[config.name], _settings.security, config, metrics=_metrics
            )
            for config in _settings.configured_databases
        }
        _circuit_breaker = CircuitBreaker(
            failure_threshold=_settings.resilience.circuit_breaker_threshold,
            recovery_timeout=_settings.resilience.circuit_breaker_timeout,
        )
        _rate_limiter = MultiRateLimiter(
            query_limit=_settings.resilience.max_concurrent_queries,
            llm_limit=_settings.resilience.max_concurrent_llm_calls,
        )
        _orchestrator = QueryOrchestrator(
            sql_generator=sql_generator,
            sql_validator=SQLValidator(_settings.security),
            sql_executor=executors,
            result_validator=result_validator,
            schema_cache=_schema_cache,
            pools=_pools,
            resilience_config=_settings.resilience,
            validation_config=_settings.validation,
            circuit_breaker=_circuit_breaker,
            rate_limiter=_rate_limiter,
            metrics=_metrics,
        )
        if _metrics:
            _metrics.start_metrics_server(_settings.observability.metrics_port)
        logger.info("Server ready", extra={"databases": list(_pools)})
        yield
    finally:
        _orchestrator = None
        try:
            if _schema_cache is not None:
                try:
                    await _schema_cache.stop_auto_refresh()
                except Exception:
                    logger.exception("Failed to stop schema refresh")
            for client in clients:
                try:
                    await client.close()
                except Exception:
                    logger.exception("Failed to close LLM client")
            if _pools:
                await close_pools(_pools, timeout=5.0)
        finally:
            if _metrics is not None:
                await asyncio.to_thread(_metrics.stop_metrics_server)
            _settings = _pools = _schema_cache = _metrics = None
            _circuit_breaker = _rate_limiter = None
        logger.info("Server shutdown complete")


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed

    Examples:
        >>> # Get query results
        >>> result = await query(
        ...     question="How many active users are there?",
        ...     return_type="result"
        ... )
        >>> print(result["data"]["rows"])

        >>> # Get SQL only
        >>> result = await query(
        ...     question="Count all products",
        ...     return_type="sql"
        ... )
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
    """
    started = time.monotonic()
    async with request_context() as request_id:
        error = None
        if _orchestrator is None:
            error = ErrorDetail(code="SERVER_NOT_INITIALIZED", message="Server is not initialized")
        elif return_type not in ("sql", "result"):
            error = ErrorDetail(
                code="INVALID_PARAMETER", message="return_type must be sql or result"
            )
        else:
            try:
                request = QueryRequest(
                    question=question,
                    database=database,
                    return_type=ReturnType(return_type),
                )
            except ValueError:
                error = ErrorDetail(code="INVALID_REQUEST", message="Invalid request parameters")
        if error is not None:
            if _metrics:
                _metrics.increment_query_request("invalid_request", "__unknown__")
                _metrics.query_duration.observe(time.monotonic() - started)
            return QueryResponse(
                success=False,
                error=error,
                confidence=0,
                request_id=request_id,
            ).to_dict()
        response = await _orchestrator.execute_query(request)
        return response.to_dict()


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)
