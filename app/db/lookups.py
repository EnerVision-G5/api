"""Consultations de référentiel partagées par plusieurs routeurs."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.energy import Site


async def ensure_site_exists(session: AsyncSession, site_id: str) -> None:
    """Lève 404 si le site est absent du référentiel.

    Partagée par les lectures de mesures et par le proxy de prédiction : un
    site inconnu doit répondre la même chose partout, message compris.
    """
    known = await session.scalar(select(Site.site_id).where(Site.site_id == site_id))
    if known is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site inconnu : {site_id}.",
        )
