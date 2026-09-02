"""Conformité du schéma lu par l'API.

Ce test verrouille les modèles ORM d'app.models.energy : il constate les
colonnes réellement créées en base et refuse qu'on en retire une que le
contrat gelé publie, en particulier les deux colonnes d'imputation ajoutées
par EV-08 sans lesquelles EnergyReadingOut n'est pas servable.

Il ne compare pas le schéma aux scripts d'initdb du repo infra, qui restent la
source de vérité : cette comparaison demanderait un accès inter-repos que la
CI de ce repo n'a pas. Une divergence entre ces modèles et
infra/enervision-db/initdb/ est donc un bug à corriger ici.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.anyio

SITE_COLUMNS = {
    "site_id",
    "site_type",
    "site_name",
    "location",
    "capacity_kw",
    "status",
}

MESURE_COLUMNS = {
    "ts",
    "site_id",
    "consumption_kw",
    "consumption_kwh",
    "voltage_v",
    "current_a",
    "power_factor",
    "temperature_celsius",
    "humidity_percent",
    "null_reasons",
    "data_quality",
    "consumption_kw_imputed",
    "imputation_method",
    "inserted_at",
}


async def columns_of(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = :table"
            ),
            {"table": table},
        )
        return {row[0] for row in result}


async def test_schema_conformite(engine: AsyncEngine, seeded_database: None) -> None:
    assert await columns_of(engine, "site") == SITE_COLUMNS
    assert await columns_of(engine, "mesure") == MESURE_COLUMNS


async def test_imputation_method_est_contrainte(
    engine: AsyncEngine,
    seeded_database: None,
) -> None:
    """La colonne n'accepte que les valeurs de l'énumération du contrat.

    L'insertion est annulée : les autres tests lisent le même jeu de données.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO mesure (site_id, ts, imputation_method)"
                    " VALUES ('SITE001', '2026-09-02T00:00:00Z', 'moyenne_glissante')"
                )
            )
        await transaction.rollback()
