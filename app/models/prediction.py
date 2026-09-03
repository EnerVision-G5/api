"""Modèles ORM des tables de prédiction : modele et prediction.

Reflet en lecture-écriture du schéma figé du repo infra (01_schema.sql). Ce
module ne fait pas évoluer le schéma : toute divergence avec ce fichier est un
bug à corriger ici.

Ce que la table `prediction` NE porte PAS, et que le contrat transporte
pourtant : `lower_bound_kw`, `upper_bound_kw`, `model_version` et
`generated_at`. La persistance est donc volontairement partielle (décision
d'équipe : persistance minimale sans migration). Une prédiction relue en base
ne dit pas son intervalle de confiance, et son modèle n'est identifiable que
par la jointure sur `modele`.
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
    """Registre des modèles de prédiction, miroir du Model Registry MLflow."""

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
    # Le contrat ne transporte que model_version, jamais le nom : la clé
    # unique portant sur (nom, version), une version seule ne désigne un
    # modèle que s'il n'en existe qu'un qui la porte.
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64))
    date_entrainement: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
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
    """Sortie d'un modèle pour un site et un horodatage cible."""

    __tablename__ = "prediction"
    __table_args__ = (
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
    # NOT NULL au schéma : sans modèle identifié en base, aucune prédiction ne
    # peut être écrite. C'est ce qui rend la persistance conditionnelle.
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
    ts_cible: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumption_kw_predite: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
