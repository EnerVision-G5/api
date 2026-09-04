"""Endpoints alertes, branchés sur la table `alerte` (EV-17).

Les alertes ne sont pas produites ici : la source les déclenche, le collecteur
du repo predict les journalise, l'API les sert. Elle n'en invente aucune et
n'en résout aucune.

Un journal et non un état, et c'est la seule décision qui compte dans ce
module. La source ne sert que les alertes **actives** : une alerte résolue
disparaît de sa réponse, c'est-à-dire au moment précis où on cherche à
l'expliquer. Servir le seul présent reviendrait à perdre l'incident à sa
clôture — d'où une lecture par fenêtre de temps, ouverte par défaut.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas. Les explications sont donc
en commentaires.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.observation import Alerte
from app.schemas.common import ErrorResponse
from app.schemas.energy import AlertOut, AlertSeverity, AlertType
from app.security import CurrentUser

router = APIRouter(prefix="/alerts", tags=["alerts"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

# Page par défaut. Les alertes sont rares comparées aux mesures : une centaine
# couvre largement ce qu'un dashboard affiche, et la borne haute protège la
# base d'une requête qui ramènerait tout l'historique.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


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
async def list_alerts(
    session: DbSession,
    user: CurrentUser,
    site_id: Annotated[
        str | None,
        Query(description="Restreindre aux alertes de ce site."),
    ] = None,
    severity: Annotated[
        AlertSeverity | None,
        Query(description="Restreindre aux alertes de cette gravité."),
    ] = None,
    type: Annotated[  # noqa: A002 - nom imposé par le contrat gelé
        AlertType | None,
        Query(description="Restreindre aux alertes de cette nature."),
    ] = None,
    start_time: Annotated[
        datetime | None,
        Query(description="Borne inférieure incluse du déclenchement, ISO 8601."),
    ] = None,
    end_time: Annotated[
        datetime | None,
        Query(description="Borne supérieure incluse du déclenchement, ISO 8601."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="Taille de page."),
    ] = DEFAULT_LIMIT,
) -> list[AlertOut]:
    # Du plus récent au plus ancien : on consulte les alertes pour savoir ce
    # qui vient de se produire, jamais pour remonter à l'origine des temps.
    #
    # Aucun 404 sur un site inconnu, contrairement aux mesures : ici le site
    # est un filtre et non une ressource, et une liste vide répond exactement
    # à la question posée.
    statement = select(Alerte).order_by(Alerte.ts.desc()).limit(limit)
    if site_id is not None:
        statement = statement.where(Alerte.site_id == site_id)
    if severity is not None:
        statement = statement.where(Alerte.severity == severity)
    if type is not None:
        statement = statement.where(Alerte.type_alerte == type)
    if start_time is not None:
        statement = statement.where(Alerte.ts >= start_time)
    if end_time is not None:
        statement = statement.where(Alerte.ts <= end_time)

    rows = await session.scalars(statement)
    return [
        AlertOut(
            alert_id=row.alert_id,
            timestamp=row.ts,
            site_id=row.site_id,
            severity=row.severity,
            # La colonne s'appelle type_alerte, `type` étant trop générique
            # pour une colonne. Le contrat publie le nom de la source.
            type=row.type_alerte,
            message=row.message,
            value=float(row.valeur) if row.valeur is not None else None,
            threshold=float(row.seuil) if row.seuil is not None else None,
        )
        for row in rows
    ]
