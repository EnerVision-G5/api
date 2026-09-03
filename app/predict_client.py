"""Client du service d'inférence (Serving).

L'API métier fait proxy : le dashboard n'appelle plus Serving directement, il
passe par POST /api/v1/predict. Ce module ne connaît que l'appel HTTP, la
traduction en réponse HTTP appartient au routeur.

Aucune transformation des données : la réponse de Serving est validée contre le
contrat, puis relayée telle quelle.
"""

import logging

import httpx

from app.core.config import get_settings
from app.schemas.prediction import PredictionOut, PredictionRequest

logger = logging.getLogger(__name__)

# Chemin fixé par le contrat de predict, donc non configurable : seule
# l'adresse du service change d'un environnement à l'autre. Le rendre
# paramétrable inviterait à le désaligner du contrat.
PREDICT_PATH = "/api/v1/predict"

# httpx.InvalidURL dérive d'Exception et non de HTTPError : il doit être cité
# à part, sans quoi une PREDICT_URL mal formée remonterait en 500 au lieu du
# 503 prévu par la politique d'échec.
_TRANSPORT_FAILURES = (httpx.HTTPError, httpx.InvalidURL)


def build_client() -> httpx.AsyncClient:
    """Fabrique le client HTTP de l'appel à Serving.

    Isolée pour servir de point de substitution aux tests, qui la remplacent
    par un client monté sur httpx.MockTransport. Sans cela, il faudrait un
    vrai service en écoute pour éprouver le délai dépassé ou une réponse 500.
    """
    return httpx.AsyncClient(timeout=get_settings().predict_timeout_seconds)


class ServingUnavailableError(RuntimeError):
    """Serving n'a pas répondu, ou n'a pas répondu une prévision exploitable.

    Le routeur la traduit en 503. Le message reste interne : ce qui part au
    client est générique, le détail va dans les logs.
    """


async def request_prediction(payload: PredictionRequest) -> PredictionOut:
    """Demande une prévision à Serving et la retourne telle quelle.

    Lève ServingUnavailableError sur délai dépassé, erreur de connexion,
    réponse non 200, ou réponse 200 dont la forme ne respecte pas le contrat.
    Ce dernier cas est traité comme une panne de Serving et non comme une
    erreur interne de l'API : le dashboard n'a rien à faire d'un 500 opaque
    quand c'est le service d'inférence qui déraille.
    """
    settings = get_settings()
    if not settings.predict_url:
        logger.error(
            "PREDICT_URL n'est pas configurée : le service d'inférence est"
            " injoignable par construction.",
        )
        raise ServingUnavailableError("PREDICT_URL absente de la configuration.")

    url = settings.predict_url.rstrip("/") + PREDICT_PATH
    try:
        async with build_client() as client:
            response = await client.post(url, json=payload.model_dump(mode="json"))
    except _TRANSPORT_FAILURES as error:
        logger.error(
            "Appel du service d'inférence en échec : url=%s site_id=%s erreur=%s: %s",
            url,
            payload.site_id,
            type(error).__name__,
            error,
        )
        raise ServingUnavailableError(str(error)) from error

    if response.status_code != httpx.codes.OK:
        logger.error(
            "Le service d'inférence a répondu %s : url=%s site_id=%s corps=%s",
            response.status_code,
            url,
            payload.site_id,
            response.text[:500],
        )
        raise ServingUnavailableError(f"Statut {response.status_code}.")

    try:
        return PredictionOut.model_validate(response.json())
    except ValueError as error:
        # ValueError couvre le JSON illisible comme la ValidationError de
        # Pydantic, qui en dérive.
        logger.error(
            "Réponse du service d'inférence non conforme au contrat :"
            " url=%s site_id=%s erreur=%s",
            url,
            payload.site_id,
            error,
        )
        raise ServingUnavailableError("Réponse non conforme au contrat.") from error
