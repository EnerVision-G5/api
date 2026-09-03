"""Persistance des prédictions servies par le proxy.

Une seule porte d'entrée, `store_prediction`, qui **ne lève jamais** : la
prédiction a déjà été obtenue de Serving et le dashboard doit la recevoir,
qu'on ait su l'archiver ou non (politique d'échec actée). Tout incident se
solde par un log d'erreur et un retour à zéro ligne écrite.

Persistance volontairement partielle, faute de colonnes : la table
`prediction` ne porte ni `lower_bound_kw`, ni `upper_bound_kw`, ni
`model_version`, ni `generated_at`. Ce qui est archivé, c'est le site,
l'horodatage cible, la valeur prédite et le modèle résolu.
"""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prediction import Modele, Prediction
from app.schemas.prediction import PredictionOut

logger = logging.getLogger(__name__)


async def resolve_modele_id(session: AsyncSession, model_version: str) -> int | None:
    """Traduit le model_version du contrat en clé de la table modele.

    Rend None si la version ne désigne pas exactement un modèle, et dit
    laquelle des deux raisons dans les logs :

    - aucune ligne : le registre `modele` ne connaît pas ce modèle. Il est
      alimenté par l'équipe Data, pas par l'API, et reste vide à ce jour ;
    - plusieurs lignes : la contrainte d'unicité porte sur (nom, version) et
      le contrat ne transporte pas le nom. Choisir serait deviner.

    Dans les deux cas la prédiction n'est pas archivée, mais elle est servie.
    """
    matches = (
        await session.scalars(
            select(Modele.modele_id).where(Modele.version == model_version),
        )
    ).all()

    if len(matches) == 1:
        return matches[0]

    if not matches:
        logger.error(
            "Prediction non archivee : aucun modele en base pour"
            " model_version=%r. Le registre modele est alimente par l'equipe"
            " Data, l'API ne l'ecrit pas.",
            model_version,
        )
    else:
        logger.error(
            "Prediction non archivee : %d modeles portent model_version=%r."
            " La cle unique est (nom, version) et le contrat ne fournit pas le"
            " nom, la resolution est donc ambigue.",
            len(matches),
            model_version,
        )
    return None


async def store_prediction(session: AsyncSession, prediction: PredictionOut) -> int:
    """Archive les points d'une prédiction. Retourne le nombre de lignes écrites.

    Ne lève jamais : la réponse est déjà due au client. Retourne 0 quand rien
    n'a pu être écrit, après avoir dit pourquoi dans les logs.
    """
    try:
        modele_id = await resolve_modele_id(session, prediction.model_version)
        if modele_id is None:
            return 0

        if not prediction.points:
            return 0

        rows = [
            {
                "modele_id": modele_id,
                "site_id": prediction.site_id,
                "ts_cible": point.timestamp,
                "consumption_kw_predite": Decimal(
                    str(point.predicted_consumption_kw),
                ),
            }
            for point in prediction.points
        ]

        statement = pg_insert(Prediction).values(rows)
        # Un même instant cible peut être prédit plusieurs fois : la dernière
        # prédiction remplace la précédente, la plus fraîche étant celle que
        # le dashboard affiche. created_at suit, pour que la fraîcheur reste
        # lisible en base.
        statement = statement.on_conflict_do_update(
            index_elements=["modele_id", "site_id", "ts_cible"],
            set_={
                "consumption_kw_predite": statement.excluded.consumption_kw_predite,
                "created_at": func.now(),
            },
        )
        await session.execute(statement)
        await session.commit()
        return len(rows)
    except Exception as error:
        # Filet volontairement large. Ce n'est pas de la paresse : la
        # prédiction est déjà obtenue et due au client, et aucune défaillance
        # d'archivage ne doit la lui retirer. Se limiter à SQLAlchemyError
        # laisserait passer une erreur du driver ou une conversion inattendue,
        # qui transformerait un archivage raté en 500 alors que le service a
        # rendu ce qu'on lui demandait.
        try:
            await session.rollback()
        except Exception:
            # Session déjà perdue : rien de plus à sauver, la réponse part
            # quand même et get_db la refermera.
            logger.exception("Rollback impossible apres un echec d'archivage.")
        logger.error(
            "Echec d'archivage de la prediction : site_id=%s model_version=%s"
            " points=%d erreur=%s: %s",
            prediction.site_id,
            prediction.model_version,
            len(prediction.points),
            type(error).__name__,
            error,
        )
        return 0
