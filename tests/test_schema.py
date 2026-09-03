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

APP_USER_COLUMNS = {
    "user_id",
    "oauth_provider",
    "oauth_subject",
    "email",
    "display_name",
    "role",
    # Ajoutée par 04_app_user_auth.sql (EV-12) : sans elle, aucun compte local
    # ne peut s'authentifier.
    "password_hash",
    "created_at",
    "last_login_at",
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


MODELE_COLUMNS = {
    "modele_id",
    "nom",
    "version",
    "mlflow_run_id",
    "date_entrainement",
    "actif",
    "created_at",
}

# Ce que cette table NE porte PAS, alors que le contrat de prédiction le
# transporte : model_version, generated_at, lower_bound_kw, upper_bound_kw.
# L'archivage est donc partiel, décision d'équipe assumée (EV-38).
PREDICTION_COLUMNS = {
    "prediction_id",
    "modele_id",
    "site_id",
    "ts_cible",
    "consumption_kw_predite",
    "created_at",
}


async def test_schema_conformite(engine: AsyncEngine, seeded_database: None) -> None:
    assert await columns_of(engine, "site") == SITE_COLUMNS
    assert await columns_of(engine, "mesure") == MESURE_COLUMNS
    assert await columns_of(engine, "app_user") == APP_USER_COLUMNS
    assert await columns_of(engine, "modele") == MODELE_COLUMNS
    assert await columns_of(engine, "prediction") == PREDICTION_COLUMNS


async def test_role_est_contraint(engine: AsyncEngine, seeded_database: None) -> None:
    """app_user.role n'accepte que les valeurs de UserOut.role du contrat.

    Les anciennes valeurs du schéma v1.0 sont refusées : c'est ce que la
    migration 04_app_user_auth.sql a converti.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO app_user"
                    " (oauth_provider, oauth_subject, email, role)"
                    " VALUES ('local', 'refuse', 'refuse@x.io', 'viewer')"
                )
            )
        await transaction.rollback()


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
