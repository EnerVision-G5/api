from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Longueur minimale de la clé de signature HS256. En deçà, un seul jeton
# capturé suffit à attaquer la clé par force brute hors ligne, et tous les
# jetons deviennent forgeables.
JWT_SECRET_MIN_LENGTH = 32


class JwtSecretError(RuntimeError):
    """Clé de signature absente ou trop courte : l'API ne peut pas démarrer."""


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

    # Authentification. true (défaut) : les endpoints protégés exigent un JWT
    # valide. false : ils passent en anonyme, ce qui reste utile au
    # développement local et aux tests, mais ne doit jamais être déployé.
    auth_enabled: bool = True

    # Clé de signature des JWT. Volontairement sans valeur par défaut : elle
    # vient de JWT_SECRET dans l'environnement et n'est jamais versionnée.
    # L'application refuse de démarrer si elle est absente ou trop courte,
    # voir get_jwt_secret ci-dessous.
    jwt_secret: str = ""
    access_token_expire_minutes: int = 60
    jwt_algorithm: str = "HS256"

    # Service d'inférence (Serving). Le job de prédiction appelle
    # POST {predict_url}/api/v1/predict : le chemin vient du contrat de
    # predict et n'est donc pas configurable, seule l'adresse du service
    # l'est. Sans valeur, le job échoue sur chaque site en le disant.
    predict_url: str = ""

    # Profondeur demandée à chaque exécution, dans les bornes du contrat de
    # predict (1 à 48).
    predict_horizon_hours: int = 24

    # Délai total par site. Plus généreux que pour une requête interactive :
    # le job n'a personne qui l'attend, et une inférence est plus lente qu'une
    # lecture en base.
    predict_timeout_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_jwt_secret() -> str:
    """Retourne la clé de signature après l'avoir validée.

    Appelée au démarrage de l'application pour échouer tôt et bruyamment, et
    de nouveau à chaque signature ou vérification de jeton : aucun chemin de
    code ne doit pouvoir se rabattre sur une clé faible ou vide, y compris si
    la configuration est rechargée en cours de vie du processus.
    """
    secret = get_settings().jwt_secret
    if not secret:
        raise JwtSecretError(
            "JWT_SECRET est absent. Générer une valeur puis la placer dans"
            " l'environnement : "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"',
        )
    if len(secret) < JWT_SECRET_MIN_LENGTH:
        raise JwtSecretError(
            f"JWT_SECRET fait {len(secret)} caractères,"
            f" {JWT_SECRET_MIN_LENGTH} au minimum.",
        )
    return secret


settings = get_settings()
