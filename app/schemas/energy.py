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


class ReadingsPage(BaseModel):
    """Page de mesures énergétiques accompagnée de ses métadonnées."""

    model_config = ConfigDict(from_attributes=True)

    items: list[EnergyReadingOut] = Field(
        description="Mesures de la page, triées par horodatage croissant.",
    )
    meta: PaginationMeta = Field(description="Métadonnées de pagination de la requête.")


class AlertOut(BaseModel):
    """Alerte énergétique, miroir de l'alerte de l'API Mock IoT."""

    model_config = ConfigDict(from_attributes=True)

    alert_id: str = Field(description="Identifiant technique de l'alerte.")
    timestamp: datetime = Field(description="Horodatage de déclenchement, ISO 8601 UTC.")
    site_id: str = Field(description="Identifiant du site concerné par l'alerte.")
    severity: AlertSeverity = Field(description="Gravité de l'alerte.")
    type: AlertType = Field(description="Nature de l'anomalie détectée.")
    message: str = Field(description="Description de l'alerte fournie par la source.")
    value: float = Field(description="Valeur mesurée ayant déclenché l'alerte.")
    threshold: float = Field(description="Seuil dont le dépassement a déclenché l'alerte.")
