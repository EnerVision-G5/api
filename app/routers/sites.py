"""Endpoints sites et mesures, branchés sur PostgreSQL / TimescaleDB (EV-11).

Les valeurs sortent de la base telles qu'elles y sont stockées : aucun null
n'est comblé, aucune valeur imputée n'est recalculée ici, data_quality et
null_reasons traversent intacts. L'imputation appartient à l'ETL (EV-08), la
qualification de la mesure appartient à la source.

Aucune fonction d'endpoint ne porte de docstring, et ce n'est pas un oubli :
FastAPI la publierait comme `description` de l'opération dans la
spécification, que le contrat gelé ne contient pas. Le job contract-drift
échouerait aussitôt. Les explications sont donc en commentaires.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.lookups import ensure_site_exists
from app.db.session import get_db
from app.models.energy import Mesure, Site
from app.schemas.common import ErrorResponse, PaginationMeta
from app.schemas.energy import EnergyReadingOut, ReadingsPage, SiteOut
from app.security import CurrentUser

router = APIRouter(prefix="/sites", tags=["sites"])

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}

SiteId = Annotated[str, Path(description="Identifiant du site.")]
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Colonnes d'une mesure telle que le contrat la publie. site_type n'est pas
# porté par la table mesure : il est dénormalisé depuis le référentiel par la
# jointure, comme l'annonce la description du champ dans EnergyReadingOut.
READING_COLUMNS = (
    Mesure.timestamp.label("timestamp"),
    Mesure.site_id,
    Site.site_type,
    Mesure.consumption_kw,
    Mesure.consumption_kwh,
    Mesure.voltage_v,
    Mesure.current_a,
    Mesure.power_factor,
    Mesure.temperature_celsius,
    Mesure.humidity_percent,
    Mesure.null_reasons,
    Mesure.data_quality,
    Mesure.consumption_kw_imputed,
    Mesure.imputation_method,
)


def _as_utc(moment: datetime) -> datetime:
    """Ramène un horodatage de requête en UTC.

    Le contrat annonce de l'ISO 8601 UTC, mais un client peut omettre le
    décalage. La colonne ts étant TIMESTAMPTZ, comparer un datetime naïf
    ferait échouer le driver : l'absence de décalage est donc interprétée
    comme de l'UTC plutôt que remontée en 500.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _window_filters(
    site_id: str,
    start_time: datetime,
    end_time: datetime,
) -> Sequence[ColumnElement[bool]]:
    """Filtres communs au comptage et à la page : fenêtre à bornes incluses."""
    return (
        Mesure.site_id == site_id,
        Mesure.timestamp >= start_time,
        Mesure.timestamp <= end_time,
    )


@router.get(
    "",
    response_model=list[SiteOut],
    summary="Lister les sites supervisés",
    responses={status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED},
)
async def list_sites(session: DbSession, user: CurrentUser) -> list[SiteOut]:
    # Le référentiel complet, trié par identifiant. Les 7 sites tiennent en une
    # seule réponse : le contrat ne prévoit pas de pagination ici, un tri stable
    # suffit à rendre la sortie déterministe.
    sites = await session.scalars(select(Site).order_by(Site.site_id))
    return [SiteOut.model_validate(site) for site in sites]


@router.get(
    "/{site_id}",
    response_model=SiteOut,
    summary="Consulter un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
    },
)
async def get_site(site_id: SiteId, session: DbSession, user: CurrentUser) -> SiteOut:
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site inconnu : {site_id}.",
        )
    return SiteOut.model_validate(site)


@router.get(
    "/{site_id}/readings",
    response_model=ReadingsPage,
    summary="Lister les mesures d'un site sur une fenêtre de temps",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_readings(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
    start_time: Annotated[
        datetime,
        Query(description="Borne inférieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    end_time: Annotated[
        datetime,
        Query(description="Borne supérieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Taille de page, entre 1 et 1000."),
    ] = 100,
    offset: Annotated[
        int,
        Query(ge=0, description="Décalage appliqué au début de la collection."),
    ] = 0,
) -> ReadingsPage:
    # Page de mesures triées par horodatage croissant.
    #
    # La fenêtre est validée avant toute consultation de la base : des bornes
    # inversées rendent la requête malformée quel que soit le site, elle est
    # donc refusée en 422 avant le 404 du référentiel.
    #
    # meta.total compte les mesures de la fenêtre entière et non celles de la
    # page, pour que le client sache combien de pages il reste à parcourir.
    start_utc, end_utc = _as_utc(start_time), _as_utc(end_time)
    if start_utc > end_utc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start_time doit être antérieur ou égal à end_time.",
        )
    await ensure_site_exists(session, site_id)

    filters = _window_filters(site_id, start_utc, end_utc)
    total = await session.scalar(
        select(func.count()).select_from(Mesure).where(*filters),
    )
    rows = await session.execute(
        select(*READING_COLUMNS)
        .join(Site, Site.site_id == Mesure.site_id)
        .where(*filters)
        .order_by(Mesure.timestamp)
        .limit(limit)
        .offset(offset),
    )
    return ReadingsPage(
        items=[EnergyReadingOut.model_validate(row) for row in rows],
        meta=PaginationMeta(total=total or 0, limit=limit, offset=offset),
    )


@router.get(
    "/{site_id}/readings/latest",
    response_model=EnergyReadingOut,
    summary="Consulter la dernière mesure connue d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
    },
)
async def get_latest_reading(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
) -> EnergyReadingOut:
    # Mesure la plus récente du site.
    #
    # Un site connu mais dépourvu de mesure sort aussi en 404 : le contrat ne
    # documente que 401 et 404 sur cet endpoint et son modèle de réponse n'est
    # pas nullable, aucun autre code n'est disponible. Le message distingue les
    # deux causes.
    await ensure_site_exists(session, site_id)
    row = (
        await session.execute(
            select(*READING_COLUMNS)
            .join(Site, Site.site_id == Mesure.site_id)
            .where(Mesure.site_id == site_id)
            .order_by(Mesure.timestamp.desc())
            .limit(1),
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Aucune mesure enregistrée pour le site {site_id}.",
        )
    return EnergyReadingOut.model_validate(row)
