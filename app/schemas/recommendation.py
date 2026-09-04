"""DTO des recommandations d'action (EV-32) et du registre des modèles.

Les recommandations ne sont pas stockées : elles se recalculent à chaque
lecture depuis les prévisions courantes. Les archiver figerait un conseil que
la prévision suivante contredirait, et un conseil périmé sur une facture
d'électricité est pire que pas de conseil.

Elles ne sont pas non plus des alertes. Une alerte constate ce qui vient de se
produire, une recommandation propose une action sur ce qui va se produire :
`AlertOut` regarde en arrière, celles-ci regardent devant.

Source de vérité du contrat. Toute modification exige une PR sur
enervision/docs/contracts et la relecture des trois consommateurs.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Trois natures, trois actions distinctes. Elles ne sont pas hiérarchisées :
# un site peut recevoir les trois en même temps, et chacune s'adresse à un
# interlocuteur différent — l'exploitant, l'acheteur d'énergie, la maintenance.
RecommendationType = Literal["predicted_peak", "capacity_overrun", "sensor_failure"]

# Même échelle que les alertes de la source, à dessein : un dashboard qui mêle
# les deux dans une même colonne ne doit pas avoir deux façons de les trier.
RecommendationSeverity = Literal["low", "medium", "high", "critical"]


class RecommendationOut(BaseModel):
    """Action proposée sur un site, à partir de sa prévision.

    `message` est prêt à afficher, mais les champs structurés à côté de lui ne
    sont pas décoratifs : un dashboard qui devrait extraire une heure d'une
    phrase française serait cassé par la première reformulation.
    """

    model_config = ConfigDict(from_attributes=True)

    type: RecommendationType = Field(description="Nature de l'action proposée.")
    severity: RecommendationSeverity = Field(
        description="Urgence de l'action, sur l'échelle des alertes.",
    )
    message: str = Field(description="Formulation prête à afficher.")
    window_start: datetime | None = Field(
        default=None,
        description=(
            "Début de la fenêtre concernée, ISO 8601 UTC. Nul pour une"
            " recommandation qui ne porte pas sur une plage."
        ),
    )
    window_end: datetime | None = Field(
        default=None,
        description="Fin de la fenêtre concernée, ISO 8601 UTC.",
    )
    at: datetime | None = Field(
        default=None,
        description=(
            "Instant précis visé, ISO 8601 UTC. Renseigné quand l'action porte"
            " sur un point et non sur une plage — un délestage, par exemple."
        ),
    )
    value_kw: float | None = Field(
        default=None,
        description=(
            "Grandeur en kilowatts attachée à l'action : puissance à délester,"
            " ou pointe prévue selon la nature."
        ),
    )


class RecommendationsOut(BaseModel):
    """Recommandations d'un site, et de quoi elles ont été tirées.

    `detail` porte la raison d'une liste vide, et ce n'est pas un luxe : « rien
    à signaler » et « aucune prévision disponible » se ressemblent au point
    d'être confondus, alors que le premier rassure et le second doit inquiéter.
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Site pour lequel les actions sont proposées.")
    generated_at: datetime = Field(
        description="Horodatage du calcul, ISO 8601 UTC. Rien n'est archivé.",
    )
    horizon_hours: int = Field(
        description="Profondeur de la fenêtre de prévision examinée, en heures.",
    )
    model_version: str | None = Field(
        default=None,
        description=(
            "Version du modèle ayant produit les prévisions examinées. Nulle"
            " quand aucune prévision n'a été trouvée. Un conseil ne vaut que"
            " ce que vaut le modèle qui le fonde."
        ),
    )
    items: list[RecommendationOut] = Field(
        description="Actions proposées, de la plus urgente à la moins urgente.",
    )
    detail: str | None = Field(
        default=None,
        description="Raison d'une liste vide, nulle quand des actions sont servies.",
    )


class ModelOut(BaseModel):
    """Modèle du registre, miroir applicatif du Model Registry MLflow.

    Alimenté par le service d'entraînement à chaque promotion, jamais par
    l'API. `actif` désigne la version servie : il en existe au plus une, et
    c'est elle qui fonde les prévisions et les recommandations.
    """

    model_config = ConfigDict(from_attributes=True)

    modele_id: int = Field(description="Identifiant technique du modèle.")
    nom: str = Field(description="Nom du modèle, par exemple enervision_xgboost.")
    version: str = Field(description="Version, telle que le registre MLflow la nomme.")
    mlflow_run_id: str | None = Field(
        default=None,
        description="Run MLflow ayant produit le modèle, pour la traçabilité.",
    )
    date_entrainement: datetime | None = Field(
        default=None,
        description=(
            "Horodatage du run d'entraînement, ISO 8601 UTC. Distinct de"
            " created_at, qui date l'entrée de la ligne dans le registre."
        ),
    )
    actif: bool = Field(description="Vrai pour la version actuellement promue.")
    created_at: datetime = Field(
        description="Entrée de la ligne dans le registre, ISO 8601 UTC.",
    )
