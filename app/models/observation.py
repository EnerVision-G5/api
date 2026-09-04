"""Modèles ORM de ce que la source observe : alertes et santé des capteurs.

Trois tables que `mesure` ne remplace pas, et qui ne se remplacent pas entre
elles.

`alerte` est un journal. La source ne sert que les alertes **actives** : une
alerte résolue disparaît de sa réponse, c'est-à-dire au moment précis où on
veut l'expliquer. Ne garder que le présent perdrait donc l'incident. Elle
porte aussi la valeur et le seuil qui l'ont déclenchée, que rien dans `mesure`
ne dit.

`capteur_etat` est un présent. Il dit **quel** capteur est tombé et **jusqu'à
quand** la source annonce qu'il le restera.

`capteur_panne` est le journal des épisodes, bornés par un début et une fin.
Il ne double ni l'un ni l'autre : `capteur_etat` ne garde que le présent, et
`mesure.null_reasons` ne connaît que les pannes visibles SUR une mesure — un
capteur tombé puis rétabli entre deux relevés n'y laisse rien. Deux lignes
suffisent à borner une panne là où empiler l'état en produirait une par
minute.

Les deux sont écrites par le collecteur du repo predict, jamais par l'API, qui
ne collecte rien. Le schéma est porté par `alembic/versions/`.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Gravités et natures déclarées par la source. Reprises en contrainte : une
# valeur hors liste rendrait le filtre du contrat silencieusement incomplet.
ALERT_SEVERITIES = ("low", "medium", "high", "critical")
ALERT_TYPES = ("spike", "threshold", "anomaly", "outage", "sensor")

# Capteurs décrits par la source, et états qu'ils peuvent prendre.
SENSOR_NAMES = ("consumption", "electrical", "temperature", "humidity", "network")
SENSOR_STATUSES = ("ok", "failing")
SENSOR_OVERALL = ("ok", "degraded", "critical")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    """Écrit un CHECK IN à partir d'une liste de valeurs admises."""
    listed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({listed})"


class Alerte(Base):
    """Alerte de consommation déclenchée par la source."""

    __tablename__ = "alerte"
    __table_args__ = (
        CheckConstraint(
            _in_clause("severity", ALERT_SEVERITIES),
            name="alerte_severity_check",
        ),
        CheckConstraint(
            _in_clause("type_alerte", ALERT_TYPES),
            name="alerte_type_alerte_check",
        ),
        # L'historique se lit par site et du plus récent au plus ancien.
        Index("idx_alerte_site_ts", "site_id", text("ts DESC")),
    )

    # Identifiant de la source, stable et de la forme ALR-<site>-<epoch>. Il
    # est la clé : le collecteur repasse chaque minute sur les alertes encore
    # actives, et sans lui une alerte d'une heure serait écrite soixante fois.
    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        nullable=False,
    )
    # Déclenchement côté source. Distinct de `collecte_le`, qui date notre
    # passage : les deux s'écartent dès qu'un collecteur a été arrêté, et
    # l'écart est l'information.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    # `type` est un mot trop générique pour une colonne. Le contrat le publie
    # sous son nom d'origine, la traduction est portée une fois, ici.
    type_alerte: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    valeur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    seuil: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    collecte_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CapteurEtat(Base):
    """État courant d'un capteur d'un site, une ligne par couple."""

    __tablename__ = "capteur_etat"
    __table_args__ = (
        CheckConstraint(
            _in_clause("capteur", SENSOR_NAMES),
            name="capteur_etat_capteur_check",
        ),
        CheckConstraint(
            _in_clause("statut", SENSOR_STATUSES),
            name="capteur_etat_statut_check",
        ),
        CheckConstraint(
            _in_clause("overall", SENSOR_OVERALL),
            name="capteur_etat_overall_check",
        ),
    )

    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        primary_key=True,
    )
    capteur: Mapped[str] = mapped_column(String(20), primary_key=True)
    statut: Mapped[str] = mapped_column(String(10), nullable=False)
    # Date de rétablissement annoncée par la source. C'est la seule
    # information que ni `mesure` ni `null_reasons` ne portent : eux disent ce
    # qui a manqué, elle dit jusqu'à quand cela manquera.
    failing_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Synthèse du site, recopiée sur chaque capteur. La table est plate : sans
    # cette redondance, connaître l'état global demanderait une agrégation.
    overall: Mapped[str] = mapped_column(String(10), nullable=False)
    # Reposée à chaque tick par le collecteur. Sans elle, un collecteur arrêté
    # depuis trois jours laisserait une table qui paraît à jour.
    releve_le: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CapteurPanne(Base):
    """Épisode de panne d'un capteur, borné par son début et sa fin.

    Dérivée de `capteur_etat` et non son doublon : l'une dit l'état présent,
    l'autre les épisodes passés. Elle ne double pas davantage
    `mesure.null_reasons`, qui ne connaît que les pannes visibles SUR une
    mesure : un capteur tombé puis rétabli entre deux relevés n'y laisse
    rien, et la date de rétablissement annoncée n'y figure jamais.

    `debut_le` est l'instant où le collecteur a CONSTATÉ la panne, pas celui
    où elle a commencé : la source dit `failing` ou `ok` au présent, jamais
    depuis quand. Les confondre ferait passer un collecteur arrêté pour un
    capteur en bonne santé.
    """

    __tablename__ = "capteur_panne"
    __table_args__ = (
        CheckConstraint(
            _in_clause("capteur", SENSOR_NAMES),
            name="capteur_panne_capteur_check",
        ),
        # Une panne se termine après avoir commencé. La contrainte paraît
        # gratuite jusqu'au jour où une horloge recule.
        CheckConstraint(
            "fin_le IS NULL OR fin_le >= debut_le",
            name="capteur_panne_fin_le_check",
        ),
        UniqueConstraint(
            "site_id",
            "capteur",
            "debut_le",
            name="capteur_panne_site_id_capteur_debut_le_key",
        ),
        Index("idx_capteur_panne_site_debut", "site_id", text("debut_le DESC")),
        # Au plus un épisode ouvert par capteur. Sans cet index partiel, deux
        # collecteurs concurrents en ouvriraient deux, et la fin ne saurait
        # plus lequel clore.
        Index(
            "idx_capteur_panne_ouverte",
            "site_id",
            "capteur",
            unique=True,
            postgresql_where=text("fin_le IS NULL"),
        ),
    )

    panne_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    site_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("site.site_id"),
        nullable=False,
    )
    capteur: Mapped[str] = mapped_column(String(20), nullable=False)
    debut_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL tant que la panne dure. C'est ce que vise l'index partiel ci-dessus,
    # et ce qui rend « les pannes en cours » une requête d'une ligne.
    fin_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failing_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
