"""Archivage des prédictions produites par le service d'inférence.

Une seule porte d'entrée, `store_prediction`.

`model_version` n'est pas écrit : la colonne n'existe pas, et c'est voulu.
La valeur est celle de `modele.version`, atteinte par le join sur `modele_id`.
Archiver la prévision demande donc de traduire d'abord la version annoncée par
le service en clé du registre, ce que fait `resolve_modele_id`.

Idempotence : la clé du schéma v1.0 est (modele_id, site_id, ts_cible) et le
conflit met la ligne à jour. Rejouer le job ne duplique rien, et une nouvelle
génération remplace la précédente pour le même instant cible.
"""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prediction import Modele, Prediction
from app.schemas.serving import ServingPrediction

logger = logging.getLogger(__name__)


class UnknownModelError(RuntimeError):
    """La version annoncée par le service ne désigne aucune ligne de `modele`."""


def _as_decimal(value: float | None) -> Decimal | None:
    """Convertit une valeur du contrat vers la colonne NUMERIC(10,2).

    Le passage par str évite les artefacts de la représentation binaire des
    flottants : Decimal(0.1) vaut 0.1000000000000000055511151231257827, là où
    Decimal("0.1") vaut exactement 0.1.
    """
    if value is None:
        return None
    return Decimal(str(value))


async def resolve_modele_id(session: AsyncSession, model_version: str) -> int:
    """Traduit la version annoncée par le service en clé du registre.

    Lève UnknownModelError quand la version ne désigne pas un modèle unique.
    Deux situations, distinguées dans le message :

    - aucune ligne : le service sert un modèle chargé par chemin d'artefact
      et non par alias, cas où il retombe sur son identifiant interne, qui
      n'est pas une version de registre. C'est un déploiement dégradé ;
    - plusieurs lignes sans version active unique : deux modèles de noms
      différents portent la même version, la clé de `modele` étant
      (nom, version). Choisir au hasard reviendrait à attribuer la prévision
      à un modèle qui ne l'a pas produite.

    Quand plusieurs lignes portent la version mais qu'une seule est active,
    c'est elle : `actif` désigne le modèle en service, et la promotion
    garantit qu'il n'y en a qu'un par nom.
    """
    rows = (
        await session.execute(
            select(Modele.modele_id, Modele.actif).where(
                Modele.version == model_version,
            ),
        )
    ).all()

    if len(rows) == 1:
        return rows[0][0]

    if not rows:
        raise UnknownModelError(
            f"aucun modele en base pour la version {model_version!r} :"
            " le service sert-il un modele charge par chemin d'artefact"
            " plutot que par alias ?",
        )

    active = [row[0] for row in rows if row[1]]
    if len(active) == 1:
        return active[0]

    raise UnknownModelError(
        f"{len(rows)} modeles portent la version {model_version!r} et"
        f" {len(active)} sont actifs : la cle de modele est (nom, version) et"
        " le contrat ne transporte pas le nom.",
    )


async def store_prediction(
    session: AsyncSession,
    prediction: ServingPrediction,
) -> int:
    """Archive les points d'une prévision et retourne le nombre de lignes visées.

    Lève ce que lèvent la résolution du modèle et la base : c'est l'appelant,
    le job, qui décide qu'un site en échec n'arrête pas la tournée. Masquer
    l'erreur ici priverait le résumé d'exécution de la seule information qui
    compte.
    """
    if not prediction.points:
        logger.info(
            "Aucun point à archiver pour le site %s (série vide).",
            prediction.site_id,
        )
        return 0

    modele_id = await resolve_modele_id(session, prediction.model_version)

    rows = [
        {
            "modele_id": modele_id,
            "site_id": prediction.site_id,
            "ts_cible": point.timestamp,
            "consumption_kw_predite": _as_decimal(point.predicted_consumption_kw),
            "lower_bound_kw": _as_decimal(point.lower_bound_kw),
            "upper_bound_kw": _as_decimal(point.upper_bound_kw),
            "generated_at": prediction.generated_at,
        }
        for point in prediction.points
    ]

    statement = pg_insert(Prediction).values(rows)
    # DO UPDATE : une nouvelle prévision du même instant cible par le même
    # modèle remplace la précédente, la plus fraîche étant celle qui vaut.
    # generated_at suit, sans quoi la ligne mise à jour porterait la date de
    # production de la prévision remplacée.
    statement = statement.on_conflict_do_update(
        index_elements=["modele_id", "site_id", "ts_cible"],
        set_={
            "consumption_kw_predite": statement.excluded.consumption_kw_predite,
            "lower_bound_kw": statement.excluded.lower_bound_kw,
            "upper_bound_kw": statement.excluded.upper_bound_kw,
            "generated_at": statement.excluded.generated_at,
            "created_at": func.now(),
        },
    )
    await session.execute(statement)
    await session.commit()
    return len(rows)
