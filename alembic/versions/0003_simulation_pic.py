"""Historique des pics de consommation simulés.

La source expose une route qui provoque un pic sur un site. Le dashboard doit
pouvoir la déclencher, donc l'API la relaie — et une fois relayée, elle laisse
une trace : `mesure` portera des valeurs anormalement hautes pendant la
fenêtre, sans que rien en elle ne dise qu'on les a provoquées. Une pointe
provoquée et une vraie pointe seraient alors le même signal, et
l'entraînement apprendrait une charge qui n'a jamais eu lieu.

Cette table est cette trace : qui a demandé quoi, quand, pour combien de
temps, et ce que la source a servi juste après.

Revision ID: 0003_simulation_pic
Revises: 0002_seed_sites
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_simulation_pic"
down_revision: str | None = "0002_seed_sites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bornes déclarées par la source pour la durée d'un pic. Au-delà elle répond
# 422 : une ligne hors bornes en base décrirait un pic qui n'a pas pu avoir
# lieu.
MIN_SPIKE_MINUTES = 1
MAX_SPIKE_MINUTES = 240


def upgrade() -> None:
    op.create_table(
        "simulation_pic",
        sa.Column(
            "simulation_id",
            sa.BigInteger,
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("statut", sa.String(20), nullable=False),
        sa.Column("evenement", sa.String(50), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        # NULL = authentification désactivée, personne n'était identifiable.
        sa.Column("declenche_par", sa.BigInteger, nullable=True),
        sa.Column(
            "declenche_le",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("consumption_kw_constatee", sa.Numeric(10, 2), nullable=True),
        sa.Column("data_quality_constatee", sa.String(10), nullable=True),
        sa.PrimaryKeyConstraint("simulation_id", name="simulation_pic_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="simulation_pic_site_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["declenche_par"],
            ["app_user.user_id"],
            name="simulation_pic_declenche_par_fkey",
        ),
        sa.CheckConstraint(
            f"duration_minutes BETWEEN {MIN_SPIKE_MINUTES} AND {MAX_SPIKE_MINUTES}",
            name="simulation_pic_duration_minutes_check",
        ),
        # La qualité constatée est nullable : la source peut avoir accepté le
        # pic puis s'être tue sur la mesure. Le CHECK doit donc laisser passer
        # le NULL explicitement, sinon il rejetterait ce cas.
        sa.CheckConstraint(
            "data_quality_constatee IS NULL OR data_quality_constatee IN"
            " ('good', 'partial', 'degraded', 'critical')",
            name="simulation_pic_data_quality_constatee_check",
        ),
    )

    op.execute(
        "CREATE INDEX idx_simulation_pic_site_date"
        " ON simulation_pic (site_id, declenche_le DESC)"
    )


def downgrade() -> None:
    op.drop_table("simulation_pic")
