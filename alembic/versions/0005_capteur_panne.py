"""Journal des pannes de capteur, bornées par un début et une fin.

`capteur_etat` ne garde que le présent : elle répond « quel capteur est tombé
maintenant », jamais « quand est-il tombé la semaine dernière ». Et
`mesure.null_reasons`, qui portait jusqu'ici l'historique, ne connaît que les
pannes visibles **sur une mesure** : un capteur tombé puis rétabli entre deux
relevés n'y laisse rien, et la date de rétablissement annoncée par la source
n'y figure jamais.

Un épisode par (site, capteur, début), plutôt qu'un état empilé à chaque
tick : deux lignes suffisent à borner une panne là où empiler en produirait
une par capteur et par minute.

Revision ID: 0005_capteur_panne
Revises: 0004_alerte_capteur_etat
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_capteur_panne"
down_revision: str | None = "0004_alerte_capteur_etat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SENSOR_NAMES = ("consumption", "electrical", "temperature", "humidity", "network")


def upgrade() -> None:
    listed = ", ".join(f"'{name}'" for name in SENSOR_NAMES)
    op.create_table(
        "capteur_panne",
        sa.Column(
            "panne_id",
            sa.BigInteger,
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("capteur", sa.String(20), nullable=False),
        # Instant où le collecteur a CONSTATÉ la panne, pas celui où elle a
        # commencé : la source dit `failing` au présent, jamais depuis quand.
        sa.Column("debut_le", sa.DateTime(timezone=True), nullable=False),
        # NULL tant que la panne dure : c'est ce que vise l'index partiel
        # ci-dessous, et ce qui fait des « pannes en cours » une requête d'une
        # ligne.
        sa.Column("fin_le", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failing_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("panne_id", name="capteur_panne_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="capteur_panne_site_id_fkey",
        ),
        sa.UniqueConstraint(
            "site_id",
            "capteur",
            "debut_le",
            name="capteur_panne_site_id_capteur_debut_le_key",
        ),
        sa.CheckConstraint(
            f"capteur IN ({listed})",
            name="capteur_panne_capteur_check",
        ),
        # Une panne se termine après avoir commencé. La contrainte paraît
        # gratuite jusqu'au jour où une horloge recule.
        sa.CheckConstraint(
            "fin_le IS NULL OR fin_le >= debut_le",
            name="capteur_panne_fin_le_check",
        ),
    )

    op.execute(
        "CREATE INDEX idx_capteur_panne_site_debut"
        " ON capteur_panne (site_id, debut_le DESC)"
    )
    # Au plus un épisode ouvert par capteur. Sans cet index partiel, deux
    # collecteurs concurrents en ouvriraient deux, et la clôture ne saurait
    # plus lequel fermer.
    op.execute(
        "CREATE UNIQUE INDEX idx_capteur_panne_ouverte"
        " ON capteur_panne (site_id, capteur) WHERE fin_le IS NULL"
    )


def downgrade() -> None:
    op.drop_table("capteur_panne")
