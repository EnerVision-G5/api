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
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.lookups import ensure_site_exists
from app.db.session import get_db
from app.models.energy import Mesure, MesureExclu, Site
from app.models.observation import CapteurEtat, CapteurPanne
from app.schemas.common import ErrorResponse, PaginationMeta
from app.schemas.energy import (
    EnergyReadingOut,
    ReadingsPage,
    SensorFailureOut,
    SensorHealthOut,
    SiteOut,
)
from app.schemas.recommendation import RecommendationsOut
from app.security import CurrentUser
from app.services.recommendations import (
    DEFAULT_HORIZON_HOURS,
    MAX_HORIZON_HOURS,
    recommendations_for,
)
from app.timewindow import parse_window

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
    # Mise à l'écart, ramenée par jointure externe : `mesure_exclu` ne porte
    # qu'une ligne par mesure écartée, et la plupart ne le sont pas.
    #
    # `excluded` est calculé et non stocké : la seule vérité est l'existence de
    # la ligne dans `mesure_exclu`, et un booléen dupliqué sur `mesure`
    # divergerait le jour où l'une des deux écritures manquerait.
    (MesureExclu.raison.is_not(None)).label("excluded"),
    MesureExclu.raison.label("exclusion_reason"),
)

# Jointure externe vers la mise à l'écart, sur la clé naturelle de `mesure`.
# Externe et jamais interne : une jointure interne ne rendrait que les mesures
# écartées, soit l'inverse de ce que le contrat publie.
EXCLUSION_JOIN = (
    MesureExclu,
    (MesureExclu.site_id == Mesure.site_id)
    & (MesureExclu.timestamp == Mesure.timestamp),
)


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
    start_utc, end_utc = parse_window(start_time, end_time)
    await ensure_site_exists(session, site_id)

    filters = _window_filters(site_id, start_utc, end_utc)
    total = await session.scalar(
        select(func.count()).select_from(Mesure).where(*filters),
    )
    rows = await session.execute(
        select(*READING_COLUMNS)
        .join(Site, Site.site_id == Mesure.site_id)
        .outerjoin(*EXCLUSION_JOIN)
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
            .outerjoin(*EXCLUSION_JOIN)
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


@router.get(
    "/{site_id}/sensors",
    response_model=list[SensorHealthOut],
    summary="Consulter l'état des capteurs d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
    },
)
async def list_sensors(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
) -> list[SensorHealthOut]:
    # État courant des capteurs, trié par nom pour une sortie déterministe.
    #
    # Une liste vide n'est pas une erreur : elle dit que le collecteur n'a
    # encore rien relevé sur ce site. La distinguer d'un 404 compte, parce que
    # les deux appellent des conduites opposées — vérifier le référentiel dans
    # un cas, vérifier la collecte dans l'autre.
    await ensure_site_exists(session, site_id)
    rows = await session.scalars(
        select(CapteurEtat)
        .where(CapteurEtat.site_id == site_id)
        .order_by(CapteurEtat.capteur),
    )
    return [SensorHealthOut.model_validate(row) for row in rows]


@router.get(
    "/{site_id}/sensors/history",
    response_model=list[SensorFailureOut],
    summary="Lister les pannes de capteur d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_sensor_failures(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
    capteur: Annotated[
        str | None,
        Query(description="Restreindre aux pannes de ce capteur."),
    ] = None,
    ongoing: Annotated[
        bool | None,
        Query(description="Vrai pour les seules pannes en cours, faux pour les closes."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Nombre maximal d'épisodes rendus."),
    ] = 100,
) -> list[SensorFailureOut]:
    # Épisodes du plus récent au plus ancien.
    #
    # Distinct de /sensors, qui dit l'état présent : ici on demande ce qui
    # s'est passé, là-bas ce qui se passe. Un capteur tombé puis rétabli
    # depuis n'apparaît que dans cette route.
    await ensure_site_exists(session, site_id)
    statement = (
        select(CapteurPanne)
        .where(CapteurPanne.site_id == site_id)
        .order_by(CapteurPanne.debut_le.desc())
        .limit(limit)
    )
    if capteur is not None:
        statement = statement.where(CapteurPanne.capteur == capteur)
    if ongoing is True:
        statement = statement.where(CapteurPanne.fin_le.is_(None))
    if ongoing is False:
        statement = statement.where(CapteurPanne.fin_le.is_not(None))

    rows = await session.scalars(statement)
    return [
        SensorFailureOut(
            site_id=row.site_id,
            capteur=row.capteur,
            started_at=row.debut_le,
            ended_at=row.fin_le,
            failing_until=row.failing_until,
            ongoing=row.fin_le is None,
        )
        for row in rows
    ]


@router.get(
    "/{site_id}/recommendations",
    response_model=RecommendationsOut,
    summary="Proposer des actions à partir de la prévision d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_recommendations(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
    horizon_hours: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_HORIZON_HOURS,
            description="Profondeur de la fenêtre de prévision examinée.",
        ),
    ] = DEFAULT_HORIZON_HOURS,
) -> RecommendationsOut:
    # Rien n'est archivé : les actions se recalculent à chaque lecture depuis
    # les prévisions courantes. Les figer donnerait un conseil que le
    # rafraîchissement suivant contredirait.
    #
    # Le site est chargé et non seulement vérifié : `capacity_kw` est l'entrée
    # de la règle de dépassement.
    site = await session.get(Site, site_id)
    if site is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site inconnu : {site_id}.",
        )
    return await recommendations_for(session, site, horizon_hours)
