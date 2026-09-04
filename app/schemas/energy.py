"""DTO métier de l'API EnerVision : sites, mesures énergétiques et alertes.

Les noms de champs reproduisent à l'identique ceux de l'API Mock IoT source
afin que l'ETL n'effectue aucun renommage.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataQuality, PaginationMeta

AlertSeverity = Literal["low", "medium", "high", "critical"]
AlertType = Literal["spike", "threshold", "anomaly", "outage", "sensor"]
ImputationMethod = Literal["none", "locf", "interpolation"]
SensorName = Literal[
    "consumption",
    "electrical",
    "temperature",
    "humidity",
    "network",
]
SensorStatus = Literal["ok", "failing"]
SensorOverall = Literal["ok", "degraded", "critical"]


class SiteOut(BaseModel):
    """Site industriel supervisé, miroir du site de l'API Mock IoT."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant technique du site.")
    site_type: str = Field(description="Catégorie de site, par exemple usine ou entrepôt.")
    site_name: str = Field(description="Libellé du site affiché dans le dashboard.")
    location: str = Field(description="Localisation géographique du site.")
    capacity_kw: float = Field(description="Puissance souscrite du site en kilowatts.")
    status: str = Field(description="État d'exploitation du site remonté par la source.")


class EnergyReadingOut(BaseModel):
    """Mesure énergétique horodatée.

    Reprend tous les champs de l'EnergyReading source, complétés par les
    colonnes d'imputation propres à notre base TimescaleDB.
    """

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(description="Horodatage de la mesure, ISO 8601 UTC.")
    site_id: str = Field(description="Identifiant du site auquel la mesure appartient.")
    site_type: str = Field(description="Catégorie du site, dénormalisée depuis le site.")
    consumption_kw: float | None = Field(
        description="Puissance instantanée mesurée en kilowatts, nulle si absente de la source.",
    )
    consumption_kwh: float | None = Field(
        description="Énergie consommée sur le pas de temps en kilowattheures.",
    )
    voltage_v: float | None = Field(description="Tension mesurée en volts.")
    current_a: float | None = Field(description="Intensité mesurée en ampères.")
    power_factor: float | None = Field(description="Facteur de puissance mesuré.")
    temperature_celsius: float | None = Field(
        description="Température ambiante relevée en degrés Celsius.",
    )
    humidity_percent: float | None = Field(
        description="Humidité relative relevée en pourcentage.",
    )
    null_reasons: list[str] = Field(
        description="Motifs fournis par la source pour chaque champ absent de la mesure.",
    )
    data_quality: DataQuality = Field(
        description="Qualité de la mesure telle que qualifiée par la source.",
    )
    consumption_kw_imputed: float | None = Field(
        description=(
            "Puissance imputée en kilowatts, calculée par l'ETL. Jamais confondue avec"
            " consumption_kw qui reste la valeur brute de la source."
        ),
    )
    imputation_method: ImputationMethod = Field(
        description=(
            "Méthode d'imputation appliquée : none si la mesure brute est exploitable,"
            " locf pour un report de la dernière valeur connue, interpolation sinon."
        ),
    )
    excluded: bool = Field(
        default=False,
        description=(
            "Vrai si la mesure a été écartée des calculs agrégés. Une mesure"
            " écartée reste servie telle quelle : c'est le consommateur qui décide"
            " de la retirer de ses moyennes, pas l'API de la cacher."
        ),
    )
    exclusion_reason: str | None = Field(
        default=None,
        description=(
            "Motif de la mise à l'écart, nul si la mesure n'est pas écartée."
            " Distinct de null_reasons, qui dit ce qui manquait à la mesure là où"
            " celui-ci dit pourquoi elle a été jugée inexploitable."
        ),
    )


class ReadingsPage(BaseModel):
    """Page de mesures énergétiques accompagnée de ses métadonnées."""

    model_config = ConfigDict(from_attributes=True)

    items: list[EnergyReadingOut] = Field(
        description="Mesures de la page, triées par horodatage croissant.",
    )
    meta: PaginationMeta = Field(description="Métadonnées de pagination de la requête.")


class AlertOut(BaseModel):
    """Alerte énergétique, miroir de l'alerte de l'API Mock IoT.

    `timestamp` date le déclenchement côté source, pas la collecte. Les deux
    s'écartent dès qu'un collecteur a été arrêté, et c'est le déclenchement
    qui intéresse le consommateur.
    """

    model_config = ConfigDict(from_attributes=True)

    alert_id: str = Field(description="Identifiant technique de l'alerte.")
    timestamp: datetime = Field(description="Horodatage de déclenchement, ISO 8601 UTC.")
    site_id: str = Field(description="Identifiant du site concerné par l'alerte.")
    severity: AlertSeverity = Field(description="Gravité de l'alerte.")
    type: AlertType = Field(description="Nature de l'anomalie détectée.")
    message: str = Field(description="Description de l'alerte fournie par la source.")
    # Nullables, alors que la source les sert toujours aujourd'hui. Une alerte
    # de type `sensor` n'a pas de seuil à dépasser, et surtout : perdre une
    # alerte parce qu'il lui manque un chiffre serait pire que la servir sans.
    value: float | None = Field(
        default=None,
        description="Valeur mesurée ayant déclenché l'alerte, nulle si non servie.",
    )
    threshold: float | None = Field(
        default=None,
        description="Seuil dont le dépassement a déclenché l'alerte, nul si non servi.",
    )


class SensorHealthOut(BaseModel):
    """État d'un capteur d'un site, tel que la source le déclare.

    Complète `null_reasons` sans le remplacer : celui-ci dit ce qui manquait à
    une mesure, celui-là dit quel capteur est en cause et jusqu'à quand la
    source annonce qu'il le restera. Un capteur qui tombe entre deux mesures
    n'apparaît que dans le second.
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site concerné.")
    capteur: SensorName = Field(description="Capteur décrit.")
    statut: SensorStatus = Field(description="État du capteur : ok ou failing.")
    failing_until: datetime | None = Field(
        default=None,
        description=(
            "Date de rétablissement annoncée par la source, nulle quand le"
            " capteur fonctionne."
        ),
    )
    overall: SensorOverall = Field(
        description=(
            "Synthèse du site : ok si tous les capteurs répondent, degraded si"
            " l'un d'eux est tombé, critical en cas de perte réseau."
        ),
    )
    releve_le: datetime = Field(
        description=(
            "Dernier passage du collecteur sur cet état, ISO 8601 UTC. Un état"
            " ancien dit que la collecte s'est tue, pas que le capteur va bien."
        ),
    )


class SensorFailureOut(BaseModel):
    """Épisode de panne d'un capteur, borné par son début et sa fin.

    `started_at` est l'instant où le collecteur a CONSTATÉ la panne, pas celui
    où elle a commencé : la source dit `failing` au présent, jamais depuis
    quand. Un collecteur arrêté décale donc ce début, et le confondre avec le
    début réel ferait passer une interruption de collecte pour un capteur sain.
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site concerné.")
    capteur: SensorName = Field(description="Capteur tombé en panne.")
    started_at: datetime = Field(
        description="Constat de la panne par le collecteur, ISO 8601 UTC.",
    )
    ended_at: datetime | None = Field(
        default=None,
        description="Constat du rétablissement, nul tant que la panne dure.",
    )
    failing_until: datetime | None = Field(
        default=None,
        description=(
            "Dernière date de rétablissement annoncée par la source pendant"
            " l'épisode. Une prévision, pas un constat : elle peut être dépassée"
            " alors que la panne dure encore."
        ),
    )
    ongoing: bool = Field(
        description="Vrai tant que le collecteur n'a pas constaté le retour à ok.",
    )
