"""Schéma d'authentification exposé dans la spécification OpenAPI.

Rien ici ne valide un jeton : ce module décrit le contrat d'appel afin que le
flux OAuth2 password et l'en-tête Authorization apparaissent dans la
spécification, et fournit un point d'ancrage unique que le ticket EV-12
remplacera par la vraie vérification.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# Alias réutilisé par tous les endpoints protégés par JWT.
BearerToken = Annotated[str | None, Depends(oauth2_scheme)]


async def get_current_user(token: BearerToken) -> str | None:
    """Dépendance d'authentification provisoire des endpoints protégés.

    Deux comportements, pilotés par la variable d'environnement AUTH_ENABLED :

    - false (défaut) : le jeton n'est ni exigé ni lu, l'appel passe en anonyme
      et la valeur retournée est None. C'est ce qui permet à EV-11 de livrer
      les lectures sans dépendre d'EV-12.
    - true : l'appel est refusé en 501, parce qu'aucune vérification n'existe
      encore. Répondre 200 sous un drapeau nommé « auth activée » laisserait
      croire à une protection inexistante, et répondre 401 laisserait croire
      que le jeton fourni est en cause.

    Le jeton est déclaré en paramètre sans être exploité : c'est lui qui fait
    apparaître l'exigence de sécurité de l'opération dans la spécification.

    Implémentation réelle : EV-12 (décodage du JWT, résolution de
    l'utilisateur en base, contrôle des rôles).
    """
    if not get_settings().auth_enabled:
        return None
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Vérification du jeton non implémentée. Implémentation réelle : EV-12.",
    )


# Alias à câbler sur chaque endpoint protégé.
CurrentUser = Annotated[str | None, Depends(get_current_user)]
