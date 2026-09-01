"""Endpoints sites et mesures. Implémentation portée par le ticket EV-11."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.schemas.common import ErrorResponse
from app.schemas.energy import EnergyReadingOut, ReadingsPage, SiteOut
from app.security import BearerToken

router = APIRouter(prefix="/sites", tags=["sites"])

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}

SiteId = Annotated[str, Path(description="Identifiant du site.")]

NOT_IMPLEMENTED = "Contrat EV-06 uniquement. Lecture des mesures implémentée par EV-11."


@router.get(
    "",
    response_model=list[SiteOut],
    summary="Lister les sites supervisés",
    responses={status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED},
)
def list_sites(token: BearerToken) -> list[SiteOut]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=NOT_IMPLEMENTED)


@router.get(
    "/{site_id}",
    response_model=SiteOut,
    summary="Consulter un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
    },
)
def get_site(site_id: SiteId, token: BearerToken) -> SiteOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=NOT_IMPLEMENTED)


@router.get(
    "/{site_id}/readings",
    response_model=ReadingsPage,
    summary="Lister les mesures d'un site sur une fenêtre de temps",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
def list_readings(
    site_id: SiteId,
    token: BearerToken,
    start_time: Annotated[
        datetime,
        Query(description="Borne inférieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    end_time: Annotated[
        datetime,
        Query(description="Borne supérieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Taille de page, entre 1 et 1000."),
    ] = 100,
    offset: Annotated[
        int,
        Query(ge=0, description="Décalage appliqué au début de la collection."),
    ] = 0,
) -> ReadingsPage:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=NOT_IMPLEMENTED)


@router.get(
    "/{site_id}/readings/latest",
    response_model=EnergyReadingOut,
    summary="Consulter la dernière mesure connue d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
    },
)
def get_latest_reading(site_id: SiteId, token: BearerToken) -> EnergyReadingOut:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=NOT_IMPLEMENTED)
