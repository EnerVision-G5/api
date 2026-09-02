"""Endpoints d'authentification (EV-12).

Aucune fonction d'endpoint ne porte de docstring : FastAPI la publierait comme
`description` de l'opération, que le contrat gelé ne contient pas, et le job
contract-drift échouerait aussitôt (piège documenté par EV-11).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.schemas.auth import TokenResponse
from app.schemas.common import ErrorResponse
from app.security import (
    INVALID_CREDENTIALS,
    AuthSession,
    authenticate_user,
    create_access_token,
    unauthorized,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Description figée, reprise mot pour mot du contrat gelé, où elle a été
# publiée depuis la docstring du stub d'EV-06. Son texte est aujourd'hui
# dépassé : la délivrance du jeton est implémentée juste en dessous. Mais le
# contrat est gelé et la republier à l'identique est ce qui garde
# contract-drift vert ; la corriger demande une PR de contrat sur enervision,
# en patch semver. Elle est passée en paramètre plutôt qu'écrite en docstring
# pour qu'elle ne trompe pas le lecteur du code.
LEGACY_TOKEN_DESCRIPTION = (
    "Déclare le flux OAuth2 password dans la spécification.\n"
    "\n"
    "form_data porte le contrat d'entrée (username, password, scope) sans être\n"
    "exploité : la délivrance réelle du jeton est le périmètre d'EV-12."
)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtenir un jeton JWT (flux OAuth2 password)",
    description=LEGACY_TOKEN_DESCRIPTION,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "Identifiants invalides.",
        },
    },
)
async def create_token(
    session: AuthSession,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    # Un compte inconnu et un mot de passe faux sortent par le même chemin,
    # avec le même message : les distinguer permettrait d'énumérer les
    # comptes existants.
    user = await authenticate_user(session, form_data.username, form_data.password)
    if user is None:
        raise unauthorized(INVALID_CREDENTIALS)

    token, expires_in = create_access_token(user.oauth_subject, user.role)
    # Trace de dernière connexion : la colonne existe au schéma pour cela, et
    # c'est le seul moment où l'API la connaît.
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
    )
