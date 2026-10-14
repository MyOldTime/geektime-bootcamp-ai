"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.
"""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class EnvironmentSettings(BaseSettings):
    """Read each configuration section from the environment and optional .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class DatabaseConfig(EnvironmentSettings):
    """PostgreSQL database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @field_validator("name", "host", "user")
    @classmethod
    def validate_connection_identity(cls, value: str) -> str:
        """Do not let empty connection fields select a driver/environment default."""
        if not value.strip() or "\x00" in value:
            raise ValueError("Database name, host and user must be non-empty and contain no NUL")
        return value

    @model_validator(mode="after")
    def validate_pool_bounds(self) -> Self:
        """Reject an invalid pool before any runtime resources are opened."""
        if self.min_pool_size > self.max_pool_size:
            raise ValueError("min_pool_size must not exceed max_pool_size")
        return self

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(EnvironmentSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    base_url: str = Field(
        default="https://api.openai.com/v1", description="Base URL for the OpenAI API"
    )
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(default=2000, ge=100, description="Maximum tokens in response")
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Require a key without imposing a provider-specific prefix."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        return v


class SecurityConfig(EnvironmentSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    allow_write_operations: bool = Field(
        default=False, description="Compatibility setting; this read-only service requires false"
    )
    blocked_tables: Annotated[list[str], NoDecode] = Field(default_factory=list)
    blocked_columns: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allow_explain: bool = False
    blocked_functions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("allow_write_operations")
    @classmethod
    def reject_writes(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "This server supports read-only queries; write operations cannot be enabled"
            )
        return value

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_blocked_functions(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        if isinstance(v, str):
            if v.strip().startswith(("[", "{")):
                v = json.loads(v)
            else:
                v = [f.strip() for f in v.split(",") if f.strip()]
        names = TypeAdapter(list[str]).validate_python(v)
        if any(not name.strip() for name in names):
            raise ValueError("Blocked object names must not be empty")
        return [name.strip() for name in names]


class ValidationConfig(EnvironmentSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70,
        ge=0,
        le=100,
        description="Minimum confidence for acceptable results",
        validation_alias=AliasChoices(
            "VALIDATION_CONFIDENCE_THRESHOLD",
            "VALIDATION_MIN_CONFIDENCE_SCORE",
            "confidence_threshold",
            "min_confidence_score",
        ),
    )

    @property
    def min_confidence_score(self) -> int:
        """Compatibility alias for the result confidence threshold."""
        return self.confidence_threshold


class CacheConfig(EnvironmentSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")
    auto_refresh: bool = False
    refresh_interval_minutes: int = Field(default=60, ge=1)
    startup_timeout_seconds: float = Field(default=30.0, gt=0)


class ResilienceConfig(EnvironmentSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_concurrent_queries: int = Field(default=10, ge=1)
    max_concurrent_llm_calls: int = Field(default=5, ge=1)
    request_queue_size: int = Field(default=100, ge=0)
    queue_timeout: float = Field(default=30.0, gt=0)
    db_retry_attempts: int = Field(default=3, ge=0, le=10)
    db_retry_delay: float = Field(default=1.0, ge=0.1, le=10)

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )


class ObservabilityConfig(EnvironmentSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="json", description="Log format")


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    databases: list[DatabaseConfig] = Field(default_factory=list)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @field_validator("databases", mode="before")
    @classmethod
    def require_explicit_database_names(cls, value: Any) -> Any:
        """A DATABASES entry must not silently inherit the legacy DATABASE_NAME."""
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "name" not in entry:
                    raise ValueError("Every DATABASES entry must provide an explicit name")
        return value

    @field_validator("databases")
    @classmethod
    def validate_databases(cls, value: list[DatabaseConfig]) -> list[DatabaseConfig]:
        names = [database.name for database in value]
        if len(names) != len(set(names)):
            raise ValueError("Database names must be unique")
        return value

    @property
    def configured_databases(self) -> list[DatabaseConfig]:
        """Use the explicit list, falling back to the legacy single database."""
        return self.databases or [self.database]

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None
