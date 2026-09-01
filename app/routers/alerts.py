"""Endpoints alertes. Implémentation portée par un ticket ultérieur."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.common import ErrorResponse
from app.schemas.energy import AlertOut, AlertSeverity
from app.security import BearerToken

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get(
    "",
    response_model=list[AlertOut],
    summary="Lister les alertes énergétiques",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "Jeton JWT absent, expiré ou invalide.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorResponse,
            "description": "Paramètres de requête invalides.",
        },
    },
)
def list_alerts(
    token: BearerToken,
    site_id: Annotated[
        str | None,
        Query(description="Restreindre aux alertes de ce site."),
    ] = None,
    severity: Annotated[
        AlertSeverity | None,
        Query(description="Restreindre aux alertes de cette gravité."),
    ] = None,
) -> list[AlertOut]:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Contrat EV-06 uniquement.",
    )
