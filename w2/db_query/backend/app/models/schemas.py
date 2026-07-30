"""API request/response schemas with camelCase aliases."""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# Database Connection Schemas
class DatabaseConnectionInput(BaseModel):
    """Input schema for creating/updating database connection."""

    url: str = Field(..., description="Database connection URL (PostgreSQL or MySQL)")
    db_type: str | None = Field(
        default=None,
        alias="dbType",
        description=(
            "Database type (postgresql or mysql). "
            "Auto-detected from URL if not provided."
        ),
    )
    description: str | None = Field(default=None, max_length=200)


class DatabaseConnectionResponse(BaseModel):
    """Response schema for database connection."""

    name: str
    url: str
    db_type: str = Field(..., alias="dbType")
    description: str | None
    created_at: datetime
    updated_at: datetime
    last_connected_at: datetime | None
    status: str


# Metadata Schemas
class ColumnMetadata(BaseModel):
    """Column metadata schema."""

    name: str = Field(..., max_length=63)
    data_type: str = Field(..., alias="dataType")
    nullable: bool
    primary_key: bool = Field(..., alias="primaryKey")
    unique: bool = False
    default_value: str | None = Field(default=None, alias="defaultValue")
    comment: str | None = None


class TableMetadata(BaseModel):
    """Table/View metadata schema."""

    name: str = Field(..., max_length=63)
    type: Literal["table", "view"]
    columns: list[ColumnMetadata]
    row_count: int | None = Field(default=None, alias="rowCount")
    schema_name: str = Field(default="public", alias="schemaName")


class DatabaseMetadataResponse(BaseModel):
    """Response schema for database metadata."""

    database_name: str = Field(..., alias="databaseName")
    tables: list[TableMetadata]
    views: list[TableMetadata]
    fetched_at: datetime = Field(..., alias="fetchedAt")
    is_stale: bool = Field(..., alias="isStale")


# Query Schemas
class QueryInput(BaseModel):
    """Input schema for SQL query execution."""

    sql: str = Field(..., min_length=1, description="SQL SELECT query to execute")


class QueryColumn(BaseModel):
    """Query result column schema."""

    name: str
    data_type: str = Field(..., alias="dataType")


class QueryResult(BaseModel):
    """Query result response schema."""

    columns: list[QueryColumn]
    rows: list[dict[str, Any]]
    row_count: int = Field(..., alias="rowCount")
    execution_time_ms: int = Field(..., alias="executionTimeMs")
    sql: str


class ExportFormat(str, Enum):
    """Supported query result export formats."""

    CSV = "csv"
    JSON = "json"


class ExportOptions(BaseModel):
    """Shared options for downloadable result files."""

    format: ExportFormat
    file_name: str | None = Field(
        default=None,
        alias="fileName",
        max_length=100,
        pattern=r"^[^/\\\r\n]+$",
    )


class QueryExportInput(QueryInput, ExportOptions):
    """Execute a SQL query and immediately export its result."""


class ResultExportInput(ExportOptions):
    """Export a result already returned by the query API."""

    columns: list[QueryColumn] = Field(..., max_length=200)
    rows: list[dict[str, Any]] = Field(..., max_length=1000)


class ExportAssistantMessage(BaseModel):
    """One turn in an export assistant conversation."""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=1000)


class ExportAssistantInput(BaseModel):
    """Conversation and page context sent to the export assistant."""

    messages: list[ExportAssistantMessage] = Field(
        ...,
        min_length=1,
        max_length=20,
    )
    current_sql: str | None = Field(
        default=None,
        alias="currentSql",
        max_length=10000,
    )
    has_current_result: bool = Field(default=False, alias="hasCurrentResult")


class ExportAssistantAction(str, Enum):
    """Actions the export assistant can ask the browser to perform."""

    CHAT = "chat"
    EXPORT_CURRENT = "export_current"
    EXECUTE_EXPORT = "execute_export"


class ExportAssistantResponse(BaseModel):
    """Structured conversational response and optional export action."""

    message: str
    action: ExportAssistantAction = ExportAssistantAction.CHAT
    format: ExportFormat | None = None
    sql: str | None = None


class QueryHistoryEntry(BaseModel):
    """Query history entry schema."""

    id: int
    database_name: str = Field(..., alias="databaseName")
    sql_text: str = Field(..., alias="sqlText")
    executed_at: datetime = Field(..., alias="executedAt")
    execution_time_ms: int | None = Field(None, alias="executionTimeMs")
    row_count: int | None = Field(None, alias="rowCount")
    success: bool
    error_message: str | None = Field(None, alias="errorMessage")
    query_source: str = Field(..., alias="querySource")


# Natural Language Schemas
class NaturalLanguageInput(BaseModel):
    """Input schema for natural language to SQL conversion."""

    prompt: str = Field(..., min_length=5, max_length=500)


class GeneratedSqlResponse(BaseModel):
    """Response schema for generated SQL."""

    sql: str
    explanation: str


# Error Schema
class ErrorResponse(BaseModel):
    """Error response schema."""

    error: dict[str, Any]
