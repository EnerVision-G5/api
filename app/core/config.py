from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Longueur minimale de la clé de signature HS256. En deçà, un seul jeton
# capturé suffit à attaquer la clé par force brute hors ligne, et tous les
# jetons deviennent forgeables.
JWT_SECRET_MIN_LENGTH = 32


class JwtSecretError(RuntimeError):
    """Clé de signature absente ou trop courte : l'API ne peut pas démarrer."""


class CorsConfigurationError(RuntimeError):
    """Origines CORS inexploitables : l'API ne peut pas démarrer."""


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

    # Origines autorisées à appeler l'API depuis un navigateur, séparées par
    # des virgules. Le dashboard est servi depuis une autre origine que l'API :
    # sans cette liste, le navigateur bloque toutes ses requêtes.
    #
    # Vide par défaut, et jamais de joker : une valeur permissive livrée par
    # inadvertance rendrait l'API lisible par n'importe quel site, et le défaut
    # est justement ce qui se déploie quand personne n'a configuré.
    cors_allowed_origins: str = ""

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

    # Clé de service présentée au service d'inférence, en en-tête X-API-Key.
    # Celui-ci relaie la simulation de pic, qui écrit sur la source : il exige
    # désormais cette clé sur les routes du contrat, et refuse de démarrer
    # sans elle (voir predict/services/serving/src/serving/auth.py).
    #
    # Vide par défaut, et l'API démarre quand même : le service d'inférence
    # peut tourner avec SERVING_AUTH_ENABLED=false sur un poste de
    # développement, où aucune clé n'est distribuée. En déploiement, la même
    # valeur doit être posée des deux côtés.
    predict_api_key: str = ""

    # --- Seuils des indicateurs de confiance ---------------------------------
    #
    # En configuration et non en constantes de module : ce sont des réglages de
    # pilote, que le client ajuste après avoir vu ses propres données. Les
    # figer dans le code ferait d'un ajustement de seuil un redéploiement.
    #
    # Ils sont publiés dans la réponse des indicateurs, à côté de la valeur
    # mesurée. Sans cela, le dashboard devrait les redéclarer de son côté, et
    # deux vérités divergeraient sans que rien ne le signale — c'est exactement
    # ce qui s'est produit avec le seuil de fraîcheur, à 120 s côté front
    # contre 180 s côté collecteur.

    # Âge au-delà duquel la dernière mesure d'un site est tenue pour en
    # retard. 180 s est la valeur de `collector.lag_warning_s` du repo predict,
    # soit trois cadences manquées : en deçà, un à-coup de la source alerterait
    # sans qu'aucune donnée ne soit perdue.
    stale_threshold_seconds: float = 180.0

    # Part de mesures dégradées au-delà de laquelle la fiabilité du site est
    # tenue pour compromise. Même valeur que le DEGRADED_RATIO des règles de
    # recommandation, et pour cause : c'est le même fait.
    degraded_ratio_threshold: float = 0.20

    # Rapport toléré entre l'erreur des prévisions servies et la consommation
    # moyenne du site sur la fenêtre. Un rapport et non des kilowatts : 20 kW
    # d'écart n'ont pas le même sens sur un bureau de 200 kW et sur une usine
    # de 1000.
    drift_mae_ratio: float = 0.15


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_cors_origins() -> list[str]:
    """Découpe CORS_ALLOWED_ORIGINS en liste d'origines.

    Rend une liste vide quand rien n'est configuré : aucune origine n'est
    alors autorisée, ce qui est le comportement sûr. Un joker est refusé au
    démarrage plutôt que silencieusement accepté, voir check_cors_origins.
    """
    raw = get_settings().cors_allowed_origins
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def check_cors_origins() -> list[str]:
    """Valide la liste des origines et la retourne.

    Refuse un joker. `allow_origins=["*"]` conjugué à `allow_credentials`
    est d'ailleurs rejeté par les navigateurs, mais l'erreur apparaîtrait
    côté client sans que l'API ne signale rien : autant échouer ici.
    """
    origins = get_cors_origins()
    if any("*" in origin for origin in origins):
        raise CorsConfigurationError(
            "CORS_ALLOWED_ORIGINS ne doit pas contenir de joker : lister les"
            " origines une à une.",
        )
    return origins


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
