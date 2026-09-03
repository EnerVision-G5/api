"""Endpoint de prédiction : proxy de l'API métier vers le service d'inférence.

Le dashboard n'appelle plus Serving directement, il passe par ici. L'API vérifie
le site en base, relaie la demande, et rend la réponse de Serving sans la
transformer.

Aucune fonction d'endpoint ne porte de docstring : FastAPI la publierait comme
`description` de l'opération, que le contrat gelé ne contient pas, et le job
contract-drift échouerait aussitôt (piège documenté par EV-11).
"""

from fastapi import APIRouter, HTTPException, status

from app.db.lookups import ensure_site_exists
from app.predict_client import ServingUnavailableError, request_prediction
from app.predictions_store import store_prediction
from app.schemas.common import ErrorResponse
from app.schemas.prediction import PredictionOut, PredictionRequest
from app.security import AuthSession, CurrentUser

router = APIRouter(tags=["predictions"])

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}
UNAVAILABLE = {
    "model": ErrorResponse,
    "description": "Service d'inférence indisponible.",
}

# Message générique : le détail de la panne va dans les logs, pas au client.
# Un consommateur n'a rien à faire de l'adresse ou du code renvoyé par Serving,
# et le lui donner renseignerait un attaquant sur l'architecture interne.
SERVICE_UNAVAILABLE = "Service de prédiction momentanément indisponible."


@router.post(
    "/predict",
    response_model=PredictionOut,
    summary="Obtenir la prévision de consommation d'un site",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
        status.HTTP_503_SERVICE_UNAVAILABLE: UNAVAILABLE,
    },
)
async def create_prediction(
    payload: PredictionRequest,
    session: AuthSession,
    user: CurrentUser,
) -> PredictionOut:
    # Le site est vérifié ici, avant tout appel réseau : un site inconnu est
    # une erreur du client, pas une affaire de Serving, et l'aller-retour
    # serait perdu. Le 404 est donc identique à celui des routes de lecture.
    await ensure_site_exists(session, payload.site_id)

    try:
        prediction = await request_prediction(payload)
    except ServingUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=SERVICE_UNAVAILABLE,
        ) from error

    # Archivage au mieux : la prédiction est déjà due au client, un échec
    # d'écriture ne la lui retire pas. store_prediction ne lève jamais et dit
    # dans les logs ce qu'elle n'a pas pu faire.
    await store_prediction(session, prediction)

    return prediction
