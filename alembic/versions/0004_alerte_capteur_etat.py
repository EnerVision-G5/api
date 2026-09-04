"""Alertes et santé des capteurs : les deux routes de la source sans base.

Sur les huit routes de lecture de l'API Mock, six atterrissaient déjà quelque
part — `/sites` dans `site`, `/sites/{id}/current` et `/readings` dans
`mesure`, `/simulate/spike` dans `simulation_pic`. Deux ne menaient nulle
part, et ce sont précisément celles que `mesure` ne peut pas reconstituer :

- `/api/v1/alerts` porte la valeur et le seuil qui ont déclenché l'alerte, et
  la source ne sert que les alertes ACTIVES — une alerte résolue disparaît de
  sa réponse, donc au moment même où on veut l'expliquer. D'où un journal ;
- `/api/v1/sensors/status` dit quel capteur est tombé et jusqu'à quand la
  source annonce qu'il le restera. `mesure.null_reasons` dit ce qui manquait
  sur une ligne, jamais la date de rétablissement annoncée. D'où un présent
  reposé à chaque tick, et non un journal : l'historique des pannes existe
  déjà dans `null_reasons`, une ligne par minute avec sa cause.

Revision ID: 0004_alerte_capteur_etat
Revises: 0003_simulation_pic
Create Date: 2026-09-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_alerte_capteur_etat"
down_revision: str | None = "0003_simulation_pic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALERT_SEVERITIES = ("low", "medium", "high", "critical")
ALERT_TYPES = ("spike", "threshold", "anomaly", "outage", "sensor")
SENSOR_NAMES = ("consumption", "electrical", "temperature", "humidity", "network")
SENSOR_STATUSES = ("ok", "failing")
SENSOR_OVERALL = ("ok", "degraded", "critical")


def in_clause(column: str, values: Sequence[str]) -> str:
    """Écrit un CHECK IN à partir d'une liste de valeurs admises."""
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


def upgrade() -> None:
    op.create_table(
        "alerte",
        # Identifiant de la source, stable (ALR-<site>-<epoch>). Il est la clé
        # parce que c'est lui qui rend la collecte rejouable : le poller
        # repasse chaque minute sur les alertes encore actives.
        sa.Column("alert_id", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("type_alerte", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("valeur", sa.Numeric(12, 2), nullable=True),
        sa.Column("seuil", sa.Numeric(12, 2), nullable=True),
        # Date de notre passage, distincte de `ts` qui date le déclenchement.
        sa.Column(
            "collecte_le",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("alert_id", name="alerte_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="alerte_site_id_fkey",
        ),
        sa.CheckConstraint(
            in_clause("severity", ALERT_SEVERITIES),
            name="alerte_severity_check",
        ),
        sa.CheckConstraint(
            in_clause("type_alerte", ALERT_TYPES),
            name="alerte_type_alerte_check",
        ),
    )

    op.execute("CREATE INDEX idx_alerte_site_ts ON alerte (site_id, ts DESC)")

    op.create_table(
        "capteur_etat",
        sa.Column("site_id", sa.String(20), nullable=False),
        sa.Column("capteur", sa.String(20), nullable=False),
        sa.Column("statut", sa.String(10), nullable=False),
        sa.Column("failing_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("overall", sa.String(10), nullable=False),
        sa.Column(
            "releve_le",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("site_id", "capteur", name="capteur_etat_pkey"),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["site.site_id"],
            name="capteur_etat_site_id_fkey",
        ),
        sa.CheckConstraint(
            in_clause("capteur", SENSOR_NAMES),
            name="capteur_etat_capteur_check",
        ),
        sa.CheckConstraint(
            in_clause("statut", SENSOR_STATUSES),
            name="capteur_etat_statut_check",
        ),
        sa.CheckConstraint(
            in_clause("overall", SENSOR_OVERALL),
            name="capteur_etat_overall_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("capteur_etat")
    op.drop_table("alerte")
