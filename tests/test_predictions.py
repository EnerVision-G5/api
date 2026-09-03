"""Tests du proxy de prédiction (EV-38).

Serving est remplacé par un `httpx.MockTransport` : le délai dépassé, l'erreur
de connexion et la réponse 500 se provoquent alors sans service en écoute, et
l'on peut affirmer que Serving n'a **pas** été appelé, ce qu'aucun vrai service
ne permettrait de constater.

La base, elle, reste réelle comme pour EV-11 : c'est elle qui décide du 404.
"""

import json
import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.prediction import Prediction
from app.schemas.prediction import PredictionOut
from tests.conftest import (
    AMBIGUOUS_MODEL_VERSION,
    KNOWN_MODEL_VERSION,
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    UNKNOWN_MODEL_VERSION,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

PREDICT_URL = "/api/v1/predict"
SERVING_BASE_URL = "http://serving-de-test:8000"

# Réponse type de Serving. Le second point a ses bornes nulles : le contrat les
# déclare nullables, et elles doivent traverser telles quelles.
SERVING_RESPONSE = {
    "site_id": SITE_WITH_READINGS,
    "model_version": KNOWN_MODEL_VERSION,
    "generated_at": "2026-09-03T08:00:00Z",
    "points": [
        {
            "timestamp": "2026-09-03T09:00:00Z",
            "predicted_consumption_kw": 131.5,
            "lower_bound_kw": 120.0,
            "upper_bound_kw": 143.0,
        },
        {
            "timestamp": "2026-09-03T10:00:00Z",
            "predicted_consumption_kw": 128.25,
            "lower_bound_kw": None,
            "upper_bound_kw": None,
        },
    ],
}


class ServingDouble:
    """Serving de test : joue une réponse et retient les appels reçus."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._handler: Callable[[httpx.Request], httpx.Response] = lambda _: (
            httpx.Response(200, json=SERVING_RESPONSE)
        )

    def responds(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def called(self) -> bool:
        return bool(self.requests)


@pytest.fixture
def serving(monkeypatch: pytest.MonkeyPatch) -> Iterator[ServingDouble]:
    """Substitue le double à Serving et configure PREDICT_URL.

    build_client est la couture prévue pour cela dans app/predict_client.py.
    Le cache des réglages est vidé de part et d'autre, à la sortie après la
    restauration de la variable, sinon il se reconstruirait sur la valeur du
    test.
    """
    double = ServingDouble()

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(double.handle))

    monkeypatch.setattr("app.predict_client.build_client", build_client)
    monkeypatch.setenv("PREDICT_URL", SERVING_BASE_URL)
    get_settings.cache_clear()
    yield double
    monkeypatch.delenv("PREDICT_URL", raising=False)
    get_settings.cache_clear()


def payload(site_id: str = SITE_WITH_READINGS, **extra: object) -> dict:
    return {"site_id": site_id, **extra}


def responding(body: dict) -> Callable[[httpx.Request], httpx.Response]:
    """Handler jouant une réponse 200 avec le corps donné."""
    return lambda _: httpx.Response(200, json=body)


@pytest.fixture
async def clean_predictions(db_session: AsyncSession) -> AsyncSession:
    """Vide la table prediction avant le test.

    Les tests d'archivage écrivent, contrairement au reste de la suite : partir
    d'une table vide les rend indépendants de leur ordre d'exécution et
    rejouables sur une base déjà utilisée.
    """
    await db_session.execute(delete(Prediction))
    await db_session.commit()
    return db_session


async def stored_points(session: AsyncSession) -> list[tuple]:
    """Relit les prédictions archivées, hors de tout cache d'identité."""
    await session.rollback()
    rows = await session.execute(
        select(
            Prediction.site_id,
            Prediction.ts_cible,
            Prediction.consumption_kw_predite,
        ).order_by(Prediction.ts_cible),
    )
    return list(rows)


async def test_prediction_is_relayed_unchanged(
    api_client: AsyncClient,
    serving: ServingDouble,
) -> None:
    """La réponse de Serving ressort telle quelle, bornes nulles comprises."""
    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    # Comparaison par le modèle et non par les dictionnaires bruts : la
    # notation du décalage UTC est un détail de sérialisation, pas le contrat.
    assert PredictionOut.model_validate(response.json()) == PredictionOut.model_validate(
        SERVING_RESPONSE,
    )

    body = response.json()
    assert body["model_version"] == KNOWN_MODEL_VERSION
    assert len(body["points"]) == 2
    assert body["points"][0]["lower_bound_kw"] == 120.0
    # Le point sans intervalle de confiance garde ses nulls.
    assert body["points"][1]["lower_bound_kw"] is None
    assert body["points"][1]["upper_bound_kw"] is None


async def test_request_is_forwarded_to_serving(
    api_client: AsyncClient,
    serving: ServingDouble,
) -> None:
    """Serving reçoit la demande sur le chemin de son contrat, sans réécriture."""
    await api_client.post(PREDICT_URL, json=payload(horizon_hours=12))

    assert len(serving.requests) == 1
    sent = serving.requests[0]
    assert str(sent.url) == f"{SERVING_BASE_URL}{PREDICT_URL}"
    assert sent.method == "POST"
    assert json.loads(sent.content) == {
        "site_id": SITE_WITH_READINGS,
        "horizon_hours": 12,
    }


async def test_default_horizon_is_sent(
    api_client: AsyncClient,
    serving: ServingDouble,
) -> None:
    """L'horizon par défaut du contrat est explicité dans l'appel à Serving."""
    await api_client.post(PREDICT_URL, json=payload())

    assert json.loads(serving.requests[0].content)["horizon_hours"] == 24


async def test_unknown_site_returns_404_without_calling_serving(
    api_client: AsyncClient,
    serving: ServingDouble,
) -> None:
    """Le 404 vient de la base, et l'aller-retour réseau n'a pas lieu."""
    response = await api_client.post(PREDICT_URL, json=payload(SITE_UNKNOWN))

    assert response.status_code == 404
    assert_error_response(response.json())
    assert serving.called is False


def times_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("delai depasse", request=request)


def refuses_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connexion refusee", request=request)


def fails_with_500(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom")


def answers_404(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": "site inconnu de serving"})


def answers_off_contract(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"inattendu": True})


def answers_unparsable(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="pas du json")


@pytest.mark.parametrize(
    ("case", "handler"),
    [
        ("delai depasse", times_out),
        ("erreur de connexion", refuses_connection),
        ("erreur 500", fails_with_500),
        ("erreur 404 de serving", answers_404),
        ("reponse hors contrat", answers_off_contract),
        ("reponse illisible", answers_unparsable),
    ],
)
async def test_serving_failures_return_503(
    api_client: AsyncClient,
    serving: ServingDouble,
    case: str,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Toute défaillance de Serving sort en 503, message générique.

    Une réponse 200 hors contrat en fait partie : c'est l'inférence qui
    déraille, le dashboard n'a rien à faire d'un 500 de l'API métier.
    """
    serving.responds(handler)

    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 503, case
    assert_error_response(response.json())
    # Le détail de la panne reste dans les logs, pas dans la réponse.
    assert "serving" not in response.json()["detail"].lower(), case


async def test_missing_predict_url_returns_503(
    api_client: AsyncClient,
    serving: ServingDouble,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans PREDICT_URL, le service est injoignable par construction : 503.

    Plutôt qu'une URL malformée remontée en 500, la configuration absente est
    traitée comme l'indisponibilité qu'elle est.
    """
    monkeypatch.setenv("PREDICT_URL", "")
    get_settings.cache_clear()

    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 503
    assert_error_response(response.json())
    assert serving.called is False


@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("horizon nul", payload(horizon_hours=0)),
        ("horizon au dela du plafond", payload(horizon_hours=49)),
        ("horizon non entier", payload(horizon_hours="douze")),
        ("site_id absent", {"horizon_hours": 24}),
        ("corps vide", {}),
    ],
)
async def test_invalid_body_returns_422(
    api_client: AsyncClient,
    serving: ServingDouble,
    case: str,
    body: dict,
) -> None:
    response = await api_client.post(PREDICT_URL, json=body)

    assert response.status_code == 422, case
    assert_error_response(response.json())
    # La validation précède l'appel réseau.
    assert serving.called is False


async def test_prediction_requires_a_token(
    anonymous_client: AsyncClient,
    serving: ServingDouble,
) -> None:
    """Sans jeton, la prédiction est refusée avant tout appel à Serving."""
    response = await anonymous_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error_response(response.json())
    assert serving.called is False


# --- Archivage en base -------------------------------------------------------
#
# L'archivage est volontairement partiel : la table prediction ne porte ni les
# bornes de l'intervalle, ni model_version, ni generated_at. Ce qui suit
# éprouve donc ce qui est archivable, et surtout que rien de tout cela ne peut
# priver le client de sa réponse.


async def test_prediction_is_stored(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Une prédiction servie est archivée, un enregistrement par point."""
    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    stored = await stored_points(clean_predictions)
    assert len(stored) == 2
    sites = {row[0] for row in stored}
    assert sites == {SITE_WITH_READINGS}
    assert [float(row[2]) for row in stored] == [131.5, 128.25]


async def test_new_prediction_replaces_the_previous_one(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Un même instant cible réévalué garde la valeur la plus fraîche.

    La contrainte d'unicité porte sur (modele_id, site_id, ts_cible) : sans
    politique de conflit, le second appel échouerait. La plus récente gagne,
    c'est celle que le dashboard affiche.
    """
    await api_client.post(PREDICT_URL, json=payload())

    revised = {**SERVING_RESPONSE, "points": list(SERVING_RESPONSE["points"])}
    revised["points"][0] = {**revised["points"][0], "predicted_consumption_kw": 140.0}
    serving.responds(responding(revised))
    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    stored = await stored_points(clean_predictions)
    assert len(stored) == 2
    assert float(stored[0][2]) == 140.0


@pytest.mark.parametrize(
    ("case", "model_version"),
    [
        ("modele absent du registre", UNKNOWN_MODEL_VERSION),
        ("version portee par deux modeles", AMBIGUOUS_MODEL_VERSION),
    ],
)
async def test_unresolvable_model_is_served_but_not_stored(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    case: str,
    model_version: str,
) -> None:
    """Modèle non résolvable : la prédiction part quand même, rien n'est archivé.

    Le registre `modele` est alimenté par l'équipe Data, pas par l'API, et sa
    clé unique est (nom, version) alors que le contrat ne transporte que la
    version. Deviner le modèle serait pire que ne pas archiver.
    """
    serving.responds(responding({**SERVING_RESPONSE, "model_version": model_version}))

    with caplog.at_level(logging.ERROR, logger="app.predictions_store"):
        response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200, case
    assert response.json()["model_version"] == model_version
    assert await stored_points(clean_predictions) == [], case
    assert caplog.records, case
    assert "non archivee" in caplog.text


async def test_storage_failure_still_serves_the_prediction(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Écriture refusée par la base : réponse 200 quand même, échec journalisé.

    La valeur dépasse le NUMERIC(10,2) de la colonne, ce qui provoque un vrai
    refus du moteur plutôt qu'une panne simulée.
    """
    serving.responds(
        responding(
            {
                **SERVING_RESPONSE,
                "points": [
                    {
                        "timestamp": "2026-09-03T09:00:00Z",
                        "predicted_consumption_kw": 999999999.99,
                        "lower_bound_kw": None,
                        "upper_bound_kw": None,
                    },
                ],
            },
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.predictions_store"):
        response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    assert response.json()["points"][0]["predicted_consumption_kw"] == 999999999.99
    assert await stored_points(clean_predictions) == []
    assert "Echec d'archivage" in caplog.text


async def test_duplicate_points_do_not_break_the_response(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deux points au même horodatage : la réponse part, l'échec est journalisé.

    PostgreSQL refuse un ON CONFLICT DO UPDATE qui viserait deux fois la même
    ligne dans un seul ordre. Rien n'oblige Serving à ne pas envoyer de
    doublon, le cas est donc traité comme un échec d'archivage ordinaire.
    """
    duplicated = SERVING_RESPONSE["points"][0]
    serving.responds(
        responding({**SERVING_RESPONSE, "points": [duplicated, duplicated]}),
    )

    with caplog.at_level(logging.ERROR, logger="app.predictions_store"):
        response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    assert await stored_points(clean_predictions) == []
    assert "Echec d'archivage" in caplog.text


async def test_serving_failure_stores_nothing(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Pas de prévision, donc rien à archiver."""
    serving.responds(fails_with_500)

    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 503
    assert await stored_points(clean_predictions) == []


async def test_empty_prediction_is_served_without_storing(
    api_client: AsyncClient,
    serving: ServingDouble,
    clean_predictions: AsyncSession,
) -> None:
    """Série vide : rien à archiver, et le contrat l'autorise (points non vide
    n'est pas une contrainte du schéma). La réponse part telle quelle."""
    serving.responds(responding({**SERVING_RESPONSE, "points": []}))

    response = await api_client.post(PREDICT_URL, json=payload())

    assert response.status_code == 200
    assert response.json()["points"] == []
    assert await stored_points(clean_predictions) == []
