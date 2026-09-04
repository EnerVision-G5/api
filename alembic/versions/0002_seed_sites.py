"""Référentiel des sites : les 7 lignes attendues par les clés étrangères.

Reprend enervision-db/initdb/02_seed_sites.sql. Ce n'est pas du schéma mais
la donnée sans laquelle rien n'entre : `mesure`, `prediction` et
`ingestion_etat` référencent toutes `site.site_id`, et un collecteur qui
démarre sur une table `site` vide échoue sur sa première insertion.

SITE004 à SITE007 sont des marque-places : le collecteur les remplace par un
UPSERT depuis GET /api/v1/sites au démarrage.

Divergence assumée avec le script d'origine, qui faisait
`ON CONFLICT DO UPDATE` : ici DO NOTHING. Le script était rejoué à la main
pour forcer le référentiel ; une migration, elle, peut rencontrer une base
déjà synchronisée par le collecteur, et réécrire ses valeurs par des
marque-places serait une régression.

Revision ID: 0002_seed_sites
Revises: 0001_schema_v1
Create Date: 2026-09-04

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_seed_sites"
down_revision: str | None = "0001_schema_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEEDED_SITE_IDS = (
    "SITE001",
    "SITE002",
    "SITE003",
    "SITE004",
    "SITE005",
    "SITE006",
    "SITE007",
)


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO site (site_id, site_type, site_name, location, capacity_kw, status)
        VALUES
            ('SITE001', 'office',     'Bureau Paris La Défense', 'Paris, France',      200, 'active'),
            ('SITE002', 'factory',    'Usine Lyon Vénissieux',   'Lyon, France',      1000, 'active'),
            ('SITE003', 'datacenter', 'Data Center Marseille',   'Marseille, France',  800, 'active'),
            ('SITE004', 'unknown',    'Site 4 (à synchroniser)', 'À synchroniser',     500, 'active'),
            ('SITE005', 'unknown',    'Site 5 (à synchroniser)', 'À synchroniser',     500, 'active'),
            ('SITE006', 'unknown',    'Site 6 (à synchroniser)', 'À synchroniser',     500, 'active'),
            ('SITE007', 'unknown',    'Site 7 (à synchroniser)', 'À synchroniser',     630, 'active')
        ON CONFLICT (site_id) DO NOTHING
        """  # noqa: E501
    )


def downgrade() -> None:
    # Les lignes filles (mesure, prediction, ingestion_etat) tiennent le
    # référentiel par clé étrangère : la suppression échoue tant qu'elles
    # existent, et c'est le comportement voulu.
    sites = ", ".join(f"'{site_id}'" for site_id in SEEDED_SITE_IDS)
    op.execute(f"DELETE FROM site WHERE site_id IN ({sites})")
