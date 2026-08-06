from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_chat_model: str = Field(..., alias="OPENAI_CHAT_MODEL")
    openai_embedding_model: str = Field(..., alias="OPENAI_EMBEDDING_MODEL")
    database_url: str = Field(..., alias="DATABASE_URL")

    observability_enabled: bool = Field(False, alias="OBSERVABILITY_ENABLED")
    otel_service_name: str = Field("fcai-rag-api", alias="OTEL_SERVICE_NAME")
    otel_traces_exporter: str = Field("console", alias="OTEL_TRACES_EXPORTER")
    otel_exporter_otlp_endpoint: str = Field("", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_exporter_otlp_headers: str = Field("", alias="OTEL_EXPORTER_OTLP_HEADERS")

    @property
    def psycopg_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


settings = Settings()
