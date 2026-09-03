"""Tests des endpoints du référentiel de sites (EV-11)."""

import pytest
from httpx import AsyncClient

from tests.conftest import (
    SITE_UNKNOWN,
    SITE_WITH_READINGS,
    SITE_WITHOUT_READINGS,
    assert_error_response,
)

pytestmark = pytest.mark.anyio

SITES_URL = "/api/v1/sites"


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


async def test_list_sites_requires_a_token(anonymous_client: AsyncClient) -> None:
    """Sans jeton, la lecture est refusée : AUTH_ENABLED vaut true depuis EV-12."""
    response = await anonymous_client.get(SITES_URL)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert_error_response(response.json())


async def test_list_sites_is_anonymous_when_auth_is_disabled(
    anonymous_client: AsyncClient,
    auth_disabled: None,
) -> None:
    """AUTH_ENABLED à false laisse passer sans jeton.

    Ce mode reste offert au développement local et ne doit jamais être
    déployé ; le test le fige pour qu'une régression se voie.
    """
    response = await anonymous_client.get(SITES_URL)

    assert response.status_code == 200
