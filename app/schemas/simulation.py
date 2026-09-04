"""DTO des simulations de pic et de la synchronisation du référentiel.

L'API ne connaît pas l'API Mock : elle passe par le service d'inférence, qui
est le seul côté predict à lui parler. Ces DTO décrivent donc ce que l'API
publie à son dashboard, pas ce que la source sert — la traduction se fait
dans le routeur.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import DataQuality
from app.schemas.energy import SiteOut


class SpikeSimulationOut(BaseModel):
    """Pic de consommation déclenché sur la source, et ce qu'il a donné.

    `consumption_kw_constatee` est la valeur relue juste après le
    déclenchement. Elle est nulle quand la source a accepté le pic puis s'est
    tue : le pic a quand même eu lieu, et le dire en échec inviterait à
    rejouer l'appel, donc à superposer deux pics.
    """

    model_config = ConfigDict(from_attributes=True)

    simulation_id: int = Field(description="Identifiant de la simulation archivée.")
    site_id: str = Field(description="Site sur lequel le pic a été déclenché.")
    duration_minutes: int = Field(description="Durée demandée du pic, en minutes.")
    statut: str = Field(
        description="Statut rendu par la source, simulated en cas de succès.",
    )
    evenement: str = Field(
        description="Nature de l'événement déclenché, consumption_spike.",
    )
    message: str | None = Field(
        default=None,
        description="Message rendu par la source, lisible tel quel.",
    )
    declenche_le: datetime = Field(
        description="Horodatage du déclenchement, ISO 8601 UTC.",
    )
    declenche_par: str | None = Field(
        default=None,
        description=(
            "Utilisateur ayant déclenché le pic, nul si l'authentification"
            " est désactivée."
        ),
    )
    consumption_kw_constatee: float | None = Field(
        default=None,
        description="Puissance relue juste après le pic, nulle si la source s'est tue.",
    )
    data_quality_constatee: DataQuality | None = Field(
        default=None,
        description="Qualification de la mesure relue, nulle si la source s'est tue.",
    )


class SiteSyncOut(BaseModel):
    """Résultat d'une synchronisation du référentiel depuis la source.

    `synchronized` compte les lignes écrites, pas les sites servis : une
    source qui décrit incomplètement un site le voit écarté, et l'écart entre
    les deux nombres est ce qui le signale.
    """

    model_config = ConfigDict(from_attributes=True)

    synchronized: int = Field(description="Nombre de sites écrits en base.")
    received: int = Field(description="Nombre de sites servis par la source.")
    sites: list[SiteOut] = Field(
        description="Référentiel complet après synchronisation, trié par identifiant.",
    )
