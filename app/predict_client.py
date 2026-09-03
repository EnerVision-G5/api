"""Client du service d'inférence (Serving).

L'API est **cliente** de Serving, elle ne réexpose pas sa route : le job de
prédiction appelle `POST /api/v1/predict` et archive le résultat. Ce module ne
connaît que l'appel HTTP ; ce qu'on fait de l'échec appartient à l'appelant.
"""

import logging

import httpx

from app.core.config import get_settings
from app.schemas.serving import ServingPrediction

logger = logging.getLogger(__name__)

# Chemin fixé par le contrat de predict, donc non configurable : seule
# l'adresse du service change d'un environnement à l'autre. Le rendre
# paramétrable inviterait à le désaligner du contrat.
PREDICT_PATH = "/api/v1/predict"

# httpx.InvalidURL dérive d'Exception et non de HTTPError : il doit être cité
# à part, sans quoi une PREDICT_URL mal formée traverserait le filet.
_TRANSPORT_FAILURES = (httpx.HTTPError, httpx.InvalidURL)


class ServingUnavailableError(RuntimeError):
    """Serving n'a pas répondu, ou n'a pas répondu une prévision exploitable.

    Le message porte le détail à journaliser. Le job la rattrape site par
    site : une inférence en échec n'arrête pas la tournée.
    """


def build_client() -> httpx.AsyncClient:
    """Fabrique le client HTTP de l'appel à Serving.

    Isolée pour servir de point de substitution aux tests, qui la remplacent
    par un client monté sur httpx.MockTransport. Sans cela, il faudrait un
    vrai service en écoute pour éprouver le délai dépassé ou une réponse 500.
    """
    return httpx.AsyncClient(timeout=get_settings().predict_timeout_seconds)


async def request_prediction(site_id: str, horizon_hours: int) -> ServingPrediction:
    """Demande la prévision d'un site à Serving.

    Lève ServingUnavailableError sur délai dépassé, erreur de connexion,
    réponse non 200, ou réponse 200 dont la forme ne respecte pas le contrat
    de predict. Ce dernier cas est un échec de Serving et non de l'API : rien
    d'exploitable n'en sort, il n'y a donc rien à archiver.
    """
    settings = get_settings()
    if not settings.predict_url:
        raise ServingUnavailableError(
            "PREDICT_URL absente de la configuration : le service d'inférence"
            " est injoignable par construction.",
        )

    url = settings.predict_url.rstrip("/") + PREDICT_PATH
    payload = {"site_id": site_id, "horizon_hours": horizon_hours}
    try:
        async with build_client() as client:
            response = await client.post(url, json=payload)
    except _TRANSPORT_FAILURES as error:
        raise ServingUnavailableError(
            f"{type(error).__name__}: {error}",
        ) from error

    if response.status_code != httpx.codes.OK:
        raise ServingUnavailableError(
            f"statut {response.status_code}, corps {response.text[:200]!r}",
        )

    try:
        return ServingPrediction.model_validate(response.json())
    except ValueError as error:
        # ValueError couvre le JSON illisible comme la ValidationError de
        # Pydantic, qui en dérive.
        raise ServingUnavailableError(
            f"réponse non conforme au contrat de predict : {error}",
        ) from error
