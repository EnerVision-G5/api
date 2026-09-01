from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration de l'application, chargée depuis l'environnement / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "EnerVision API"
    environment: str = "development"
    debug: bool = False

    # Base de données
    database_url: str = "postgresql+psycopg://enervision:enervision@db:5432/enervision"

    # Sécurité / JWT
    secret_key: str = "change-me-in-env"
    access_token_expire_minutes: int = 60
    jwt_algorithm: str = "HS256"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
