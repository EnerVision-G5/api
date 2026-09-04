"""Client du service d'inférence (Serving).

L'API est **cliente** de Serving. Trois appels, et une raison distincte pour
chacun : le job de prédiction appelle `POST /api/v1/predict` et archive le
résultat ; le référentiel et la simulation de pic passent par Serving parce
que l'API ne connaît pas l'API Mock et ne doit pas la connaître. Lui donner
son adresse ferait d'elle un second client de la source, avec sa propre façon
de la lire.

Ce module ne connaît que l'appel HTTP ; ce qu'on fait de l'échec appartient à
l'appelant.
"""

import logging

import httpx

from app.core.config import get_settings
from app.schemas.serving import ServingPrediction, ServingSite, ServingSpike

logger = logging.getLogger(__name__)

# Chemin fixé par le contrat de predict, donc non configurable : seule
# l'adresse du service change d'un environnement à l'autre. Le rendre
# paramétrable inviterait à le désaligner du contrat.
PREDICT_PATH = "/api/v1/predict"
SITES_PATH = "/api/v1/sites"
SPIKE_PATH = "/api/v1/simulate/spike/{site_id}"

# httpx.InvalidURL dérive d'Exception et non de HTTPError : il doit être cité
# à part, sans quoi une PREDICT_URL mal formée traverserait le filet.
_TRANSPORT_FAILURES = (httpx.HTTPError, httpx.InvalidURL)


class ServingUnavailableError(RuntimeError):
    """Serving n'a pas répondu, ou n'a pas répondu une prévision exploitable.

    Le message porte le détail à journaliser. Le job la rattrape site par
    site : une inférence en échec n'arrête pas la tournée.
    """


class ServingNotReadyError(ServingUnavailableError):
    """Serving répond, mais n'a rien à servir : aucun modèle résolu.

    Sous-classe, donc rattrapée par tout appelant qui ne veut pas la
    distinguer. Elle existe parce que deux pannes très différentes sortaient
    jusqu'ici sous la même exception : un service injoignable, qui est un
    incident, et un registre vide, qui est l'état NORMAL du projet tant
    qu'aucun modèle n'est promu.

    La distinction se fait sur le code de statut et jamais sur le texte du
    message : c'est le contrat de predict qui déclare le 503, un message est
    libre de changer sans PR de contrat.

    C'est elle qui permettra au mode dégradé des recommandations de répondre
    200 en disant pourquoi, au lieu de propager un 503 que le dashboard ne
    saurait pas expliquer.
    """


class SourceUnavailableError(ServingUnavailableError):
    """Serving répond, mais la source qu'il relaie n'a rien servi.

    Serving la signale par un 502 : la panne est chez l'API Mock, pas chez
    lui. La distinguer permet à l'API de dire lequel des deux est tombé, là
    où une exception unique enverrait chercher la panne au hasard.
    """


def build_client() -> httpx.AsyncClient:
    """Fabrique le client HTTP de l'appel à Serving.

    Isolée pour servir de point de substitution aux tests, qui la remplacent
    par un client monté sur httpx.MockTransport. Sans cela, il faudrait un
    vrai service en écoute pour éprouver le délai dépassé ou une réponse 500.
    """
    return httpx.AsyncClient(timeout=get_settings().predict_timeout_seconds)


async def _call(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    """Appelle Serving et rend sa réponse, les pannes traduites en exceptions.

    Trois pannes, trois exceptions, parce qu'elles n'appellent pas la même
    conduite : Serving injoignable est un incident, un registre vide (503) est
    l'état normal tant qu'aucun modèle n'est promu, et une source muette (502)
    ne se règle pas du côté de predict.

    La distinction se fait sur le code de statut et jamais sur le texte du
    message : c'est le contrat de predict qui déclare ces codes, un message
    est libre de changer sans PR de contrat.
    """
    settings = get_settings()
    if not settings.predict_url:
        raise ServingUnavailableError(
            "PREDICT_URL absente de la configuration : le service d'inférence"
            " est injoignable par construction.",
        )

    url = settings.predict_url.rstrip("/") + path
    try:
        async with build_client() as client:
            response = await client.request(method, url, json=json, params=params)
    except _TRANSPORT_FAILURES as error:
        raise ServingUnavailableError(f"{type(error).__name__}: {error}") from error

    if response.status_code == httpx.codes.BAD_GATEWAY:
        raise SourceUnavailableError(
            f"la source n'a pas répondu (502) : {response.text[:200]!r}",
        )

    if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
        raise ServingNotReadyError(
            "service d'inférence indisponible (503) :"
            f" {response.text[:200]!r}",
        )

    if response.status_code != httpx.codes.OK:
        raise ServingUnavailableError(
            f"statut {response.status_code}, corps {response.text[:200]!r}",
        )
    return response


def _parse(response: httpx.Response, model: type, *, many: bool = False):
    """Valide le corps contre le contrat de predict, ou refuse de l'exploiter.

    Une réponse 200 hors contrat est un échec de Serving et non de l'API :
    rien d'exploitable n'en sort, il n'y a donc rien à archiver.
    """
    try:
        payload = response.json()
        if many:
            return [model.model_validate(item) for item in payload]
        return model.model_validate(payload)
    except (ValueError, TypeError) as error:
        # ValueError couvre le JSON illisible comme la ValidationError de
        # Pydantic, qui en dérive.
        raise ServingUnavailableError(
            f"réponse non conforme au contrat de predict : {error}",
        ) from error


async def request_sites() -> list[ServingSite]:
    """Demande à Serving le référentiel que la source expose.

    L'API ne le stocke pas ici : ce module rend ce que Serving a servi, et
    c'est l'appelant qui décide d'en faire un référentiel en base.
    """
    response = await _call("GET", SITES_PATH)
    return _parse(response, ServingSite, many=True)


async def request_spike(site_id: str, duration_minutes: int) -> ServingSpike:
    """Demande à Serving de déclencher un pic sur un site de la source.

    Jamais retentée, à aucun niveau de la chaîne : rejouer l'appel
    déclencherait un second pic par-dessus le premier, et deux pics qui se
    recouvrent ne sont pas ce qu'on a demandé.
    """
    response = await _call(
        "POST",
        SPIKE_PATH.format(site_id=site_id),
        params={"duration_minutes": duration_minutes},
    )
    return _parse(response, ServingSpike)


async def request_prediction(site_id: str, horizon_hours: int) -> ServingPrediction:
    """Demande la prévision d'un site à Serving.

    Lève ServingUnavailableError sur délai dépassé, erreur de connexion,
    réponse non 200, ou réponse 200 dont la forme ne respecte pas le contrat
    de predict.

    Le 503 sort en ServingNotReadyError : un registre vide est l'état normal
    du projet tant qu'aucun modèle n'est promu, et le confondre avec un
    service injoignable ferait chercher une panne d'infrastructure là où il
    n'y a rien à servir.
    """
    response = await _call(
        "POST",
        PREDICT_PATH,
        json={"site_id": site_id, "horizon_hours": horizon_hours},
    )
    return _parse(response, ServingPrediction)
