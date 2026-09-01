"""Modèles partagés par l'ensemble des schémas de l'API métier EnerVision.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Qualité d'une mesure telle que produite par l'API Mock IoT source.
DataQuality = Literal["good", "partial", "degraded", "critical"]


class ErrorResponse(BaseModel):
    """Corps de réponse commun à toutes les erreurs de l'API."""

    model_config = ConfigDict(from_attributes=True)

    detail: str = Field(
        description="Message d'erreur destiné au consommateur de l'API.",
    )


class PaginationMeta(BaseModel):
    """Métadonnées de pagination accompagnant toute collection paginée."""

    model_config = ConfigDict(from_attributes=True)

    total: int = Field(
        ge=0,
        description="Nombre total d'éléments disponibles pour la requête.",
    )
    limit: int = Field(
        ge=1,
        le=1000,
        description="Taille de page demandée, entre 1 et 1000 (défaut 100).",
    )
    offset: int = Field(
        ge=0,
        description="Décalage appliqué au début de la collection.",
    )
