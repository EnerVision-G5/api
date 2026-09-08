"""Gestion des comptes utilisateurs (EV-55).

Le dashboard avait besoin d'une page de configuration, et le contrat ne
publiait aucune route de réglage. La gestion des comptes est le premier
paramètre fonctionnel réellement partagé : il vit en base, il concerne toute
l'équipe, et il ne peut pas être stocké dans un navigateur.

Toutes ces routes exigent le rôle `writer`, **lecture comprise**. La liste des
comptes n'est pas une donnée d'exploitation : elle dit qui a accès à la
plateforme, et un `reader` n'a pas à la connaître. C'est le seul endroit de
l'API où une lecture est réservée, et c'est délibéré.

Trois garde-fous, tous là pour la même raison — une API d'administration doit
refuser de se rendre inadministrable :

1. on ne supprime pas son propre compte, sinon on se déconnecte soi-même en
   croyant faire le ménage ;
2. on ne retire pas le dernier `writer`, ni par changement de rôle ni par
   suppression : plus personne ne pourrait alors administrer, et la seule issue
   serait un `UPDATE` à la main en base ;
3. seuls les comptes **locaux** sont gérables. Une identité fédérée n'a pas de
   mot de passe local, et lui en poser un par ces routes créerait un second
   chemin d'authentification que personne n'a demandé.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas. Les explications sont donc
en commentaires.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.user import LOCAL_PROVIDER, AppUser
from app.password import hash_password
from app.schemas.auth import UserOut
from app.schemas.common import ErrorResponse
from app.schemas.user_admin import UserCreateIn, UserDetailOut, UserUpdateIn
from app.security import require_role

router = APIRouter(prefix="/users", tags=["users"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
WritersOnly = Annotated[UserOut | None, Depends(require_role("writer"))]

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
FORBIDDEN = {
    "model": ErrorResponse,
    "description": "Rôle writer requis pour gérer les comptes.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Compte inconnu."}
CONFLICT = {
    "model": ErrorResponse,
    "description": "Identifiant de connexion déjà pris, ou dernier writer.",
}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Corps de requête invalide.",
}


def _to_out(user: AppUser) -> UserDetailOut:
    return UserDetailOut(
        user_id=user.user_id,
        username=user.oauth_subject,
        email=user.email,
        display_name=user.display_name,
        # Le rôle est contraint en base et par le Literal du DTO : la valeur
        # lue ne peut pas sortir de l'énumération.
        role=user.role,  # type: ignore[arg-type]
        can_sign_in=user.password_hash is not None,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


async def _local_user(session: AsyncSession, user_id: int) -> AppUser:
    """Compte local par son identifiant, ou 404.

    Le filtre sur `oauth_provider` fait partie de la recherche, pas d'un
    contrôle ultérieur : une identité fédérée n'est pas « interdite » pour ces
    routes, elle leur est inconnue.
    """
    user = await session.scalar(
        select(AppUser).where(
            AppUser.user_id == user_id,
            AppUser.oauth_provider == LOCAL_PROVIDER,
        ),
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Compte {user_id} inconnu parmi les comptes locaux.",
        )
    return user


async def _count_writers(session: AsyncSession) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(AppUser)
        .where(AppUser.role == "writer", AppUser.oauth_provider == LOCAL_PROVIDER),
    )
    return total or 0


async def _refuse_last_writer(session: AsyncSession, user: AppUser, action: str) -> None:
    """Refuse de retirer le dernier compte capable d'administrer."""
    if user.role != "writer":
        return
    if await _count_writers(session) > 1:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"{action} : c'est le dernier compte writer, et plus personne ne"
            " pourrait alors gérer les comptes. Promouvoir un autre compte"
            " d'abord."
        ),
    )


@router.get(
    "",
    response_model=list[UserDetailOut],
    summary="Lister les comptes utilisateurs",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
    },
)
async def list_users(session: DbSession, user: WritersOnly) -> list[UserDetailOut]:
    # Par identifiant croissant : c'est l'ordre de création, et il ne bouge pas
    # quand un compte est renommé ou change de rôle. Trier par nom ferait
    # sauter les lignes d'un rafraîchissement à l'autre.
    #
    # Les identités fédérées sont exclues : ces routes ne savent pas les gérer,
    # et les afficher promettrait une action impossible.
    rows = await session.scalars(
        select(AppUser)
        .where(AppUser.oauth_provider == LOCAL_PROVIDER)
        .order_by(AppUser.user_id),
    )
    return [_to_out(row) for row in rows]


@router.post(
    "",
    response_model=UserDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Créer un compte utilisateur",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
        status.HTTP_409_CONFLICT: CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def create_user(
    payload: UserCreateIn,
    session: DbSession,
    user: WritersOnly,
) -> UserDetailOut:
    # Unicité vérifiée avant l'insertion : la contrainte de la base la
    # refuserait de toute façon, mais par une erreur d'intégrité que le client
    # recevrait en 500. Un 409 dit ce qui s'est passé.
    existing = await session.scalar(
        select(AppUser).where(
            AppUser.oauth_provider == LOCAL_PROVIDER,
            AppUser.oauth_subject == payload.username,
        ),
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"L'identifiant {payload.username} est déjà pris.",
        )

    created = AppUser(
        oauth_provider=LOCAL_PROVIDER,
        oauth_subject=payload.username,
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        # Haché ici, jamais conservé en clair, jamais journalisé.
        password_hash=hash_password(payload.password),
    )
    session.add(created)
    await session.commit()
    await session.refresh(created)
    return _to_out(created)


@router.patch(
    "/{user_id}",
    response_model=UserDetailOut,
    summary="Modifier un compte utilisateur",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_409_CONFLICT: CONFLICT,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def update_user(
    payload: UserUpdateIn,
    session: DbSession,
    user: WritersOnly,
    user_id: Annotated[int, Path(description="Identifiant technique du compte.")],
) -> UserDetailOut:
    target = await _local_user(session, user_id)

    # Rétrograder le dernier writer rendrait la plateforme inadministrable.
    if payload.role is not None and payload.role != target.role:
        await _refuse_last_writer(session, target, "Rôle non modifié")
        target.role = payload.role

    if payload.email is not None:
        target.email = payload.email
    # Le nom affiché ne peut pas être effacé par cette route : `None` signifie
    # « champ absent de la requête », et il faudrait un moyen de distinguer
    # l'absence d'un effacement voulu. Le besoin n'existe pas encore.
    if payload.display_name is not None:
        target.display_name = payload.display_name
    if payload.password is not None:
        target.password_hash = hash_password(payload.password)

    await session.commit()
    await session.refresh(target)
    return _to_out(target)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Supprimer un compte utilisateur",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_409_CONFLICT: CONFLICT,
    },
)
async def delete_user(
    session: DbSession,
    user: WritersOnly,
    user_id: Annotated[int, Path(description="Identifiant technique du compte.")],
) -> None:
    target = await _local_user(session, user_id)

    # Supprimer son propre compte, c'est se déconnecter en croyant faire le
    # ménage. `user` est nul quand AUTH_ENABLED vaut false : le contrôle est
    # alors sans objet, comme pour les rôles.
    if user is not None and target.oauth_subject == user.username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Un compte ne peut pas se supprimer lui-même.",
        )

    await _refuse_last_writer(session, target, "Compte non supprimé")

    await session.delete(target)
    await session.commit()
