"""Modèles ORM des tables de prédiction : modele et prediction.

Reflet du schéma figé du repo infra : 01_schema.sql pour le socle,
05_prediction_contrat.sql pour les trois colonnes que le contrat réclamait.
Ce module ne fait pas évoluer le schéma, toute divergence avec ces fichiers
est un bug à corriger ici.

Deux points du schéma commandent le comportement du job et de la lecture :

- `model_version` n'est pas une colonne de `prediction`. C'est
  `modele.version`, atteint par le join sur `modele_id` : la valeur est la
  même, le service d'inférence la lisant dans le registre MLflow et
  l'entraînement l'y reposant à la promotion. La dupliquer ouvrirait la porte
  à deux valeurs divergentes sans arbitre ;
- `modele_id` est NOT NULL, ce qui garantit que ce join aboutit. La table
  `modele` est alimentée à chaque promotion par le service d'entraînement,
  jamais par l'API.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Modele(Base):
    """Registre des modèles de prédiction, miroir du Model Registry MLflow.

    Déclaré ici pour la clé étrangère de `prediction` et pour que le schéma
    des tests soit complet. L'API ne l'écrit pas : ce registre est alimenté
    par l'équipe Data.
    """

    __tablename__ = "modele"
    __table_args__ = (
        UniqueConstraint("nom", "version", name="modele_nom_version_key"),
    )

    modele_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    nom: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64))
    date_entrainement: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actif: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class Prediction(Base):
    """Point prédit pour un site et un horodatage cible."""

    __tablename__ = "prediction"
    __table_args__ = (
        # Clé du schéma v1.0, inchangée : une prévision par modèle, site et
        # instant cible. Le job repose dessus pour son idempotence, la
        # nouvelle génération remplaçant la précédente.
        UniqueConstraint(
            "modele_id",
            "site_id",
            "ts_cible",
            name="prediction_modele_id_site_id_ts_cible_key",
        ),
    )

    prediction_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    # NOT NULL : c'est par lui que model_version est atteint, et le registre
    # `modele` est alimenté à chaque promotion.
    modele_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("modele.modele_id"),
        nullable=False,
    )
    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        nullable=False,
    )
    # Instant prédit, publié comme `timestamp` par le contrat.
    ts_cible: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumption_kw_predite: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )
    # Date de production par le service d'inférence, distincte de created_at
    # qui date l'insertion. Nullable au schéma ; le job la renseigne toujours.
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lower_bound_kw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    upper_bound_kw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
