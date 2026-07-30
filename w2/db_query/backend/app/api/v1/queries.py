"""Query execution and result export API endpoints."""

import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.database import get_session
from app.models.database import DatabaseConnection
from app.models.query import QueryHistory, QuerySource
from app.models.schemas import (
    ExportAssistantInput,
    ExportAssistantResponse,
    GeneratedSqlResponse,
    NaturalLanguageInput,
    QueryExportInput,
    QueryHistoryEntry,
    QueryInput,
    QueryResult,
    ResultExportInput,
)
from app.services.export import create_export_response
from app.services.export_assistant import export_assistant_service
from app.services.metadata import get_cached_metadata
from app.services.nl2sql import nl2sql_service
from app.services.query import get_query_history
from app.services.query_wrapper import execute_query_with_service
from app.services.sql_validator import SqlValidationError

router = APIRouter(prefix="/api/v1/dbs", tags=["queries"])


def to_history_entry(history: QueryHistory) -> QueryHistoryEntry:
    """Convert QueryHistory to QueryHistoryEntry schema."""
    if history.id is None:
        raise ValueError("Query history entry must be persisted before serialization")

    return QueryHistoryEntry(
        id=history.id,
        databaseName=history.database_name,
        sqlText=history.sql_text,
        executedAt=history.executed_at,
        executionTimeMs=history.execution_time_ms,
        rowCount=history.row_count,
        success=history.success,
        errorMessage=history.error_message,
        querySource=history.query_source.value,
    )


@router.post("/{name}/query", response_model=QueryResult)
async def execute_sql_query(
    name: str,
    input_data: QueryInput,
    session: Session = Depends(get_session),
) -> QueryResult:
    """
    Execute SQL query against a database.

    Args:
        name: Database connection name
        input_data: Query input with SQL
        session: Database session

    Returns:
        Query result with columns and rows
    """
    # Get connection
    statement = select(DatabaseConnection).where(
        DatabaseConnection.name == name
    )
    connection = session.exec(statement).first()

    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    # Execute query
    try:
        result = await execute_query_with_service(
            session,
            name,
            connection.db_type,
            connection.url,
            input_data.sql,
            QuerySource.MANUAL,
        )
        return result
    except SqlValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {str(e)}",
        )


@router.post("/{name}/export")
async def export_query_result(
    name: str,
    input_data: ResultExportInput,
    session: Session = Depends(get_session),
) -> Response:
    """Export a query result already available in the browser."""
    statement = select(DatabaseConnection).where(DatabaseConnection.name == name)
    connection = session.exec(statement).first()
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    return create_export_response(
        database_name=name,
        export_format=input_data.format,
        columns=[column.name for column in input_data.columns],
        rows=input_data.rows,
        requested_file_name=input_data.file_name,
    )


@router.post("/{name}/query/export")
async def execute_sql_query_and_export(
    name: str,
    input_data: QueryExportInput,
    session: Session = Depends(get_session),
) -> Response:
    """Execute a validated SELECT query and immediately download its result."""
    statement = select(DatabaseConnection).where(DatabaseConnection.name == name)
    connection = session.exec(statement).first()
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    try:
        result = await execute_query_with_service(
            session,
            name,
            connection.db_type,
            connection.url,
            input_data.sql,
            QuerySource.MANUAL,
        )
        return create_export_response(
            database_name=name,
            export_format=input_data.format,
            columns=[column.name for column in result.columns],
            rows=result.rows,
            requested_file_name=input_data.file_name,
            execution_time_ms=result.execution_time_ms,
        )
    except SqlValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Query execution failed: {exc}",
        ) from exc


@router.post(
    "/{name}/export/assistant",
    response_model=ExportAssistantResponse,
)
async def converse_with_export_assistant(
    name: str,
    input_data: ExportAssistantInput,
    session: Session = Depends(get_session),
) -> ExportAssistantResponse:
    """Plan an export through a schema-aware, multi-turn conversation."""
    statement = select(DatabaseConnection).where(DatabaseConnection.name == name)
    connection = session.exec(statement).first()
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    metadata_obj = await get_cached_metadata(session, connection.name)
    if not metadata_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Metadata not found for database '{name}'. "
                "Please refresh metadata first."
            ),
        )

    try:
        metadata = json.loads(metadata_obj.metadata_json)
        return await export_assistant_service.respond(
            messages=input_data.messages,
            metadata=metadata,
            db_type=connection.db_type,
            current_sql=input_data.current_sql,
            has_current_result=input_data.has_current_result,
        )
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load metadata: {exc}",
        ) from exc


@router.get("/{name}/history", response_model=list[QueryHistoryEntry])
async def get_query_history_for_database(
    name: str,
    limit: int = 50,
    session: Session = Depends(get_session),
) -> list[QueryHistoryEntry]:
    """
    Get query history for a database.

    Args:
        name: Database connection name
        limit: Maximum number of queries to return
        session: Database session

    Returns:
        List of query history entries
    """
    # Verify connection exists
    statement = select(DatabaseConnection).where(
        DatabaseConnection.name == name
    )
    connection = session.exec(statement).first()

    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    # Get history
    history_list = await get_query_history(session, name, limit)
    return [to_history_entry(h) for h in history_list]


@router.post("/{name}/query/natural", response_model=GeneratedSqlResponse)
async def natural_language_to_sql(
    name: str,
    input_data: NaturalLanguageInput,
    session: Session = Depends(get_session),
) -> GeneratedSqlResponse:
    """
    Convert natural language to SQL query using OpenAI.

    Args:
        name: Database connection name
        input_data: Natural language prompt
        session: Database session

    Returns:
        Generated SQL query with explanation
    """
    # Get connection
    statement = select(DatabaseConnection).where(DatabaseConnection.name == name)
    connection = session.exec(statement).first()

    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{name}' not found",
        )

    # Get metadata for context
    try:
        metadata_obj = await get_cached_metadata(session, connection.name)
        if not metadata_obj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Metadata not found for database '{name}'. Please refresh metadata first.",
            )
        metadata = json.loads(metadata_obj.metadata_json)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load metadata: {str(e)}",
        )

    # Generate SQL
    try:
        result = await nl2sql_service.generate_sql(input_data.prompt, metadata, connection.db_type)
        return GeneratedSqlResponse(
            sql=result["sql"],
            explanation=result["explanation"],
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate SQL: {str(e)}",
        )
