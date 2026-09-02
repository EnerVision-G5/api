"""Tests des endpoints du référentiel de sites (EV-11)."""

import pytest
from httpx import AsyncClient

from tests.conftest import (
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    SITE_WITHOUT_READINGS,
)

pytestmark = pytest.mark.anyio

SITES_URL = "/api/v1/sites"


def assert_error_response(payload: dict) -> None:
    """Vérifie qu'un corps d'erreur respecte le modèle ErrorResponse du contrat."""
    assert set(payload) == {"detail"}
    assert isinstance(payload["detail"], str)
    assert payload["detail"]


async def test_list_sites_returns_referential(api_client: AsyncClient) -> None:
    response = await api_client.get(SITES_URL)

    assert response.status_code == 200
    sites = response.json()
    assert [site["site_id"] for site in sites] == [
        SITE_WITH_READINGS,
        "SITE002",
        SITE_WITHOUT_READINGS,
    ]
    # Tous les champs du contrat sont présents, et location n'est jamais nulle.
    for site in sites:
        assert set(site) == {
            "site_id",
            "site_type",
            "site_name",
            "location",
            "capacity_kw",
            "status",
        }
        assert isinstance(site["location"], str)


async def test_get_site_returns_one_site(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{SITES_URL}/{SITE_WITH_READINGS}")

    assert response.status_code == 200
    assert response.json() == {
        "site_id": SITE_WITH_READINGS,
        "site_type": "office",
        "site_name": "Bureau Paris La Défense",
        "location": "Paris, France",
        "capacity_kw": 200.0,
        "status": "active",
    }


async def test_get_site_unknown_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{SITES_URL}/{SITE_UNKNOWN}")

    assert response.status_code == 404
    assert_error_response(response.json())


async def test_list_sites_is_anonymous_by_default(api_client: AsyncClient) -> None:
    """AUTH_ENABLED vaut false par défaut : la lecture passe sans jeton.

    C'est la frontière avec EV-12 : EV-11 livre les lectures sans dépendre du
    flux d'authentification, qui n'existe pas encore.
    """
    response = await api_client.get(SITES_URL, headers={})

    assert response.status_code == 200


async def test_reads_are_refused_when_auth_is_enabled(
    api_client: AsyncClient,
    auth_enabled: None,
) -> None:
    """AUTH_ENABLED à true refuse en 501, pas en 200 ni en 401.

    Un 200 laisserait croire à une protection inexistante, un 401 laisserait
    croire que le jeton fourni est en cause. La vérification réelle est EV-12.
    """
    response = await api_client.get(SITES_URL)

    assert response.status_code == 501
    assert_error_response(response.json())
