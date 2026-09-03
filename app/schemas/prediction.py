"""DTO de lecture des prédictions stockées.

Le dashboard ne déclenche jamais une prédiction : un job planifié de l'API
interroge le service d'inférence et archive le résultat, que ces DTO servent
ensuite. Le contrat de predict reste celui de Serving, pas celui-ci.

Chaque point porte `model_version` et `generated_at` : une page de prédictions
peut mêler plusieurs générations pour un même instant cible, et chaque ligne
doit se suffire à elle-même pour dire d'où elle vient.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginationMeta


class PredictionPointOut(BaseModel):
    """Point prédit, tel qu'il a été archivé."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(
        description="Horodatage cible de la prévision, ISO 8601 UTC.",
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
    model_version: str = Field(
        description="Version du modèle ayant produit ce point.",
    )
    generated_at: datetime = Field(
        description=(
            "Horodatage de production de la prévision par le service"
            " d'inférence, ISO 8601 UTC. Distinct de l'horodatage cible."
        ),
    )


class PredictionsPage(BaseModel):
    """Page de prédictions accompagnée de ses métadonnées."""

    model_config = ConfigDict(from_attributes=True)

    items: list[PredictionPointOut] = Field(
        description="Prédictions de la page, triées par horodatage cible croissant.",
    )
    meta: PaginationMeta = Field(description="Métadonnées de pagination de la requête.")
