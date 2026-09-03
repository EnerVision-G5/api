"""Tests du proxy de prédiction (EV-38).

Serving est remplacé par un `httpx.MockTransport` : le délai dépassé, l'erreur
de connexion et la réponse 500 se provoquent alors sans service en écoute, et
l'on peut affirmer que Serving n'a **pas** été appelé, ce qu'aucun vrai service
ne permettrait de constater.

La base, elle, reste réelle comme pour EV-11 : c'est elle qui décide du 404.
"""

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.schemas.prediction import PredictionOut
from tests.conftest import (
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

PREDICT_URL = "/api/v1/predict"
SERVING_BASE_URL = "http://serving-de-test:8000"

# Réponse type de Serving. Le second point a ses bornes nulles : le contrat les
# déclare nullables, et elles doivent traverser telles quelles.
SERVING_RESPONSE = {
    "site_id": SITE_WITH_READINGS,
    "model_version": "enervision_xgboost:3",
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
    assert body["model_version"] == "enervision_xgboost:3"
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
