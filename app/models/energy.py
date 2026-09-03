"""Modèles ORM des tables lues par l'API métier : site et mesure.

Ces classes sont le REFLET en lecture du schéma figé, dont la source de vérité
est le repo infra (enervision-db/initdb/01_schema.sql pour le socle,
03_mesure_imputation.sql pour les colonnes d'imputation ajoutées par EV-08,
06_ingestion_etat.sql et 07_mesure_quality_source.sql pour les indicateurs de
confiance d'EV-18). Elles ne créent ni ne font évoluer le schéma : toute
divergence constatée avec ces fichiers est un bug de ce module, jamais une
évolution à appliquer en base.

Les contraintes CHECK sont reprises telles quelles pour que le schéma construit
dans les tests d'intégration se comporte comme la vraie base.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Site(Base):
    """Référentiel des sites supervisés, servi par GET /api/v1/sites."""

    __tablename__ = "site"
    __table_args__ = (
        CheckConstraint("capacity_kw > 0", name="site_capacity_kw_check"),
        CheckConstraint("status IN ('active', 'inactive')", name="site_status_check"),
    )

    site_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    site_type: Mapped[str] = mapped_column(String(50), nullable=False)
    site_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Nullable en base, alors que le contrat gelé déclare SiteOut.location non
    # nullable. Le référentiel doit donc toujours porter une valeur : elle vient
    # du seed puis de la synchronisation du Collector. Le modèle reste fidèle à
    # la base plutôt que de masquer un NULL par une chaîne inventée.
    location: Mapped[str | None] = mapped_column(String(150))
    capacity_kw: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'active'"),
    )


class Mesure(Base):
    """Mesure énergétique brute (hypertable TimescaleDB partitionnée sur ts).

    L'attribut Python s'appelle `timestamp` pour coller au champ du contrat,
    la colonne SQL reste `ts` : le renommage est porté ici, une fois, plutôt
    que dans chaque requête.
    """

    __tablename__ = "mesure"
    __table_args__ = (
        CheckConstraint("power_factor BETWEEN 0 AND 1", name="mesure_power_factor_check"),
        CheckConstraint(
            "humidity_percent BETWEEN 0 AND 100",
            name="mesure_humidity_percent_check",
        ),
        CheckConstraint(
            "data_quality IN ('good', 'partial', 'degraded', 'critical')",
            name="mesure_data_quality_check",
        ),
        CheckConstraint(
            "imputation_method IN ('none', 'locf', 'interpolation')",
            name="mesure_imputation_method_check",
        ),
        CheckConstraint(
            "quality_source IN ('source', 'etl')",
            name="mesure_quality_source_check",
        ),
    )

    # PK composite (site_id, ts) : contrainte TimescaleDB, toute clé doit
    # inclure la colonne de partitionnement.
    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        primary_key=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        "ts",
        DateTime(timezone=True),
        primary_key=True,
    )

    consumption_kw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    consumption_kwh: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    voltage_v: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    current_a: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    power_factor: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    temperature_celsius: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    humidity_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    null_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'"),
    )
    data_quality: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default=text("'good'"),
    )

    # Colonnes d'imputation (EV-08). consumption_kw_imputed est la meilleure
    # valeur exploitable, imputation_method dit d'où elle vient. La valeur
    # brute consumption_kw n'est jamais réécrite.
    consumption_kw_imputed: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    imputation_method: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'none'"),
    )

    # Qui a posé data_quality et null_reasons (EV-18). `data_quality` est
    # NOT NULL DEFAULT 'good' : le collecteur retombe sur le défaut quand la
    # source se tait, et l'ETL repose la vraie qualification à son passage.
    # Sans cette colonne, les deux `good` sont le même caractère, et une
    # fenêtre non encore traitée par l'ETL compterait 0 % de dégradation.
    quality_source: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        server_default=text("'source'"),
    )

    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class IngestionEtat(Base):
    """État courant de la collecte, une ligne par site (EV-18).

    Écrite par le collecteur du repo predict — jamais par l'API, qui ne
    collecte rien — et lue ici pour l'indicateur de fraîcheur d'ingestion.

    Elle existe parce que `mesure` ne peut pas répondre. `inserted_at` dit
    quand une ligne est entrée, et cela suffit tant qu'il y a des lignes : un
    capteur mort en produit encore, nulles, avec leurs motifs. Mais un
    collecteur arrêté, une source qui répond 500 ou une base injoignable n'en
    produisent aucune, et `max(inserted_at)` se fige exactement comme si le
    site avait cessé d'exister. Aucune requête sur `mesure` ne distingue « la
    collecte a tourné et il n'y avait rien » de « la collecte n'a pas
    tourné ».
    """

    __tablename__ = "ingestion_etat"
    __table_args__ = (
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ingestion_etat_consecutive_failures_check",
        ),
        CheckConstraint(
            "source IN ('poller', 'backfill')",
            name="ingestion_etat_source_check",
        ),
    )

    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        primary_key=True,
    )

    # Le couple qui porte tout le diagnostic. Égales, la collecte va bien.
    # Écartées, elle tourne et échoue — l'écart dit depuis quand. Les deux
    # figées, c'est le collecteur lui-même qui ne tourne plus, ce qu'une seule
    # date n'aurait pas permis de voir.
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )

    last_rows: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    # Âge de la mesure servie par la source au dernier essai abouti, mesuré
    # par le collecteur. Non reconstructible depuis inserted_at - ts, qui
    # mélange retard de source et retard d'écriture et devient énorme sur un
    # rattrapage sans qu'aucune panne n'existe.
    last_data_lag_s: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    # 'poller' ou 'backfill'. Un rattrapage lancé à la main pendant que la
    # collecte continue est arrêtée ne doit pas faire paraître l'ingestion
    # vivante.
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
