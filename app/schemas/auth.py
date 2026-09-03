"""DTO d'authentification de l'API EnerVision.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TokenResponse(BaseModel):
    """Jeton JWT délivré par le flux OAuth2 password."""

    model_config = ConfigDict(from_attributes=True)

    access_token: str = Field(description="Jeton JWT à placer dans l'en-tête Authorization.")
    token_type: Literal["bearer"] = Field(
        description="Schéma d'authentification à utiliser, toujours bearer.",
    )
    expires_in: int = Field(
        gt=0,
        description="Durée de validité du jeton en secondes.",
    )


class UserOut(BaseModel):
    """Utilisateur authentifié tel qu'exposé par l'API."""

    model_config = ConfigDict(from_attributes=True)

    username: str = Field(description="Identifiant de connexion de l'utilisateur.")
    role: Literal["reader", "writer"] = Field(
        description="Rôle applicatif : reader en lecture seule, writer en écriture.",
    )
