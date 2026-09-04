"""Modèle ORM de l'historique des pics de consommation simulés.

Un pic simulé est un acte, pas une mesure : quelqu'un a demandé à la source
de se comporter anormalement, à un instant, sur un site, pour une durée. La
table `mesure` en portera la conséquence — des valeurs plus hautes pendant la
fenêtre — mais rien en elle ne dira que ces valeurs ont été provoquées. Sans
cette trace, une pointe de consommation dans l'historique est indiscernable
d'une vraie, et l'entraînement du modèle apprendrait une charge qui n'a
jamais eu lieu.

La mesure relue juste après le déclenchement est conservée ici aussi, et ce
n'est pas un doublon de `mesure` : elle date de l'instant du pic, avant que
le collecteur ne passe, et elle est ce que l'appelant a vu. Si le collecteur
n'a jamais tourné sur cette fenêtre, c'est la seule preuve que le pic a
produit quelque chose.

Le schéma est porté par `alembic/versions/` : une divergence entre ce module
et les révisions est un bug à corriger ici.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Bornes de la source pour `duration_minutes`. Reprises en contrainte plutôt
# que seulement validées à l'entrée : une ligne hors bornes en base voudrait
# dire qu'un pic a été demandé que la source n'a pas pu servir.
MIN_SPIKE_MINUTES = 1
MAX_SPIKE_MINUTES = 240


class SimulationPic(Base):
    """Pic de consommation déclenché sur la source, et ce qu'il a donné."""

    __tablename__ = "simulation_pic"
    __table_args__ = (
        CheckConstraint(
            f"duration_minutes BETWEEN {MIN_SPIKE_MINUTES} AND {MAX_SPIKE_MINUTES}",
            name="simulation_pic_duration_minutes_check",
        ),
        CheckConstraint(
            "data_quality_constatee IS NULL OR data_quality_constatee IN"
            " ('good', 'partial', 'degraded', 'critical')",
            name="simulation_pic_data_quality_constatee_check",
        ),
        # L'historique se lit par site et du plus récent au plus ancien :
        # c'est la seule question qu'on pose à cette table.
        Index("idx_simulation_pic_site_date", "site_id", text("declenche_le DESC")),
    )

    simulation_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        nullable=False,
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Ce que la source a répondu, repris tel quel. `statut` vaut 'simulated'
    # quand elle a accepté ; le conserver permet de distinguer plus tard un
    # refus d'un succès sans relire les journaux.
    statut: Mapped[str] = mapped_column(String(20), nullable=False)
    evenement: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    # NULL quand l'authentification est désactivée : personne n'est
    # identifiable, et inventer un auteur serait pire que ne pas en avoir.
    declenche_par: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app_user.user_id"),
    )
    declenche_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Mesure relue immédiatement après le déclenchement. Nulles quand la
    # source s'est tue : le pic a quand même eu lieu.
    consumption_kw_constatee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    data_quality_constatee: Mapped[str | None] = mapped_column(String(10))
