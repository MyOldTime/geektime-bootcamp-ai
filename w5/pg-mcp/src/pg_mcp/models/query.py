"""Query request and response models.

This module defines data models for query requests from clients and
responses containing query results or errors.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ReturnType(StrEnum):
    """Type of return value requested by the client."""

    SQL = "sql"  # Return only the generated SQL
    RESULT = "result"  # Execute and return query results


class QueryRequest(BaseModel):
    """Query request from client containing natural language question."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description="Natural language question about the data",
    )
    database: str | None = Field(
        default=None, description="Target database name (required when multiple databases exist)"
    )
    return_type: ReturnType = Field(
        default=ReturnType.RESULT, description="Whether to return SQL or execute and return results"
    )

    @field_validator("question")
    @classmethod
    def sanitize_question(cls, v: str) -> str:
        """Sanitize and validate the question.

        Args:
            v: The input question string.

        Returns:
            str: Sanitized question.

        Raises:
            ValueError: If question is empty after stripping.
        """
        v = v.strip()
        if not v:
            raise ValueError("Question cannot be empty")
        return v


class ValidationResult(BaseModel):
    """Result of SQL validation checks."""

    is_valid: bool = Field(..., description="Whether SQL passed validation")
    is_select: bool = Field(default=False, description="Whether SQL is a SELECT statement")
    allows_data_modification: bool = Field(
        default=False, description="Whether SQL contains write operations"
    )
    uses_blocked_functions: list[str] = Field(
        default_factory=list, description="List of blocked functions found in SQL"
    )
    error_message: str | None = Field(
        default=None, description="Validation error message if invalid"
    )

    @property
    def is_safe(self) -> bool:
        """Check if SQL is safe to execute.

        Returns:
            bool: True if SQL is safe (valid, read-only, no blocked functions).
        """
        return (
            self.is_valid
            and self.is_select
            and not self.allows_data_modification
            and not self.uses_blocked_functions
        )


class ResultValidationResult(BaseModel):
    """Result of query result validation using LLM.

    This model represents the outcome of validating whether query results
    match the user's original question. It uses LLM to assess the semantic
    correctness and relevance of the results.
    """

    confidence: int = Field(
        ...,
        ge=0,
        le=100,
        description="Confidence score (0-100) that results match the question",
    )
    explanation: str = Field(..., description="Explanation of the validation assessment")
    suggestion: str | None = Field(
        default=None, description="Optional suggestion for improving the query"
    )
    is_acceptable: bool = Field(
        ..., description="Whether results are acceptable based on confidence threshold"
    )


class QueryResult(BaseModel):
    """Result data from query execution."""

    columns: list[str] = Field(default_factory=list, description="Column names in result set")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="Result rows as dicts")
    row_count: int = Field(
        default=0, ge=0, validate_default=True, description="Number of rows returned"
    )
    execution_time_ms: float = Field(default=0.0, ge=0.0, description="Query execution time in ms")

    @field_validator("row_count", mode="before")
    @classmethod
    def validate_row_count(cls, v: int, info: Any) -> int:
        """Ensure row_count matches length of rows.

        Args:
            v: The row count value.
            info: Validation info containing other fields.

        Returns:
            int: Validated row count.
        """
        # If rows exist in values, use its length
        if hasattr(info, "data") and "rows" in info.data:
            return len(info.data["rows"])
        return v

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary.

        Returns:
            dict: Dictionary representation of query result.
        """
        return self.model_dump()


class ErrorDetail(BaseModel):
    """Detailed error information."""

    code: str = Field(..., description="Error code identifier")
    message: str = Field(..., description="Human-readable error message")
    details: dict[str, Any] | None = Field(default=None, description="Additional error context")


class QueryResponse(BaseModel):
    """Complete query response to client."""

    success: bool = Field(..., description="Whether query succeeded")
    generated_sql: str | None = Field(default=None, description="Generated SQL query")
    validation: ValidationResult | None = Field(default=None, description="SQL validation results")
    data: QueryResult | None = Field(default=None, description="Query result data (if executed)")
    error: ErrorDetail | None = Field(default=None, description="Error information if failed")
    confidence: int = Field(
        default=100, ge=0, le=100, description="Confidence score of generated SQL (0-100)"
    )
    tokens_used: int = Field(
        default=0,
        ge=0,
        description="Reported LLM tokens across generation, corrections and result validation",
    )
    token_usage_complete: bool = Field(
        default=True, description="Whether every received LLM response supplied usable token usage"
    )

    request_id: str | None = None
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert response to dictionary for MCP tool return.

        Returns:
            dict: Dictionary representation compatible with MCP protocol.
        """
        return self.model_dump(mode="json", exclude_none=False)

    @model_validator(mode="after")
    def validate_response(self) -> "QueryResponse":
        """Validate the completed response, independent of field ordering."""
        if self.success and self.error is not None:
            raise ValueError("Successful response cannot contain an error")
        if not self.success:
            if self.error is None:
                raise ValueError("Error must be present when success is False")
            if self.data is not None:
                raise ValueError("Data should not be present when success is False")
        return self
