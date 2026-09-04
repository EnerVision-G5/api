"""DTO de la réponse du service d'inférence.

Ce ne sont **pas** des DTO du contrat de l'API : aucune route ne les expose,
ils ne figurent donc pas dans openapi-api.json. Ils décrivent ce que Serving
renvoie, et leurs formes sont celles de son contrat
(enervision/docs/contracts/openapi-predict.json).

Les nommer avec un préfixe Serving évite de les confondre avec
PredictionPointOut, qui est ce que l'API publie, à plat et par point.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServingPredictionPoint(BaseModel):
    """Point de la série prédite, tel que Serving le renvoie."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime = Field(description="Horodatage du point prédit, ISO 8601 UTC.")
    predicted_consumption_kw: float = Field(description="Puissance prédite en kilowatts.")
    lower_bound_kw: float | None = Field(description="Borne basse de l'intervalle.")
    upper_bound_kw: float | None = Field(description="Borne haute de l'intervalle.")


class ServingPrediction(BaseModel):
    """Prévision complète renvoyée par Serving pour un site."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site prédit.")
    model_version: str = Field(description="Version du modèle ayant servi la réponse.")
    generated_at: datetime = Field(description="Horodatage de production, ISO 8601 UTC.")
    points: list[ServingPredictionPoint] = Field(description="Série prédite.")


class ServingSite(BaseModel):
    """Site du référentiel de la source, relayé par Serving.

    Tout est optionnel sauf l'identifiant, comme dans le contrat de predict.
    Exiger la forme complète ici ferait échouer la synchronisation entière sur
    un seul site mal décrit : c'est le routeur qui écarte les sites que la
    base ne peut pas accepter, un par un.
    """

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Identifiant du site.")
    site_type: str | None = Field(
        default=None, description="Type de site servi par la source."
    )
    site_name: str | None = Field(default=None, description="Nom lisible du site.")
    location: str | None = Field(default=None, description="Localisation du site.")
    capacity_kw: float | None = Field(
        default=None, description="Puissance installée en kilowatts."
    )
    status: str | None = Field(default=None, description="État déclaré par la source.")


class ServingSpikeReading(BaseModel):
    """Mesure relue par Serving juste après un pic simulé.

    Seuls les deux champs dont l'API fait quelque chose sont déclarés. Le
    reste de la mesure appartient à `mesure`, que le collecteur remplit : le
    recopier ici donnerait deux vérités sur le même instant.
    """

    model_config = ConfigDict(from_attributes=True)

    consumption_kw: float | None = Field(
        default=None, description="Puissance instantanée relue, en kilowatts."
    )
    data_quality: str = Field(description="Qualification rendue par la source.")


class ServingSpike(BaseModel):
    """Résultat d'une simulation de pic, tel que Serving le renvoie."""

    model_config = ConfigDict(from_attributes=True)

    site_id: str = Field(description="Site sur lequel le pic a été déclenché.")
    status: str = Field(description="Statut rendu par la source.")
    event: str = Field(description="Nature de l'événement déclenché.")
    duration_minutes: int = Field(description="Durée demandée du pic, en minutes.")
    message: str = Field(description="Message rendu par la source.")
    simulated_at: datetime = Field(description="Horodatage du déclenchement.")
    reading: ServingSpikeReading | None = Field(
        default=None,
        description="Mesure relue après le pic, absente si la source s'est tue.",
    )
