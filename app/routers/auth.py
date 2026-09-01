"""Endpoints d'authentification. Implémentation portée par le ticket EV-12."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.schemas.auth import TokenResponse
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtenir un jeton JWT (flux OAuth2 password)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "Identifiants invalides.",
        },
    },
)
def create_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    """Déclare le flux OAuth2 password dans la spécification.

    form_data porte le contrat d'entrée (username, password, scope) sans être
    exploité : la délivrance réelle du jeton est le périmètre d'EV-12.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Contrat EV-06 uniquement. Délivrance du jeton implémentée par EV-12.",
    )
