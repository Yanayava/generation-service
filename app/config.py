from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gen:gen@localhost:5432/gen"
    webhook_secret: str = "dev-secret"
    provider_timeout: int = 60
    provider_http_timeout: float = 5.0
    dispatch_grace_seconds: float = 15.0
    watchdog_interval_seconds: float = 1.0
    provider_alpha_url: str = "http://localhost:8001/alpha"
    provider_beta_url: str = "http://localhost:8001/beta"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def grace_outlasts_http(self) -> Settings:
        if self.dispatch_grace_seconds <= self.provider_http_timeout:
            raise ValueError("DISPATCH_GRACE_SECONDS must be greater than PROVIDER_HTTP_TIMEOUT")
        if self.provider_timeout <= 0:
            raise ValueError("PROVIDER_TIMEOUT must be positive")
        return self

    def provider_url(self, name: str) -> str:
        urls = {"alpha": self.provider_alpha_url, "beta": self.provider_beta_url}
        try:
            return urls[name]
        except KeyError as exc:
            raise ValueError(f"unknown provider {name}") from exc


settings = Settings()
