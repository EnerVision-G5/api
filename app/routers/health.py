"""Endpoint de supervision de l'API métier."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    """État de disponibilité de l'API.

    Le brief décrit un dict {status, timestamp} : il est typé ici pour
    respecter la convention de projet (description sur chaque champ, valeurs
    fermées en Literal) et pour que le dashboard en dérive un type utilisable.
    """

    model_config = ConfigDict(from_attributes=True)

    status: Literal["ok"] = Field(description="Statut de l'API, ok si elle répond.")
    timestamp: datetime = Field(description="Horodatage de la réponse, ISO 8601 UTC.")


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Vérifier la disponibilité de l'API",
)
def get_health() -> HealthOut:
    """Endpoint trivial, réellement implémenté : aucune dépendance externe."""
    return HealthOut(status="ok", timestamp=datetime.now(UTC))
