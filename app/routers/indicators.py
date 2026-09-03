"""Indicateurs de confiance dans la donnée (EV-18).

Deux routes, une réponse. Le dashboard lit la version d'un site sous la courbe
qu'il affiche, et la collection dans le sélecteur de site : comparer sept sites
ne doit pas coûter sept appels.

La collection est à `/api/v1/indicators` et NON à `/api/v1/sites/indicators`,
et ce n'est pas une préférence de nommage. FastAPI résout les routes dans
l'ordre d'enregistrement : `/sites/{site_id}` est déclaré par `sites.py`, inclus
avant ce module, et un chemin littéral sous `/sites` serait avalé par lui —
la réponse serait `404 Site inconnu : indicators`. Une route de premier niveau
règle la question sans dépendre d'un ordre d'`include_router`, donc sans
qu'une réorganisation de `main.py` puisse la casser en silence.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas, et le job contract-drift
échouerait aussitôt (piège documenté par EV-11). Les explications sont donc en
commentaires.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.lookups import ensure_site_exists
from app.db.session import get_db
from app.models.energy import Site
from app.schemas.common import ErrorResponse
from app.schemas.indicators import SiteIndicatorsOut
from app.security import CurrentUser
from app.services.indicators import (
    accuracy_indicators,
    ingestion_indicators,
    quality_indicators,
    window_bounds,
)

router = APIRouter(tags=["indicators"])

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}

# Profondeur par défaut de la fenêtre. Alignée sur DEFAULT_WINDOW_HOURS du
# dashboard : les parts publiées doivent porter sur la même période que la
# courbe au-dessus de laquelle elles s'affichent.
DEFAULT_WINDOW_HOURS = 24

# Borne haute : une semaine. Au-delà, la part de mesures dégradées cesse de
# décrire l'état courant du site et devient un historique, que le graphique de
# qualité portera le jour où il existera.
MAX_WINDOW_HOURS = 168

SiteId = Annotated[str, Path(description="Identifiant du site.")]
DbSession = Annotated[AsyncSession, Depends(get_db)]
Config = Annotated[Settings, Depends(get_settings)]
WindowHours = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_WINDOW_HOURS,
        description=(
            "Profondeur de la fenêtre en heures, entre 1 et 168 (défaut 24)."
        ),
    ),
]


async def build_indicators(
    session: AsyncSession,
    settings: Settings,
    site_ids: Sequence[str],
    window_hours: int,
) -> list[SiteIndicatorsOut]:
    """Assemble les trois blocs des sites demandés, dans l'ordre donné.

    Un seul `now` pour toute la réponse : les trois agrégats et les âges
    publiés doivent partager le même présent, sans quoi une même réponse
    décrirait deux instants différents.

    Les trois lectures sont séquentielles et non concurrentes : elles
    partagent la session, qu'une seule requête à la fois peut occuper. Les
    paralléliser demanderait trois sessions, donc trois connexions par appel,
    pour trois agrégats qui portent chacun sur une fenêtre déjà indexée.
    """
    now = datetime.now(UTC)
    start_time, end_time = window_bounds(now, window_hours)

    ingestion = await ingestion_indicators(
        session,
        site_ids,
        now,
        settings.stale_threshold_seconds,
    )
    quality = await quality_indicators(
        session,
        site_ids,
        start_time,
        end_time,
        settings.degraded_ratio_threshold,
    )
    accuracy = await accuracy_indicators(
        session,
        site_ids,
        start_time,
        end_time,
        settings.drift_mae_ratio,
    )
    return [
        SiteIndicatorsOut(
            site_id=site_id,
            generated_at=now,
            window_hours=window_hours,
            ingestion=ingestion[site_id],
            quality=quality[site_id],
            accuracy=accuracy[site_id],
        )
        for site_id in site_ids
    ]


@router.get(
    "/indicators",
    response_model=list[SiteIndicatorsOut],
    summary="Lister les indicateurs de confiance de tous les sites",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_indicators(
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    window_hours: WindowHours = DEFAULT_WINDOW_HOURS,
) -> list[SiteIndicatorsOut]:
    # Le référentiel complet, trié par identifiant, comme GET /sites. Les sept
    # sites tiennent en une réponse : le contrat ne prévoit pas de pagination
    # ici, un tri stable suffit à rendre la sortie déterministe.
    #
    # Les sites inactifs y figurent. C'est voulu : un site qu'on vient de
    # passer inactif est exactement celui dont on veut voir si sa collecte
    # s'est arrêtée, et le filtrer masquerait la panne au moment de la
    # regarder.
    site_ids = list(await session.scalars(select(Site.site_id).order_by(Site.site_id)))
    return await build_indicators(session, settings, site_ids, window_hours)


@router.get(
    "/sites/{site_id}/indicators",
    response_model=SiteIndicatorsOut,
    summary="Consulter les indicateurs de confiance d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def get_site_indicators(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
    settings: Config,
    window_hours: WindowHours = DEFAULT_WINDOW_HOURS,
) -> SiteIndicatorsOut:
    # Le 404 du référentiel passe avant tout calcul : des indicateurs à zéro
    # sur un site inconnu se liraient comme un site sain sans donnée, ce qui
    # est le contresens exact de ce que ces trois chiffres servent à dire.
    #
    # Un site connu mais sans aucune mesure, lui, répond 200 : ses blocs
    # portent des comptes à zéro et `is_stale` vaut vrai, ce qui est
    # l'information juste.
    await ensure_site_exists(session, site_id)
    indicators = await build_indicators(session, settings, [site_id], window_hours)
    return indicators[0]
