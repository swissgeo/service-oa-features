from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(
    env_file=(".env", ".env.default"),
    env_file_encoding="utf-8",
    enable_decoding=False,
    extra="ignore",
  )

  # OTEL configuration
  otel_sdk_disabled: bool = False
  # Instrumentation
  otel_enable_asgi: bool = True
  otel_enable_psycopg2: bool = True
  # OTLP exporter
  otel_enable_otlp_exporter: bool = True
  otel_exporter_otlp_endpoint: str = "http://localhost:4317"
  otel_exporter_otlp_headers: str = ""
  otel_exporter_otlp_insecure: bool = False
  # Metrics
  otel_enable_metrics: bool = False

  @property
  def otlp_kwargs(self) -> dict:
    return {
      "endpoint": self.otel_exporter_otlp_endpoint,
      "headers": self.otel_exporter_otlp_headers,
      "insecure": self.otel_exporter_otlp_insecure,
    }


@lru_cache
def get_settings() -> Settings:
  return Settings()
