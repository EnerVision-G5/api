"""Endpoints de simulation de pic et de synchronisation du référentiel.

Les deux relaient la source, que l'API ne connaît pas : elle passe par le
service d'inférence, seul côté predict à parler à l'API Mock. C'est aussi ce
qui explique le 502 — la panne peut être en amont de predict, et un 503 nu
enverrait chercher l'incident du mauvais côté.

Les deux écrivent, et ce sont les premiers endpoints du contrat à le faire :
d'où le rôle `writer` exigé ici alors que les lectures d'EV-11 se contentent
d'un utilisateur authentifié.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas. Les explications sont
donc en commentaires.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.lookups import ensure_site_exists
from app.db.session import get_db
from app.models.energy import Site
from app.models.simulation import (
    MAX_SPIKE_MINUTES,
    MIN_SPIKE_MINUTES,
    SimulationPic,
)
from app.models.user import AppUser
from app.predict_client import (
    ServingUnavailableError,
    request_sites,
    request_spike,
)
from app.schemas.auth import UserOut
from app.schemas.common import ErrorResponse
from app.schemas.energy import SiteOut
from app.schemas.serving import ServingSite
from app.schemas.simulation import SiteSyncOut, SpikeSimulationOut
from app.security import require_role

# Sans prefixe : les deux endpoints relaient la même source mais n'appartiennent
# pas à la même collection du contrat. Un préfixe commun donnerait
# /simulations/sites/sync, qui décrirait la synchronisation d'un référentiel
# comme une simulation.
router = APIRouter()

WritersOnly = Annotated[UserOut | None, Depends(require_role("writer"))]
DbSession = Annotated[AsyncSession, Depends(get_db)]

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
FORBIDDEN = {
    "model": ErrorResponse,
    "description": "Rôle writer requis pour cette opération.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}
BAD_GATEWAY = {
    "model": ErrorResponse,
    "description": "Le service d'inférence ou la source n'a pas répondu.",
}

# Colonnes du référentiel que la source décrit et que la base attend. Un site
# auquel il en manque une n'est pas écrit : une capacité absente ferait
# échouer la contrainte, un identifiant absent rendrait la ligne inutilisable.
SITE_REQUIRED = ("site_id", "site_type", "site_name", "capacity_kw", "status")

# Colonnes reposées par une synchronisation. `site_id` en est absente : c'est
# la clé du conflit, la réécrire n'aurait aucun sens.
SITE_UPDATED = ("site_type", "site_name", "location", "capacity_kw", "status")


def _spike_out(row: SimulationPic, username: str | None) -> SpikeSimulationOut:
    """Traduit la ligne archivée en réponse du contrat."""
    return SpikeSimulationOut(
        simulation_id=row.simulation_id,
        site_id=row.site_id,
        duration_minutes=row.duration_minutes,
        statut=row.statut,
        evenement=row.evenement,
        message=row.message,
        declenche_le=row.declenche_le,
        declenche_par=username,
        consumption_kw_constatee=(
            float(row.consumption_kw_constatee)
            if row.consumption_kw_constatee is not None
            else None
        ),
        data_quality_constatee=row.data_quality_constatee,
    )


async def _author_id(session: AsyncSession, user: UserOut | None) -> int | None:
    """Retrouve la clé de l'utilisateur, ou None si personne n'est identifié.

    AUTH_ENABLED à false ne donne aucun utilisateur : la simulation est
    archivée sans auteur plutôt que refusée, sinon le mode de développement
    local ne permettrait plus de déclencher un pic.
    """
    if user is None:
        return None
    return await session.scalar(
        select(AppUser.user_id).where(AppUser.oauth_subject == user.username),
    )


def _usable_sites(sites: list[ServingSite]) -> list[dict]:
    """Ne garde que les sites que la base peut accepter."""
    return [
        {
            "site_id": site.site_id,
            "site_type": site.site_type,
            "site_name": site.site_name,
            "location": site.location,
            "capacity_kw": site.capacity_kw,
            "status": site.status,
        }
        for site in sites
        if all(getattr(site, name, None) is not None for name in SITE_REQUIRED)
    ]


@router.post(
    "/simulations/spike/{site_id}",
    response_model=SpikeSimulationOut,
    tags=["simulations"],
    status_code=status.HTTP_201_CREATED,
    summary="Déclencher un pic de consommation sur un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
        status.HTTP_502_BAD_GATEWAY: BAD_GATEWAY,
    },
)
async def trigger_spike(
    site_id: Annotated[str, Path(description="Identifiant du site cible.")],
    session: DbSession,
    user: WritersOnly,
    duration_minutes: Annotated[
        int,
        Query(
            ge=MIN_SPIKE_MINUTES,
            le=MAX_SPIKE_MINUTES,
            description="Durée du pic simulé, en minutes.",
        ),
    ] = 30,
) -> SpikeSimulationOut:
    # Le site est vérifié en base AVANT l'appel : la clé étrangère de
    # simulation_pic le refuserait de toute façon, mais après avoir déclenché
    # un pic bien réel sur la source. Un 404 tardif laisserait la source dans
    # un état que la base ne raconte pas.
    await ensure_site_exists(session, site_id)

    try:
        spike = await request_spike(site_id, duration_minutes)
    except ServingUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Pic non déclenché : {error}",
        ) from error

    reading = spike.reading
    row = SimulationPic(
        site_id=site_id,
        duration_minutes=spike.duration_minutes,
        statut=spike.status,
        evenement=spike.event,
        message=spike.message or None,
        declenche_par=await _author_id(session, user),
        declenche_le=datetime.now(UTC),
        consumption_kw_constatee=(
            reading.consumption_kw if reading is not None else None
        ),
        data_quality_constatee=(reading.data_quality if reading is not None else None),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _spike_out(row, user.username if user else None)


@router.get(
    "/simulations/spike",
    response_model=list[SpikeSimulationOut],
    tags=["simulations"],
    summary="Lister les pics de consommation simulés",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_spikes(
    session: DbSession,
    user: Annotated[UserOut | None, Depends(require_role("reader", "writer"))],
    site_id: Annotated[
        str | None,
        Query(description="Restreindre aux pics de ce site."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Nombre maximal de pics rendus."),
    ] = 100,
) -> list[SpikeSimulationOut]:
    # Du plus récent au plus ancien : l'historique se consulte pour savoir ce
    # qui vient de se passer, jamais pour remonter à l'origine des temps.
    statement = (
        select(SimulationPic, AppUser.oauth_subject)
        .join(AppUser, AppUser.user_id == SimulationPic.declenche_par, isouter=True)
        .order_by(SimulationPic.declenche_le.desc())
        .limit(limit)
    )
    if site_id is not None:
        statement = statement.where(SimulationPic.site_id == site_id)
    rows = await session.execute(statement)
    return [_spike_out(row, username) for row, username in rows]


@router.post(
    "/sites/sync",
    response_model=SiteSyncOut,
    tags=["sites"],
    summary="Synchroniser le référentiel des sites depuis la source",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: FORBIDDEN,
        status.HTTP_502_BAD_GATEWAY: BAD_GATEWAY,
    },
)
async def sync_sites(session: DbSession, user: WritersOnly) -> SiteSyncOut:
    # UPSERT et non INSERT : le référentiel existe déjà, seed compris, et le
    # remplacer effacerait les sites qu'une source momentanément incomplète
    # ne servirait plus — avec eux toutes les mesures qui les référencent.
    try:
        served = await request_sites()
    except ServingUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Référentiel non synchronisé : {error}",
        ) from error

    usable = _usable_sites(served)
    if usable:
        statement = insert(Site).values(usable)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["site_id"],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in SITE_UPDATED
                },
            )
        )
        await session.commit()

    sites = await session.scalars(select(Site).order_by(Site.site_id))
    return SiteSyncOut(
        synchronized=len(usable),
        received=len(served),
        sites=[SiteOut.model_validate(site) for site in sites],
    )
