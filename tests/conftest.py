"""Fixtures de test de l'API métier.

Les endpoints de lecture (EV-11) sont testés contre une base PostgreSQL /
TimescaleDB réelle, pas contre un double : les comportements qui comptent ici
sont ceux du moteur, à savoir le tri sur une hypertable, le décalage de
pagination, le comptage exact, la traversée des NULL et celle d'un TEXT[].
Un faux dépôt en mémoire ne prouverait rien de tout cela.

La base est fournie par le service container du workflow CI et par
compose.test.yml en local, dans les deux cas avec l'image
timescale/timescaledb:2.17.2-pg16, celle de la vraie base.

Limite assumée : le schéma de test est construit depuis les modèles ORM
(app.models.energy), pas depuis les scripts d'initdb, qui vivent dans le repo
infra et ne sont pas accessibles à la CI de ce repo sans clé de déploiement
supplémentaire. Les modèles portent les mêmes colonnes et les mêmes CHECK, et
test_schema_conformite verrouille cette correspondance ; une divergence
constatée avec infra/enervision-db/initdb/ est un bug des modèles.
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.energy import Mesure, Site

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


def _readings() -> list[Mesure]:
    """Mesures de test, volontairement hétérogènes.

    Le jeu couvre les quatre situations que l'API doit laisser passer intactes :
    une mesure complète, une mesure trouée reconstruite par report de la
    dernière valeur connue, une mesure massivement trouée que rien ne permet de
    reconstruire, et une mesure reconstruite par interpolation.
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
        ),
    ]


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

    Portée session : tous les tests d'EV-11 sont des lectures, aucun ne
    modifie les données, donc rien ne justifie de reconstruire la base à
    chaque test.
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
        await session.flush()
        session.add_all(_readings())
        await session.commit()


@pytest.fixture
async def api_client(engine, seeded_database) -> AsyncClient:
    """Client HTTP asynchrone parlant à l'application, branché sur la base de test."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch):
    """Active AUTH_ENABLED le temps d'un test.

    Le cache de get_settings est vidé de part et d'autre : à l'entrée pour que
    la nouvelle valeur soit lue, à la sortie pour qu'elle ne fuite pas vers les
    tests suivants. Le vidage de sortie a lieu après le retrait de la variable,
    sinon le cache serait reconstruit sur l'ancienne valeur.
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    get_settings.cache_clear()
