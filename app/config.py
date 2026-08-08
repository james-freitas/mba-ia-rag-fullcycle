from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

Environment = Literal["development", "staging", "production"]
DEVELOPMENT: Environment = "development"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_chat_model: str = Field(..., alias="OPENAI_CHAT_MODEL")
    openai_embedding_model: str = Field(..., alias="OPENAI_EMBEDDING_MODEL")
    database_url: str = Field(..., alias="DATABASE_URL")

    app_env: Environment = Field(DEVELOPMENT, alias="APP_ENV")
    observability_enabled: bool = Field(False, alias="OBSERVABILITY_ENABLED")
    observability_sample_rate: float = Field(
        1.0, alias="OBSERVABILITY_SAMPLE_RATE", ge=0.0, le=1.0
    )
    # Captures the question and the final answer as debug events, and only in
    # development. Prompts, context, chunks and documents stay out of the telemetry
    # in every environment.
    observability_capture_content: bool = Field(
        False, alias="OBSERVABILITY_CAPTURE_CONTENT"
    )
    otel_service_name: str = Field("fcai-rag-api", alias="OTEL_SERVICE_NAME")
    otel_traces_exporter: str = Field("console", alias="OTEL_TRACES_EXPORTER")
    otel_exporter_otlp_endpoint: str = Field("", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_exporter_otlp_headers: str = Field("", alias="OTEL_EXPORTER_OTLP_HEADERS")

    langfuse_public_key: str = Field("", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field("", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("http://localhost:3000", alias="LANGFUSE_HOST")

    @property
    def psycopg_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
