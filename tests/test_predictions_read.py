"""Tests de la lecture des prédictions archivées (EV-38).

La route est déclarée symétrique de /readings : les mêmes paramètres, le même
tri, la même pagination et les mêmes codes d'erreur. Ces tests éprouvent cette
symétrie plutôt que de la supposer.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prediction import Prediction
from tests.conftest import (
    ARBITRATED_MODEL_NAME,
    ARBITRATED_MODEL_VERSION,
    KNOWN_MODEL_VERSION,
    SITE_OTHER,
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    SITE_WITHOUT_READINGS,
    T0,
    assert_error_response,
    modele_id_of,
)

pytestmark = pytest.mark.anyio

PREDICTIONS_URL = f"/api/v1/sites/{SITE_WITH_READINGS}/predictions"
HOUR = timedelta(hours=1)

# Une seule génération, à T0, pour cinq instants cibles consécutifs.
GENERATED_AT = T0
POINT_COUNT = 5

PREDICTION_FIELDS = {
    "timestamp",
    "predicted_consumption_kw",
    "lower_bound_kw",
    "upper_bound_kw",
    "model_version",
    "generated_at",
}

# La version publiée vient de modele.version par la jointure, pas d'une
# colonne de prediction.
MODEL_VERSION = KNOWN_MODEL_VERSION

FULL_WINDOW = {
    "start_time": (T0 + HOUR).isoformat(),
    "end_time": (T0 + POINT_COUNT * HOUR).isoformat(),
}


def archived(
    modele_id: int,
    site_id: str,
    target: datetime,
    value: str,
    generated_at: datetime = GENERATED_AT,
    bounds: tuple[str, str] | None = ("100.00", "160.00"),
) -> Prediction:
    """Ligne archivée telle que le job l'écrit, rattachée au registre.

    modele_id est requis : c'est par lui que la route retrouve model_version.
    """
    low, high = (None, None) if bounds is None else bounds
    return Prediction(
        modele_id=modele_id,
        site_id=site_id,
        ts_cible=target,
        consumption_kw_predite=Decimal(value),
        lower_bound_kw=None if low is None else Decimal(low),
        upper_bound_kw=None if high is None else Decimal(high),
        generated_at=generated_at,
    )


@pytest.fixture
async def archived_predictions(
    db_session: AsyncSession,
    known_model_id: int,
) -> AsyncSession:
    """Pose un jeu de prédictions déterministe et repart d'une table vide.

    Les tests de lecture ne modifient rien, mais ceux du job écrivent dans la
    même table : partir d'une table vide les rend indépendants de leur ordre.
    """
    await db_session.execute(delete(Prediction))
    db_session.add_all(
        [
            # Cinq instants cibles pour le site principal, valeurs croissantes.
            *(
                archived(
                    known_model_id,
                    SITE_WITH_READINGS,
                    T0 + index * HOUR,
                    f"{120 + index}.50",
                    bounds=None if index == 2 else ("100.00", "160.00"),
                )
                for index in range(1, POINT_COUNT + 1)
            ),
            # Un autre site, au même instant : le filtre doit l'écarter.
            archived(known_model_id, SITE_OTHER, T0 + HOUR, "640.00"),
        ],
    )
    await db_session.commit()
    return db_session


def timestamps(body: dict) -> list[datetime]:
    return [datetime.fromisoformat(item["timestamp"]) for item in body["items"]]


async def test_predictions_are_returned_sorted(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    response = await api_client.get(PREDICTIONS_URL, params=FULL_WINDOW)

    assert response.status_code == 200
    body = response.json()
    assert body["meta"] == {"total": POINT_COUNT, "limit": 100, "offset": 0}
    assert timestamps(body) == sorted(timestamps(body))
    assert timestamps(body)[0] == T0 + HOUR
    for item in body["items"]:
        assert set(item) == PREDICTION_FIELDS
        assert item["model_version"] == MODEL_VERSION
    # Le point sans intervalle de confiance garde ses nulls.
    without_bounds = body["items"][1]
    assert without_bounds["lower_bound_kw"] is None
    assert without_bounds["upper_bound_kw"] is None


async def test_window_bounds_are_inclusive(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    """Bornes incluses des deux côtés, comme sur /readings."""
    response = await api_client.get(
        PREDICTIONS_URL,
        params={
            "start_time": (T0 + 2 * HOUR).isoformat(),
            "end_time": (T0 + 3 * HOUR).isoformat(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 2
    assert timestamps(body) == [T0 + 2 * HOUR, T0 + 3 * HOUR]


async def test_pagination_is_consistent(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    """Deux pages successives ne se recouvrent pas, total reste celui de la fenêtre."""
    first = await api_client.get(PREDICTIONS_URL, params={**FULL_WINDOW, "limit": 2})
    second = await api_client.get(
        PREDICTIONS_URL,
        params={**FULL_WINDOW, "limit": 2, "offset": 2},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["meta"] == {"total": POINT_COUNT, "limit": 2, "offset": 0}
    assert second.json()["meta"] == {"total": POINT_COUNT, "limit": 2, "offset": 2}

    first_stamps = timestamps(first.json())
    second_stamps = timestamps(second.json())
    assert first_stamps == [T0 + HOUR, T0 + 2 * HOUR]
    assert second_stamps == [T0 + 3 * HOUR, T0 + 4 * HOUR]
    assert not set(first_stamps) & set(second_stamps)

    last = await api_client.get(
        PREDICTIONS_URL,
        params={**FULL_WINDOW, "limit": 2, "offset": 4},
    )
    assert len(last.json()["items"]) == 1


async def test_other_site_has_its_own_predictions(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    response = await api_client.get(
        f"/api/v1/sites/{SITE_OTHER}/predictions",
        params=FULL_WINDOW,
    )

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 1


async def test_two_models_predicting_the_same_hour_give_two_rows(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
    db_session: AsyncSession,
) -> None:
    """Deux modèles pour un même instant cible : deux lignes, deux versions.

    La clé du schéma v1.0 est (modele_id, site_id, ts_cible) : elle interdit
    deux prévisions du même modèle pour le même instant, mais autorise deux
    modèles distincts, que leur model_version distingue.
    """
    other_model_id = await modele_id_of(
        db_session,
        ARBITRATED_MODEL_VERSION,
        ARBITRATED_MODEL_NAME,
    )
    archived_predictions.add(
        archived(other_model_id, SITE_WITH_READINGS, T0 + HOUR, "125.00"),
    )
    await archived_predictions.commit()

    response = await api_client.get(PREDICTIONS_URL, params=FULL_WINDOW)

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == POINT_COUNT + 1
    same_target = [
        item
        for item in body["items"]
        if datetime.fromisoformat(item["timestamp"]) == T0 + HOUR
    ]
    assert len(same_target) == 2
    assert {item["model_version"] for item in same_target} == {
        MODEL_VERSION,
        ARBITRATED_MODEL_VERSION,
    }


async def test_known_site_without_predictions_is_an_empty_page(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    """Un site connu sans prédiction donne une page vide, pas un 404."""
    response = await api_client.get(
        f"/api/v1/sites/{SITE_WITHOUT_READINGS}/predictions",
        params=FULL_WINDOW,
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "meta": {"total": 0, "limit": 100, "offset": 0},
    }


async def test_unknown_site_returns_404(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    response = await api_client.get(
        f"/api/v1/sites/{SITE_UNKNOWN}/predictions",
        params=FULL_WINDOW,
    )

    assert response.status_code == 404
    assert_error_response(response.json())


@pytest.mark.parametrize(
    ("case", "params"),
    [
        ("date invalide", {**FULL_WINDOW, "start_time": "hier matin"}),
        ("limit nulle", {**FULL_WINDOW, "limit": 0}),
        ("limit au dela du plafond", {**FULL_WINDOW, "limit": 1001}),
        ("offset negatif", {**FULL_WINDOW, "offset": -1}),
        (
            "fenetre inversee",
            {
                "start_time": (T0 + POINT_COUNT * HOUR).isoformat(),
                "end_time": T0.isoformat(),
            },
        ),
        ("start_time absent", {"end_time": T0.isoformat()}),
        ("end_time absent", {"start_time": T0.isoformat()}),
    ],
)
async def test_invalid_parameters_return_422(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
    case: str,
    params: dict,
) -> None:
    response = await api_client.get(PREDICTIONS_URL, params=params)

    assert response.status_code == 422, case
    assert_error_response(response.json())


async def test_inverted_window_is_refused_before_the_site_lookup(
    api_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    """Bornes inversées : 422 même sur un site inconnu, comme sur /readings."""
    response = await api_client.get(
        f"/api/v1/sites/{SITE_UNKNOWN}/predictions",
        params={
            "start_time": (T0 + POINT_COUNT * HOUR).isoformat(),
            "end_time": T0.isoformat(),
        },
    )

    assert response.status_code == 422


async def test_predictions_require_a_token(
    anonymous_client: AsyncClient,
    archived_predictions: AsyncSession,
) -> None:
    response = await anonymous_client.get(PREDICTIONS_URL, params=FULL_WINDOW)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error_response(response.json())
