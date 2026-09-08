"""Fixtures de test de l'API métier.

Les endpoints de lecture (EV-11) et l'authentification (EV-12) sont testés
contre une base PostgreSQL / TimescaleDB réelle, pas contre un double : les
comportements qui comptent ici sont ceux du moteur, à savoir le tri sur une
hypertable, le décalage de pagination, le comptage exact, la traversée des
NULL et celle d'un TEXT[]. Un faux dépôt en mémoire ne prouverait rien de
tout cela.

La base est fournie par le service container du workflow CI et par
compose.test.yml en local, dans les deux cas avec l'image
timescale/timescaledb:2.17.2-pg16, celle de la vraie base.

Limite assumée : le schéma de test est construit depuis les modèles ORM
(app.models), pas par `alembic upgrade head`. Reconstruire par migration à
chaque session coûterait plus cher sans rien prouver de plus sur les
comportements testés ici. Les deux doivent rester d'accord :
test_schema_conformite verrouille les colonnes et les index, et une divergence
avec alembic/versions/ est un bug des modèles.
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

# Posé AVANT l'import de l'application : app.core.config instancie ses réglages
# à l'import et mémoïse le résultat. Un JWT_SECRET fixé plus tard arriverait
# après la mise en cache, et l'API démarrerait sans clé utilisable.
#
# Le test porte sur la valeur et pas seulement sur la présence de la clé :
# .env.example livre JWT_SECRET vide, et le conteneur de développement injecte
# ce fichier tel quel. Un setdefault verrait la variable définie, la laisserait
# vide, et toute la suite échouerait faute de clé signable. Un vrai secret déjà
# présent dans l'environnement, lui, est respecté.
if not os.environ.get("JWT_SECRET"):
    os.environ["JWT_SECRET"] = "cle-de-signature-de-test-suffisamment-longue"

# Forcé, non pas par défaut : la suite est écrite pour l'authentification
# active, et un .env local à false la ferait échouer en masse. Les tests du
# mode anonyme passent par la fixture auth_disabled.
os.environ["AUTH_ENABLED"] = "true"

# Les origines CORS sont lues au montage du middleware, donc à l'import de
# l'application : les poser plus tard n'aurait aucun effet.
ALLOWED_ORIGIN = "https://dashboard.enervision.test"
REFUSED_ORIGIN = "https://ailleurs.example"
os.environ["CORS_ALLOWED_ORIGINS"] = ALLOWED_ORIGIN

import pytest  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.energy import IngestionEtat, Mesure, MesureExclu, Site  # noqa: E402
from app.models.observation import Alerte, CapteurEtat, CapteurPanne  # noqa: E402
from app.models.prediction import Modele, Prediction  # noqa: E402
from app.models.user import LOCAL_PROVIDER, AppUser  # noqa: E402
from app.password import hash_password  # noqa: E402

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enervision:enervision@localhost:5433/enervision_test",
)

# Repères temporels du jeu de données. Horodatages fixes plutôt que relatifs à
# maintenant : un test qui dépend de l'heure courante devient un test qui
# échoue un jour sans que le code ait bougé.
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)

SITE_WITH_READINGS = "SITE001"
SITE_OTHER = "SITE002"
SITE_WITHOUT_READINGS = "SITE003"
SITE_UNKNOWN = "SITE999"

# Nombre de mesures de SITE_WITH_READINGS, base des assertions de pagination.
READINGS_COUNT = 5

# Comptes de test, miroirs des deux comptes du seed de développement
# (infra/enervision-db/dev-seed/01_dev_users.sql).
READER_USERNAME = "dev.reader"
WRITER_USERNAME = "dev.writer"
TEST_PASSWORD = "mot-de-passe-de-test"

# Identité fédérée : présente en base, sans mot de passe local. Elle ne doit
# pas pouvoir se connecter par le flux mot de passe.
FEDERATED_USERNAME = "federe.sans.mot.de.passe"
FEDERATED_PROVIDER = "keycloak"

# Compte dont le hachage a été produit avec des paramètres dépassés, pour
# observer la remise à niveau à la connexion.
LEGACY_USERNAME = "dev.parametres.depasses"

# Paramètres volontairement en dessous de ceux d'argon2-cffi, donc de ce que
# app.password produit. C'est le seul endroit du projet qui construit un
# PasswordHasher à la main : fabriquer un hachage périmé exige de sortir de
# app.password, qui n'expose que les paramètres courants.
weak_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)

UNKNOWN_USERNAME = "personne"

TOKEN_URL = "/api/v1/auth/token"

# Registre des modèles. Le contrat ne transporte que model_version, et la clé
# de `modele` porte sur (nom, version) : une version ne désigne un modèle que
# si elle est unique, ou si une seule des lignes qui la portent est active.
KNOWN_MODEL_NAME = "enervision_xgboost"
KNOWN_MODEL_VERSION = "7"

# Deux modèles portent cette version et aucun n'est actif : la résolution est
# alors ambiguë et l'archivage doit être abandonné plutôt que d'en choisir un.
AMBIGUOUS_MODEL_VERSION = "9"

# Deux la portent aussi, mais un seul est actif : c'est lui qui tranche.
ARBITRATED_MODEL_VERSION = "11"
ARBITRATED_MODEL_NAME = "enervision_lstm"

# Version qu'aucune ligne ne porte. C'est la forme que prend model_version
# quand le service sert un modèle chargé par chemin d'artefact plutôt que par
# alias : il retombe alors sur son identifiant interne.
UNKNOWN_MODEL_VERSION = "9f2c1ab4e7d0"

# Sites ajoutés le temps d'un test par la fixture extra_active_sites, pour que
# le job de prédiction rencontre les sept sites du projet. Hors du seed global,
# qui doit rester celui sur lequel les tests d'EV-11 comptent.
EXTRA_ACTIVE_SITE_IDS = ("SITE004", "SITE005", "SITE006", "SITE007")


def _sites() -> list[Site]:
    """Référentiel de test.

    location est renseignée partout : le contrat déclare SiteOut.location non
    nullable, une valeur absente ferait échouer la sérialisation. C'est
    exactement ce que corrige le seed du repo infra.
    """
    return [
        Site(
            site_id=SITE_WITH_READINGS,
            site_type="office",
            site_name="Bureau Paris La Défense",
            location="Paris, France",
            capacity_kw=Decimal("200.00"),
            status="active",
        ),
        Site(
            site_id=SITE_OTHER,
            site_type="factory",
            site_name="Usine Lyon Vénissieux",
            location="Lyon, France",
            capacity_kw=Decimal("1000.00"),
            status="active",
        ),
        Site(
            site_id=SITE_WITHOUT_READINGS,
            site_type="datacenter",
            site_name="Data Center Marseille",
            location="Marseille, France",
            capacity_kw=Decimal("800.00"),
            status="inactive",
        ),
    ]


def _users() -> list[AppUser]:
    """Comptes de test : un reader, un writer, et une identité fédérée.

    Les mots de passe sont hachés ici comme l'API les hache, jamais stockés en
    clair, y compris dans un jeu de test.
    """
    password_hash = hash_password(TEST_PASSWORD)
    return [
        AppUser(
            oauth_provider=LOCAL_PROVIDER,
            oauth_subject=READER_USERNAME,
            email="dev.reader@enervision.local",
            display_name="Dev Reader",
            role="reader",
            password_hash=password_hash,
        ),
        AppUser(
            oauth_provider=LOCAL_PROVIDER,
            oauth_subject=WRITER_USERNAME,
            email="dev.writer@enervision.local",
            display_name="Dev Writer",
            role="writer",
            password_hash=password_hash,
        ),
        AppUser(
            oauth_provider=FEDERATED_PROVIDER,
            oauth_subject=FEDERATED_USERNAME,
            email="federe@enervision.local",
            display_name="Identité fédérée",
            role="reader",
            password_hash=None,
        ),
        AppUser(
            oauth_provider=LOCAL_PROVIDER,
            oauth_subject=LEGACY_USERNAME,
            email="depasse@enervision.local",
            display_name="Hachage à remettre à niveau",
            role="reader",
            password_hash=weak_hasher.hash(TEST_PASSWORD),
        ),
    ]


def _models() -> list[Modele]:
    """Registre des modèles de test.

    Alimenté par le service d'entraînement à chaque promotion en exploitation,
    jamais par l'API. Ces lignes servent à éprouver les quatre cas de
    résolution : unique, arbitré par `actif`, ambigu, absent.
    """
    return [
        Modele(nom=KNOWN_MODEL_NAME, version=KNOWN_MODEL_VERSION, actif=True),
        Modele(nom="baseline", version=AMBIGUOUS_MODEL_VERSION, actif=False),
        Modele(nom="enervision_lstm", version=AMBIGUOUS_MODEL_VERSION, actif=False),
        Modele(nom="baseline", version=ARBITRATED_MODEL_VERSION, actif=False),
        Modele(
            nom=ARBITRATED_MODEL_NAME,
            version=ARBITRATED_MODEL_VERSION,
            actif=True,
        ),
    ]


def _readings() -> list[Mesure]:
    """Mesures de test, volontairement hétérogènes.

    Le jeu couvre les quatre situations que l'API doit laisser passer intactes :
    une mesure complète, une mesure trouée reconstruite par report de la
    dernière valeur connue, une mesure massivement trouée que rien ne permet de
    reconstruire, et une mesure reconstruite par interpolation.

    Toutes portent quality_source="etl" : leurs colonnes d'imputation sont
    renseignées, donc l'ETL est passé dessus. Les laisser au défaut "source"
    décrirait un état impossible — une mesure imputée par un ETL qui ne
    l'aurait pas qualifiée.
    """
    return [
        # Mesure complète, rien à imputer.
        Mesure(
            site_id=SITE_WITH_READINGS,
            timestamp=T0,
            consumption_kw=Decimal("120.50"),
            consumption_kwh=Decimal("2.01"),
            voltage_v=Decimal("400.10"),
            current_a=Decimal("175.30"),
            power_factor=Decimal("0.950"),
            temperature_celsius=Decimal("21.50"),
            humidity_percent=Decimal("45.20"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("120.50"),
            imputation_method="none",
            quality_source="etl",
        ),
        # Capteur de puissance muet : la brute reste NULL, l'imputée reporte
        # la dernière valeur connue.
        Mesure(
            site_id=SITE_WITH_READINGS,
            timestamp=T0 + MINUTE,
            consumption_kw=None,
            consumption_kwh=Decimal("2.00"),
            voltage_v=Decimal("399.80"),
            current_a=Decimal("174.90"),
            power_factor=Decimal("0.948"),
            temperature_celsius=Decimal("21.60"),
            humidity_percent=Decimal("45.30"),
            null_reasons=["consumption_kw:sensor_timeout"],
            data_quality="partial",
            consumption_kw_imputed=Decimal("120.50"),
            imputation_method="locf",
            quality_source="etl",
        ),
        # Perte réseau : rien d'exploitable, donc aucune imputation.
        Mesure(
            site_id=SITE_WITH_READINGS,
            timestamp=T0 + 2 * MINUTE,
            consumption_kw=None,
            consumption_kwh=None,
            voltage_v=None,
            current_a=None,
            power_factor=None,
            temperature_celsius=Decimal("21.70"),
            humidity_percent=Decimal("45.10"),
            null_reasons=[
                "consumption_kw:sensor_offline",
                "voltage_v:sensor_offline",
                "network_loss",
            ],
            data_quality="critical",
            consumption_kw_imputed=None,
            imputation_method="none",
            quality_source="etl",
        ),
        # Trou encadré par deux valeurs connues : interpolation.
        Mesure(
            site_id=SITE_WITH_READINGS,
            timestamp=T0 + 3 * MINUTE,
            consumption_kw=None,
            consumption_kwh=Decimal("2.05"),
            voltage_v=Decimal("400.00"),
            current_a=Decimal("176.00"),
            power_factor=Decimal("0.951"),
            temperature_celsius=Decimal("21.80"),
            humidity_percent=Decimal("44.90"),
            null_reasons=["consumption_kw:sensor_timeout"],
            data_quality="partial",
            consumption_kw_imputed=Decimal("123.00"),
            imputation_method="interpolation",
            quality_source="etl",
        ),
        Mesure(
            site_id=SITE_WITH_READINGS,
            timestamp=T0 + 4 * MINUTE,
            consumption_kw=Decimal("125.50"),
            consumption_kwh=Decimal("2.09"),
            voltage_v=Decimal("400.30"),
            current_a=Decimal("177.10"),
            power_factor=Decimal("0.953"),
            temperature_celsius=Decimal("21.90"),
            humidity_percent=Decimal("44.80"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("125.50"),
            imputation_method="none",
            quality_source="etl",
        ),
        # Second site, aux mêmes horodatages : vérifie que le filtre par site
        # isole bien les séries.
        Mesure(
            site_id=SITE_OTHER,
            timestamp=T0,
            consumption_kw=Decimal("640.00"),
            consumption_kwh=Decimal("10.67"),
            voltage_v=Decimal("401.00"),
            current_a=Decimal("920.40"),
            power_factor=Decimal("0.930"),
            temperature_celsius=Decimal("24.10"),
            humidity_percent=Decimal("38.50"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("640.00"),
            imputation_method="none",
            quality_source="etl",
        ),
        Mesure(
            site_id=SITE_OTHER,
            timestamp=T0 + MINUTE,
            consumption_kw=Decimal("642.20"),
            consumption_kwh=Decimal("10.70"),
            voltage_v=Decimal("401.20"),
            current_a=Decimal("922.00"),
            power_factor=Decimal("0.931"),
            temperature_celsius=Decimal("24.20"),
            humidity_percent=Decimal("38.40"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("642.20"),
            imputation_method="none",
            quality_source="etl",
        ),
    ]


def assert_error_response(payload: dict) -> None:
    """Vérifie qu'un corps d'erreur respecte le modèle ErrorResponse du contrat."""
    assert set(payload) == {"detail"}
    assert isinstance(payload["detail"], str)
    assert payload["detail"]


@pytest.fixture
def client() -> TestClient:
    """Client synchrone, pour les tests qui ne touchent pas la base."""
    return TestClient(app)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Backend unique : asyncio. Portée session pour autoriser les fixtures
    asynchrones de même portée ci-dessous."""
    return "asyncio"


@pytest.fixture(scope="session")
async def engine(anyio_backend: str):
    engine = create_async_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def seeded_database(engine) -> None:
    """Construit le schéma, le peuple, et le laisse en place.

    Portée session : les tests sont des lectures et des authentifications,
    aucun ne modifie le jeu de données, donc rien ne justifie de reconstruire
    la base à chaque test.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        # mesure est une hypertable en production : les tests lisent donc une
        # hypertable, pas une table ordinaire.
        await conn.execute(
            text(
                "SELECT create_hypertable("
                "'mesure', 'ts', chunk_time_interval => INTERVAL '7 days')"
            )
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(_sites())
        session.add_all(_users())
        session.add_all(_models())
        await session.flush()
        session.add_all(_readings())
        await session.flush()
        # Après les mesures, jamais avant. La base ne l'impose plus depuis
        # que la clé étrangère vers l'hypertable a été retirée (révision
        # 0006), mais l'ordre reste celui de l'ETL : une exclusion n'a de sens
        # que posée sur une mesure écrite, et un jeu de test qui l'inverserait
        # décrirait un état que la chaîne ne produit pas.
        session.add_all(_exclusions())
        session.add_all(_alertes())
        session.add_all(_capteurs())
        session.add_all(_pannes())
        await session.commit()


# Identifiants d'alertes du jeu de données. La source les construit stables,
# de la forme ALR-<site>-<epoch> : c'est ce qui rend la collecte rejouable.
ALERT_CRITICAL = "ALR-SITE001-1000000000"
ALERT_LOW = "ALR-SITE001-1000000060"
ALERT_OTHER_SITE = "ALR-SITE002-1000000120"

# Mesure du jeu de données écartée des agrégats, et son motif. Elle reste
# servie par les routes de lecture : c'est le consommateur qui la retire de
# ses moyennes, pas l'API qui la cache.
EXCLUDED_READING_INDEX = 2
EXCLUSION_REASON = "network_loss"


def _exclusions() -> list[MesureExclu]:
    """Une mesure écartée, pour que `excluded` ait deux valeurs à prendre.

    Une seule suffit : ce qui est éprouvé est la jointure externe, et un jeu
    où toutes les mesures seraient écartées ne distinguerait pas une jointure
    externe d'une jointure interne.
    """
    return [
        MesureExclu(
            site_id=SITE_WITH_READINGS,
            timestamp=T0 + EXCLUDED_READING_INDEX * MINUTE,
            raison=EXCLUSION_REASON,
        )
    ]


def _alertes() -> list[Alerte]:
    """Trois alertes : deux gravités sur un site, une sur un autre.

    De quoi éprouver les trois filtres du contrat sans que le jeu ne devienne
    illisible : le site, la gravité, et la nature.
    """
    return [
        Alerte(
            alert_id=ALERT_CRITICAL,
            site_id=SITE_WITH_READINGS,
            ts=T0 + MINUTE,
            severity="critical",
            type_alerte="outage",
            message="Risque de surcharge",
            valeur=Decimal("812.50"),
            seuil=Decimal("720.00"),
        ),
        Alerte(
            alert_id=ALERT_LOW,
            site_id=SITE_WITH_READINGS,
            ts=T0,
            severity="low",
            type_alerte="sensor",
            message="Capteur de température muet",
            # Une alerte de capteur n'a ni valeur ni seuil : c'est le cas qui
            # justifie que les deux colonnes soient nullables.
            valeur=None,
            seuil=None,
        ),
        Alerte(
            alert_id=ALERT_OTHER_SITE,
            site_id="SITE002",
            ts=T0 + 2 * MINUTE,
            severity="high",
            type_alerte="spike",
            message="Pic de consommation",
            valeur=Decimal("900.00"),
            seuil=Decimal("850.00"),
        ),
    ]


def _capteurs() -> list[CapteurEtat]:
    """État des cinq capteurs d'un site, dont un en panne annoncée.

    `failing_until` est la seule information que ni `mesure` ni `null_reasons`
    ne portent : la source annonce jusqu'à quand elle sera muette.
    """
    return [
        CapteurEtat(
            site_id=SITE_WITH_READINGS,
            capteur=capteur,
            statut="failing" if capteur == "temperature" else "ok",
            failing_until=(T0 + 5 * MINUTE) if capteur == "temperature" else None,
            overall="degraded",
        )
        for capteur in ("consumption", "electrical", "temperature", "humidity", "network")
    ]


async def modele_id_of(session: AsyncSession, version: str, nom: str) -> int:
    """Clé du registre pour un modèle du seed."""
    return await session.scalar(
        select(Modele.modele_id).where(Modele.nom == nom, Modele.version == version),
    )


@pytest.fixture
async def known_model_id(db_session: AsyncSession) -> int:
    """Clé du modèle actif du seed, à rattacher aux prédictions archivées."""
    return await modele_id_of(db_session, KNOWN_MODEL_VERSION, KNOWN_MODEL_NAME)


@pytest.fixture
async def extra_active_sites(db_session: AsyncSession):
    """Complète le référentiel à sept sites actifs, puis le remet en état.

    Le seed global en compte trois, dont un inactif, et les tests d'EV-11
    vérifient cette liste au site près. Les ajouts sont donc locaux et défaits
    à la sortie, prédictions rattachées comprises.
    """
    db_session.add_all(
        [
            Site(
                site_id=site_id,
                site_type="unknown",
                site_name=f"Site de test {site_id}",
                location="À synchroniser",
                capacity_kw=Decimal("500.00"),
                status="active",
            )
            for site_id in EXTRA_ACTIVE_SITE_IDS
        ],
    )
    await db_session.commit()
    yield EXTRA_ACTIVE_SITE_IDS
    await db_session.execute(
        delete(Prediction).where(Prediction.site_id.in_(EXTRA_ACTIVE_SITE_IDS)),
    )
    await db_session.execute(delete(Site).where(Site.site_id.in_(EXTRA_ACTIVE_SITE_IDS)))
    await db_session.commit()


@pytest.fixture
async def db_session(engine, seeded_database):
    """Session directe sur la base de test, pour observer ce que l'API y écrit."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def anonymous_client(engine, seeded_database) -> AsyncClient:
    """Client HTTP asynchrone sans jeton, branché sur la base de test."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_db, None)


async def obtain_token(http_client: AsyncClient, username: str) -> str:
    """Récupère un jeton par le vrai endpoint, pas en le forgeant.

    Les tests des routes protégées passent ainsi par le même chemin que le
    dashboard : si la délivrance casse, ils cassent aussi.
    """
    response = await http_client.post(
        TOKEN_URL,
        data={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
async def api_client(anonymous_client: AsyncClient) -> AsyncClient:
    """Client authentifié en reader : le cas courant depuis EV-12."""
    token = await obtain_token(anonymous_client, READER_USERNAME)
    anonymous_client.headers["Authorization"] = f"Bearer {token}"
    return anonymous_client


@pytest.fixture
async def writer_client(anonymous_client: AsyncClient) -> AsyncClient:
    """Client authentifié en writer."""
    token = await obtain_token(anonymous_client, WRITER_USERNAME)
    anonymous_client.headers["Authorization"] = f"Bearer {token}"
    return anonymous_client


@pytest.fixture
def auth_disabled(monkeypatch: pytest.MonkeyPatch):
    """Repasse AUTH_ENABLED à false le temps d'un test.

    Le cache de get_settings est vidé de part et d'autre : à l'entrée pour que
    la nouvelle valeur soit lue, à la sortie pour qu'elle ne fuite pas vers les
    tests suivants. Le vidage de sortie a lieu après la restauration de la
    variable, sinon le cache serait reconstruit sur la valeur du test.
    """
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()


# --- Indicateurs de confiance (EV-18) ---------------------------------------
#
# Ces deux sites sont hors du seed global, comme extra_active_sites, et leurs
# horodatages sont RELATIFS à maintenant — contrairement au reste du jeu de
# données, et pour une raison de fond : les indicateurs mesurent un âge, en
# comparant la donnée à l'instant de l'appel. Un jeu figé les ferait tous
# répondre « en retard » quel que soit le code.
#
# L'ancrage est fait sur des heures pleines passées : les prévisions sont
# rattachées à l'heure de leur horodatage cible, et des mesures posées à
# l'aveugle autour de « maintenant » basculeraient d'un seau à l'autre selon
# la minute d'exécution.

# Site collecté à l'instant, avec ses prévisions archivées.
FRESH_SITE = "SITE008"

# Site connu du référentiel, jamais collecté avec succès : aucune mesure, et
# un état de collecte en échec. C'est la panne que `mesure` ne peut pas dire.
NEVER_COLLECTED_SITE = "SITE009"

INDICATOR_SITE_IDS = (FRESH_SITE, NEVER_COLLECTED_SITE)

# Comptes attendus de FRESH_SITE sur une fenêtre de 24 h.
FRESH_TOTAL = 7
FRESH_DEGRADED = 2
FRESH_QUALIFIED = 6
FRESH_SENSOR_FAILURES = 1
FRESH_IMPUTED = 1

# Écart attendu : deux heures appariées, dont une seule portait un intervalle.
FRESH_PAIRED_POINTS = 2
FRESH_MAE_KW = 10.0
FRESH_BIAS_KW = 0.0
FRESH_MEAN_ACTUAL_KW = 155.0

# Échecs consécutifs de NEVER_COLLECTED_SITE, au-delà de tout à-coup.
NEVER_COLLECTED_FAILURES = 3


def _indicator_sites() -> list[Site]:
    """Les deux sites que les tests d'indicateurs ajoutent au référentiel."""
    return [
        Site(
            site_id=site_id,
            site_type="office",
            site_name=f"Site d'indicateurs {site_id}",
            location="Nantes, France",
            capacity_kw=Decimal("300.00"),
            status="active",
        )
        for site_id in INDICATOR_SITE_IDS
    ]


def _fresh_readings(now: datetime, first_hour: datetime) -> list[Mesure]:
    """Sept mesures couvrant chaque compte de l'indicateur de qualité.

    Deux heures pleines pour l'appariement avec les prévisions, plus une
    mesure posée à `now` : c'est elle qui rend le site frais, et sans elle
    aucun test ne distinguerait un site à jour d'un site en retard.
    """
    second_hour = first_hour + timedelta(hours=1)
    return [
        # Heure 1 : moyenne des puissances brutes = (100 + 110 + 120) / 3.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=first_hour + timedelta(minutes=10),
            consumption_kw=Decimal("100.00"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("100.00"),
            imputation_method="none",
            quality_source="etl",
        ),
        Mesure(
            site_id=FRESH_SITE,
            timestamp=first_hour + timedelta(minutes=20),
            consumption_kw=Decimal("110.00"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("110.00"),
            imputation_method="none",
            quality_source="etl",
        ),
        # Panne capteur déclarée par la source, dans SON vocabulaire.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=first_hour + timedelta(minutes=30),
            consumption_kw=None,
            null_reasons=["consumption_sensor_failure"],
            data_quality="critical",
            consumption_kw_imputed=None,
            imputation_method="none",
            quality_source="etl",
        ),
        # Valeur reconstruite : dégradée sans être qualifiée critical, et sans
        # motif de panne capteur. C'est le cas qu'aucun des deux autres
        # symptômes de `_is_degraded` n'attraperait.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=first_hour + timedelta(minutes=40),
            consumption_kw=None,
            null_reasons=["consumption_kw:sensor_timeout"],
            data_quality="partial",
            consumption_kw_imputed=Decimal("105.00"),
            imputation_method="locf",
            quality_source="etl",
        ),
        # Collectée mais pas encore qualifiée : data_quality vaut good parce
        # que c'est le défaut de la colonne, pas parce que l'ETL l'a établi.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=first_hour + timedelta(minutes=50),
            consumption_kw=Decimal("120.00"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=None,
            imputation_method="none",
            quality_source="source",
        ),
        # Heure 2 : une seule mesure, donc moyenne = 200.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=second_hour + timedelta(minutes=10),
            consumption_kw=Decimal("200.00"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("200.00"),
            imputation_method="none",
            quality_source="etl",
        ),
        # Heure courante, sans prévision en face : elle ne forme pas de paire.
        Mesure(
            site_id=FRESH_SITE,
            timestamp=now,
            consumption_kw=Decimal("210.00"),
            null_reasons=[],
            data_quality="good",
            consumption_kw_imputed=Decimal("210.00"),
            imputation_method="none",
            quality_source="etl",
        ),
    ]


def _fresh_predictions(modele_id: int, first_hour: datetime) -> list[Prediction]:
    """Trois prévisions, dont une seule sans mesure en face.

    La troisième est délibérément posée sur une heure vide : la jointure est
    interne, et une prévision sans mesure ne doit pas peser zéro dans une
    moyenne — elle doit ne pas exister.
    """
    return [
        # Erreur +10 kW contre une moyenne réelle de 110, dans les bornes.
        Prediction(
            modele_id=modele_id,
            site_id=FRESH_SITE,
            ts_cible=first_hour,
            consumption_kw_predite=Decimal("120.00"),
            lower_bound_kw=Decimal("100.00"),
            upper_bound_kw=Decimal("130.00"),
            generated_at=first_hour,
        ),
        # Erreur -10 kW contre 200, sans intervalle : la version servie ne
        # déclarait pas sa dispersion.
        Prediction(
            modele_id=modele_id,
            site_id=FRESH_SITE,
            ts_cible=first_hour + timedelta(hours=1),
            consumption_kw_predite=Decimal("190.00"),
            lower_bound_kw=None,
            upper_bound_kw=None,
            generated_at=first_hour,
        ),
        Prediction(
            modele_id=modele_id,
            site_id=FRESH_SITE,
            ts_cible=first_hour - timedelta(hours=1),
            consumption_kw_predite=Decimal("500.00"),
            lower_bound_kw=None,
            upper_bound_kw=None,
            generated_at=first_hour,
        ),
    ]


def _collector_states(now: datetime) -> list[IngestionEtat]:
    """Les deux états de collecte que l'indicateur doit savoir raconter."""
    return [
        IngestionEtat(
            site_id=FRESH_SITE,
            last_attempt_at=now,
            last_success_at=now,
            last_rows=1,
            last_data_lag_s=Decimal("12.50"),
            consecutive_failures=0,
            last_error=None,
            source="poller",
        ),
        # Le collecteur tourne et échoue : dernier essai récent, aucun succès.
        # Sans cette ligne, ce site serait indiscernable d'un site que
        # personne n'a jamais demandé à collecter.
        IngestionEtat(
            site_id=NEVER_COLLECTED_SITE,
            last_attempt_at=now,
            last_success_at=None,
            last_rows=0,
            last_data_lag_s=None,
            consecutive_failures=NEVER_COLLECTED_FAILURES,
            last_error="SourceError: 503 sur /current",
            source="poller",
        ),
    ]


@pytest.fixture
async def indicator_sites(db_session: AsyncSession, known_model_id: int):
    """Pose les deux sites d'indicateurs, puis les retire entièrement.

    Le nettoyage suit l'ordre des clés étrangères : prédictions et état de
    collecte d'abord, mesures ensuite, sites enfin. Les tests d'EV-11 comptent
    le référentiel au site près.
    """
    now = datetime.now(UTC)
    first_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)

    db_session.add_all(_indicator_sites())
    await db_session.flush()
    db_session.add_all(_fresh_readings(now, first_hour))
    db_session.add_all(_fresh_predictions(known_model_id, first_hour))
    db_session.add_all(_collector_states(now))
    await db_session.commit()

    yield now

    for statement in (
        delete(Prediction).where(Prediction.site_id.in_(INDICATOR_SITE_IDS)),
        delete(IngestionEtat).where(IngestionEtat.site_id.in_(INDICATOR_SITE_IDS)),
        delete(Mesure).where(Mesure.site_id.in_(INDICATOR_SITE_IDS)),
        delete(Site).where(Site.site_id.in_(INDICATOR_SITE_IDS)),
    ):
        await db_session.execute(statement)
    await db_session.commit()


# Site des recommandations. Il lui faut ses propres données : les règles
# lisent des prévisions à VENIR, et le jeu principal est figé dans le passé.
RECO_SITE = "SITE010"
RECO_CAPACITY_KW = Decimal("500.00")

# Puissances prévues, heure par heure à partir de la prochaine. Le sommet
# dépasse la capacité souscrite de 120 kW, soit plus du dixième qui fait
# basculer le délestage en critique, et les deux points hauts se suivent :
# c'est ce qui en fait une plage et non deux accidents.
RECO_FORECAST_KW = (300.0, 450.0, 620.0, 610.0, 200.0, 180.0)
RECO_PEAK_KW = 620.0
RECO_OVERRUN_KW = 120.0


def _reco_site() -> list[Site]:
    """Site dédié aux recommandations, avec une capacité franchie."""
    return [
        Site(
            site_id=RECO_SITE,
            site_type="factory",
            site_name="Usine de test des recommandations",
            location="Nantes, France",
            capacity_kw=RECO_CAPACITY_KW,
            status="active",
        )
    ]


def _reco_predictions(modele_id: int, first_hour: datetime) -> list[Prediction]:
    """Six prévisions à venir, dont deux au-dessus de la puissance souscrite."""
    return [
        Prediction(
            modele_id=modele_id,
            site_id=RECO_SITE,
            ts_cible=first_hour + timedelta(hours=offset),
            consumption_kw_predite=Decimal(str(value)),
            generated_at=first_hour,
        )
        for offset, value in enumerate(RECO_FORECAST_KW)
    ]


def _reco_readings(now: datetime) -> list[Mesure]:
    """Six mesures récentes dont quatre reconstruites par l'ETL.

    Deux tiers d'imputation : au-dessus du seuil qui tient la prévision pour
    mal fondée. C'est le déclencheur qui n'a pas besoin que la source déclare
    quoi que ce soit — celui qui attrape les pannes que personne n'a vues.
    """
    return [
        Mesure(
            site_id=RECO_SITE,
            timestamp=now - timedelta(minutes=10 * (index + 1)),
            consumption_kw=None if index < 4 else Decimal("300.00"),
            null_reasons=["consumption_sensor_failure"] if index < 4 else [],
            data_quality="degraded" if index < 4 else "good",
            consumption_kw_imputed=Decimal("300.00"),
            imputation_method="locf" if index < 4 else "none",
            quality_source="etl",
        )
        for index in range(6)
    ]


def _reco_sensors() -> list[CapteurEtat]:
    """Un capteur de consommation en panne déclarée sur le site."""
    return [
        CapteurEtat(
            site_id=RECO_SITE,
            capteur="consumption",
            statut="failing",
            failing_until=None,
            overall="degraded",
        )
    ]


@pytest.fixture
async def recommendation_site(db_session: AsyncSession, known_model_id: int):
    """Pose le site des recommandations, puis le retire entièrement.

    Le nettoyage suit l'ordre des clés étrangères, comme `indicator_sites` :
    les tests d'EV-11 comptent le référentiel au site près.
    """
    now = datetime.now(UTC)
    first_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    db_session.add_all(_reco_site())
    await db_session.flush()
    db_session.add_all(_reco_predictions(known_model_id, first_hour))
    db_session.add_all(_reco_readings(now))
    db_session.add_all(_reco_sensors())
    await db_session.commit()

    yield first_hour

    for statement in (
        delete(Prediction).where(Prediction.site_id == RECO_SITE),
        delete(CapteurEtat).where(CapteurEtat.site_id == RECO_SITE),
        delete(Mesure).where(Mesure.site_id == RECO_SITE),
        delete(Site).where(Site.site_id == RECO_SITE),
    ):
        await db_session.execute(statement)
    await db_session.commit()


# Épisodes de panne du jeu de données : un clos, un en cours. Les deux sont
# nécessaires — un jeu où toutes les pannes seraient closes ne distinguerait
# pas `ongoing` d'une constante.
PANNE_CLOSE_CAPTEUR = "humidity"
PANNE_EN_COURS_CAPTEUR = "temperature"


def _pannes() -> list[CapteurPanne]:
    """Deux épisodes sur le site principal, dont un toujours ouvert.

    Le second n'a pas de fin : c'est lui que vise l'index partiel unique, et
    c'est lui qui fait des « pannes en cours » une requête d'une ligne.
    """
    return [
        CapteurPanne(
            site_id=SITE_WITH_READINGS,
            capteur=PANNE_CLOSE_CAPTEUR,
            debut_le=T0,
            fin_le=T0 + 2 * MINUTE,
            failing_until=T0 + MINUTE,
        ),
        CapteurPanne(
            site_id=SITE_WITH_READINGS,
            capteur=PANNE_EN_COURS_CAPTEUR,
            debut_le=T0 + 3 * MINUTE,
            fin_le=None,
            failing_until=T0 + 5 * MINUTE,
        ),
    ]
