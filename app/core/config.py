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

    # Base de données — dialecte asyncpg (SQLAlchemy 2 asynchrone).
    # La valeur réelle vient toujours de DATABASE_URL dans l'environnement :
    # le défaut ci-dessous ne vaut que pour le poste de développement, où le
    # service Postgres du compose porte ces identifiants.
    database_url: str = "postgresql+asyncpg://enervision:enervision@db:5432/enervision"

    # Authentification. false (défaut) : les lectures sont servies en anonyme.
    # true : elles répondent 501 tant qu'EV-12 n'a pas livré la vérification du
    # jeton, ce qui évite de laisser croire à une protection inexistante.
    auth_enabled: bool = False

    # Sécurité / JWT
    secret_key: str = "change-me-in-env"
    access_token_expire_minutes: int = 60
    jwt_algorithm: str = "HS256"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
