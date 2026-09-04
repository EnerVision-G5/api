"""Conformité du schéma lu par l'API.

Ce test verrouille les modèles ORM d'app.models.energy : il constate les
colonnes réellement créées en base et refuse qu'on en retire une dont un
endpoint dépend — les deux colonnes d'imputation d'EV-08 sans lesquelles
EnergyReadingOut n'est pas servable, et les deux ajouts d'EV-18 sans lesquels
les indicateurs de confiance ne veulent rien dire.

Il ne rejoue pas les migrations : le schéma testé est celui que produisent
les modèles. C'est ce qui fait de ce fichier le point de rencontre des deux
descriptions du schéma — si les modèles et alembic/versions/ divergent, l'une
des deux ne satisfera plus ces assertions.
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
    # Ajoutée par 07_mesure_quality_source.sql (EV-18) : sans elle, un `good`
    # posé par le collecteur faute de mieux et un `good` confirmé par l'ETL
    # sont le même caractère, et l'indicateur de mesures dégradées compte 0 %
    # sur une fenêtre qui n'a simplement pas encore été qualifiée.
    "quality_source",
    "inserted_at",
}

# Table de 06_ingestion_etat.sql (EV-18). Écrite par le collecteur du repo
# predict, jamais par l'API : elle porte la seule chose que `mesure` ne peut
# pas dire, un collecteur arrêté n'écrivant aucune ligne.
INGESTION_ETAT_COLUMNS = {
    "site_id",
    "last_attempt_at",
    "last_success_at",
    "last_rows",
    "last_data_lag_s",
    "consecutive_failures",
    "last_error",
    "source",
    "updated_at",
}


# Table du périmètre gelé v1.0 (01_schema.sql). L'API ne l'écrit pas : elle est
# alimentée par un analyste ou par l'ETL, et retirée des agrégats servis.
MESURE_EXCLU_COLUMNS = {
    "exclusion_id",
    "site_id",
    "ts",
    "raison",
    "exclu_par",
    "exclu_le",
}

# Index du schéma figé. Ils ne changent aucun résultat, donc aucun test
# fonctionnel ne les verrait manquer : seule leur absence sur une table qui
# grossit se ferait sentir, et trop tard.
INDEX_NAMES = {
    "idx_mesure_quality",
    "idx_mesure_imputation",
    "idx_prediction_site_ts",
    "idx_simulation_pic_site_date",
    "idx_alerte_site_ts",
    "idx_capteur_panne_site_debut",
    # Index partiel unique : au plus un épisode ouvert par capteur. Sans lui,
    # deux collecteurs concurrents en ouvriraient deux, et la clôture ne
    # saurait plus lequel fermer.
    "idx_capteur_panne_ouverte",
}

# Table de 0005_capteur_panne. `capteur_etat` ne garde que le présent, et
# `mesure.null_reasons` ne connaît que les pannes visibles sur une mesure :
# un capteur tombé puis rétabli entre deux relevés n'apparaît qu'ici.
CAPTEUR_PANNE_COLUMNS = {
    "panne_id",
    "site_id",
    "capteur",
    "debut_le",
    "fin_le",
    "failing_until",
}

# Tables de 0004_alerte_capteur_etat. Les deux seules routes de la source qui
# n'atterrissaient nulle part, et que `mesure` ne peut pas reconstituer.
ALERTE_COLUMNS = {
    "alert_id",
    "site_id",
    "ts",
    "severity",
    "type_alerte",
    "message",
    "valeur",
    "seuil",
    "collecte_le",
}

CAPTEUR_ETAT_COLUMNS = {
    "site_id",
    "capteur",
    "statut",
    "failing_until",
    "overall",
    "releve_le",
}

# Table de 0003_simulation_pic. Un pic simulé est un acte, pas une mesure :
# `mesure` en portera la conséquence sans jamais dire qu'on l'a provoquée, et
# sans cette trace une pointe provoquée serait indiscernable d'une vraie.
SIMULATION_PIC_COLUMNS = {
    "simulation_id",
    "site_id",
    "duration_minutes",
    "statut",
    "evenement",
    "message",
    "declenche_par",
    "declenche_le",
    "consumption_kw_constatee",
    "data_quality_constatee",
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

# Les trois dernières viennent de 05_prediction_contrat.sql (EV-38). Il n'y a
# PAS de colonne model_version : c'est modele.version, atteint par la jointure
# sur modele_id, et la dupliquer ouvrirait deux valeurs pour un même fait.
PREDICTION_COLUMNS = {
    "prediction_id",
    "modele_id",
    "site_id",
    "ts_cible",
    "consumption_kw_predite",
    "created_at",
    "lower_bound_kw",
    "upper_bound_kw",
    "generated_at",
}


async def test_schema_conformite(engine: AsyncEngine, seeded_database: None) -> None:
    assert await columns_of(engine, "site") == SITE_COLUMNS
    assert await columns_of(engine, "mesure") == MESURE_COLUMNS
    assert await columns_of(engine, "app_user") == APP_USER_COLUMNS
    assert await columns_of(engine, "modele") == MODELE_COLUMNS
    assert await columns_of(engine, "prediction") == PREDICTION_COLUMNS
    assert await columns_of(engine, "ingestion_etat") == INGESTION_ETAT_COLUMNS
    assert await columns_of(engine, "mesure_exclu") == MESURE_EXCLU_COLUMNS
    assert await columns_of(engine, "simulation_pic") == SIMULATION_PIC_COLUMNS
    assert await columns_of(engine, "alerte") == ALERTE_COLUMNS
    assert await columns_of(engine, "capteur_etat") == CAPTEUR_ETAT_COLUMNS
    assert await columns_of(engine, "capteur_panne") == CAPTEUR_PANNE_COLUMNS


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


async def test_quality_source_est_contrainte(
    engine: AsyncEngine,
    seeded_database: None,
) -> None:
    """La colonne n'accepte que les deux auteurs possibles de la qualification.

    Une troisième valeur ferait taire l'indicateur de couverture ETL sans
    qu'aucun compte ne bouge : `qualified` cesserait de reconnaître la ligne,
    et la fenêtre paraîtrait non qualifiée alors qu'elle l'est.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO mesure (site_id, ts, quality_source)"
                    " VALUES ('SITE001', '2026-09-02T01:00:00Z', 'dashboard')"
                )
            )
        await transaction.rollback()


async def test_la_source_de_collecte_est_contrainte(
    engine: AsyncEngine,
    seeded_database: None,
) -> None:
    """`ingestion_etat.source` n'admet que les deux points d'entrée réels.

    C'est cette contrainte qui empêche un rattrapage de se faire passer pour
    une collecte vivante : sans elle, n'importe quelle chaîne passerait, et la
    fraîcheur affichée ne voudrait plus rien dire.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO ingestion_etat"
                    " (site_id, last_attempt_at, source)"
                    " VALUES ('SITE001', '2026-09-02T00:00:00Z', 'a-la-main')"
                )
            )
        await transaction.rollback()


async def test_les_index_du_schema_existent(
    engine: AsyncEngine,
    seeded_database: None,
) -> None:
    """Les trois index du schéma figé sont bien créés.

    Deux sont partiels : ils n'indexent que les mesures dégradées et les
    mesures imputées, la trace d'audit, pas le volume courant. Aucun résultat
    ne change s'ils manquent, ce qui est exactement pourquoi ils ont besoin
    d'un test à eux.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT indexname FROM pg_indexes"
                " WHERE schemaname = 'public' AND indexname LIKE 'idx_%'"
            )
        )
        assert {row[0] for row in result} == INDEX_NAMES


async def test_une_exclusion_reference_une_mesure_existante(
    engine: AsyncEngine,
    seeded_database: None,
) -> None:
    """`mesure_exclu` ne peut pas écarter une mesure qui n'existe pas.

    C'est la clé étrangère composite (site_id, ts) vers l'hypertable qui le
    garantit. Sans elle, une exclusion posée sur un horodatage erroné
    passerait sans bruit et retirerait des agrégats... rien du tout, en
    laissant croire le contraire.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO mesure_exclu (site_id, ts, raison)"
                    " VALUES ('SITE001', '1999-01-01T00:00:00Z', 'inexistante')"
                )
            )
        await transaction.rollback()
