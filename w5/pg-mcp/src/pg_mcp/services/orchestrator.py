"""Coordinate routing, security, resilience and telemetry for every query."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, TypeVar

from asyncpg import Pool
from openai import APIConnectionError, APIStatusError

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import (
    get_request_id,
    get_tokens_used,
    is_token_usage_complete,
    request_context,
)
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)
T = TypeVar("T")


class QueryOrchestrator:
    """Use the selected database consistently throughout the request pipeline."""

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor | dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        circuit_breaker: CircuitBreaker | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        # Legacy single-executor callers remain supported only for a single pool.
        if isinstance(sql_executor, dict):
            self.sql_executors = dict(sql_executor)
        elif len(pools) <= 1:
            self.sql_executors = dict.fromkeys(pools, sql_executor)
        else:
            raise ValueError("Multiple databases require a separate executor for each database")
        if self.sql_executors.keys() != pools.keys():
            raise ValueError("Database pools and executor mappings must have exactly the same keys")
        for name, executor in self.sql_executors.items():
            if isinstance(executor, SQLExecutor) and (
                executor.pool is not pools[name] or executor.db_config.name != name
            ):
                raise ValueError(f"Database '{name}' is bound to the wrong executor")
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.metrics = metrics
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )
        self.rate_limiter = rate_limiter or MultiRateLimiter(
            query_limit=resilience_config.max_concurrent_queries,
            llm_limit=resilience_config.max_concurrent_llm_calls,
        )
        self._pending_requests = 0

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        started = time.monotonic()
        database = "__unknown__"
        status = "cancelled"
        acquired = admitted = False
        async with request_context(get_request_id()) as request_id:
            try:
                if len(request.question) > self.validation_config.max_question_length:
                    raise PgMcpError(
                        code=ErrorCode.QUESTION_TOO_LONG,
                        message="Question exceeds configured limit",
                        details={"max_length": self.validation_config.max_question_length},
                    )
                database = self._resolve_database(request.database)
                capacity = (
                    self.resilience_config.max_concurrent_queries
                    + self.resilience_config.request_queue_size
                )
                if self._pending_requests >= capacity:
                    raise RateLimitExceededError("Request queue is full")
                self._pending_requests += 1
                admitted = True
                acquired = await self.rate_limiter.query_limiter.acquire(
                    timeout=self.resilience_config.queue_timeout,
                )
                if not acquired:
                    raise RateLimitExceededError("Timed out waiting for query capacity")
                response = await self._execute(request, database, request_id)
                status = "success"
            except PgMcpError as error:
                status = error.code.value
                response = QueryResponse(
                    success=False,
                    confidence=0,
                    error=ErrorDetail(
                        code=error.code.value,
                        message=error.message,
                        details=error.details,
                    ),
                )
            except Exception:
                logger.exception("Unexpected query failure")
                status = ErrorCode.INTERNAL_ERROR.value
                response = QueryResponse(
                    success=False,
                    confidence=0,
                    error=ErrorDetail(
                        code=status,
                        message="Internal server error",
                    ),
                )
            finally:
                if acquired:
                    self.rate_limiter.query_limiter.release()
                if admitted:
                    self._pending_requests -= 1
                if self.metrics:
                    self.metrics.increment_query_request(status, database)
                    self.metrics.query_duration.observe(time.monotonic() - started)
                logger.info(
                    "Query completed",
                    extra={
                        "request_id": request_id,
                        "database": database,
                        "status": status,
                    },
                )
            response.request_id = request_id
            response.tokens_used = get_tokens_used()
            response.token_usage_complete = is_token_usage_complete()
            if not response.token_usage_complete:
                warning = (
                    "LLM usage unavailable for some responses; tokens_used is a reported subtotal"
                )
                response.warning = f"{response.warning}; {warning}" if response.warning else warning
            return response

    async def _execute(
        self, request: QueryRequest, database: str, request_id: str
    ) -> QueryResponse:
        executor = self.sql_executors.get(database)
        if executor is None:
            raise DatabaseError(f"No executor configured for database '{database}'")
        schema = self.schema_cache.get(database)
        if schema is None:
            try:
                schema = await self.schema_cache.load(database, self.pools[database])
            except Exception as error:
                raise SchemaLoadError(f"Failed to load schema for '{database}'") from error
        if self.metrics:
            age = self.schema_cache.get_cache_age(database)
            if isinstance(age, (int, float)):
                self.metrics.set_schema_cache_age(database, age)
        sql, validation, _ = await self._generate_sql_with_retry(
            request.question, schema, request_id
        )
        if request.return_type == ReturnType.SQL:
            return QueryResponse(success=True, generated_sql=sql, validation=validation)
        started = time.monotonic()
        try:
            rows, total = await self._execute_sql(executor, sql)
        finally:
            if self.metrics:
                self.metrics.observe_db_query_duration(time.monotonic() - started)
                pool = self.pools[database]
                active = pool.get_size() - pool.get_idle_size()
                if isinstance(active, int):
                    self.metrics.set_db_connections_active(database, active)
        execution_time_ms = (time.monotonic() - started) * 1000
        confidence = await self._validate_results_safely(
            request.question,
            sql,
            rows,
            total,
            request_id,
        )
        warning = None
        if not self.validation_config.enabled:
            warning = "Result validation is disabled"
        elif confidence < self.validation_config.confidence_threshold:
            warning = "Result validation failed or confidence is below the configured threshold"
        return QueryResponse(
            success=True,
            generated_sql=sql,
            validation=validation,
            confidence=confidence,
            warning=warning,
            data=QueryResult(
                columns=list(rows[0]) if rows else [],
                rows=rows,
                row_count=len(rows),
                execution_time_ms=execution_time_ms,
            ),
        )

    def _resolve_database(self, database: str | None) -> str:
        if database is not None:
            if database not in self.pools:
                raise DatabaseError(
                    f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools),
                    },
                )
            return database
        if not self.pools:
            raise DatabaseError("No databases configured")
        if len(self.pools) == 1:
            return next(iter(self.pools))
        raise DatabaseError(
            "Multiple databases available, please specify which to query",
            details={
                "available_databases": list(self.pools),
            },
        )

    @staticmethod
    def _transient(error: BaseException) -> bool:
        if isinstance(error, (LLMTimeoutError, APIConnectionError, TimeoutError, ConnectionError)):
            return True
        if isinstance(error, APIStatusError):
            return error.status_code in (408, 409, 429) or error.status_code >= 500
        if isinstance(error, PgMcpError) and error.details.get("retryable"):
            return True
        return error.__cause__ is not None and QueryOrchestrator._transient(error.__cause__)

    async def _backoff(self, attempt: int, delay: float | None = None) -> None:
        initial = self.resilience_config.retry_delay if delay is None else delay
        await asyncio.sleep(min(initial * self.resilience_config.backoff_factor**attempt, 60))

    async def _call_llm(self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        acquired = await self.rate_limiter.llm_limiter.acquire(
            timeout=self.resilience_config.queue_timeout,
        )
        if not acquired:
            raise RateLimitExceededError("Timed out waiting for LLM capacity")
        started = time.monotonic()
        tokens_before = get_tokens_used()
        called = False
        try:
            if not self.circuit_breaker.allow_request():
                raise LLMError("LLM service is temporarily unavailable (circuit breaker open)")
            called = True
            if self.metrics:
                self.metrics.increment_llm_call(operation)
            try:
                result = await call()
            except Exception:
                self.circuit_breaker.record_failure()
                raise
            self.circuit_breaker.record_success()
            return result
        finally:
            self.rate_limiter.llm_limiter.release()
            if self.metrics and called:
                self.metrics.observe_llm_latency(operation, time.monotonic() - started)
                self.metrics.increment_llm_tokens(operation, get_tokens_used() - tokens_before)

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int]:
        previous_sql = feedback = None
        for attempt in range(self.resilience_config.max_retries + 1):
            try:
                sql = await self._call_llm(
                    "generate",
                    partial(
                        self.sql_generator.generate,
                        question=question,
                        schema=schema,
                        previous_attempt=previous_sql,
                        error_feedback=feedback,
                    ),
                )
                try:
                    self.sql_validator.validate_or_raise(sql)
                except (SecurityViolationError, SQLParseError) as error:
                    if self.metrics:
                        self.metrics.increment_sql_rejected(error.code.value)
                    previous_sql, feedback = sql, str(error)
                    if attempt == self.resilience_config.max_retries:
                        raise
                    await self._backoff(attempt)
                    continue
                return sql, ValidationResult(is_valid=True, is_select=True), get_tokens_used()
            except LLMError as error:
                if attempt == self.resilience_config.max_retries or not self._transient(error):
                    raise
                await self._backoff(attempt)
            except (SecurityViolationError, SQLParseError, RateLimitExceededError):
                raise
            except Exception as error:
                raise LLMError("SQL generation failed unexpectedly") from error
        raise LLMError("SQL generation failed after all retry attempts")

    async def _execute_sql(
        self, executor: SQLExecutor, sql: str
    ) -> tuple[list[dict[str, Any]], int]:
        for attempt in range(self.resilience_config.db_retry_attempts + 1):
            try:
                return await executor.execute(sql)
            except DatabaseError as error:
                if attempt == self.resilience_config.db_retry_attempts or not self._transient(
                    error
                ):
                    raise
                await self._backoff(attempt, self.resilience_config.db_retry_delay)
        raise DatabaseError("Database retry budget exhausted")

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        if not self.validation_config.enabled:
            return 100
        for attempt in range(self.resilience_config.max_retries + 1):
            try:
                result = await self._call_llm(
                    "validate",
                    lambda: self.result_validator.validate(
                        question=question,
                        sql=sql,
                        results=results,
                        row_count=row_count,
                    ),
                )
                if (
                    result.is_acceptable is False
                    and result.confidence >= self.validation_config.confidence_threshold
                ):
                    return 0
                return result.confidence
            except Exception as error:
                if attempt < self.resilience_config.max_retries and self._transient(error):
                    await self._backoff(attempt)
                    continue
                logger.warning("Result validation unavailable", extra={"request_id": request_id})
                return 0
        return 0
