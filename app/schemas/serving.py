"""DTO de la réponse du service d'inférence.

Ce ne sont **pas** des DTO du contrat de l'API : aucune route ne les expose,
ils ne figurent donc pas dans openapi-api.json. Ils décrivent ce que Serving
renvoie, et leurs formes sont celles de son contrat
(enervision/docs/contracts/openapi-predict.json).

Les nommer avec un préfixe Serving évite de les confondre avec
PredictionPointOut, qui est ce que l'API publie, à plat et par point.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServingPredictionPoint(BaseModel):
    """Point de la série prédite, tel que Serving le renvoie."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(description="Horodatage du point prédit, ISO 8601 UTC.")
    predicted_consumption_kw: float = Field(description="Puissance prédite en kilowatts.")
    lower_bound_kw: float | None = Field(description="Borne basse de l'intervalle.")
    upper_bound_kw: float | None = Field(description="Borne haute de l'intervalle.")


class ServingPrediction(BaseModel):
    """Prévision complète renvoyée par Serving pour un site."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site prédit.")
    model_version: str = Field(description="Version du modèle ayant servi la réponse.")
    generated_at: datetime = Field(description="Horodatage de production, ISO 8601 UTC.")
    points: list[ServingPredictionPoint] = Field(description="Série prédite.")
