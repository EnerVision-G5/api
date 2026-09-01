"""Déclaration du schéma d'authentification exposé dans la spécification OpenAPI.

La dépendance ci-dessous ne valide rien : elle décrit le contrat d'appel afin
que le flux OAuth2 password et l'en-tête Authorization apparaissent dans la
spécification. La vérification du jeton relève du ticket EV-12.
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# Alias réutilisé par tous les endpoints protégés par JWT.
BearerToken = Annotated[str | None, Depends(oauth2_scheme)]
