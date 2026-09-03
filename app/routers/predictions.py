"""Lecture des prédictions archivées, symétrique des lectures de mesures.

Le dashboard ne déclenche jamais une prédiction : un job planifié interroge le
service d'inférence et archive le résultat, cette route le relit. Les
paramètres, le tri, la pagination et les codes d'erreur sont exactement ceux
de /readings, dont la fenêtre de temps et la vérification de site sont
partagées.

Aucune fonction d'endpoint ne porte de docstring : FastAPI la publierait comme
`description` de l'opération, que le contrat gelé ne contient pas, et le job
contract-drift échouerait aussitôt (piège documenté par EV-11).
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.lookups import ensure_site_exists
from app.db.session import get_db
from app.models.prediction import Modele, Prediction
from app.schemas.common import ErrorResponse, PaginationMeta
from app.schemas.prediction import PredictionPointOut, PredictionsPage
from app.security import CurrentUser
from app.timewindow import parse_window

router = APIRouter(prefix="/sites", tags=["predictions"])

UNAUTHORIZED = {
    "model": ErrorResponse,
    "description": "Jeton JWT absent, expiré ou invalide.",
}
NOT_FOUND = {"model": ErrorResponse, "description": "Site inconnu."}
UNPROCESSABLE = {
    "model": ErrorResponse,
    "description": "Paramètres de requête invalides.",
}

SiteId = Annotated[str, Path(description="Identifiant du site.")]
DbSession = Annotated[AsyncSession, Depends(get_db)]

# Colonnes publiées par PredictionPointOut. ts_cible est l'instant prédit, que
# le contrat nomme timestamp. model_version n'est pas une colonne de
# prediction : c'est modele.version, atteint par la jointure ci-dessous.
PREDICTION_COLUMNS = (
    Prediction.ts_cible.label("timestamp"),
    Prediction.consumption_kw_predite.label("predicted_consumption_kw"),
    Prediction.lower_bound_kw,
    Prediction.upper_bound_kw,
    Modele.version.label("model_version"),
    Prediction.generated_at,
)


@router.get(
    "/{site_id}/predictions",
    response_model=PredictionsPage,
    summary="Lister les prédictions d'un site sur une fenêtre de temps",
    responses={
        status.HTTP_401_UNAUTHORIZED: UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: UNPROCESSABLE,
    },
)
async def list_predictions(
    site_id: SiteId,
    session: DbSession,
    user: CurrentUser,
    start_time: Annotated[
        datetime,
        Query(description="Borne inférieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    end_time: Annotated[
        datetime,
        Query(description="Borne supérieure incluse de la fenêtre, ISO 8601 UTC."),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=1000, description="Taille de page, entre 1 et 1000."),
    ] = 100,
    offset: Annotated[
        int,
        Query(ge=0, description="Décalage appliqué au début de la collection."),
    ] = 0,
) -> PredictionsPage:
    # La fenêtre porte sur l'horodatage CIBLE, pas sur la date de génération :
    # le client demande « que prévoit-on pour telle période », pas « qu'a-t-on
    # calculé à tel moment ».
    #
    # Une seule ligne par modèle, site et instant cible : la clé du schéma
    # v1.0 en décide, et le job y repose la prévision la plus fraîche. Deux
    # modèles distincts prédisant le même instant donneraient deux lignes,
    # distinguées par leur model_version.
    start_utc, end_utc = parse_window(start_time, end_time)
    await ensure_site_exists(session, site_id)

    filters = (
        Prediction.site_id == site_id,
        Prediction.ts_cible >= start_utc,
        Prediction.ts_cible <= end_utc,
    )
    total = await session.scalar(
        select(func.count()).select_from(Prediction).where(*filters),
    )
    rows = await session.execute(
        select(*PREDICTION_COLUMNS)
        # Jointure interne, et non externe : modele_id est NOT NULL et le
        # registre est alimenté à chaque promotion. Une jointure externe
        # rendrait un model_version nul, que le contrat n'admet pas.
        .join(Modele, Modele.modele_id == Prediction.modele_id)
        .where(*filters)
        .order_by(Prediction.ts_cible, Prediction.generated_at)
        .limit(limit)
        .offset(offset),
    )
    return PredictionsPage(
        items=[PredictionPointOut.model_validate(row) for row in rows],
        meta=PaginationMeta(total=total or 0, limit=limit, offset=offset),
    )
