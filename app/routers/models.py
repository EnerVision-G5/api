"""Endpoints du registre des modèles de prédiction.

L'API ne l'écrit pas : le registre est alimenté par le service d'entraînement
du repo predict, à chaque promotion. Elle le sert, parce qu'elle est la seule
à parler au dashboard et que celui-ci a besoin de savoir sur quoi reposent les
chiffres qu'il affiche.

La table existait depuis le schéma figé v1.0 et n'était lue que par une
jointure interne, pour retrouver `model_version` sur une prévision. Rien ne
permettait de répondre à « quel modèle sert en ce moment », ni de voir ce qui
a servi avant lui — deux questions qu'on se pose exactement le jour où une
prévision paraît fausse.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas. Les explications sont donc
en commentaires.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.prediction import Modele
from app.schemas.common import ErrorResponse
from app.schemas.recommendation import ModelOut
from app.security import CurrentUser

router = APIRouter(prefix="/models", tags=["models"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NO_ACTIVE_MODEL = {
    "model": ErrorResponse,
    "description": "Aucun modèle n'est promu.",
}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@router.get(
    "/current",
    response_model=ModelOut,
    summary="Consulter le modèle actuellement promu",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NO_ACTIVE_MODEL,
    },
)
async def get_current_model(session: DbSession, user: CurrentUser) -> ModelOut:
    # Déclaré AVANT /{modele_id} : un chemin littéral placé après un chemin
    # paramétré serait avalé par lui, et « current » finirait interprété comme
    # un identifiant.
    #
    # 404 et non une réponse nullable : aucun modèle promu est un état
    # anormal, pas une valeur. Le dire en 200 le ferait passer pour un cas de
    # fonctionnement.
    #
    # Le plus récent des promus si plusieurs le sont : le registre ne contraint
    # pas l'unicité, et servir arbitrairement l'un des deux serait pire que
    # servir celui qui décrit ce que le site reçoit maintenant.
    model = await session.scalar(
        select(Modele)
        .where(Modele.actif.is_(True))
        .order_by(Modele.created_at.desc())
        .limit(1),
    )
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Aucun modèle promu : le registre est vide ou aucune version"
                " n'est active. Les prévisions ne peuvent pas être servies."
            ),
        )
    return ModelOut.model_validate(model)


@router.get(
    "",
    response_model=list[ModelOut],
    summary="Lister les modèles du registre",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_models(
    session: DbSession,
    user: CurrentUser,
    nom: Annotated[
        str | None,
        Query(description="Restreindre aux versions de ce modèle."),
    ] = None,
    actif: Annotated[
        bool | None,
        Query(description="Vrai pour les seules versions promues."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Nombre maximal de modèles rendus."),
    ] = DEFAULT_LIMIT,
) -> list[ModelOut]:
    # Du plus récemment entré au plus ancien : l'historique se consulte pour
    # savoir ce qui a changé, pas pour remonter à la première version.
    statement = select(Modele).order_by(Modele.created_at.desc()).limit(limit)
    if nom is not None:
        statement = statement.where(Modele.nom == nom)
    if actif is not None:
        statement = statement.where(Modele.actif.is_(actif))

    rows = await session.scalars(statement)
    return [ModelOut.model_validate(row) for row in rows]
