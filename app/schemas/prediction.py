"""DTO de prédiction, relayés tels quels depuis le service d'inférence.

Formes strictement identiques à celles du contrat de predict
(enervision/docs/contracts/openapi-predict.json, DTO de
services/serving/src/serving/schemas.py) : l'API métier fait proxy sans rien
transformer. Les descriptions sont reprises mot pour mot afin que les deux
contrats racontent la même chose au dashboard, qui dérive ses types de l'un
comme de l'autre.

Un écart de forme entre ces classes et celles de serving casserait le relais
sans qu'aucun des deux jobs contract-drift ne le voie : chacun compare son
service à son propre fichier gelé, aucun ne compare les deux entre eux.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PredictionRequest(BaseModel):
    """Demande de prévision de consommation pour un site."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site à prédire.")
    horizon_hours: int = Field(
        default=24,
        ge=1,
        le=48,
        description="Profondeur de la prévision en heures, entre 1 et 48.",
    )


class PredictionPoint(BaseModel):
    """Point de la série prédite, avec son intervalle de confiance."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(
        description="Horodatage du point prédit, ISO 8601 UTC.",
    )
    predicted_consumption_kw: float = Field(
        description="Puissance prédite en kilowatts.",
    )
    lower_bound_kw: float | None = Field(
        description="Borne basse de l'intervalle de confiance, nulle si non calculée.",
    )
    upper_bound_kw: float | None = Field(
        description="Borne haute de l'intervalle de confiance, nulle si non calculée.",
    )


class PredictionOut(BaseModel):
    """Prévision complète renvoyée pour un site."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site prédit.")
    model_version: str = Field(
        description="Tag MLflow du modèle ayant servi la réponse.",
    )
    generated_at: datetime = Field(
        description="Horodatage de production de la prévision, ISO 8601 UTC.",
    )
    points: list[PredictionPoint] = Field(
        description="Série prédite, triée par horodatage croissant.",
    )
