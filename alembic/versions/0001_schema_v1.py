"""Schéma figé v1.0 : socle EnerVision complet.

Reprend, à l'identique, l'état obtenu par les scripts SQL du repo infra
(enervision-db/initdb/01 à 07) qui faisaient jusqu'ici office de schéma :

    01_schema.sql                 socle des six tables + hypertable + index
    03_mesure_imputation.sql      colonnes d'imputation sur mesure (EV-08)
    04_app_user_auth.sql          password_hash, rôles reader / writer (EV-12)
    05_prediction_contrat.sql     bornes et generated_at sur prediction
    06_ingestion_etat.sql         état de collecte, une ligne par site (EV-18)
    07_mesure_quality_source.sql  origine de data_quality (EV-18)

Ces sept fichiers ne créaient le schéma qu'au premier démarrage d'un conteneur
Postgres, sur un volume vide. Cette révision remplace ce chemin.

Le contenu est écrit à la main plutôt qu'autogénéré : trois choses ne vivent
pas dans Base.metadata et qu'un autogenerate perdrait — l'extension
TimescaleDB, la conversion de `mesure` en hypertable, et l'ordre dans lequel
les deux doivent arriver.

Revision ID: 0001_schema_v1
Revises:
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_schema_v1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Chunks de 7 jours : ~10k lignes/jour pour 7 sites à la minute, soit des
# chunks que l'on peut détacher ou compresser sans manipuler des mois.
CHUNK_TIME_INTERVAL = "7 days"

# Commentaires portés en base par les scripts d'origine. Ils ne sont pas
# décoratifs : le 04 s'en sert pour corriger ce que le 01, figé, raconte de
# app_user. Les perdre ferait mentir un \d+ sur une base neuve.
TABLE_COMMENTS = {
    "app_user": (
        "Utilisateurs de l'API. Un compte local porte oauth_provider = 'local', oauth_subject = "
        "son username et password_hash renseigné (ADR-009)."
    ),
    "ingestion_etat": (
        "Etat courant de la collecte, une ligne par site. Ecrite par le collecteur (poller et "
        "rattrapage), lue par l'API metier pour l'indicateur de fraicheur d'ingestion. Repond a la "
        "question que mesure.inserted_at ne peut pas trancher : la collecte a-t-elle tourne, ou "
        "n'y avait-il rien a collecter."
    ),
}

COLUMN_COMMENTS = {
    ("app_user", "oauth_provider"): (
        "Origine de l'identité. 'local' pour un compte authentifié par mot de passe contre cette "
        "table ; un nom de fournisseur pour une identité fédérée, cas non implémenté à ce jour."
    ),
    ("app_user", "oauth_subject"): (
        "Identifiant de connexion, publié tel quel dans UserOut.username et dans le claim sub du "
        "JWT. Unique par oauth_provider."
    ),
    ("app_user", "role"): (
        "Rôle applicatif, aligné sur UserOut.role du contrat gelé : reader en lecture seule, "
        "writer en écriture."
    ),
    ("app_user", "password_hash"): (
        "Hachage bcrypt du mot de passe, produit par l'API (passlib) ou par le seed de "
        "développement (pgcrypto). Jamais de mot de passe en clair. NULL pour une identité "
        "fédérée, qui ne peut alors pas se connecter par mot de passe."
    ),
    ("ingestion_etat", "last_attempt_at"): (
        "Dernier essai de collecte, abouti ou non. Compare a last_success_at : egales, la collecte "
        "va bien ; ecartees, elle tourne et echoue ; les deux figees, le collecteur lui-meme ne "
        "tourne plus."
    ),
    ("ingestion_etat", "last_success_at"): "Dernier essai abouti. NULL tant qu'aucun n'a reussi.",
    ("ingestion_etat", "last_data_lag_s"): (
        "Age de la mesure servie par la source au dernier essai reussi, en secondes, mesure par le "
        "collecteur. Distinct de inserted_at - ts, qui melange retard de source et retard "
        "d'ecriture et devient enorme sur un rattrapage sans qu'aucune panne n'existe."
    ),
    ("ingestion_etat", "consecutive_failures"): (
        "Echecs consecutifs depuis le dernier succes. Remis a zero par un succes, jamais "
        "decremente : il distingue l'a-coup de la panne installee."
    ),
    ("ingestion_etat", "source"): (
        "Point d'entree ayant ecrit la ligne : poller pour la collecte continue, backfill pour un "
        "rattrapage manuel. Un rattrapage ne doit pas se faire passer pour une collecte vivante."
    ),
    ("mesure", "consumption_kw_imputed"): (
        "Meilleure valeur exploitable : la valeur brute si elle existe, la valeur reconstruite par "
        "l'ETL sinon. NULL quand rien ne permettait de la reconstruire."
    ),
    ("mesure", "imputation_method"): (
        "Provenance de consumption_kw_imputed : none (valeur brute, ou rien à reconstruire), locf "
        "(report de la dernière valeur connue), interpolation (encadrée par deux valeurs connues)."
    ),
    ("mesure", "quality_source"): (
        "Qui a pose data_quality et null_reasons : source pour la valeur ecrite par le collecteur, "
        "qui retombe sur le defaut good quand la source se tait ; etl quand la qualification a ete "
        "deduite des donnees. Une fenetre encore en source n'est pas une fenetre saine, c'est une "
        "fenetre non qualifiee."
    ),
    ("prediction", "lower_bound_kw"): (
        "Borne basse de l'intervalle de confiance. NULL quand la version servie ne declare pas sa "
        "dispersion : pas d'intervalle, et non un intervalle de largeur nulle."
    ),
    ("prediction", "upper_bound_kw"): (
        "Borne haute de l'intervalle de confiance. NULL dans le meme cas que "
        "lower_bound_kw."
    ),
    ("prediction", "generated_at"): (
        "Horodatage de production de la prevision par le service d'inference, distinct de "
        "created_at qui date l'insertion. Publie dans PredictionPointOut.generated_at."
    ),
}


def sql_literal(value: str) -> str:
    """Littéral SQL entre quotes : COMMENT ON n'accepte pas de paramètre lié."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "site",
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("site_type", sa.String(50), nullable=False),
        sa.Column("site_name", sa.String(150), nullable=False),
        sa.Column("location", sa.String(150), nullable=True),
        sa.Column("capacity_kw", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.PrimaryKeyConstraint("site_id", name="site_pkey"),
        sa.CheckConstraint("capacity_kw > 0", name="site_capacity_kw_check"),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="site_status_check",
        ),
    )

    op.create_table(
        "app_user",
        sa.Column("user_id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column("oauth_provider", sa.String(50), nullable=False),
        sa.Column("oauth_subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(150), nullable=True),
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'reader'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        # En fin de table, l'ordre qu'a produit l'ALTER TABLE du 04 : une base
        # deja en service et une base neuve doivent etre indiscernables.
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("user_id", name="app_user_pkey"),
        sa.UniqueConstraint(
            "oauth_provider",
            "oauth_subject",
            name="app_user_oauth_provider_oauth_subject_key",
        ),
        sa.CheckConstraint(
            "role IN ('reader', 'writer')",
            name="app_user_role_check",
        ),
    )

    op.create_table(
        "mesure",
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("consumption_kw", sa.Numeric(10, 2), nullable=True),
        sa.Column("consumption_kwh", sa.Numeric(10, 2), nullable=True),
        sa.Column("voltage_v", sa.Numeric(8, 2), nullable=True),
        sa.Column("current_a", sa.Numeric(8, 2), nullable=True),
        sa.Column("power_factor", sa.Numeric(4, 3), nullable=True),
        sa.Column("temperature_celsius", sa.Numeric(5, 2), nullable=True),
        sa.Column("humidity_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "null_reasons",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "data_quality",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'good'"),
        ),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Ajouts du 03 puis du 07, dans cet ordre.
        sa.Column("consumption_kw_imputed", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "imputation_method",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
        sa.Column(
            "quality_source",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'source'"),
        ),
        # PK composite : TimescaleDB exige que toute clé unique inclue la
        # colonne de partitionnement.
        sa.PrimaryKeyConstraint("site_id", "ts", name="mesure_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="mesure_site_id_fkey",
        ),
        sa.CheckConstraint(
            "power_factor BETWEEN 0 AND 1",
            name="mesure_power_factor_check",
        ),
        sa.CheckConstraint(
            "humidity_percent BETWEEN 0 AND 100",
            name="mesure_humidity_percent_check",
        ),
        sa.CheckConstraint(
            "data_quality IN ('good', 'partial', 'degraded', 'critical')",
            name="mesure_data_quality_check",
        ),
        sa.CheckConstraint(
            "imputation_method IN ('none', 'locf', 'interpolation')",
            name="mesure_imputation_method_check",
        ),
        sa.CheckConstraint(
            "quality_source IN ('source', 'etl')",
            name="mesure_quality_source_check",
        ),
    )

    # Conversion avant mesure_exclu, comme dans 01_schema.sql. TimescaleDB
    # 2.17 accepte aussi l'ordre inverse ; c'est la fidélité au script
    # d'origine qui fixe celui-ci, pas une contrainte du moteur.
    op.execute(
        "SELECT create_hypertable('mesure', 'ts', "
        f"chunk_time_interval => INTERVAL '{CHUNK_TIME_INTERVAL}')"
    )

    op.execute(
        "CREATE INDEX idx_mesure_quality ON mesure (data_quality, ts DESC) "
        "WHERE data_quality <> 'good'"
    )
    op.execute(
        "CREATE INDEX idx_mesure_imputation ON mesure (site_id, ts DESC) "
        "WHERE imputation_method <> 'none'"
    )

    op.create_table(
        "mesure_exclu",
        sa.Column(
            "exclusion_id",
            sa.BigInteger,
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raison", sa.Text(), nullable=False),
        sa.Column("exclu_par", sa.BigInteger, nullable=True),
        sa.Column(
            "exclu_le",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("exclusion_id", name="mesure_exclu_pkey"),
        sa.UniqueConstraint("site_id", "ts", name="mesure_exclu_site_id_ts_key"),
        # FK vers une hypertable : TimescaleDB >= 2.16.
        sa.ForeignKeyConstraint(
            ["site_id", "ts"],
            ["mesure.site_id", "mesure.ts"],
            name="mesure_exclu_site_id_ts_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["exclu_par"],
            ["app_user.user_id"],
            name="mesure_exclu_exclu_par_fkey",
        ),
    )

    op.create_table(
        "modele",
        sa.Column(
            "modele_id",
            sa.BigInteger,
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("nom", sa.String(100), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
        sa.Column("date_entrainement", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "actif",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("modele_id", name="modele_pkey"),
        sa.UniqueConstraint("nom", "version", name="modele_nom_version_key"),
    )

    op.create_table(
        "prediction",
        sa.Column(
            "prediction_id",
            sa.BigInteger,
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("modele_id", sa.BigInteger, nullable=False),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("ts_cible", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumption_kw_predite", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Ajouts du 05, en fin de table.
        sa.Column("lower_bound_kw", sa.Numeric(10, 2), nullable=True),
        sa.Column("upper_bound_kw", sa.Numeric(10, 2), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("prediction_id", name="prediction_pkey"),
        sa.UniqueConstraint(
            "modele_id",
            "site_id",
            "ts_cible",
            name="prediction_modele_id_site_id_ts_cible_key",
        ),
        sa.ForeignKeyConstraint(
            ["modele_id"],
            ["modele.modele_id"],
            name="prediction_modele_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="prediction_site_id_fkey",
        ),
    )

    op.execute(
        "CREATE INDEX idx_prediction_site_ts ON prediction (site_id, ts_cible DESC)"
    )

    op.create_table(
        "ingestion_etat",
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_rows",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_data_lag_s", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("site_id", name="ingestion_etat_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="ingestion_etat_site_id_fkey",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ingestion_etat_consecutive_failures_check",
        ),
        sa.CheckConstraint(
            "source IN ('poller', 'backfill')",
            name="ingestion_etat_source_check",
        ),
    )

    for table, comment in TABLE_COMMENTS.items():
        op.execute(f"COMMENT ON TABLE {table} IS {sql_literal(comment)}")

    for (table, column), comment in COLUMN_COMMENTS.items():
        op.execute(
            f"COMMENT ON COLUMN {table}.{column} IS {sql_literal(comment)}"
        )


def downgrade() -> None:
    # Ordre inverse des dépendances. L'extension timescaledb n'est pas
    # retirée : elle peut servir à d'autres schémas de la même instance.
    op.drop_table("ingestion_etat")
    op.drop_table("prediction")
    op.drop_table("modele")
    op.drop_table("mesure_exclu")
    op.drop_table("mesure")
    op.drop_table("app_user")
    op.drop_table("site")
