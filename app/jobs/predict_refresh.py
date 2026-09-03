"""Rafraîchissement périodique des prédictions.

Interroge le service d'inférence pour chaque site actif et archive le résultat.
Destiné à un conteneur cron dédié, à la manière du Collector :

    python -m app.jobs.predict_refresh

Aucun ordonnanceur n'est embarqué dans le processus de l'API : le déclenchement
appartient à l'infrastructure, ce qui laisse l'API sans état et permet de
rejouer le job à la main sans la redémarrer.

Un site en échec n'arrête pas la tournée. Le code de sortie ne vaut 1 que si
**aucun** site n'a abouti : une panne isolée ne doit pas réveiller
l'astreinte, une panne générale doit le faire.

Variables attendues : DATABASE_URL, PREDICT_URL, PREDICT_HORIZON_HOURS
(défaut 24), PREDICT_TIMEOUT_SECONDS (défaut 10).
"""

import asyncio
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_engine, get_session_factory
from app.models.energy import Site
from app.predict_client import (
    ServingNotReadyError,
    ServingUnavailableError,
    request_prediction,
)
from app.predictions_store import UnknownModelError, store_prediction

logger = logging.getLogger("app.jobs.predict_refresh")

ACTIVE_STATUS = "active"


@dataclass(frozen=True)
class SiteOutcome:
    """Résultat de la tournée pour un site."""

    site_id: str
    stored: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error is None:
            return f"{self.site_id} ok {self.stored} points"
        return f"{self.site_id} echec {self.error}"


async def active_site_ids(session: AsyncSession) -> list[str]:
    """Identifiants des sites à prédire, triés pour un journal déterministe."""
    rows = await session.scalars(
        select(Site.site_id).where(Site.status == ACTIVE_STATUS).order_by(Site.site_id),
    )
    return list(rows)


async def refresh_site(site_id: str, horizon_hours: int) -> SiteOutcome:
    """Prédit un site et archive le résultat, sans jamais propager d'échec.

    Chaque site a sa propre session : une transaction avortée sur l'un ne doit
    pas emporter les suivants.
    """
    try:
        prediction = await request_prediction(site_id, horizon_hours)
    except ServingNotReadyError as error:
        # Registre vide : ce n'est pas un incident, c'est l'état du projet
        # tant qu'aucun modèle n'est promu. Journalisé en avertissement pour
        # que l'astreinte ne parte pas chercher une panne de service.
        logger.warning("Rien à servir pour %s : %s", site_id, error)
        return SiteOutcome(site_id, stored=0, error=str(error))
    except ServingUnavailableError as error:
        logger.error("Prédiction indisponible pour %s : %s", site_id, error)
        return SiteOutcome(site_id, stored=0, error=str(error))

    try:
        async with get_session_factory()() as session:
            stored = await store_prediction(session, prediction)
    except UnknownModelError as error:
        # La prévision a bien été calculée, mais rien en base ne dit quel
        # modèle l'a produite : l'archiver demanderait d'inventer ce
        # rattachement, que prediction.modele_id exige.
        logger.error("Modèle non résolu pour %s : %s", site_id, error)
        return SiteOutcome(site_id, stored=0, error=f"modele non resolu: {error}")
    except Exception as error:
        # Filet large : l'échec d'un site est une ligne du résumé, pas
        # l'interruption de la tournée. Se limiter aux erreurs SQLAlchemy
        # laisserait une erreur de driver arrêter les six autres sites.
        logger.error(
            "Archivage impossible pour %s : %s: %s",
            site_id,
            type(error).__name__,
            error,
        )
        return SiteOutcome(site_id, stored=0, error=f"{type(error).__name__}: {error}")

    return SiteOutcome(site_id, stored=stored)


async def refresh_all() -> int:
    """Exécute la tournée complète et retourne le code de sortie du processus."""
    settings = get_settings()
    horizon_hours = settings.predict_horizon_hours

    async with get_session_factory()() as session:
        site_ids = await active_site_ids(session)

    if not site_ids:
        # Un référentiel sans site actif n'est pas un succès : il n'y a rien à
        # prédire, et c'est probablement une base non initialisée.
        logger.error("Aucun site actif dans le référentiel : rien à prédire.")
        return 1

    logger.info(
        "Rafraîchissement de %d site(s) sur un horizon de %d h.",
        len(site_ids),
        horizon_hours,
    )
    outcomes = [await refresh_site(site_id, horizon_hours) for site_id in site_ids]

    for outcome in outcomes:
        logger.info("%s", outcome.summary())

    succeeded = [outcome for outcome in outcomes if outcome.succeeded]
    stored = sum(outcome.stored for outcome in succeeded)
    logger.info(
        "Bilan : %d/%d site(s) traité(s), %d ligne(s) archivée(s).",
        len(succeeded),
        len(outcomes),
        stored,
    )

    if not succeeded:
        logger.error("Tous les sites ont échoué.")
        return 1
    return 0


async def main() -> int:
    """Point d'entrée asynchrone : exécute la tournée et libère le moteur."""
    try:
        return await refresh_all()
    finally:
        # Le processus s'arrête juste après : fermer le pool évite un
        # avertissement de connexions abandonnées à la sortie.
        await get_engine().dispose()


def run() -> int:
    """Point d'entrée synchrone, utilisé par python -m."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(run())
