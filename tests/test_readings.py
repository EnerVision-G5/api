"""Tests des endpoints de lecture des mesures (EV-11)."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from tests.conftest import (
    MINUTE,
    READINGS_COUNT,
    SITE_OTHER,
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    SITE_WITHOUT_READINGS,
    T0,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

READINGS_URL = f"/api/v1/sites/{SITE_WITH_READINGS}/readings"
LATEST_URL = f"{READINGS_URL}/latest"

# Champs publiés par EnergyReadingOut, contrat gelé openapi-api.json.
READING_FIELDS = {
    "timestamp",
    "site_id",
    "site_type",
    "consumption_kw",
    "consumption_kwh",
    "voltage_v",
    "current_a",
    "power_factor",
    "temperature_celsius",
    "humidity_percent",
    "null_reasons",
    "data_quality",
    "consumption_kw_imputed",
    "imputation_method",
    # Ajoutés en 1.4.0 : la mise à l'écart était en base depuis EV-08 sans que
    # rien ne la publie, et `null_reasons` ne la remplace pas — il dit ce qui
    # manquait à la mesure, pas pourquoi elle a été jugée inexploitable.
    "excluded",
    "exclusion_reason",
}

FULL_WINDOW = {
    "start_time": T0.isoformat(),
    "end_time": (T0 + 4 * MINUTE).isoformat(),
}


def parse_timestamp(item: dict) -> datetime:
    """Relit l'horodatage sérialisé sans dépendre de sa notation.

    Pydantic peut écrire le décalage UTC en Z ou en +00:00 : comparer les
    chaînes rendrait le test sensible à un détail de sérialisation.
    """
    return datetime.fromisoformat(item["timestamp"])


async def test_readings_returns_window_sorted_ascending(api_client: AsyncClient) -> None:
    response = await api_client.get(READINGS_URL, params=FULL_WINDOW)

    assert response.status_code == 200
    body = response.json()
    assert body["meta"] == {"total": READINGS_COUNT, "limit": 100, "offset": 0}

    items = body["items"]
    assert len(items) == READINGS_COUNT
    timestamps = [parse_timestamp(item) for item in items]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == T0
    # Le filtre isole bien la série du site demandé.
    assert {item["site_id"] for item in items} == {SITE_WITH_READINGS}
    # site_type est dénormalisé depuis le référentiel par la jointure.
    assert {item["site_type"] for item in items} == {"office"}
    for item in items:
        assert set(item) == READING_FIELDS


async def test_readings_window_bounds_are_inclusive(api_client: AsyncClient) -> None:
    """Les deux bornes sont incluses, comme l'annonce la description du contrat."""
    response = await api_client.get(
        READINGS_URL,
        params={
            "start_time": (T0 + MINUTE).isoformat(),
            "end_time": (T0 + 2 * MINUTE).isoformat(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 2
    assert [parse_timestamp(item) for item in body["items"]] == [
        T0 + MINUTE,
        T0 + 2 * MINUTE,
    ]


async def test_readings_accepts_a_window_in_another_timezone(
    api_client: AsyncClient,
) -> None:
    """Une fenêtre datée dans un autre fuseau désigne le même instant.

    Le contrat annonce de l'UTC, mais un client qui envoie un décalage
    explicite doit obtenir la même fenêtre, pas un décalage silencieux.
    """
    paris = timezone(timedelta(hours=2))
    response = await api_client.get(
        READINGS_URL,
        params={
            "start_time": T0.astimezone(paris).isoformat(),
            "end_time": (T0 + 4 * MINUTE).astimezone(paris).isoformat(),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == READINGS_COUNT
    assert parse_timestamp(body["items"][0]) == T0


async def test_readings_accepts_a_window_without_offset(
    api_client: AsyncClient,
) -> None:
    """Une fenêtre sans décentrage horaire est lue comme de l'UTC.

    Le contrat annonce de l'UTC : un client qui omet le décalage doit obtenir
    la fenêtre attendue, et non un 500 du driver sur la comparaison d'un
    horodatage naïf avec une colonne TIMESTAMPTZ.
    """
    response = await api_client.get(
        READINGS_URL,
        params={
            "start_time": T0.replace(tzinfo=None).isoformat(),
            "end_time": (T0 + 4 * MINUTE).replace(tzinfo=None).isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == READINGS_COUNT


async def test_readings_pagination_is_consistent(api_client: AsyncClient) -> None:
    """Deux pages successives ne se recouvrent pas et total reste celui de la fenêtre."""
    first = await api_client.get(READINGS_URL, params={**FULL_WINDOW, "limit": 2})
    second = await api_client.get(
        READINGS_URL,
        params={**FULL_WINDOW, "limit": 2, "offset": 2},
    )

    assert first.status_code == second.status_code == 200
    first_body, second_body = first.json(), second.json()

    # total décrit la fenêtre entière, pas la page : c'est ce qui permet au
    # client de savoir combien de pages il lui reste.
    assert first_body["meta"] == {"total": READINGS_COUNT, "limit": 2, "offset": 0}
    assert second_body["meta"] == {"total": READINGS_COUNT, "limit": 2, "offset": 2}

    first_stamps = [parse_timestamp(item) for item in first_body["items"]]
    second_stamps = [parse_timestamp(item) for item in second_body["items"]]
    assert first_stamps == [T0, T0 + MINUTE]
    assert second_stamps == [T0 + 2 * MINUTE, T0 + 3 * MINUTE]
    assert not set(first_stamps) & set(second_stamps)

    # La dernière page est partielle : 5 mesures paginées par 2.
    last = await api_client.get(
        READINGS_URL,
        params={**FULL_WINDOW, "limit": 2, "offset": 4},
    )
    assert len(last.json()["items"]) == 1


async def test_readings_preserve_nulls_quality_and_imputation(
    api_client: AsyncClient,
) -> None:
    """Les NULL, data_quality, null_reasons et les colonnes d'imputation sortent intacts.

    Rien n'est comblé ni recalculé par l'API : consumption_kw reste ce que la
    source a envoyé, y compris NULL, et consumption_kw_imputed reste ce que
    l'ETL a écrit.
    """
    response = await api_client.get(READINGS_URL, params=FULL_WINDOW)

    assert response.status_code == 200
    by_stamp = {parse_timestamp(item): item for item in response.json()["items"]}

    # Report de la dernière valeur connue : brute nulle, imputée renseignée.
    locf = by_stamp[T0 + MINUTE]
    assert locf["consumption_kw"] is None
    assert locf["data_quality"] == "partial"
    assert locf["null_reasons"] == ["consumption_kw:sensor_timeout"]
    assert locf["consumption_kw_imputed"] == 120.5
    assert locf["imputation_method"] == "locf"

    # Perte réseau : rien d'exploitable, aucune imputation, plusieurs motifs.
    critical = by_stamp[T0 + 2 * MINUTE]
    assert critical["consumption_kw"] is None
    assert critical["consumption_kwh"] is None
    assert critical["voltage_v"] is None
    assert critical["current_a"] is None
    assert critical["power_factor"] is None
    assert critical["data_quality"] == "critical"
    assert critical["null_reasons"] == [
        "consumption_kw:sensor_offline",
        "voltage_v:sensor_offline",
        "network_loss",
    ]
    assert critical["consumption_kw_imputed"] is None
    assert critical["imputation_method"] == "none"

    # Trou encadré : interpolation.
    interpolated = by_stamp[T0 + 3 * MINUTE]
    assert interpolated["consumption_kw"] is None
    assert interpolated["consumption_kw_imputed"] == 123.0
    assert interpolated["imputation_method"] == "interpolation"

    # Mesure saine : la brute et l'imputée coïncident, méthode none.
    healthy = by_stamp[T0]
    assert healthy["consumption_kw"] == 120.5
    assert healthy["consumption_kw_imputed"] == 120.5
    assert healthy["imputation_method"] == "none"
    assert healthy["null_reasons"] == []
    assert healthy["data_quality"] == "good"


async def test_readings_other_site_has_its_own_series(api_client: AsyncClient) -> None:
    response = await api_client.get(
        f"/api/v1/sites/{SITE_OTHER}/readings",
        params=FULL_WINDOW,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["total"] == 2
    assert {item["site_type"] for item in body["items"]} == {"factory"}


async def test_readings_unknown_site_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(
        f"/api/v1/sites/{SITE_UNKNOWN}/readings",
        params=FULL_WINDOW,
    )

    assert response.status_code == 404
    assert_error_response(response.json())


async def test_readings_known_site_without_measure_is_empty_page(
    api_client: AsyncClient,
) -> None:
    """Un site connu sans mesure donne une page vide, pas un 404.

    Le 404 de cet endpoint qualifie le site, pas l'absence de données.
    """
    response = await api_client.get(
        f"/api/v1/sites/{SITE_WITHOUT_READINGS}/readings",
        params=FULL_WINDOW,
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "meta": {"total": 0, "limit": 100, "offset": 0},
    }


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
                "start_time": (T0 + 4 * MINUTE).isoformat(),
                "end_time": T0.isoformat(),
            },
        ),
        ("start_time absent", {"end_time": T0.isoformat()}),
        ("end_time absent", {"start_time": T0.isoformat()}),
    ],
)
async def test_readings_invalid_parameters_return_422(
    api_client: AsyncClient,
    case: str,
    params: dict,
) -> None:
    response = await api_client.get(READINGS_URL, params=params)

    assert response.status_code == 422, case
    assert_error_response(response.json())


async def test_readings_inverted_window_is_refused_before_the_site_lookup(
    api_client: AsyncClient,
) -> None:
    """Bornes inversées : 422 même sur un site inconnu.

    La requête est malformée quel que soit le site, elle est donc rejetée
    avant la consultation du référentiel.
    """
    response = await api_client.get(
        f"/api/v1/sites/{SITE_UNKNOWN}/readings",
        params={
            "start_time": (T0 + 4 * MINUTE).isoformat(),
            "end_time": T0.isoformat(),
        },
    )

    assert response.status_code == 422


async def test_latest_returns_the_most_recent_measure(api_client: AsyncClient) -> None:
    response = await api_client.get(LATEST_URL)

    assert response.status_code == 200
    item = response.json()
    assert set(item) == READING_FIELDS
    assert parse_timestamp(item) == T0 + 4 * MINUTE
    assert item["consumption_kw"] == 125.5
    assert item["site_id"] == SITE_WITH_READINGS
    assert item["site_type"] == "office"


async def test_latest_unknown_site_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/api/v1/sites/{SITE_UNKNOWN}/readings/latest")

    assert response.status_code == 404
    assert_error_response(response.json())


async def test_latest_known_site_without_measure_returns_404(
    api_client: AsyncClient,
) -> None:
    """Faute de modèle de réponse nullable et de code dédié au contrat, un site
    connu mais sans mesure sort en 404, avec un message qui le dit."""
    response = await api_client.get(
        f"/api/v1/sites/{SITE_WITHOUT_READINGS}/readings/latest",
    )

    assert response.status_code == 404
    payload = response.json()
    assert_error_response(payload)
    assert SITE_WITHOUT_READINGS in payload["detail"]
