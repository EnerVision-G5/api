"""Authentification de l'API : jetons JWT et dépendances FastAPI.

Le flux est celui d'ADR-009 : OAuth2 mot de passe, jetons JWT signés en HS256
par l'API et vérifiés à chaque requête, sans état côté serveur. La révocation
avant expiration n'est pas possible sans liste de rejet, ce que l'ADR accepte
à ce stade et qui justifie une durée de vie courte.

Le hachage des mots de passe n'est pas ici : il vit dans app.password, seul
module autorisé à le manipuler.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_jwt_secret, get_settings
from app.db.session import get_db
from app.models.user import LOCAL_PROVIDER, AppUser
from app.password import hash_password, verify_password
from app.schemas.auth import UserOut

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# Alias réutilisé par tous les endpoints protégés par JWT.
BearerToken = Annotated[str | None, Depends(oauth2_scheme)]
AuthSession = Annotated[AsyncSession, Depends(get_db)]

# Message unique de refus. Utilisateur inconnu et mot de passe faux renvoient
# le même texte : distinguer les deux permettrait d'énumérer les comptes.
INVALID_CREDENTIALS = "Identifiants invalides."
INVALID_TOKEN = "Jeton absent, invalide ou expiré."

# La RFC 6750 impose cet en-tête sur un 401 d'un flux Bearer : il indique au
# client comment s'authentifier plutôt que de le laisser deviner.
BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@lru_cache
def _absent_user_hash() -> str:
    """Hachage de rebut, vérifié quand le compte demandé n'existe pas.

    Sans lui, un utilisateur inconnu répondrait sans passer par argon2, donc
    bien plus vite qu'un mot de passe faux : le temps de réponse révélerait
    quels comptes existent, ce que le message d'erreur unique cherche
    précisément à cacher. Calculé au premier appel et mémoïsé, pour ne pas
    payer un hachage à l'import du module.
    """
    return hash_password("aucun-compte-ne-porte-ce-mot-de-passe")


def unauthorized(detail: str) -> HTTPException:
    """Construit un 401 conforme au contrat, avec le défi Bearer."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=BEARER_CHALLENGE,
    )


async def authenticate_user(
    session: AsyncSession,
    username: str,
    password: str,
) -> AppUser | None:
    """Retourne l'utilisateur si le couple identifiant / mot de passe est bon.

    Le hachage est toujours vérifié, même sans compte correspondant, pour que
    le coût de la réponse ne dépende pas de l'existence du compte.
    """
    user = await session.scalar(
        select(AppUser).where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == username,
        ),
    )
    stored_hash = user.password_hash if user and user.password_hash else None
    password_matches = verify_password(password, stored_hash or _absent_user_hash())
    if stored_hash is None or not password_matches:
        return None
    return user


def create_access_token(username: str, role: str) -> tuple[str, int]:
    """Signe un jeton d'accès et retourne le couple (jeton, durée en secondes).

    Le rôle voyage dans le jeton pour que le dashboard connaisse le sien sans
    appel supplémentaire, le contrat ne publiant aucun endpoint de profil. Il
    ne fait pas autorité pour autant : les vérifications relisent le rôle en
    base, voir get_current_user.
    """
    settings = get_settings()
    expires_in = settings.access_token_expire_minutes * 60
    issued_at = datetime.now(UTC)
    claims = {
        "sub": username,
        "role": role,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=expires_in),
    }
    token = jwt.encode(claims, get_jwt_secret(), algorithm=settings.jwt_algorithm)
    return token, expires_in


async def get_current_user(
    token: BearerToken,
    session: AuthSession,
) -> UserOut | None:
    """Résout l'utilisateur porté par le jeton Bearer, ou refuse en 401.

    AUTH_ENABLED à false rend None sans rien vérifier : le mode anonyme du
    développement local, hérité d'EV-11, reste disponible et ne doit jamais
    être déployé.

    Le rôle exposé est celui de la base, pas celui du claim : un privilège
    retiré doit prendre effet sans attendre l'expiration du jeton. Les deux
    concordent tant que le rôle n'a pas changé depuis la connexion.
    """
    if not get_settings().auth_enabled:
        return None
    if not token:
        raise unauthorized(INVALID_TOKEN)

    settings = get_settings()
    try:
        # jose vérifie la signature et l'expiration, et rejette un jeton signé
        # avec un autre algorithme que celui autorisé ici.
        claims = jwt.decode(
            token,
            get_jwt_secret(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as error:
        raise unauthorized(INVALID_TOKEN) from error

    username = claims.get("sub")
    if not username:
        raise unauthorized(INVALID_TOKEN)

    user = await session.scalar(
        select(AppUser).where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == username,
        ),
    )
    # Jeton bien signé mais dont le porteur a disparu de la base : refusé.
    if user is None:
        raise unauthorized(INVALID_TOKEN)
    return UserOut(username=user.oauth_subject, role=user.role)


# Alias câblé sur chaque endpoint protégé.
CurrentUser = Annotated[UserOut | None, Depends(get_current_user)]


def require_role(
    *roles: str,
) -> Callable[[UserOut | None], Awaitable[UserOut | None]]:
    """Fabrique une dépendance n'admettant que les rôles donnés.

    Câblée sur les endpoints qui écrivent : le déclenchement d'un pic et la
    synchronisation du référentiel exigent `writer`. Les lectures d'EV-11
    restent ouvertes à tout utilisateur authentifié, comme le contrat le dit.
    """
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> UserOut | None:
        # AUTH_ENABLED à false : aucun utilisateur n'est identifiable, le
        # contrôle de rôle est donc sans objet. L'appliquer quand même
        # rendrait toute écriture impossible en développement local.
        if user is None:
            return None
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Rôle insuffisant :"
                    f" {' ou '.join(sorted(allowed))} requis pour cette opération."
                ),
            )
        return user

    return dependency
